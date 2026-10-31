"""BAG (Basisregistratie Adressen en Gebouwen) data import for Dutch addresses.

Uses PDOK BAG WFS service to fetch street data for woonplaatsen.
Woonplaatsen-province mapping is stored locally for reliability.
"""

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from .city_abbreviations import get_abbreviation

logger = logging.getLogger(__name__)

# Dutch provinces
PROVINCES = [
    "Drenthe",
    "Flevoland",
    "Friesland",
    "Gelderland",
    "Groningen",
    "Limburg",
    "Noord-Brabant",
    "Noord-Holland",
    "Overijssel",
    "Utrecht",
    "Zeeland",
    "Zuid-Holland",
]

# PDOK uses the official name for some provinces
PDOK_PROVINCE_NAMES = {"Friesland": "Fryslân"}
_FROM_PDOK_PROVINCE = {v: k for k, v in PDOK_PROVINCE_NAMES.items()}

# PDOK centroids are WKT points in WGS84, longitude first: "POINT(5.2376 52.0828)"
_POINT_RE = re.compile(r"POINT\(\s*([-\d.]+)\s+([-\d.]+)\s*\)")

LOCATIE_BASE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
PDOK_ROWS = 100  # Max rows per Locatieserver request
PDOK_MAX_START = 10000  # Locatieserver rejects start > 10000

# Global progress tracking
_import_progress: dict[str, Any] = {
    "running": False,
    "province": "",
    "cities": 0,
    "streets": 0,
    "phase": "",
    "percent": 0,
    "status": "idle",
    "warnings": [],
}
_start_lock = threading.Lock()


def get_provinces() -> list[str]:
    """Return list of available Dutch provinces."""
    return PROVINCES


def get_import_progress() -> dict[str, Any]:
    """Get current import progress."""
    return _import_progress.copy()


def reset_progress(province: str = "") -> None:
    """Reset progress state - call before starting import thread."""
    global _import_progress
    _import_progress = {
        "running": True,
        "province": province,
        "cities": 0,
        "streets": 0,
        "phase": "Starting",
        "percent": 0,
        "status": f"Starting import for {province or 'all places'}...",
        "warnings": [],
    }


def try_start_import(province: str = "") -> bool:
    """Atomically check that no import is running and mark one as started.

    Call this before starting the import thread. Returns False if an import
    is already running. Raises ValueError for an unknown province (an empty
    province means the all-places import).
    """
    if province and province not in PROVINCES:
        raise ValueError(f"Unknown province: {province!r}")
    with _start_lock:
        if _import_progress.get("running"):
            return False
        reset_progress(province)
        return True


def _warn(message: str) -> None:
    """Log a partial/truncated import and record it in the progress state."""
    logger.warning(message)
    _import_progress.setdefault("warnings", []).append(message)


def _solr_quote(value: str) -> str:
    """Quote a value for a Solr fq clause."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pdok_search(params: dict[str, str], label: str, on_page=None) -> tuple[list[dict], bool]:
    """Fetch all pages of a PDOK Locatieserver query.

    Each page is retried a few times. Returns (docs, complete) where
    complete is False if a page kept failing or the result set exceeds what
    the Locatieserver lets us page through.
    """
    docs_out: list[dict] = []
    start = 0
    while True:
        query = {**params, "rows": str(PDOK_ROWS), "start": str(start)}
        url = f"{LOCATIE_BASE}?{urllib.parse.urlencode(query)}"
        data = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "P2000-Studio/2.1"})
                with urllib.request.urlopen(req, timeout=60) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except Exception as e:
                if attempt < 2:
                    logger.debug(f"PDOK request for {label} failed (attempt {attempt + 1}), retrying: {e}")
                    time.sleep(2 * (attempt + 1))
                else:
                    logger.warning(f"PDOK request for {label} failed at offset {start}: {e}")
        if data is None:
            return docs_out, False

        docs = data.get("response", {}).get("docs", [])
        total = data.get("response", {}).get("numFound", 0)
        docs_out.extend(docs)
        start += len(docs)
        if on_page:
            on_page(start, total)

        if not docs or start >= total:
            return docs_out, True
        if start > PDOK_MAX_START:
            logger.warning(f"PDOK results for {label} truncated at {start} of {total}")
            return docs_out, False


def fetch_all_woonplaatsen() -> list[dict[str, str]]:
    """
    Fetch ALL woonplaatsen (places) from the Netherlands using PDOK.
    Returns list of dicts with: name, gemeente (municipality), province
    """
    global _import_progress
    _import_progress["status"] = "Loading all Dutch places from PDOK..."
    _import_progress["phase"] = "Fetching woonplaatsen"

    def on_page(fetched: int, total: int) -> None:
        _import_progress["cities"] = fetched
        _import_progress["status"] = f"Loaded {fetched} places..."
        _import_progress["percent"] = min(95, int(fetched / max(total, 1) * 100))

    docs, complete = _pdok_search(
        {"q": "type:woonplaats", "fl": "woonplaatsnaam,gemeentenaam,provincienaam"}, "all woonplaatsen", on_page
    )
    if not complete:
        _warn(f"Woonplaatsen list is incomplete ({len(docs)} loaded)")

    woonplaatsen = []
    for doc in docs:
        name = doc.get("woonplaatsnaam", "").strip()
        gemeente = doc.get("gemeentenaam", "").strip()
        province = doc.get("provincienaam", "").strip()
        province = _FROM_PDOK_PROVINCE.get(province, province)

        if name and len(name) >= 2:
            woonplaatsen.append({"name": name, "gemeente": gemeente, "province": province})

    logger.info(f"Fetched {len(woonplaatsen)} woonplaatsen from PDOK")
    return woonplaatsen


def import_all_places(database) -> int:
    """
    Import all Dutch woonplaatsen into the places table.
    Returns number of places imported.
    """
    global _import_progress

    _import_progress["running"] = True
    _import_progress["phase"] = "Loading all places"
    _import_progress["status"] = "Fetching all Dutch woonplaatsen..."

    try:
        woonplaatsen = fetch_all_woonplaatsen()

        if not woonplaatsen:
            _import_progress["status"] = "No places fetched"
            _import_progress["running"] = False
            return 0

        _import_progress["status"] = f"Importing {len(woonplaatsen)} places into database..."
        _import_progress["phase"] = "Importing"

        # Upsert keeps existing ids (streets link to them) and coordinates
        imported = database.upsert_places(
            [
                {"city": wp["name"], "abbreviation": get_abbreviation(wp["name"]), "province": wp["province"]}
                for wp in woonplaatsen
            ]
        )

        warnings = _import_progress.get("warnings", [])
        if warnings:
            _import_progress["status"] = f"Imported {imported} places - INCOMPLETE: {'; '.join(warnings)}"
        else:
            _import_progress["status"] = f"Imported {imported} places successfully!"
        _import_progress["percent"] = 100
        _import_progress["cities"] = imported
        _import_progress["running"] = False

        logger.info(f"Imported {imported} woonplaatsen into places table")
        return imported

    except Exception as e:
        _import_progress["status"] = f"Error: {e}"
        _import_progress["running"] = False
        logger.error(f"Failed to import all places: {e}")
        return 0


def _parse_point(value: str) -> tuple[float | None, float | None]:
    """(latitude, longitude) from a PDOK centroide_ll value, (None, None) when absent."""
    match = _POINT_RE.match(value or "")
    if not match:
        return None, None
    return float(match.group(2)), float(match.group(1))


def _fetch_streets_for_woonplaats(woonplaats: str) -> list[dict[str, Any]]:
    """
    Fetch unique streets for a woonplaats from PDOK Locatieserver.
    Uses the 'type:weg' (street) search which returns one record per street.
    Postcode areas (first 4 digits) are added best-effort from 'type:postcode',
    which returns one record per postcode and is capped by the Locatieserver
    paging limit for large cities.
    The street's centroid (centroide_ll) gives its coordinates.
    Returns list of dicts with: street, postcode, latitude, longitude
    """
    # fq with a quoted value handles multi-word names like "Hoek van Holland"
    fq = f"woonplaatsnaam:{_solr_quote(woonplaats)}"
    streets = {}  # dict to dedupe: street_name -> postcode
    coordinates = {}  # street_name -> (latitude, longitude)

    docs, complete = _pdok_search(
        {"q": "type:weg", "fq": fq, "fl": "straatnaam,centroide_ll"}, f"streets of {woonplaats}"
    )
    if not complete:
        _warn(f"Street list for {woonplaats} is incomplete ({len(docs)} loaded)")
    for doc in docs:
        street = doc.get("straatnaam", "").strip()
        # Skip very short street names (e.g., "A", "B") - they match too many messages
        if street and len(street) >= 3:
            streets.setdefault(street, "")
            coordinates.setdefault(street, _parse_point(doc.get("centroide_ll", "")))

    docs, complete = _pdok_search(
        {"q": "type:postcode", "fq": fq, "fl": "straatnaam,postcode"}, f"postcodes of {woonplaats}"
    )
    if not complete:
        logger.info(f"Postcodes for {woonplaats} incomplete - some streets will have no postcode")
    for doc in docs:
        street = doc.get("straatnaam", "").strip()
        postcode = doc.get("postcode", "").strip()
        if street and len(street) >= 3 and not streets.get(street):
            streets[street] = postcode[:4] if postcode else ""

    # Convert to list format
    return [
        {"street": name, "postcode": pc, "latitude": coordinates[name][0], "longitude": coordinates[name][1]}
        for name, pc in streets.items()
    ]


def _fetch_woonplaatsen_for_province(province: str) -> list[str]:
    """
    Fetch all woonplaatsen for a province via the PDOK Locatieserver.

    Replaces the old, fragile CBS OData lookup with the same PDOK API that
    the places import already uses successfully.
    """
    global _import_progress
    _import_progress["status"] = f"Loading woonplaatsen for {province}..."

    def on_page(fetched: int, total: int) -> None:
        _import_progress["status"] = f"Loading woonplaatsen for {province}: {fetched}/{total}..."

    docs, complete = _pdok_search(
        {
            "q": "type:woonplaats",
            "fq": f"provincienaam:{_solr_quote(PDOK_PROVINCE_NAMES.get(province, province))}",
            "fl": "woonplaatsnaam",
        },
        f"woonplaatsen of {province}",
        on_page,
    )
    if not complete:
        _warn(f"Woonplaatsen list for {province} is incomplete ({len(docs)} loaded)")

    woonplaatsen = set()
    for doc in docs:
        name = doc.get("woonplaatsnaam", "").strip()
        if name and len(name) >= 2:
            woonplaatsen.add(name)

    result = sorted(woonplaatsen)
    logger.info(f"Found {len(result)} woonplaatsen in {province}")
    return result


def import_province_data(province: str, database) -> dict[str, Any]:
    """
    Import all woonplaatsen and streets for a Dutch province.

    Args:
        province: Province name (e.g., "Zuid-Holland")
        database: Database instance with add_place and add_street methods

    Returns:
        Dict with import statistics
    """
    global _import_progress

    if province not in PROVINCES:
        _import_progress.update({"running": False, "status": f"Unknown province: {province}"})
        raise ValueError(f"Unknown province: {province!r}")

    cities_added = 0
    streets_added = 0

    try:
        # Get all woonplaatsen for this province
        woonplaatsen = _fetch_woonplaatsen_for_province(province)

        if not woonplaatsen:
            _import_progress.update({"running": False, "status": f"No woonplaatsen found for {province}"})
            return {"province": province, "cities": 0, "streets": 0}

        _import_progress["status"] = f"Found {len(woonplaatsen)} woonplaatsen in {province}"
        logger.info(f"Importing {len(woonplaatsen)} woonplaatsen for {province}")

        # Process each woonplaats
        for i, woonplaats in enumerate(woonplaatsen):
            # Update progress
            percent = int((i / len(woonplaatsen)) * 100)
            _import_progress.update(
                {
                    "percent": percent,
                    "status": f"Processing {i + 1}/{len(woonplaatsen)}: {woonplaats}",
                    "cities": cities_added,
                    "streets": streets_added,
                }
            )

            # Add woonplaats to database (fills in the province of existing places)
            try:
                city_id, created = database.upsert_place(
                    city=woonplaats, abbreviation=get_abbreviation(woonplaats), province=province
                )
                if created:
                    cities_added += 1
            except Exception as e:
                _warn(f"Failed to store woonplaats {woonplaats}: {e}")
                continue

            # Fetch and add streets (one transaction per woonplaats)
            try:
                streets = _fetch_streets_for_woonplaats(woonplaats)
                streets_added += database.add_streets(city_id, streets)
            except Exception as e:
                _warn(f"Failed to import streets for {woonplaats}: {e}")

            # Log progress every 20 cities
            if (i + 1) % 20 == 0:
                logger.info(f"Progress: {i + 1}/{len(woonplaatsen)} woonplaatsen, {streets_added} streets")

        # Complete
        status = f"Complete! {cities_added} cities, {streets_added} streets"
        warnings = _import_progress.get("warnings", [])
        if warnings:
            status += f" - INCOMPLETE, {len(warnings)} warning(s): {'; '.join(warnings[:5])}"
        _import_progress.update(
            {
                "running": False,
                "cities": cities_added,
                "streets": streets_added,
                "percent": 100,
                "status": status,
            }
        )

        logger.info(f"Completed: {cities_added} cities, {streets_added} streets for {province}")

    except Exception as e:
        _import_progress.update({"running": False, "status": f"Error: {e}"})
        logger.error(f"Import failed: {e}")
        raise

    return {"province": province, "cities": cities_added, "streets": streets_added}
