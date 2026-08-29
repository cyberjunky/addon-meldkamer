"""P2000 FLEX decoder using rtl_fm or rx_fm (SoapySDR) and multimon-ng."""

import asyncio
import fnmatch
import logging
import re
from collections.abc import Callable

from . import abbreviation_import
from .config import Config
from .database import Database
from .geocoding import Geocoder
from .ha_client import HAClient
from .message import P2000Message
from .mqtt_client import MQTTClient
from .parser import Parser
from .sensor_manager import SensorManager, load_sensors_from_config

logger = logging.getLogger(__name__)


class Decoder:
    """Manages SDR + multimon-ng subprocess pipeline for P2000 FLEX."""

    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database
        self.parser = Parser(database)

        # Only enable geocoder if opencage is enabled and token is provided
        opencage_token = config.opencage_token if config.opencage_enabled else ""
        self.geocoder = Geocoder(opencage_token, database)

        # Initialize HA client for sensor publishing
        self.ha_client = HAClient()

        # Initialize MQTT client (optional, additive alongside the HA REST API sensors above)
        self.mqtt_client: MQTTClient | None = MQTTClient(config) if config.mqtt_enabled else None

        # Initialize sensor manager if sensors configured and enabled
        self.sensor_manager: SensorManager | None = None
        if config.sensors_enabled and config.sensors:
            sensors = load_sensors_from_config(config.sensors)
            self.sensor_manager = SensorManager(sensors)
            if not self.ha_client.available and not (self.mqtt_client and self.mqtt_client.enabled):
                logger.warning("SUPERVISOR_TOKEN not found and MQTT disabled - sensors will not be published anywhere")

        self._process: asyncio.subprocess.Process | None = None
        self._running = False
        self.on_message: Callable[[P2000Message], None] | None = None

        # Seed the built-in P2000 abbreviation dictionary (only new codes, won't
        # overwrite a user's own edits to an existing abbreviation's text)
        imported_abbrevs = abbreviation_import.import_abbreviations(database, replace=False)
        if imported_abbrevs:
            logger.info(f"Seeded {imported_abbrevs} default abbreviation(s) into database")

        # Seed default TTS replacements into database (only new ones, won't overwrite)
        DEFAULT_TTS_REPLACEMENTS = [
            # Priority codes - real messages use e.g. "A1"/"P1" with no space, but
            # allow an optional one since the parser itself sees both forms
            {"pattern": r"\bA\s?1\b", "replacement": "Ambulance met spoed"},
            {"pattern": r"\bA\s?2\b", "replacement": "Ambulance zonder spoed"},
            {"pattern": r"\bB\s?1\b", "replacement": "Brandweer met spoed"},
            {"pattern": r"\bB\s?2\b", "replacement": "Brandweer zonder spoed"},
            {"pattern": r"\bP\s?1\b", "replacement": "Prio 1"},
            {"pattern": r"\bP\s?2\b", "replacement": "Prio 2"},
            # Meldkamer gespreksgroep codes - short spoken forms
            {"pattern": r"BDH\-[0-9]+", "replacement": "Brandweer Den Haag"},
            {"pattern": r"BOB\-[0-9]+", "replacement": "Brandweer Oost-Brabant"},
            {"pattern": r"BON\-[0-9]+", "replacement": "Brandweer Oost-Nederland"},
            {"pattern": r"BNN\-[0-9]+", "replacement": "Brandweer Noord-Nederland"},
            {"pattern": r"BNH\-[0-9]+", "replacement": "Brandweer Noord-Holland"},
            {"pattern": r"BLB\-[0-9]+", "replacement": "Brandweer Limburg"},
            {"pattern": r"BAD\-[0-9]+", "replacement": "Brandweer Amsterdam-Amstelland"},
            {"pattern": r"BMD\-[0-9]+", "replacement": "Brandweer Midden-Nederland"},
            {"pattern": r"BRT\-[0-9]+", "replacement": "Brandweer Rotterdam"},
            {"pattern": r"BZB\-[0-9]+", "replacement": "Brandweer Zeeland"},
            # Ambulance dienstposten - short spoken form
            {"pattern": r"DP[0-9]+", "replacement": "Dienstpost"},
            # AMBU right after a priority code already says "Ambulance ... spoed" -
            # drop the redundant repeat; otherwise (no priority code) speak it out
            {"pattern": r"(?<=spoed\s)AMBU\b\s*", "replacement": ""},
            {"pattern": r"\bAMBU\b", "replacement": "Ambulance"},
            # City abbreviations - short spoken forms (see city_abbreviations.py
            # for the full list used for address/city matching; only ones worth
            # a distinct spoken form belong here)
            {"pattern": r"\bSGRAVH\b", "replacement": "Den Haag"},
            # Status flags - drop the "(... : ja)" wrapper, just speak the label
            {"pattern": r"\(Directe inzet:\s*ja\)", "replacement": "Directe inzet"},
            {"pattern": r"\(DIA:\s*ja\)", "replacement": "Directe inzet ambulance"},
            # Reference numbers - not useful spoken aloud
            {"pattern": r"\s*\bbon:?\s*\d+", "replacement": ""},
            {"pattern": r"\s*\brit:?\s*\d+", "replacement": ""},
            {"pattern": r"\s*\bicnum:?\s*\d+", "replacement": ""},
            # Common role/discipline abbreviations - spoken in full
            {"pattern": r"\bGHOR\b", "replacement": "Geneeskundige hulpverlening bij ongevallen en rampen"},
            {"pattern": r"\bMMT\b", "replacement": "Mobiel medisch team"},
            {"pattern": r"\bSEH\b", "replacement": "Spoedeisende Hulp"},
            {"pattern": r"\bOVD-B\b", "replacement": "Officier van Dienst Brandweer"},
            {"pattern": r"\bOVD-G\b", "replacement": "Officier van Dienst Geneeskundig"},
            {"pattern": r"\bOVD-P\b", "replacement": "Officier van Dienst Politie"},
            {"pattern": r"\bOvD\b", "replacement": "Officier van Dienst"},
            {"pattern": r"\bPOL\b", "replacement": "Politie"},
            {"pattern": r"\bRAV\b", "replacement": "Regionale ambulancevoorziening"},
            # Long standalone numbers (capcodes, other reference numbers) - postal
            # codes are 4 digits so they're left alone
            {"pattern": r"\s*\b\d{5,}\b", "replacement": ""},
        ]
        imported = database.import_tts_replacements(DEFAULT_TTS_REPLACEMENTS)
        if imported:
            logger.info(f"Seeded {imported} default TTS replacement(s) into database")
        self._tts_replacements = database.get_all_tts_replacements()
        if self._tts_replacements:
            logger.info(f"Loaded {len(self._tts_replacements)} TTS replacement(s)")

        # Seed default global ignore filters into database (only new ones, won't overwrite)
        DEFAULT_IGNORE_TEXT = ["*TESTOPROEP*", "*MOB*"]
        imported_ignore_text = database.import_ignore_text(DEFAULT_IGNORE_TEXT)
        if imported_ignore_text:
            logger.info(f"Seeded {imported_ignore_text} default ignore-text pattern(s) into database")
        self._ignore_text = database.get_all_ignore_text()
        self._ignore_capcodes = database.get_all_ignore_capcodes()
        logger.info(
            f"Loaded {len(self._ignore_text)} ignore-text pattern(s), {len(self._ignore_capcodes)} ignored capcode(s)"
        )

        # Cache command and device type (built once)
        self._cached_command: str | None = None
        self.device_type: str = ""
        self.device_driver: str = ""
        # Last fatal SDR error (e.g. "No supported devices found"), shown in the UI
        self.last_error: str = ""

    async def _check_and_reset_hackrf(self) -> bool:
        """Check HackRF status and reset if stuck. Returns True if OK."""
        import subprocess

        logger.info("Checking HackRF status...")

        # First, kill any existing rx_fm or hackrf processes that might be holding the device
        try:
            subprocess.run(["pkill", "-9", "rx_fm"], capture_output=True, timeout=2)
            subprocess.run(["pkill", "-9", "hackrf"], capture_output=True, timeout=2)
            await asyncio.sleep(1)  # Let USB settle
        except Exception:
            pass  # pkill may not exist or may fail, that's OK

        try:
            # Try hackrf_info to see if device responds
            result = subprocess.run(["hackrf_info"], capture_output=True, timeout=5)

            if result.returncode == 0 and b"Serial number" in result.stdout:
                # Check if external clock is being used (causes no reception!)
                if b"clock source=external" in result.stdout:
                    logger.warning("HackRF is using EXTERNAL clock - forcing back to INTERNAL clock!")
                    try:
                        # Reset to internal clock
                        subprocess.run(["hackrf_clock", "-i"], capture_output=True, timeout=5)
                        await asyncio.sleep(2)  # Let device reconfigure
                        logger.info("HackRF clock reset to internal")
                    except FileNotFoundError:
                        # hackrf_clock might not exist, try spiflash reset instead
                        logger.warning("hackrf_clock not available, attempting full reset...")
                        try:
                            subprocess.run(["hackrf_spiflash", "-R"], capture_output=True, timeout=10)
                            await asyncio.sleep(5)
                            logger.info("HackRF reset via spiflash")
                        except Exception as e:
                            logger.error(f"HackRF reset failed: {e}")
                    except Exception as e:
                        logger.warning(f"HackRF clock reset failed: {e}")
                else:
                    logger.info("HackRF responding normally (internal clock)")
                return True

            logger.warning("HackRF not responding properly, attempting reset...")

        except subprocess.TimeoutExpired:
            logger.warning("HackRF timed out, attempting reset...")
        except FileNotFoundError:
            logger.debug("hackrf_info not found, skipping check")
            return True
        except Exception as e:
            logger.warning(f"HackRF check failed: {e}")

        # Try reset methods
        try:
            # Method 1: hackrf_spiflash to reset the device
            logger.info("Attempting HackRF reset via hackrf_spiflash...")
            subprocess.run(["hackrf_spiflash", "-R"], capture_output=True, timeout=10)
            await asyncio.sleep(5)  # Wait longer for USB re-enumeration

            # Check if it worked
            result = subprocess.run(["hackrf_info"], capture_output=True, timeout=5)
            if result.returncode == 0:
                logger.info("HackRF reset successful!")
                return True

        except Exception as e:
            logger.warning(f"HackRF reset failed: {e}")

        logger.error("HackRF could not be reset - may need physical power cycle")
        return False

    @property
    def command(self) -> str:
        """Build the SDR | multimon-ng command for P2000 FLEX.

        Supports: rtl-sdr (default), hackrf, soapysdr, network.
        "auto" is accepted as an alias for rtl-sdr (no device probing).
        """
        # Return cached command if already built
        if self._cached_command:
            return self._cached_command

        freq = self.config.frequency
        rate = self.config.sample_rate
        decoder = self.config.decoder
        gain = self.config.gain
        ppm = self.config.ppm_correction
        receiver_type = self.config.receiver_type.lower()

        multimon = f"multimon-ng -a {decoder} -t raw -"

        # Network receiver - connect to rtl_tcp server via netcat
        if receiver_type == "network":
            host = self.config.network_host
            port = self.config.network_port
            if not host:
                raise RuntimeError("Network receiver requires network_host to be set")
            logger.info(f"Using network receiver: rtl_tcp at {host}:{port}")
            self.device_type = f"Network ({host}:{port})"
            self.device_driver = "rtl_fm (rtl_tcp)"
            # rtl_fm can connect directly to rtl_tcp server
            self._cached_command = f"rtl_fm -d rtl_tcp={host}:{port} -f {freq} -g {gain} -M fm -s {rate} | {multimon}"

        # Explicit RTL-SDR
        elif receiver_type == "rtl-sdr":
            logger.info("Using rtl_fm (RTL-SDR selected)")
            self.device_type = "RTL-SDR"
            self.device_driver = "rtl_fm"
            self._cached_command = f"rtl_fm -f {freq} -g {gain} -M fm -s {rate} -p {ppm} | {multimon}"

        # Explicit HackRF
        elif receiver_type == "hackrf":
            logger.info("Using rx_fm with HackRF driver")
            self.device_type = "HackRF"
            self.device_driver = "rx_fm"
            # HackRF has separate gain stages: LNA (0-40), VGA (0-62), AMP (0=off/1=on, ~14dB when enabled)
            lna = self.config.hackrf_lna_gain
            vga = self.config.hackrf_vga_gain
            amp = 1 if self.config.hackrf_amp_enable else 0
            logger.info(f"HackRF gains: LNA={lna}dB, VGA={vga}dB, AMP={amp} (RF amplifier {'on' if amp else 'off'})")
            # Use SoapySDR gain string format for rx_fm
            gain_str = f"LNA={lna},VGA={vga},AMP={amp}"
            self._cached_command = f'rx_fm -d driver=hackrf -f {freq} -g "{gain_str}" -M fm -s {rate} | {multimon}'

        # Explicit SoapySDR (auto-detect device)
        elif receiver_type == "soapysdr":
            logger.info("Using rx_fm (SoapySDR auto-detect)")
            self.device_type = "SoapySDR"
            self.device_driver = "rx_fm"
            # SoapySDR devices may not support auto gain, use 40 as default
            soapy_gain = gain if gain > 0 else 40
            if gain == 0:
                logger.info("SoapySDR may not support auto gain, using gain=40")
            self._cached_command = f"rx_fm -f {freq} -g {soapy_gain} -M fm -s {rate} | {multimon}"

        # RTL-SDR (default) - "auto" is treated as rtl-sdr, no device probing.
        # Probing proved unreliable: a failed probe cached the wrong driver
        # (rx_fm/SoapyRTLSDR) for the whole session, leaving the tuner deaf.
        else:
            logger.info("Using rtl_fm (RTL-SDR, default)")
            self.device_type = "RTL-SDR"
            self.device_driver = "rtl_fm"
            self._cached_command = f"rtl_fm -f {freq} -g {gain} -M fm -s {rate} -p {ppm} | {multimon}"

        return self._cached_command

    @property
    def is_running(self) -> bool:
        """True when the decoder pipeline process is alive and being read."""
        return self._running and self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Start the decoder subprocess and supervise it (restart on unexpected exit)."""
        logger.info(f"Starting decoder for {self.config.network_name}")
        logger.info(f"Command: {self.command}")

        # Check and reset HackRF if needed (checks for "HackRF" in device_type)
        if "HackRF" in self.device_type:
            await self._check_and_reset_hackrf()

        if self.mqtt_client:
            await self.mqtt_client.start()

        # Pre-create sensors in Home Assistant so they appear immediately,
        # but don't wipe the state/attributes of sensors that already exist
        if self.sensor_manager and self.ha_client.available:
            logger.info(f"Registering {len(self.sensor_manager.sensors)} sensor(s) in Home Assistant...")
            for sensor in self.sensor_manager.sensors:
                existing = await self.ha_client.get_state(sensor.entity_id)
                if existing is not None:
                    logger.debug(f"sensor.{sensor.entity_id} already exists, keeping current state")
                    continue
                await self.ha_client.update_sensor(
                    sensor.entity_id,
                    state="Waiting for data...",
                    attributes={
                        "friendly_name": sensor.name,
                        "icon": sensor.icon,
                    },
                )
            logger.info("Sensors registered in Home Assistant")

        self._running = True
        backoff = 5  # Seconds between restarts, doubles up to 60 on repeated failures

        while self._running:
            got_output = False
            try:
                got_output = await self._run_process()
            except asyncio.CancelledError:
                logger.debug("Decoder cancelled")
                break
            except Exception as e:
                logger.error(f"Decoder error: {e}")

            if not self._running:
                break  # Intentional stop() - do not restart

            if got_output:
                backoff = 5  # Pipeline produced output, so it basically works
            else:
                # No decoded frames: the auto-detected device/driver may be
                # wrong or was probed while the device was busy. Drop the cached
                # command so the next restart re-runs auto-detection.
                if self._cached_command:
                    logger.info("No decoded output - will re-detect receiver on restart")
                self._cached_command = None

            logger.warning(f"Decoder subprocess exited unexpectedly, restarting in {backoff}s...")
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, 60)

        logger.info("Decoder stopped")

    async def _run_process(self) -> bool:
        """Run the SDR | multimon-ng pipeline once until it exits.

        Returns True if any output was received (pipeline basically works).
        """
        got_output = False

        self._process = await asyncio.create_subprocess_shell(
            self.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        logger.debug("Decoder subprocess started, reading output...")

        # Log stderr in a separate task
        stderr_task = asyncio.create_task(self._log_stderr(self._process))

        try:
            # Read output line by line
            while self._running and self._process and self._process.stdout:
                try:
                    line = await self._process.stdout.readline()
                    if not line:
                        if self._process and self._process.returncode is not None:
                            logger.warning(f"Decoder process exited with code {self._process.returncode}")
                        break

                    decoded = line.decode("utf-8", errors="ignore").strip()
                    if decoded:
                        # Only real decoded frames count as output. The
                        # multimon-ng startup banner ("Enabled demodulators:")
                        # must not reset the restart backoff.
                        if decoded.startswith("FLEX"):
                            got_output = True
                            self.last_error = ""  # Receiving data again
                        logger.debug(f"Raw line: {decoded[:100]}")
                        await self._process_line(decoded)
                except asyncio.CancelledError:
                    logger.debug("Decoder stdout reading cancelled")
                    raise
                except Exception as e:
                    logger.error(f"Error processing line: {e}")
                    # Continue reading unless cancelled
                    if not self._running:
                        break
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            # Make sure the pipeline is really dead before a possible restart
            if self._process and self._process.returncode is None:
                try:
                    self._process.kill()
                    await self._process.wait()
                except ProcessLookupError:
                    pass
                except Exception as e:
                    logger.debug(f"Error killing decoder process: {e}")
            self._process = None

        return got_output

    async def _log_stderr(self, process: asyncio.subprocess.Process) -> None:
        """Log stderr output for debugging."""
        if not process or not process.stderr:
            return
        while self._running and process.stderr:
            try:
                line = await process.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="ignore").strip()
                if decoded:
                    lowered = decoded.lower()
                    # Fatal SDR device errors: surface them loudly and in the UI
                    if "no supported devices found" in lowered:
                        self.last_error = (
                            "No SDR device found - check that the USB receiver is "
                            "plugged in and that the addon can access it"
                        )
                        logger.error(self.last_error)
                    elif "usb_claim_interface error" in lowered or "failed to open rtlsdr device" in lowered:
                        self.last_error = decoded[:200]
                        logger.error(f"SDR: {decoded[:200]}")
                    # Format SDR device info nicely
                    elif (
                        ("Found" in decoded and "device" in decoded)
                        or "Using" in decoded
                        or "Tuned" in decoded
                        or "tuner" in decoded.lower()
                        or "Sampling" in decoded
                        or "Output" in decoded
                    ):
                        logger.info(f"SDR: {decoded}")
                    elif decoded.startswith("multimon-ng") or "(C)" in decoded:
                        logger.debug(f"multimon-ng: {decoded}")
                    else:
                        logger.debug(f"stderr: {decoded[:200]}")
            except asyncio.CancelledError:
                logger.debug("Decoder stderr reading cancelled")
                break
            except Exception as e:
                logger.debug(f"Error reading stderr: {e}")
                break

    def _should_ignore(self, msg: P2000Message) -> bool:
        """Check the global ignore filters (ignore_text / ignore_capcode).

        A message is dropped when its body matches an ignore_text pattern, or when
        ALL of its capcodes are in the ignore_capcode list (a single ignored
        capcode must not kill a multi-capcode group call).
        """
        # Check text patterns (supports wildcards like *test*, *TEST*)
        for pattern in self._ignore_text:
            if fnmatch.fnmatch(msg.body.lower(), pattern.lower()):
                logger.debug(f"Ignoring message (text filter): {pattern}")
                return True

        # Check capcodes: drop only when every capcode on the message is ignored
        ignore_capcodes = self._ignore_capcodes
        if ignore_capcodes and msg.capcodes and all(capcode in ignore_capcodes for capcode in msg.capcodes):
            logger.debug(f"Ignoring message (capcode filter): {msg.capcodes}")
            return True

        return False

    async def _process_line(self, line: str) -> None:
        """Process a single FLEX output line."""
        # Handle FLEX messages only
        if not line.startswith("FLEX"):
            return

        logger.debug(f"Received: {line[:80]}...")

        # Parse the message
        msg = self.parser.parse(line)
        if not msg:
            return

        # Global ignore filters - covers HA sensors, webui and TCP broadcast
        if self._should_ignore(msg):
            return

        # Geocode if address is available and geocoder is enabled
        geocoded = False
        logger.debug(f"Geocoding check: address='{msg.address}', geocoder.enabled={self.geocoder.enabled}")
        if msg.address and self.geocoder.enabled:
            # The OpenCage request is synchronous - run it off the event loop
            loop = asyncio.get_running_loop()
            geo = await loop.run_in_executor(None, self.geocoder.geocode, msg.address)
            geocoded = self.geocoder.geocoded
            if geo:
                msg.latitude = geo["latitude"]
                msg.longitude = geo["longitude"]
                msg.mapurl = geo["mapurl"]
                logger.debug(f"Geocoded: {msg.latitude}, {msg.longitude}")
            else:
                logger.debug(f"Geocoding returned None for: {msg.address}")

        # Fallback: city-level coordinates from the local places database, so the
        # map and auto-focus also work with geocoding disabled or failed lookups
        if msg.latitude is None and msg.city:
            coords = self.database.get_place_coordinates(msg.city)
            if coords:
                msg.latitude = coords["latitude"]
                msg.longitude = coords["longitude"]
                msg.location_accuracy = "city"
                logger.debug(f"City-level coords for {msg.city}: {msg.latitude}, {msg.longitude}")

        logger.info(
            f"{self.config.network_name}: {msg.body[:60]}... "
            f"[{msg.discipline or 'unknown'}] "
            f"[{msg.region or 'unknown'}]"
        )

        # Publish to matching sensors, via Home Assistant REST API and/or MQTT
        mqtt_active = bool(self.mqtt_client and self.mqtt_client.enabled)
        if self.sensor_manager and (self.ha_client.available or mqtt_active):
            matching_sensors = self.sensor_manager.get_matching_sensors(msg)
            if matching_sensors:
                # TTS-replaced text: full version as attribute, truncated to 255 chars (HA limit) as state
                tts_text = self._apply_tts(msg.body) if msg.body else ""
                opencage_info = (
                    f"enabled: {self.geocoder.enabled} "
                    f"ratelimit: {self.geocoder.rate_limited} ({self.geocoder.rate_remaining}) "
                    f"geocoded: {geocoded}"
                )
                for sensor in matching_sensors:
                    if self.ha_client.available:
                        attributes = msg.to_dict()
                        attributes["friendly_name"] = sensor.name
                        attributes["icon"] = sensor.icon
                        attributes["tts"] = tts_text
                        attributes["opencage"] = opencage_info
                        state_text = tts_text[:255]
                        await self.ha_client.update_sensor(
                            sensor.entity_id,
                            state=state_text,
                            attributes=attributes,
                        )
                    if mqtt_active:
                        self.mqtt_client.publish_for_sensor(sensor, msg, tts_text, opencage_info)

        # Send to callback (webui / TCP broadcast) - never let it kill the read loop
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception as e:
                logger.error(f"on_message callback failed: {e}")

    def _apply_tts(self, text: str) -> str:
        """Apply TTS regex replacements to text.

        Case-insensitive to match the browser's TTS, which always applies the
        JS 'i' flag (patterns can't rely on an inline (?i) - it's a Python-only
        construct and throws in JS, silently no-opping that rule there).
        """
        if not self._tts_replacements:
            return text
        result = text
        for rule in self._tts_replacements:
            try:
                result = re.sub(rule["pattern"], rule["replacement"], result, flags=re.IGNORECASE)
            except re.error as e:
                logger.warning(f"Invalid TTS regex pattern '{rule['pattern']}': {e}")
        return result

    def reload_tts_replacements(self):
        """Reload TTS replacements from database (called after DB edits)."""
        self._tts_replacements = self.database.get_all_tts_replacements()
        logger.info(f"Reloaded {len(self._tts_replacements)} TTS replacement(s)")

    def reload_ignore_filters(self):
        """Reload global ignore filters from database (called after DB edits)."""
        self._ignore_text = self.database.get_all_ignore_text()
        self._ignore_capcodes = self.database.get_all_ignore_capcodes()
        logger.info(
            f"Reloaded {len(self._ignore_text)} ignore-text pattern(s), {len(self._ignore_capcodes)} ignored capcode(s)"
        )

    async def stop(self) -> None:
        """Stop the decoder subprocess."""
        self._running = False

        # Close HA client
        await self.ha_client.close()

        if self.mqtt_client:
            await self.mqtt_client.stop()

        if self._process:
            try:
                # First try graceful termination
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)
                except TimeoutError:
                    # Force kill if not responding
                    logger.warning("Process not responding, force killing...")
                    self._process.kill()
                    await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except Exception as e:
                logger.error(f"Error stopping process: {e}")
            finally:
                # Don't manually call feed_eof() - it causes "feed_data after feed_eof" errors
                # Let the streams close naturally when the process terminates
                self._process = None
                # Give USB device time to reset (especially for HackRF)
                if "HackRF" in self.device_type:
                    logger.info("Waiting for HackRF USB reset...")
                    await asyncio.sleep(3.0)
