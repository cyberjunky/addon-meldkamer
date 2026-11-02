"""Position of a full Dutch postcode ("3068HA") from the PDOK Locatieserver, cached in the geocodes table.

A full postcode covers a handful of houses, so this is more precise than a
street centroid and needs no API key.
"""

import logging
import re

from .bag_import import _parse_point, _pdok_request
from .zones import osm_url

logger = logging.getLogger(__name__)

POSTCODE_RE = re.compile(r"\b([1-9]\d{3})\s?([A-Z]{2})\b")
_UNUSED_LETTERS = {"SA", "SD", "SS"}  # never issued


def full_postcode(text: str) -> str:
    """The first full postcode in the text as "1234AB", or ""."""
    for m in POSTCODE_RE.finditer(text or ""):
        if m.group(2) not in _UNUSED_LETTERS:
            return m.group(1) + m.group(2)
    return ""


def lookup_postcode(postcode: str) -> dict | None:
    """Centre of a full postcode from PDOK: {latitude, longitude, street, city}, or None."""
    data = _pdok_request(
        {
            "q": "type:postcode",
            "fq": f"postcode:{postcode}",
            "fl": "centroide_ll,straatnaam,woonplaatsnaam",
            "rows": "1",
        },
        f"postcode {postcode}",
    )
    docs = (data or {}).get("response", {}).get("docs", [])
    if not docs:
        return None
    lat, lon = _parse_point(docs[0].get("centroide_ll", ""))
    if lat is None or not (50.75 <= lat <= 53.55 and 3.36 <= lon <= 7.21):
        return None
    return {
        "latitude": lat,
        "longitude": lon,
        "street": docs[0].get("straatnaam", ""),
        "city": docs[0].get("woonplaatsnaam", ""),
    }


def postcode_position(database, postcode: str) -> dict | None:
    """Cached position of a full postcode: {latitude, longitude, mapurl}, looked up at PDOK once."""
    key = f"postcode:{postcode}"
    cached = database.find_geocode(key)
    if cached:
        try:
            lat, lon = float(cached["latitude"]), float(cached["longitude"])
            if 50.75 <= lat <= 53.55 and 3.36 <= lon <= 7.21:
                return {"latitude": lat, "longitude": lon, "mapurl": cached["mapurl"] or osm_url(lat, lon)}
        except (TypeError, ValueError):
            pass
    found = lookup_postcode(postcode)
    if not found:
        logger.debug(f"No PDOK position for postcode {postcode}")
        return None
    mapurl = osm_url(found["latitude"], found["longitude"])
    database.store_geocode(
        key,
        "pdok",
        found["latitude"],
        found["longitude"],
        postcode,
        found["street"],
        found["city"],
        f"{found['street']}, {postcode} {found['city']}",
        mapurl,
    )
    return {"latitude": found["latitude"], "longitude": found["longitude"], "mapurl": mapurl}
