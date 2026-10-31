"""Home Assistant zones and distance calculations."""

import asyncio
import logging
import math
from dataclasses import dataclass

from .ha_client import HAClientError

logger = logging.getLogger(__name__)

HOME_ZONE = "zone.home"

EARTH_RADIUS_KM = 6371


@dataclass(frozen=True)
class Zone:
    """A Home Assistant zone: centre point and radius."""

    entity_id: str
    latitude: float
    longitude: float
    radius_km: float


def zone_from_state(state: dict) -> Zone | None:
    """Build a Zone from a Home Assistant zone state, or None when it has no coordinates."""
    attributes = state.get("attributes", {})
    try:
        return Zone(
            entity_id=state["entity_id"],
            latitude=float(attributes["latitude"]),
            longitude=float(attributes["longitude"]),
            radius_km=float(attributes.get("radius", 0)) / 1000,
        )
    except (KeyError, TypeError, ValueError):
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometers."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


def osm_url(latitude: float, longitude: float) -> str:
    """OpenStreetMap link to a point, like the mapurl OpenCage returns."""
    return f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map=17/{latitude}/{longitude}"


def distance_km(msg, latitude: float, longitude: float) -> float | None:
    """Distance from a message's coordinates to a point, rounded to 2 decimals.

    None when the message has no usable coordinates.
    """
    try:
        msg_lat = float(msg.latitude)
        msg_lon = float(msg.longitude)
    except (TypeError, ValueError):
        return None
    return round(haversine_km(msg_lat, msg_lon, latitude, longitude), 2)


async def load_zones(ha_client, entity_ids: set[str], on_zone) -> None:
    """Read zones from Home Assistant, calling on_zone(zone) for each one found.

    Retries with backoff while Home Assistant is unreachable (e.g. Core still
    starting). A zone that doesn't exist or has no coordinates is logged and
    skipped: filters on it never match.
    """
    pending = set(entity_ids)
    backoff = 5
    while pending:
        for entity_id in sorted(pending):
            try:
                state = await ha_client.get_state(entity_id)
            except HAClientError as e:
                logger.debug(f"Could not read {entity_id}: {e}")
                continue
            pending.discard(entity_id)
            zone = zone_from_state(state) if state else None
            if zone is None:
                logger.error(f"{entity_id} not found in Home Assistant or has no location - filters on it never match")
                continue
            logger.info(f"Loaded {entity_id}: {zone.latitude}, {zone.longitude}, radius {zone.radius_km:.2f} km")
            on_zone(zone)
        if pending:
            logger.warning(f"Could not read {', '.join(sorted(pending))} from Home Assistant, retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
