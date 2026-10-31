"""Meldkamer - Main entry point."""

import asyncio
import contextlib
import logging
import signal
import sys

from .config import Config
from .database import Database
from .decoder import Decoder
from .ha_client import HAClient
from .processor import MessageProcessor
from .publisher import Publisher
from .sensor_manager import SensorManager, load_sensors_from_config
from .server import P2000Server
from .tts import SpeechRules
from .webui import WebUI
from .zones import HOME_ZONE, load_zones

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

# Flag to prevent multiple shutdown calls
_shutting_down = False


async def main() -> int:
    """Main application entry point. Returns the process exit code."""
    global _shutting_down

    logger.info("Meldkamer starting...")

    # Load configuration from options.json (Home Assistant addon)
    try:
        config = Config.from_options_file()
    except ValueError as e:
        logger.error(f"{e} - fix the add-on configuration and restart")
        return 1

    if config.log_level == "debug":
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"Network: {config.network_name}")
    logger.info(f"Frequency: {config.frequency}")
    logger.info(f"Decoder: {config.decoder}")
    logger.info(
        f"Geocoding: {'enabled' if config.opencage_enabled else 'disabled'}"
        f"{' (only sensor matches)' if config.opencage_enabled and config.geocode_only_sensor_matches else ''}"
    )

    database = Database()
    db_stats = database.get_stats()
    logger.info(
        f"Database loaded: {db_stats['places']} places, {db_stats['capcodes']} capcodes, {db_stats['geocodes']} geocodes"
    )

    ha_client = HAClient()
    speech = SpeechRules(database)
    sensor_manager = SensorManager(load_sensors_from_config(config.sensors))
    publisher = Publisher(config, ha_client, sensor_manager)
    processor = MessageProcessor(config, database, sensor_manager, publisher, speech)
    decoder = Decoder(config, ha_client)
    decoder.on_line = processor.process

    # TCP server for integration communication
    server = P2000Server(host="0.0.0.0", port=5000)

    webui = WebUI(8099, database, speech, processor, decoder, sensor_manager, ha_client)
    webui.update_db_stats(
        db_stats["places"], db_stats["streets"], db_stats["capcodes"], db_stats["texts"], db_stats["geocodes"]
    )

    # An invalid receiver config must not crash-loop the add-on: keep the web
    # UI up and show the error there instead of starting the decoder.
    decoder_ok = True
    try:
        decoder.validate()
    except RuntimeError as e:
        decoder_ok = False
        decoder.last_error = f"{e} - fix the add-on configuration and restart"
        logger.error(decoder.last_error)

    def on_message(msg):
        server.broadcast_message(msg)
        webui.add_message(msg)

    processor.on_message = on_message
    processor.on_repeat = webui.mark_repeat

    async def publish_health():
        """Refresh database counts for the web UI and publish receiver health to Home Assistant."""
        while True:
            # The receiver gets a minute to decode its first frames before it's reported
            await asyncio.sleep(60)
            # Never let one failure (e.g. a locked database) kill the loop
            try:
                db_stats = database.get_stats()
                webui.update_db_stats(
                    db_stats["places"],
                    db_stats["streets"],
                    db_stats["capcodes"],
                    db_stats["texts"],
                    db_stats["geocodes"],
                )
                await publisher.publish_health(
                    decoder.stats.online(config.offline_after_minutes), decoder.stats.messages_last_hour()
                )
            except Exception:
                logger.exception("Health update failed")

    # Keep a reference so the task isn't garbage-collected mid-run; it lives
    # for the app's lifetime and is cancelled along with everything else in
    # do_shutdown() below.
    background_tasks: set[asyncio.Task] = set()

    def run_in_background(coro):
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    run_in_background(publish_health())
    if ha_client.available:
        # The home zone (distance_km) and the zones sensors filter on
        run_in_background(load_zones(ha_client, {HOME_ZONE} | sensor_manager.zone_ids, processor.set_zone))

    async def do_shutdown():
        """Perform shutdown once."""
        global _shutting_down
        if _shutting_down:
            return
        _shutting_down = True

        logger.info("Shutting down...")

        async def stop_services():
            try:
                await decoder.stop()
            except Exception as e:
                logger.debug(f"Decoder stop error: {e}")
            try:
                await publisher.stop()
            except Exception as e:
                logger.debug(f"Publisher stop error: {e}")
            await ha_client.close()
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

        # Stay within S6_SERVICES_GRACETIME (see Dockerfile), or s6 kills us
        # mid-shutdown and the SDR pipeline may be left running
        try:
            await asyncio.wait_for(stop_services(), timeout=7.0)
        except TimeoutError:
            logger.warning("Shutdown took too long, stopping anyway")

        # Cancel all running tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

        # Wait for tasks to complete cancellation
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Setup graceful shutdown
    loop = asyncio.get_event_loop()

    def on_signal():
        run_in_background(do_shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)

    # Start services. A service failing (e.g. port already in use) is fatal:
    # exit non-zero so the finish script stops the add-on instead of
    # restarting it in a loop.
    exit_code = 0
    await publisher.start()
    services = [server.start(), webui.start()]
    if decoder_ok:
        services.append(decoder.start())
    try:
        await asyncio.gather(*services)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Service error")
        exit_code = 1
    finally:
        # Ensure cleanup happens
        if not _shutting_down:
            await do_shutdown()

    logger.info("Meldkamer stopped")
    return exit_code


if __name__ == "__main__":
    exit_code = 0
    with contextlib.suppress(KeyboardInterrupt):
        exit_code = asyncio.run(main())
    sys.exit(exit_code)
