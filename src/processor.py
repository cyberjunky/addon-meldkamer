"""Turns decoded FLEX lines into enriched, stored and published P2000 messages."""

import asyncio
import copy
import fnmatch
import logging
import time
from collections.abc import Callable

from . import abbreviation_import
from .config import Config
from .database import Database
from .geocoding import Geocoder
from .incidents import IncidentTracker
from .message import P2000Message
from .parser import Parser
from .publisher import Publisher
from .sensor_manager import SensorManager
from .tts import SpeechRules
from .zones import HOME_ZONE, Zone, distance_km

logger = logging.getLogger(__name__)

DEFAULT_IGNORE_TEXT = ["*TESTOPROEP*", "*MOB*"]


class MessageProcessor:
    """Parse, filter, enrich, store and publish messages."""

    def __init__(
        self,
        config: Config,
        database: Database,
        sensor_manager: SensorManager,
        publisher: Publisher,
        speech: SpeechRules,
    ):
        self.config = config
        self.database = database
        self.parser = Parser(database)
        self.sensor_manager = sensor_manager
        self.publisher = publisher
        self.speech = speech

        # Only enable geocoder if opencage is enabled and token is provided
        self.geocoder = Geocoder(config.opencage_token if config.opencage_enabled else "", database)
        # Messages not sent to OpenCage because no sensor matches them (only_sensor_matches)
        self.geocode_skipped = 0
        if config.geocode_only_sensor_matches and not sensor_manager.sensors:
            logger.warning("geocoding.only_sensor_matches is set but no sensors are configured - geocoding everything")

        self.home: Zone | None = None
        self.incidents = IncidentTracker(config.incident_window_minutes)
        self.incidents.restore(database.get_messages_since(config.incident_window_minutes * 60))

        # Most recent message per body, for repeat suppression: body -> (message, monotonic time)
        self._recent: dict[str, tuple[P2000Message, float]] = {}

        # New message, and a repeat of an earlier one (webui / TCP broadcast)
        self.on_message: Callable[[P2000Message], None] | None = None
        self.on_repeat: Callable[[P2000Message], None] | None = None

        # Seed the built-in P2000 abbreviation dictionary (only new codes, a
        # user's own edits to an existing abbreviation are kept)
        imported = abbreviation_import.import_abbreviations(database, replace=False)
        if imported:
            logger.info(f"Seeded {imported} default abbreviation(s) into database")

        # Seed default global ignore filters into database (only new ones, won't overwrite)
        imported = database.import_ignore_text(DEFAULT_IGNORE_TEXT)
        if imported:
            logger.info(f"Seeded {imported} default ignore-text pattern(s) into database")
        self.reload_ignore_filters()

    def set_zone(self, zone: Zone) -> None:
        """A zone was read from Home Assistant: the home zone and/or a sensor filter zone."""
        if zone.entity_id == HOME_ZONE:
            self.home = zone
        self.sensor_manager.set_zone(zone)

    def reload_ignore_filters(self) -> None:
        """Reload global ignore filters from database (called after DB edits)."""
        self._ignore_text = self.database.get_all_ignore_text()
        self._ignore_capcodes = self.database.get_all_ignore_capcodes()
        logger.info(
            f"Loaded {len(self._ignore_text)} ignore-text pattern(s), {len(self._ignore_capcodes)} ignored capcode(s)"
        )

    def _should_ignore(self, msg: P2000Message) -> bool:
        """Check the global ignore filters (ignore_text / ignore_capcode).

        A message is dropped when its body matches an ignore_text pattern, or when
        ALL of its capcodes are in the ignore_capcode list (a single ignored
        capcode must not kill a multi-capcode group call).
        """
        for pattern in self._ignore_text:
            if fnmatch.fnmatch(msg.body.lower(), pattern.lower()):
                logger.debug(f"Ignoring message (text filter): {pattern}")
                return True

        if self._ignore_capcodes and msg.capcodes and all(c in self._ignore_capcodes for c in msg.capcodes):
            logger.debug(f"Ignoring message (capcode filter): {msg.capcodes}")
            return True

        return False

    def _original_of_repeat(self, msg: P2000Message) -> P2000Message | None:
        """The earlier message this one repeats, or None when it is new (and then remember it)."""
        window = self.config.repeat_window_seconds
        if not window:
            return None
        now = time.monotonic()
        self._recent = {body: entry for body, entry in self._recent.items() if now - entry[1] <= window}
        key = " ".join(msg.body.split())
        entry = self._recent.get(key)
        if entry:
            return entry[0]
        self._recent[key] = (msg, now)
        return None

    async def process(self, line: str) -> None:
        """Process one decoded FLEX message line."""
        msg = self.parser.parse(line)
        if not msg or self._should_ignore(msg):
            return

        original = self._original_of_repeat(msg)
        if original:
            original.repeats += 1
            if original.id is not None:
                self.database.set_message_repeats(original.id, original.repeats)
            logger.debug(f"Repeat #{original.repeats} of: {msg.body[:60]}")
            if self.on_repeat:
                self.on_repeat(original)
            return

        geocoded = await self._locate(msg)
        # Only for a street-level position: a message placed at the city centre
        # (no street, or the street wasn't geocoded) would get the centre's distance
        if self.home and msg.location_accuracy == "street":
            msg.distance_km = distance_km(msg, self.home.latitude, self.home.longitude)
        msg.tts = self.speech.speak(msg)
        incident, escalation = self.incidents.assign(msg)
        msg.id = self.database.save_message(msg)

        logger.info(
            f"{self.config.network_name}: {msg.body[:60]}... "
            f"[{msg.discipline or 'unknown'}] "
            f"[{msg.region or 'unknown'}]"
        )
        if escalation:
            logger.info(f"Incident at {incident.address} escalated: {', '.join(escalation)}")

        opencage_info = (
            f"enabled: {self.geocoder.enabled} "
            f"ratelimit: {self.geocoder.rate_limited} ({self.geocoder.rate_remaining}) "
            f"geocoded: {geocoded}"
        )
        await self.publisher.publish_message(
            msg, self.sensor_manager.get_matching_sensors(msg), opencage_info, incident, escalation
        )

        if self.on_message:
            try:
                self.on_message(msg)
            except Exception as e:
                logger.error(f"on_message callback failed: {e}")

    async def _locate(self, msg: P2000Message) -> bool:
        """Set the message's coordinates. Returns whether OpenCage geocoded it."""
        geocoded = False
        if msg.address and self.geocoder.enabled:
            cache_only = self._geocode_cache_only(msg)
            # The OpenCage request is synchronous - run it off the event loop
            geo = await asyncio.to_thread(self.geocoder.geocode, msg.address, cache_only)
            geocoded = self.geocoder.geocoded
            if geo:
                msg.latitude = geo["latitude"]
                msg.longitude = geo["longitude"]
                msg.mapurl = geo["mapurl"]
            elif cache_only:
                self.geocode_skipped += 1
                logger.debug(f"Geocoding skipped (no sensor match): {msg.address}")

        # City-level coordinates from the local places database, so the map and
        # auto-focus also work with geocoding disabled or failed lookups
        if msg.latitude is None and msg.city:
            coords = self.database.get_place_coordinates(msg.city)
            if coords:
                msg.latitude = coords["latitude"]
                msg.longitude = coords["longitude"]
                msg.location_accuracy = "city"
        return geocoded

    def _geocode_cache_only(self, msg: P2000Message) -> bool:
        """Whether to skip OpenCage for the message: only_sensor_matches is set and no sensor can match it."""
        if not self.config.geocode_only_sensor_matches or not self.sensor_manager.sensors:
            return False
        # Match the sensors on the city-level position the message gets without OpenCage
        probe = copy.copy(msg)
        coords = self.database.get_place_coordinates(msg.city) if msg.city else None
        if coords:
            probe.latitude = coords["latitude"]
            probe.longitude = coords["longitude"]
        return not self.sensor_manager.is_geocode_candidate(probe)
