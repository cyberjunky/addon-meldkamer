"""BAG (Basisregistratie Adressen en Gebouwen) data import for Dutch addresses.

Uses PDOK BAG WFS service to fetch street data for woonplaatsen.
Woonplaatsen-province mapping is stored locally for reliability.
"""

import json
import logging
import sqlite3
import urllib.parse
import urllib.request
from typing import Any

# Import city abbreviations from local lookup table
try:
    from .city_abbreviations import get_abbreviation
except ImportError:

    def get_abbreviation(city: str) -> str:
        return ""


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

# Global progress tracking
_import_progress: dict[str, Any] = {
    "running": False,
    "province": "",
    "cities": 0,
    "streets": 0,
    "phase": "",
    "percent": 0,
    "status": "idle",
}


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
        "status": f"Starting import for {province}...",
    }


def fetch_all_woonplaatsen() -> list[dict[str, str]]:
    """
    Fetch ALL woonplaatsen (places) from the Netherlands using PDOK.
    Returns list of dicts with: name, gemeente (municipality), province
    """
    global _import_progress
    _import_progress["status"] = "Loading all Dutch places from PDOK..."
    _import_progress["phase"] = "Fetching woonplaatsen"

    woonplaatsen = []
    start = 0
    rows = 100  # Max per request
    max_requests = 100  # Safety limit (max ~10000 places)

    LOCATIE_BASE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

    for i in range(max_requests):
        params = {
            "q": "type:woonplaats",
            "rows": str(rows),
            "start": str(start),
            "fl": "woonplaatsnaam,gemeentenaam,provincienaam",
        }

        url = f"{LOCATIE_BASE}?{urllib.parse.urlencode(params)}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "P2000-Studio/2.1"})
            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))

            docs = data.get("response", {}).get("docs", [])
            total = data.get("response", {}).get("numFound", 0)

            if not docs:
                break

            for doc in docs:
                name = doc.get("woonplaatsnaam", "").strip()
                gemeente = doc.get("gemeentenaam", "").strip()
                province = doc.get("provincienaam", "").strip()

                if name and len(name) >= 2:
                    woonplaatsen.append({"name": name, "gemeente": gemeente, "province": province})

            start += len(docs)
            _import_progress["cities"] = len(woonplaatsen)
            _import_progress["status"] = f"Loaded {len(woonplaatsen)} places..."
            _import_progress["percent"] = min(95, int(start / max(total, 1) * 100))

            if start >= total:
                break

        except Exception as e:
            logger.warning(f"Failed to fetch woonplaatsen batch {i}: {e}")
            break

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

        imported = 0
        for i, wp in enumerate(woonplaatsen):
            name = wp["name"]
            abbreviation = get_abbreviation(name)

            # Insert or update place
            try:
                database.cursor.execute(
                    """
                    INSERT OR REPLACE INTO places (city, abbreviation) VALUES (?, ?)
                """,
                    (name, abbreviation),
                )
                imported += 1
            except Exception as e:
                logger.debug(f"Failed to insert place {name}: {e}")

            if i % 100 == 0:
                _import_progress["percent"] = int(i / len(woonplaatsen) * 100)
                _import_progress["cities"] = imported
                database.conn.commit()

        database.conn.commit()

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


def _fetch_streets_for_woonplaats(woonplaats: str) -> list[dict[str, str]]:
    """
    Fetch unique streets for a woonplaats from PDOK Locatieserver.
    Uses the 'type:weg' (street) search which returns all streets.
    Returns list of dicts with: street, postcode
    """
    # PDOK Locatieserver API - much more reliable than WFS
    LOCATIE_BASE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

    streets = {}  # dict to dedupe: street_name -> postcode
    start = 0
    rows = 100  # Max 100 per request
    max_requests = 100  # Safety limit (10000 entries max per city)

    # Search for all postcodes (type:postcode) in this woonplaats - includes street+postcode
    # fq with a quoted value handles multi-word names like "Hoek van Holland"
    for _ in range(max_requests):
        params = {
            "q": "type:postcode",
            "fq": f'woonplaatsnaam:"{woonplaats}"',
            "rows": str(rows),
            "start": str(start),
            "fl": "straatnaam,postcode",
        }

        url = f"{LOCATIE_BASE}?{urllib.parse.urlencode(params)}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "P2000-Studio/2.1"})
            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))

            docs = data.get("response", {}).get("docs", [])
            total = data.get("response", {}).get("numFound", 0)

            if not docs:
                break

            for doc in docs:
                street = doc.get("straatnaam", "").strip()
                postcode = doc.get("postcode", "").strip()

                # Skip very short street names (e.g., "A", "B") - they match too many messages
                if street and len(street) >= 3 and street not in streets:
                    streets[street] = postcode[:4] if postcode else ""

            start += len(docs)

            if start >= total:
                break

        except Exception as e:
            logger.warning(f"Failed to fetch streets for {woonplaats}: {e}")
            break

    # Convert to list format
    return [{"street": name, "postcode": pc} for name, pc in streets.items()]


def _fetch_woonplaatsen_for_province(province: str) -> list[str]:
    """
    Fetch all woonplaatsen for a province via the PDOK Locatieserver.

    Replaces the old, fragile CBS OData lookup with the same PDOK API that
    the places import already uses successfully.
    """
    global _import_progress
    woonplaatsen = set()
    start = 0
    rows = 100
    max_requests = 100  # Safety limit (~10000 places)

    LOCATIE_BASE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

    _import_progress["status"] = f"Loading woonplaatsen for {province}..."

    for _ in range(max_requests):
        params = {
            "q": "type:woonplaats",
            "fq": f'provincienaam:"{province}"',
            "rows": str(rows),
            "start": str(start),
            "fl": "woonplaatsnaam",
        }

        url = f"{LOCATIE_BASE}?{urllib.parse.urlencode(params)}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "P2000-Studio/2.1"})
            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))

            docs = data.get("response", {}).get("docs", [])
            total = data.get("response", {}).get("numFound", 0)

            if not docs:
                break

            for doc in docs:
                name = doc.get("woonplaatsnaam", "").strip()
                if name and len(name) >= 2:
                    woonplaatsen.add(name)

            start += len(docs)
            _import_progress["status"] = f"Loading woonplaatsen for {province}: {len(woonplaatsen)}/{total}..."

            if start >= total:
                break

        except Exception as e:
            logger.warning(f"Failed to fetch woonplaatsen batch for {province}: {e}")
            break

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

            # Add woonplaats to database
            try:
                abbreviation = get_abbreviation(woonplaats)
                success = database.add_place(city=woonplaats, abbreviation=abbreviation, province=province)
                if success:
                    cities_added += 1
            except Exception as e:
                logger.debug(f"Woonplaats {woonplaats} might already exist: {e}")

            # Get city_id
            city_id = None
            try:
                database.cursor.execute("SELECT id FROM places WHERE city = ? LIMIT 1", (woonplaats,))
                row = database.cursor.fetchone()
                if row:
                    city_id = row[0]
            except sqlite3.Error:
                pass

            # Fetch and add streets
            try:
                streets = _fetch_streets_for_woonplaats(woonplaats)

                for street_data in streets:
                    try:
                        success = database.add_street(
                            street=street_data["street"], city_id=city_id, postalcode=street_data["postcode"]
                        )
                        if success:
                            streets_added += 1
                    except Exception:
                        pass

            except Exception as e:
                logger.warning(f"Failed to fetch streets for {woonplaats}: {e}")

            # Log progress every 20 cities
            if (i + 1) % 20 == 0:
                logger.info(f"Progress: {i + 1}/{len(woonplaatsen)} woonplaatsen, {streets_added} streets")

        # Complete
        _import_progress.update(
            {
                "running": False,
                "cities": cities_added,
                "streets": streets_added,
                "percent": 100,
                "status": f"Complete! {cities_added} cities, {streets_added} streets",
            }
        )

        logger.info(f"Completed: {cities_added} cities, {streets_added} streets for {province}")

    except Exception as e:
        _import_progress.update({"running": False, "status": f"Error: {e}"})
        logger.error(f"Import failed: {e}")
        raise

    return {"province": province, "cities": cities_added, "streets": streets_added}
