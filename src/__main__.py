"""Meldkamer - Main entry point."""

import asyncio
import contextlib
import logging
import signal

from .config import Config
from .database import Database
from .decoder import Decoder
from .sensor_manager import load_sensors_from_config
from .server import P2000Server
from .webui import WebUI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

# Flag to prevent multiple shutdown calls
_shutting_down = False


async def main() -> None:
    """Main application entry point."""
    global _shutting_down

    logger.info("Meldkamer starting...")

    # Load configuration from options.json (Home Assistant addon)
    config = Config.from_options_file()

    if config.log_level == "debug":
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"Network: {config.network_name}")
    logger.info(f"Frequency: {config.frequency}")
    logger.info(f"Decoder: {config.decoder}")
    logger.info(f"Geocoding: {'enabled' if config.opencage_enabled else 'disabled'}")

    # Load P2000 database
    database = Database()
    db_stats = database.get_stats()
    logger.info(
        f"Database loaded: {db_stats['places']} places, {db_stats['capcodes']} capcodes, {db_stats['geocodes']} geocodes"
    )

    # Initialize decoder
    decoder = Decoder(config=config, database=database)

    # Initialize TCP server for integration communication
    server = P2000Server(host="0.0.0.0", port=5000)

    # Initialize Web UI (ingress)
    webui = WebUI(port=8099, database=database)
    webui.set_network_name(config.network_name)
    webui.set_config(config.frequency, config.sample_rate, config.decoder)

    # Build command once (this also sets device_type and device_driver)
    _ = decoder.command
    webui.set_device_info(decoder.device_type, decoder.device_driver)
    # Map zone circles are a pure visualization aid, so they're shown regardless
    # of whether sensor publishing (REST/MQTT) is actually enabled - reuse the
    # decoder's already-parsed sensors when available to avoid parsing twice.
    if decoder.sensor_manager:
        webui.set_sensors(decoder.sensor_manager.sensors)
    elif config.sensors:
        webui.set_sensors(load_sensors_from_config(config.sensors))

    webui.update_db_stats(
        db_stats["places"], db_stats["streets"], db_stats["capcodes"], db_stats["texts"], db_stats["geocodes"]
    )

    # Connect decoder output to server and webui
    # (global ignore filters are applied in the decoder, before this callback)
    def on_message(msg):
        server.broadcast_message(msg)
        webui.add_message(msg)
        # Update decoder status when we receive messages
        webui.update_status(decoder_running=decoder.is_running)

    decoder.on_message = on_message
    # Reload TTS rules / ignore filters in the decoder when edited via the web UI
    webui.on_tts_changed = decoder.reload_tts_replacements
    webui.on_ignore_changed = decoder.reload_ignore_filters

    # Keep the webui decoder status in sync with the actual pipeline state
    async def update_decoder_status():
        ticks = 0
        while True:
            webui.update_status(decoder_running=decoder.is_running)
            webui.set_decoder_error(decoder.last_error)
            webui.set_geocoder_info(
                configured=config.opencage_enabled,
                enabled=decoder.geocoder.enabled,
                rate_remaining=decoder.geocoder.rate_remaining,
                rate_limited=decoder.geocoder.rate_limited,
            )
            ticks += 1
            if ticks % 15 == 0:
                # Refresh DB counts so the Advanced tab reflects imports
                # without needing a restart
                db_stats = database.get_stats()
                webui.update_db_stats(
                    db_stats["places"],
                    db_stats["streets"],
                    db_stats["capcodes"],
                    db_stats["texts"],
                    db_stats["geocodes"],
                )
            await asyncio.sleep(2)

    # Keep a reference so the task isn't garbage-collected mid-run; it lives
    # for the app's lifetime and is cancelled along with everything else in
    # do_shutdown() below.
    background_tasks: set[asyncio.Task] = set()
    status_task = asyncio.create_task(update_decoder_status())
    background_tasks.add(status_task)
    status_task.add_done_callback(background_tasks.discard)

    async def do_shutdown():
        """Perform shutdown once."""
        global _shutting_down
        if _shutting_down:
            return
        _shutting_down = True

        logger.info("Shutting down...")
        try:
            await decoder.stop()
        except Exception as e:
            logger.debug(f"Decoder stop error: {e}")
        try:
            await server.stop()
        except Exception as e:
            logger.debug(f"Server stop error: {e}")
        try:
            await webui.stop()
        except Exception as e:
            logger.debug(f"WebUI stop error: {e}")

        # Give subprocesses time to fully terminate
        await asyncio.sleep(0.5)

        # Cancel all running tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

        # Wait for tasks to complete cancellation
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Setup graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(do_shutdown()))

    # Start services
    try:
        await asyncio.gather(decoder.start(), server.start(), webui.start())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Service error: {e}")
    finally:
        # Ensure cleanup happens
        if not _shutting_down:
            await do_shutdown()

    logger.info("Meldkamer stopped")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
