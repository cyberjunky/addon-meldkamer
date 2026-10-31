"""Sensor manager for filtering and matching P2000 messages."""

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .message import P2000Message
from .zones import Zone, distance_km

logger = logging.getLogger(__name__)

# Priority filter entries: optional P/A/B/PRIO prefix + digit ("P1", "a 1", "PRIO 2", "1")
_PRIORITY_ENTRY_RE = re.compile(r"^(PRIO|P|A|B)?\s*([0-9])$", re.IGNORECASE)


def slugify(value: str) -> str:
    """Reduce a string to a valid HA object id: [a-z0-9_], no leading/trailing/double underscores.

    Accents are transliterated ("Café" -> "cafe", "Ĳsselmonde" -> "ijsselmonde");
    characters with no ASCII equivalent become underscores. May return "".
    """
    value = unicodedata.normalize("NFKD", value.casefold()).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def sanitize_mqtt_id(value: str) -> str:
    """Make a user-set mqtt_id safe as an MQTT topic level / discovery object id ([A-Za-z0-9_-]).

    Already-valid ids are returned unchanged so existing unique_ids keep working.
    """
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")


def parse_priority(entry: str) -> int | None:
    """Parse a priority filter entry to the int the parser stores in msg.priority.

    Mirrors parser.PRIO_PATTERNS: P1/A1/PRIO 1 -> 1, P2/A2 -> 2, B1-B3/P3 -> 3, P4 -> 4.
    Returns None when the entry can't be parsed.
    """
    match = _PRIORITY_ENTRY_RE.match(entry.strip())
    if not match:
        return None
    prefix = (match.group(1) or "").upper()
    level = int(match.group(2))
    if prefix == "B":
        return 3 if 1 <= level <= 3 else None
    if prefix == "A":
        return level if level in (1, 2) else None
    return level if 1 <= level <= 4 else None


@dataclass
class SensorFilter:
    """Filter criteria for a P2000 sensor."""

    disciplines: list[str] | None = None
    cities: list[str] | None = None
    capcodes: list[str] | None = None
    priorities: list[str] | None = None
    region: str | None = None
    zone: str | None = None  # Home Assistant zone entity id, e.g. "zone.home"
    text_contains: str | None = None


@dataclass
class SensorConfig:
    """Configuration for a P2000 sensor."""

    name: str
    entity_id: str
    filters: SensorFilter
    icon: str = "mdi:radio-tower"
    # Optional stable identifier for this sensor's MQTT topics/unique_id.
    # Falls back to entity_id when not set.
    mqtt_id: str | None = None
    # Speak matching messages on the configured Home Assistant media players
    announce: bool = False
    # The zone named in filters.zone, once read from Home Assistant
    zone: Zone | None = None


# A zone filter counts a message as a geocoding candidate when its city-level
# position is within the zone radius plus this margin: the street may lie closer
GEOCODE_ZONE_MARGIN_KM = 2.0


class SensorManager:
    """Manages P2000 sensors and message filtering."""

    def __init__(self, sensors: list[SensorConfig]):
        self.sensors = sensors
        self._priority_warned: set = set()  # (sensor, entry) pairs already warned about as unparseable
        logger.info(f"Loaded {len(sensors)} sensor(s)")

    @property
    def zone_ids(self) -> set[str]:
        """Zone entity ids used by sensor filters."""
        return {s.filters.zone for s in self.sensors if s.filters.zone}

    def set_zone(self, zone: Zone) -> None:
        """Attach a zone read from Home Assistant to the sensors filtering on it."""
        for sensor in self.sensors:
            if sensor.filters.zone == zone.entity_id:
                sensor.zone = zone

    def get_matching_sensors(self, msg: P2000Message) -> list[SensorConfig]:
        """Get all sensors that match the given message."""
        matching = [sensor for sensor in self.sensors if self.matches(msg, sensor)]
        for sensor in matching:
            logger.debug(f"Message matched sensor: {sensor.entity_id}")
        return matching

    def is_geocode_candidate(self, msg: P2000Message) -> bool:
        """Whether a sensor could match the message once it has street-level coordinates.

        msg holds city-level coordinates (or none): zone filters get a margin, and
        an unknown position counts as a candidate.
        """
        return any(self.matches(msg, sensor, GEOCODE_ZONE_MARGIN_KM) for sensor in self.sensors)

    def matches(self, msg: P2000Message, sensor: SensorConfig, zone_margin_km: float | None = None) -> bool:
        """Check if message matches all filter criteria (AND logic).

        With zone_margin_km, the zone filter accepts messages up to that far
        outside the zone, or with an unknown position.
        """
        filters = sensor.filters

        # Discipline filter (msg.discipline may hold multiple, ", "-joined disciplines)
        if filters.disciplines:
            msg_disciplines = [d.strip().lower() for d in msg.discipline.split(",")] if msg.discipline else []
            filter_disciplines = [d.strip().lower() for d in filters.disciplines]
            if not any(d in filter_disciplines for d in msg_disciplines):
                return False

        # City filter (case-insensitive)
        if filters.cities and (
            not msg.city or msg.city.strip().casefold() not in {c.casefold() for c in filters.cities}
        ):
            return False

        # Capcode filter (match if ANY capcode in message matches; compared as 9-digit zero-padded)
        if filters.capcodes:
            filter_capcodes = {c.strip().zfill(9) for c in filters.capcodes}
            if not msg.capcodes or not any(str(code).strip().zfill(9) in filter_capcodes for code in msg.capcodes):
                return False

        # Priority filter (filter entries are strings like "P1"/"A1"/"1", msg.priority is an int)
        if filters.priorities:
            allowed_priorities = set()
            for entry in filters.priorities:
                priority = parse_priority(entry)
                if priority is not None:
                    allowed_priorities.add(priority)
                elif (sensor.name, entry) not in self._priority_warned:
                    logger.warning(
                        f"Sensor '{sensor.name}' has unrecognised priority '{entry}' (expected e.g. P1, A1, B1, 1) "
                        "- ignoring it"
                    )
                    self._priority_warned.add((sensor.name, entry))
            if not msg.priority or msg.priority not in allowed_priorities:
                return False

        # Region filter (case-insensitive)
        if filters.region and (not msg.region or filters.region.lower() not in msg.region.lower()):
            return False

        # Zone filter: inside the zone's radius (never matches until the zone is read from HA)
        if filters.zone:
            distance = self.zone_distance(msg, sensor)
            if zone_margin_km is None:
                if distance is None or distance > sensor.zone.radius_km:
                    return False
            elif sensor.zone is None or (distance is not None and distance > sensor.zone.radius_km + zone_margin_km):
                return False

        # Text contains filter (case-insensitive)
        return not (filters.text_contains and (not msg.body or filters.text_contains.lower() not in msg.body.lower()))

    @staticmethod
    def zone_distance(msg: P2000Message, sensor: SensorConfig) -> float | None:
        """Distance from the message to the centre of the sensor's zone (km), None when unknown."""
        if sensor.zone is None:
            return None
        return distance_km(msg, sensor.zone.latitude, sensor.zone.longitude)


def load_sensors_from_config(config_data: list[dict[str, Any]]) -> list[SensorConfig]:
    """Load sensor configurations from config dict."""
    sensors = []
    seen_entity_ids: set = set()
    seen_mqtt_keys: set = set()

    for sensor_data in config_data:
        try:
            # The entity id is generated from the name, reduced to a valid HA object id
            name = sensor_data["name"]
            entity_id = slugify(str(name))
            if not entity_id:
                logger.error(f"Sensor '{name}' skipped: its name gives an empty entity_id (use letters or digits)")
                continue

            # Parse comma-separated filter strings into lists
            def parse_csv(value):
                """Parse comma-separated string into list, return None if empty."""
                if not value or not value.strip():
                    return None
                return [item.strip() for item in value.split(",") if item.strip()]

            filters = SensorFilter(
                disciplines=parse_csv(sensor_data.get("disciplines")),
                cities=parse_csv(sensor_data.get("cities")),
                capcodes=parse_csv(sensor_data.get("capcodes")),
                priorities=parse_csv(sensor_data.get("priorities")),
                region=sensor_data.get("region"),
                zone=sensor_data.get("zone") or None,
                text_contains=sensor_data.get("text_contains"),
            )

            mqtt_id = sensor_data.get("mqtt_id")
            mqtt_id = sanitize_mqtt_id(str(mqtt_id)) if mqtt_id not in (None, "") else None
            if mqtt_id is not None and mqtt_id != str(sensor_data["mqtt_id"]).strip():
                logger.warning(
                    f"Sensor '{name}' mqtt_id '{sensor_data['mqtt_id']}' sanitised to '{mqtt_id or entity_id}'"
                )

            # Two sensors sharing an entity id or MQTT key would overwrite each other
            mqtt_key = mqtt_id or entity_id
            if entity_id in seen_entity_ids or mqtt_key in seen_mqtt_keys:
                logger.error(f"Sensor '{name}' skipped: entity_id '{entity_id}' / mqtt_id '{mqtt_key}' already in use")
                continue
            seen_entity_ids.add(entity_id)
            seen_mqtt_keys.add(mqtt_key)

            sensor = SensorConfig(
                name=name,
                entity_id=entity_id,
                filters=filters,
                icon=sensor_data.get("icon", "mdi:radio-tower"),
                mqtt_id=mqtt_id or None,
                announce=sensor_data.get("announce", False),
            )

            sensors.append(sensor)
            logger.info(f"Loaded sensor '{name}' with entity_id '{entity_id}'")

        except KeyError as e:
            logger.error(f"Invalid sensor config, missing field: {e}")

    return sensors
