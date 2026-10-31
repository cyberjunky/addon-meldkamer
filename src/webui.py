"""Web UI server for Meldkamer dashboard with map and history."""

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from aiohttp import web

from . import abbreviation_import, bag_import, capcode_import, vehicle_types
from .database import Database
from .decoder import Decoder
from .ha_client import HAClient, HAClientError
from .message import P2000Message
from .processor import MessageProcessor
from .sensor_manager import SensorManager
from .tts import SpeechRules

logger = logging.getLogger(__name__)

MAX_MESSAGES = 500
# Only per_page is capped tightly; page just needs to keep SQL OFFSET in range
MAX_PAGE = 1_000_000_000
# CSV imports arrive as one JSON body; aiohttp's default limit is 1 MiB
MAX_REQUEST_SIZE = 50 * 1024 * 1024
# Vehicle photo uploads (multipart, which client_max_size does not bound)
MAX_PHOTO_SIZE = 5 * 1024 * 1024
MAX_FORM_FIELD_SIZE = 4 * 1024
PHOTO_MIME_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")

ADDON_VERSION = "1.2.8"

# The dashboard's map tiles come from Home Assistant's built-in map_tiles proxy:
# browsers loading tiles straight from OpenStreetMap inside the ingress iframe
# get 403 "Access blocked", and core identifies its requests the way OSM's
# tile usage policy requires.
MAP_MAX_ZOOM = 19
# Seconds between live-stream heartbeats while nothing changes, so the
# dashboard can tell a quiet feed from a dropped connection
STREAM_HEARTBEAT = 15
# OSM asks consumers to cache tiles for at least a week
MAP_TILE_MAX_AGE = 7 * 24 * 60 * 60


def _int_param(request: web.Request, name: str, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    """Parse an integer query parameter, falling back to a clamped default."""
    try:
        value = int(request.query.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _bad_request(message: str) -> web.HTTPBadRequest:
    """Build a 400 with the same {"error": ...} JSON body the handlers return."""
    return web.HTTPBadRequest(text=json.dumps({"error": message}), content_type="application/json")


async def _json_object(request: web.Request) -> dict:
    """Parse the request body as a JSON object, raising a JSON 400 otherwise."""
    try:
        data = await request.json()
    except ValueError:
        raise _bad_request("Invalid JSON body") from None
    if not isinstance(data, dict):
        raise _bad_request("JSON body must be an object")
    return data


def _city_id(value) -> int | None:
    """Convert a street's city_id form value to int, or None when blank."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise _bad_request("city_id must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _bad_request("city_id must be an integer") from None


def _sniff_image_type(data: bytes) -> str | None:
    """Return the image MIME type from the file's magic bytes, or None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class _DictMessageView:
    """Lets a plain message dict (from P2000Message.to_dict()) be matched against
    SensorManager's filter logic, which expects P2000Message-style attribute access.
    """

    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name):
        return self._data.get(name)


# Statistics kept across restarts in the stats file
PERSISTED_STATS = ("total_messages", "by_region", "by_discipline", "start_time")


def _float_or_none(value) -> float | None:
    """Convert a form value to float, or None when empty/invalid."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_stats_file() -> Path:
    """Get stats file path."""
    return Path("/data/webui_stats_p2000.json")


class WebUI:
    """Web UI server for Meldkamer addon ingress with map support."""

    def __init__(
        self,
        port: int,
        database: Database,
        speech: SpeechRules,
        processor: MessageProcessor,
        decoder: Decoder,
        sensor_manager: SensorManager,
        ha_client: HAClient,
    ):
        self.port = port
        self.database = database
        self.speech = speech
        self.processor = processor
        self.decoder = decoder
        self.sensor_manager = sensor_manager
        self.ha_client = ha_client
        self.app = web.Application(client_max_size=MAX_REQUEST_SIZE)
        self.messages: deque = deque(maxlen=MAX_MESSAGES)
        self.stats_file = get_stats_file()
        self.stats = self._load_stats()
        self._last_stats_save = 0.0
        self._setup_routes()
        self._runner: web.AppRunner | None = None

    def _tts_changed(self) -> None:
        """Apply edited TTS replacement rules right away."""
        self.speech.reload()

    def _ignore_changed(self) -> None:
        """Apply edited global ignore filters right away."""
        self.processor.reload_ignore_filters()

    def _vehicles_changed(self) -> None:
        """Show edited vehicles on the messages already in memory too."""
        for item in self.messages:
            item.update(self.database.resolve_vehicle(item.get("body") or "", item.get("discipline") or ""))

    def _load_stats(self) -> dict:
        """Load stats from file/database and restore message history."""
        db_messages = self.database.get_recent_messages(MAX_MESSAGES)
        self.messages.extend(db_messages)
        logger.info(f"Loaded {len(db_messages)} messages from database")

        stats = {"by_region": {}, "by_discipline": {}, "start_time": datetime.now(UTC).isoformat()}
        if self.stats_file.exists():
            try:
                with open(self.stats_file) as f:
                    saved = json.load(f)
                # Keep the persisted start_time so uptime survives restarts
                stats.update({key: saved[key] for key in PERSISTED_STATS if key in saved})
            except Exception as e:
                logger.warning(f"Failed to load stats: {e}")
        # The database message count is the source of truth
        stats["total_messages"] = self.database.get_message_count()
        return stats

    def _save_stats(self) -> None:
        """Save stats to file."""
        try:
            with open(self.stats_file, "w") as f:
                json.dump({key: self.stats[key] for key in PERSISTED_STATS}, f)
            self._last_stats_save = time.monotonic()
        except Exception as e:
            logger.warning(f"Failed to save stats: {e}")

    def _save_stats_throttled(self) -> None:
        """Save stats at most once per 10 seconds."""
        if time.monotonic() - self._last_stats_save >= 10.0:
            self._save_stats()

    def _setup_routes(self) -> None:
        """Set up web routes."""
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/api/messages", self._handle_messages)
        self.app.router.add_get("/api/sensors", self._handle_sensors)
        self.app.router.add_get("/api/stats", self._handle_stats)
        self.app.router.add_get("/api/stream", self._handle_stream)
        self.app.router.add_get("/api/history/{location}", self._handle_history)
        self.app.router.add_get("/api/map-tiles/{z:[0-9]+}/{x:[0-9]+}/{y:[0-9]+}.png", self._handle_map_tile)
        self.app.router.add_get("/api/map-history", self._handle_map_history)
        self.app.router.add_get("/api/incident/{incident_id}", self._handle_incident)

        # Receiver quality
        self.app.router.add_get("/api/receiver", self._handle_receiver)

        # Database management API
        self.app.router.add_get("/api/db/capcodes", self._handle_list_capcodes)
        self.app.router.add_post("/api/db/capcodes", self._handle_add_capcode)
        self.app.router.add_put("/api/db/capcodes/{capcode}", self._handle_update_capcode)
        self.app.router.add_delete("/api/db/capcodes/{capcode}", self._handle_delete_capcode)

        self.app.router.add_get("/api/db/places", self._handle_list_places)
        self.app.router.add_post("/api/db/places", self._handle_add_place)
        self.app.router.add_put("/api/db/places/{id}", self._handle_update_place)
        self.app.router.add_delete("/api/db/places/{id}", self._handle_delete_place)

        self.app.router.add_get("/api/db/streets", self._handle_list_streets)
        self.app.router.add_post("/api/db/streets", self._handle_add_street)
        self.app.router.add_put("/api/db/streets/{id}", self._handle_update_street)
        self.app.router.add_delete("/api/db/streets/{id}", self._handle_delete_street)

        self.app.router.add_get("/api/db/geocodes", self._handle_list_geocodes)
        self.app.router.add_delete("/api/db/geocodes/{query}", self._handle_delete_geocode)
        self.app.router.add_delete("/api/db/geocodes", self._handle_delete_all_geocodes)

        # CSV Export/Import
        self.app.router.add_get("/api/db/export/{table}", self._handle_export_csv)
        self.app.router.add_post("/api/db/import/{table}", self._handle_import_csv)

        # BAG Data Import (Dutch address data by province)
        self.app.router.add_get("/api/db/bag/provinces", self._handle_list_provinces)
        self.app.router.add_post("/api/db/bag/import/{province}", self._handle_import_province)
        self.app.router.add_post("/api/db/bag/import-all-places", self._handle_import_all_places)
        self.app.router.add_get("/api/db/bag/progress", self._handle_bag_progress)

        # Capcode Import (from bommel.net)
        self.app.router.add_post("/api/db/capcodes/import-bommel", self._handle_import_bommel)
        self.app.router.add_get("/api/db/capcodes/import-progress", self._handle_capcode_progress)

        # Message history management
        self.app.router.add_get("/api/db/messages", self._handle_list_messages)
        self.app.router.add_delete("/api/db/messages/{id}", self._handle_delete_message)
        self.app.router.add_delete("/api/db/messages", self._handle_delete_all_messages)

        # Abbreviations
        self.app.router.add_get("/api/db/abbreviations", self._handle_list_abbreviations)
        self.app.router.add_post("/api/db/abbreviations", self._handle_add_abbreviation)
        self.app.router.add_delete("/api/db/abbreviations/{id}", self._handle_delete_abbreviation)
        self.app.router.add_post("/api/db/abbreviations/import", self._handle_import_abbreviations)
        self.app.router.add_post("/api/db/abbreviations/find", self._handle_find_abbreviations)

        # TTS Replacements
        self.app.router.add_get("/api/db/tts_replacements", self._handle_list_tts)
        self.app.router.add_post("/api/db/tts_replacements", self._handle_add_tts)
        self.app.router.add_put("/api/db/tts_replacements/{id}", self._handle_update_tts)
        self.app.router.add_delete("/api/db/tts_replacements/{id}", self._handle_delete_tts)
        self.app.router.add_delete("/api/db/tts_replacements", self._handle_delete_all_tts)

        # Global message filters
        self.app.router.add_get("/api/db/ignore_text", self._handle_list_ignore_text)
        self.app.router.add_post("/api/db/ignore_text", self._handle_add_ignore_text)
        self.app.router.add_put("/api/db/ignore_text/{id}", self._handle_update_ignore_text)
        self.app.router.add_delete("/api/db/ignore_text/{id}", self._handle_delete_ignore_text)
        self.app.router.add_delete("/api/db/ignore_text", self._handle_delete_all_ignore_text)
        self.app.router.add_get("/api/db/ignore_capcodes", self._handle_list_ignore_capcodes)
        self.app.router.add_post("/api/db/ignore_capcodes", self._handle_add_ignore_capcode)
        self.app.router.add_put("/api/db/ignore_capcodes/{id}", self._handle_update_ignore_capcode)
        self.app.router.add_delete("/api/db/ignore_capcodes/{id}", self._handle_delete_ignore_capcode)
        self.app.router.add_delete("/api/db/ignore_capcodes", self._handle_delete_all_ignore_capcodes)

        # Vehicle photos
        self.app.router.add_get("/api/vehicle-stock", self._handle_resolve_stock_photo)
        self.app.router.add_get("/api/vehicle-stock/{name}", self._handle_get_stock_photo)
        self.app.router.add_get("/api/vehicle-photo/{key_type}/{key_value}", self._handle_get_vehicle_photo)
        self.app.router.add_post("/api/vehicle-photo", self._handle_set_vehicle_photo)
        self.app.router.add_delete("/api/vehicle-photo/{key_type}/{key_value}", self._handle_delete_vehicle_photo)
        self.app.router.add_get("/api/db/vehicle_photos", self._handle_list_vehicle_photos)
        self.app.router.add_get("/api/db/vehicles", self._handle_list_vehicles)
        self.app.router.add_post("/api/db/vehicles", self._handle_set_vehicle)
        self.app.router.add_put("/api/db/vehicles/{number}", self._handle_update_vehicle)
        self.app.router.add_delete("/api/db/vehicles/{number}", self._handle_delete_vehicle)
        self.app.router.add_get("/api/vehicle-categories", self._handle_list_vehicle_categories)

        # Database reset
        self.app.router.add_delete("/api/db/reset", self._handle_reset_database)
        # Clear a single table
        self.app.router.add_delete("/api/db/clear/{table}", self._handle_clear_table)

    async def start(self) -> None:
        """Start the web server."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Web UI listening on port {self.port}")

    async def stop(self) -> None:
        """Stop the web server."""
        self._save_stats()
        if self._runner:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None

    def add_message(self, msg: P2000Message) -> None:
        """Add a new (already stored) P2000 message and update stats."""
        self.messages.appendleft(msg.to_dict())
        self.stats["total_messages"] += 1

        # Track by region and discipline (split multi-discipline combos so each
        # discipline is counted individually instead of as a combined key)
        region = msg.region or "Unknown"
        self.stats["by_region"][region] = self.stats["by_region"].get(region, 0) + 1
        disciplines = [d.strip() for d in (msg.discipline or "").split(",") if d.strip()] or ["Unknown"]
        for discipline in disciplines:
            self.stats["by_discipline"][discipline] = self.stats["by_discipline"].get(discipline, 0) + 1

        self._save_stats_throttled()

    def mark_repeat(self, msg: P2000Message) -> None:
        """The pager network resent a message: update its repeat count."""
        for item in self.messages:
            if item.get("id") == msg.id:
                item["repeats"] = msg.repeats
                return

    def _stats_payload(self) -> dict:
        """Stats plus the live receiver/decoder state, for the dashboard."""
        decoder = self.decoder
        geocoder = self.processor.geocoder
        config = decoder.config
        return {
            **self.stats,
            "network_name": config.network_name,
            "frequency": config.frequency,
            "sample_rate": config.sample_rate,
            "decoder": config.decoder,
            "decoder_running": decoder.is_running,
            "decoder_error": decoder.last_error,
            "receiver_online": decoder.stats.online(config.offline_after_minutes),
            "frames_last_minute": decoder.stats.frames_last_minute(),
            "messages_last_hour": decoder.stats.messages_last_hour(),
            "open_incidents": self.processor.incidents.open_count(),
            "incident_window_minutes": config.incident_window_minutes,
            "ppm": decoder.ppm,
            "receiver_source": decoder.source.label,
            "device_type": decoder.device_type,
            "device_driver": decoder.device_driver,
            "geocoding_configured": config.opencage_enabled,
            "geocoding_enabled": geocoder.enabled,
            "geocode_rate_remaining": geocoder.rate_remaining,
            "geocode_rate_limited": geocoder.rate_limited,
            "geocode_only_sensor_matches": config.geocode_only_sensor_matches,
            "geocode_skipped": self.processor.geocode_skipped,
            "sensor_zones": [
                {
                    "name": s.name,
                    "radius_km": s.zone.radius_km,
                    "center_lat": s.zone.latitude,
                    "center_lon": s.zone.longitude,
                }
                for s in self.sensor_manager.sensors
                if s.zone
            ],
        }

    def update_db_stats(self, places: int, streets: int, capcodes: int, texts: int, geocodes: int) -> None:
        """Update database statistics for the Info tab."""
        self.stats["db_places"] = places
        self.stats["db_streets"] = streets
        self.stats["db_capcodes"] = capcodes
        self.stats["db_texts"] = texts
        self.stats["db_geocodes"] = geocodes

    async def _handle_index(self, request: web.Request) -> web.Response:
        html = DASHBOARD_HTML.replace("__ADDON_VERSION__", ADDON_VERSION)
        return web.Response(
            text=html,
            content_type="text/html",
            # Never let the browser serve a stale cached page after an addon update
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    def _with_fresh_abbreviations(self, messages: list[dict]) -> list[dict]:
        """Re-derive each message's abbreviation tags from its body against the
        current Texts glossary, instead of the frozen text captured at parse
        time - so editing an abbreviation's meaning (e.g. "A2" from "geen
        spoed" to "zonder spoed") is reflected immediately on already-received
        messages, not just newly-decoded ones.
        """
        result = []
        for m in messages:
            body = m.get("body")
            if body:
                m = {**m, "abbreviations": self.database.find_abbreviations_in_text(body, m.get("discipline", ""))}
            result.append(m)
        return result

    def _filter_by_sensor(self, messages: list[dict], sensor_name: str) -> list[dict]:
        """Filter message dicts down to those matching a configured sensor's criteria.

        Reuses SensorManager's actual filter-matching logic (discipline, radius/
        haversine, priority, etc.) via a thin attribute-access adapter, rather than
        re-implementing that logic in JS - keeps "what counts as a match" identical
        to what the HA/MQTT sensor publishing path uses.
        """
        manager = self.sensor_manager
        if not sensor_name:
            return messages
        if sensor_name == "__any__":
            return [m for m in messages if manager.get_matching_sensors(_DictMessageView(m))]
        sensor = next((s for s in manager.sensors if s.name == sensor_name), None)
        if not sensor:
            return messages
        return [m for m in messages if manager.matches(_DictMessageView(m), sensor)]

    async def _handle_sensors(self, request: web.Request) -> web.Response:
        """List configured sensors, for the dashboard's filter toggle."""
        return web.json_response([{"name": s.name, "icon": s.icon} for s in self.sensor_manager.sensors])

    async def _handle_messages(self, request: web.Request) -> web.Response:
        messages = self._filter_by_sensor(list(self.messages), request.query.get("sensor", ""))
        return web.json_response(self._with_fresh_abbreviations(messages))

    async def _handle_stats(self, request: web.Request) -> web.Response:
        return web.json_response(self._stats_payload())

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        await response.prepare(request)
        sensor_name = request.query.get("sensor", "")
        last_sent = None
        last_write = time.monotonic()
        try:
            while True:
                latest = self._filter_by_sensor(list(self.messages), sensor_name)[:50]
                data = json.dumps({"stats": self._stats_payload(), "latest": self._with_fresh_abbreviations(latest)})
                # Only push when something changed (the client recomputes uptime
                # locally), with a heartbeat in between
                if data != last_sent:
                    await response.write(f"data: {data}\n\n".encode())
                    last_sent = data
                    last_write = time.monotonic()
                elif time.monotonic() - last_write >= STREAM_HEARTBEAT:
                    await response.write(b"event: ping\ndata: {}\n\n")
                    last_write = time.monotonic()
                await asyncio.sleep(2)
        except (ConnectionResetError, TimeoutError, asyncio.CancelledError):
            pass
        return response

    async def _handle_history(self, request: web.Request) -> web.Response:
        """Get message history for a location (address or city)."""
        location = request.match_info.get("location", "")
        limit = _int_param(request, "limit", 20, maximum=100)
        history = self.database.get_history_by_address(location, limit=limit)
        if not history:
            # Nothing at this exact address: search body/street/city instead
            history = self.database.list_messages(page=1, per_page=limit, search=location)["items"]
        return web.json_response(history[:limit])

    async def _handle_incident(self, request: web.Request) -> web.Response:
        """All messages of one incident, oldest first."""
        return web.json_response(self.database.get_incident_messages(request.match_info["incident_id"]))

    async def _handle_map_history(self, request: web.Request) -> web.Response:
        """Located messages of the last `hours` hours, for the map's history layer."""
        hours = _int_param(request, "hours", 24, maximum=24 * 365)
        return web.json_response(self.database.get_map_points(hours))

    async def _handle_receiver(self, request: web.Request) -> web.Response:
        """Receiver decode quality per minute."""
        decoder = self.decoder
        return web.json_response(
            {
                **decoder.stats.snapshot(),
                "offline_after_minutes": decoder.config.offline_after_minutes,
                "sources": [
                    {"label": s.label, "type": s.receiver_type, "ppm": s.ppm, "active": s is decoder.source}
                    for s in decoder.sources
                ],
                "ppm": decoder.ppm,
            }
        )

    # =========================================================================
    # Database Management API Handlers
    # =========================================================================

    async def _handle_list_capcodes(self, request: web.Request) -> web.Response:
        """List capcodes with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        result = self.database.list_capcodes(page, per_page, search)
        return web.json_response(result)

    async def _handle_add_capcode(self, request: web.Request) -> web.Response:
        """Add a new capcode."""
        data = await _json_object(request)
        success = self.database.add_capcode(
            data.get("capcode", ""),
            data.get("discipline", ""),
            data.get("region", ""),
            data.get("location", ""),
            data.get("description", ""),
            data.get("remark", ""),
        )
        if success:
            return web.json_response({"success": True})
        return web.json_response({"error": "Failed to add capcode (may already exist)"}, status=400)

    async def _handle_update_capcode(self, request: web.Request) -> web.Response:
        """Update a capcode."""
        capcode = request.match_info.get("capcode", "")
        data = await _json_object(request)
        success = self.database.update_capcode(
            capcode,
            data.get("discipline", ""),
            data.get("region", ""),
            data.get("location", ""),
            data.get("description", ""),
            data.get("remark", ""),
        )
        return web.json_response({"success": success})

    async def _handle_delete_capcode(self, request: web.Request) -> web.Response:
        """Delete a capcode."""
        capcode = request.match_info.get("capcode", "")
        success = self.database.delete_capcode(capcode)
        return web.json_response({"success": success})

    async def _handle_list_places(self, request: web.Request) -> web.Response:
        """List places with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        result = self.database.list_places(page, per_page, search)
        return web.json_response(result)

    async def _handle_add_place(self, request: web.Request) -> web.Response:
        """Add a new place."""
        data = await _json_object(request)
        success = self.database.add_place(
            data.get("city", ""),
            data.get("abbreviation", ""),
            data.get("province", ""),
            _float_or_none(data.get("latitude")),
            _float_or_none(data.get("longitude")),
        )
        if success:
            return web.json_response({"success": True})
        return web.json_response({"error": "Failed to add place (may already exist)"}, status=400)

    async def _handle_update_place(self, request: web.Request) -> web.Response:
        """Update a place."""
        try:
            place_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            place_id = 0
        data = await _json_object(request)
        success = self.database.update_place(
            place_id,
            data.get("city", ""),
            data.get("abbreviation", ""),
            data.get("province", ""),
            _float_or_none(data.get("latitude")),
            _float_or_none(data.get("longitude")),
        )
        return web.json_response({"success": success})

    async def _handle_delete_place(self, request: web.Request) -> web.Response:
        """Delete a place."""
        try:
            place_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            place_id = 0
        success = self.database.delete_place(place_id)
        return web.json_response({"success": success})

    async def _handle_list_streets(self, request: web.Request) -> web.Response:
        """List streets with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        city_id = request.query.get("city_id")
        try:
            city_id = int(city_id) if city_id else None
        except (TypeError, ValueError):
            city_id = None
        result = self.database.list_streets(page, per_page, search, city_id)
        return web.json_response(result)

    async def _handle_add_street(self, request: web.Request) -> web.Response:
        """Add a new street."""
        data = await _json_object(request)
        success = self.database.add_street(
            data.get("street", ""), _city_id(data.get("city_id")), data.get("postalcode", "")
        )
        if success:
            return web.json_response({"success": True})
        return web.json_response({"error": "Failed to add street"}, status=400)

    async def _handle_update_street(self, request: web.Request) -> web.Response:
        """Update a street."""
        try:
            street_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            street_id = 0
        data = await _json_object(request)
        success = self.database.update_street(
            street_id, data.get("street", ""), _city_id(data.get("city_id")), data.get("postalcode", "")
        )
        return web.json_response({"success": success})

    async def _handle_delete_street(self, request: web.Request) -> web.Response:
        """Delete a street."""
        try:
            street_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            street_id = 0
        success = self.database.delete_street(street_id)
        return web.json_response({"success": success})

    async def _handle_list_geocodes(self, request: web.Request) -> web.Response:
        """List cached geocodes with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        result = self.database.list_geocodes(page, per_page, search)
        return web.json_response(result)

    async def _handle_delete_geocode(self, request: web.Request) -> web.Response:
        """Delete a cached geocode."""
        query = request.match_info.get("query", "")
        success = self.database.delete_geocode(query)
        return web.json_response({"success": success})

    async def _handle_delete_all_geocodes(self, request: web.Request) -> web.Response:
        """Delete all cached geocodes."""
        count = self.database.delete_all_geocodes()
        return web.json_response({"success": True, "deleted": count})

    async def _handle_export_csv(self, request: web.Request) -> web.Response:
        """Export a table to CSV."""
        table = request.match_info.get("table", "")

        exporters = {
            "capcodes": self.database.export_capcodes_csv,
            "places": self.database.export_places_csv,
            "streets": self.database.export_streets_csv,
            "messages": self.database.export_messages_csv,
            "abbreviations": self.database.export_abbreviations_csv,
            "tts_replacements": self.database.export_tts_csv,
            "ignore_text": self.database.export_ignore_text_csv,
            "ignore_capcodes": self.database.export_ignore_capcodes_csv,
            "vehicles": self.database.export_vehicles_csv,
        }
        exporter = exporters.get(table)
        if exporter is None:
            return web.json_response({"error": "Invalid table"}, status=400)
        # Off the event loop so SSE and the decoder feed don't stall on big tables
        csv_data = await asyncio.to_thread(exporter)

        return web.Response(
            text=csv_data, content_type="text/csv", headers={"Content-Disposition": f"attachment; filename={table}.csv"}
        )

    async def _handle_import_csv(self, request: web.Request) -> web.Response:
        """Import CSV data to a table."""
        table = request.match_info.get("table", "")

        data = await _json_object(request)
        csv_content = data.get("csv", "")
        replace = bool(data.get("replace", False))
        if not isinstance(csv_content, str):
            return web.json_response({"error": "csv must be a string"}, status=400)

        importers = {
            "capcodes": self.database.import_capcodes_csv,
            "places": self.database.import_places_csv,
            "streets": self.database.import_streets_csv,
            "vehicles": self.database.import_vehicles_csv,
        }
        if table == "messages":
            return web.json_response(
                {"error": "Messages cannot be imported. They come from the P2000 decoder."}, status=400
            )
        importer = importers.get(table)
        if importer is None:
            return web.json_response({"error": "Invalid table"}, status=400)
        if busy := self._import_running():
            return web.json_response({"error": busy}, status=409)

        try:
            # Off the event loop so SSE and the decoder feed don't stall on big files
            result = await asyncio.to_thread(importer, csv_content, replace)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if table == "vehicles":
            self._vehicles_changed()
        return web.json_response(result)

    async def _handle_list_provinces(self, request: web.Request) -> web.Response:
        """List available Dutch provinces for BAG import."""
        provinces = bag_import.get_provinces()
        return web.json_response({"provinces": provinces})

    async def _handle_import_province(self, request: web.Request) -> web.Response:
        """Start BAG import for a specific Dutch province in background thread."""

        province = request.match_info.get("province", "")
        if province not in bag_import.PROVINCES:
            return web.json_response({"error": f"Unknown province '{province}'"}, status=400)

        # Check if already running
        if bag_import.get_import_progress().get("running"):
            return web.json_response({"error": "Import already in progress"}, status=400)

        # Reset progress BEFORE starting thread to avoid race condition
        bag_import.reset_progress(province)

        # Start import in background thread
        def run_import():
            bag_import.import_province_data(province, self.database)

        thread = threading.Thread(target=run_import, daemon=True)
        thread.start()

        # Return immediately - client will poll for progress
        return web.json_response({"started": True, "province": province})

    async def _handle_import_all_places(self, request: web.Request) -> web.Response:
        """Import ALL Dutch woonplaatsen (places) into database."""
        # Check if already running
        if bag_import.get_import_progress().get("running"):
            return web.json_response({"error": "Import already in progress"}, status=400)

        # Reset progress BEFORE starting thread so the first poll can't see the previous run's result
        bag_import.reset_progress("all places")

        # Start import in background thread
        def run_import():
            bag_import.import_all_places(self.database)
            # Update stats after import
            stats = self.database.get_stats()
            self.update_db_stats(
                stats["places"], stats["streets"], stats["capcodes"], stats["texts"], stats["geocodes"]
            )

        thread = threading.Thread(target=run_import, daemon=True)
        thread.start()

        return web.json_response({"started": True, "message": "Importing all Dutch places..."})

    async def _handle_bag_progress(self, request: web.Request) -> web.Response:
        """Get current BAG import progress."""
        return web.json_response(bag_import.get_import_progress())

    async def _handle_import_bommel(self, request: web.Request) -> web.Response:
        """Start capcode import from bommel.net in background thread."""
        # Check if already running
        if capcode_import.get_import_progress().get("running"):
            return web.json_response({"error": "Import already in progress"}, status=400)

        # Reset progress BEFORE starting thread to avoid race condition
        capcode_import.reset_progress()

        # Start import in background thread
        def run_import():
            capcode_import.import_all_capcodes(self.database)
            # Refresh DB counts so the Info tab updates right away
            stats = self.database.get_stats()
            self.update_db_stats(
                stats["places"], stats["streets"], stats["capcodes"], stats["texts"], stats["geocodes"]
            )

        thread = threading.Thread(target=run_import, daemon=True)
        thread.start()

        # Return immediately - client will poll for progress
        return web.json_response({"started": True, "message": "Import started"})

    async def _handle_capcode_progress(self, request: web.Request) -> web.Response:
        """Get current capcode import progress."""
        return web.json_response(capcode_import.get_import_progress())

    # =========================================================================
    # MESSAGE HISTORY HANDLERS
    # =========================================================================

    async def _handle_list_messages(self, request: web.Request) -> web.Response:
        """List messages with pagination and search."""

        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        discipline = request.query.get("discipline", "")
        city = request.query.get("city", "")

        result = self.database.list_messages(
            page=page, per_page=per_page, search=search, discipline=discipline, city=city
        )
        return web.json_response(result)

    async def _handle_delete_message(self, request: web.Request) -> web.Response:
        """Delete a single message."""

        try:
            message_id = int(request.match_info["id"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "Invalid id"}, status=400)
        success = self.database.delete_message(message_id)

        if success:
            return web.json_response({"success": True})
        return web.json_response({"error": "Message not found"}, status=404)

    async def _handle_delete_all_messages(self, request: web.Request) -> web.Response:
        """Delete all messages."""

        count = self.database.delete_all_messages()
        # Clear in-memory cache and counters so the dashboard reflects the wipe
        self.messages.clear()
        self.stats["total_messages"] = 0
        self.stats["by_region"] = {}
        self.stats["by_discipline"] = {}
        self._save_stats()
        return web.json_response({"success": True, "deleted": count})

    @staticmethod
    def _import_running() -> str | None:
        """Return an error message while a BAG or capcode import thread is running."""
        if bag_import.get_import_progress().get("running"):
            return "A BAG import is running; wait for it to finish"
        if capcode_import.get_import_progress().get("running"):
            return "A capcode import is running; wait for it to finish"
        return None

    async def _handle_clear_table(self, request: web.Request) -> web.Response:
        """Delete all rows from a single table (whitelisted in database.clear_table)."""
        table = request.match_info.get("table", "")
        if busy := self._import_running():
            return web.json_response({"error": busy}, status=409)
        count = self.database.clear_table(table)
        if count < 0:
            return web.json_response({"error": f"Cannot clear table '{table}'"}, status=400)
        if table == "messages":
            # Keep in-memory cache and counters in sync
            self.messages.clear()
            self.stats["total_messages"] = 0
            self.stats["by_region"] = {}
            self.stats["by_discipline"] = {}
            self._save_stats()
        elif table == "tts_replacements":
            self._tts_changed()
        elif table in ("ignore_text", "ignore_capcodes"):
            self._ignore_changed()
        elif table == "vehicles":
            self._vehicles_changed()
        return web.json_response({"success": True, "deleted": count})

    async def _handle_reset_database(self, request: web.Request) -> web.Response:
        """Reset the database - delete all data and recreate schema."""
        if busy := self._import_running():
            return web.json_response({"error": busy}, status=409)

        try:
            # Drop all tables
            tables = [
                "capcodes",
                "places",
                "streets",
                "geocodes",
                "messages",
                "stats",
                "abbreviations",
                "tts_replacements",
                "ignore_text",
                "ignore_capcodes",
                "vehicle_photos",
                "vehicles",
            ]
            for table in tables:
                with contextlib.suppress(Exception):
                    self.database.cursor.execute(f"DROP TABLE IF EXISTS {table}")
            self.database.conn.commit()

            # Recreate schema and drop cached text-matching data
            self.database._migrate_schema()
            self.database.invalidate_text_caches()
            self._tts_changed()
            self._ignore_changed()

            # Clear in-memory message cache and counters
            self.messages.clear()
            self.stats["total_messages"] = 0
            self.stats["by_region"] = {}
            self.stats["by_discipline"] = {}
            self._save_stats()

            # Update stats
            stats = self.database.get_stats()
            self.update_db_stats(
                stats["places"], stats["streets"], stats["capcodes"], stats["texts"], stats["geocodes"]
            )

            return web.json_response({"success": True, "message": "Database reset successfully"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_list_abbreviations(self, request: web.Request) -> web.Response:
        """List abbreviations with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        result = self.database.list_abbreviations(page, per_page, search)
        return web.json_response(result)

    async def _handle_add_abbreviation(self, request: web.Request) -> web.Response:
        """Add a new abbreviation."""
        data = await _json_object(request)
        abbrev = data.get("abbreviation", "")
        full_text = data.get("full_text", "")
        if not abbrev or not full_text:
            return web.json_response({"error": "abbreviation and full_text are required"}, status=400)
        success = self.database.add_abbreviation(abbrev, full_text)
        return web.json_response({"success": success})

    async def _handle_delete_abbreviation(self, request: web.Request) -> web.Response:
        """Delete an abbreviation by id."""
        try:
            abbrev_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "Invalid id"}, status=400)
        success = self.database.delete_abbreviation(abbrev_id)
        return web.json_response({"success": success})

    async def _handle_import_abbreviations(self, request: web.Request) -> web.Response:
        """Import abbreviations from Bommel."""
        try:
            count = abbreviation_import.import_abbreviations(self.database)
            return web.json_response({"success": True, "count": count, "message": f"Imported {count} abbreviations"})
        except Exception as e:
            logger.error(f"Failed to import abbreviations: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_find_abbreviations(self, request: web.Request) -> web.Response:
        """Find abbreviations in text and return their full meanings."""
        data = await _json_object(request)
        try:
            text = data.get("text", "")
            found = self.database.find_abbreviations_in_text(text)
            return web.json_response({"abbreviations": found})
        except Exception as e:
            logger.error(f"Failed to find abbreviations: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_list_tts(self, request: web.Request) -> web.Response:
        """List TTS replacements with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        result = self.database.list_tts_replacements(page, per_page, search)
        return web.json_response(result)

    async def _handle_add_tts(self, request: web.Request) -> web.Response:
        """Add a TTS replacement."""
        data = await _json_object(request)
        pattern = data.get("pattern", "")
        replacement = data.get("replacement", "")
        if not pattern:
            return web.json_response({"error": "Pattern is required"}, status=400)
        success = self.database.add_tts_replacement(pattern, replacement)
        if success:
            self._tts_changed()
        return web.json_response({"success": success})

    async def _handle_update_tts(self, request: web.Request) -> web.Response:
        """Update a TTS replacement."""
        try:
            tts_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "Invalid id"}, status=400)
        data = await _json_object(request)
        pattern = data.get("pattern", "")
        if not pattern:
            return web.json_response({"error": "Pattern is required"}, status=400)
        enabled = data.get("enabled")  # None = keep current value
        success = self.database.update_tts_replacement(tts_id, pattern, data.get("replacement", ""), enabled)
        if success:
            self._tts_changed()
        return web.json_response({"success": success})

    async def _handle_delete_tts(self, request: web.Request) -> web.Response:
        """Delete a TTS replacement."""
        try:
            tts_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            tts_id = 0
        success = self.database.delete_tts_replacement(tts_id)
        if success:
            self._tts_changed()
        return web.json_response({"success": success})

    async def _handle_delete_all_tts(self, request: web.Request) -> web.Response:
        """Delete all TTS replacements."""
        count = self.database.delete_all_tts_replacements()
        if count:
            self._tts_changed()
        return web.json_response({"success": True, "deleted": count})

    async def _handle_list_ignore_text(self, request: web.Request) -> web.Response:
        """List ignore-text patterns with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        result = self.database.list_ignore_text(page, per_page, search)
        return web.json_response(result)

    async def _handle_add_ignore_text(self, request: web.Request) -> web.Response:
        """Add an ignore-text pattern."""
        data = await _json_object(request)
        pattern = data.get("pattern", "")
        if not pattern:
            return web.json_response({"error": "Pattern is required"}, status=400)
        success = self.database.add_ignore_text(pattern)
        if success:
            self._ignore_changed()
        return web.json_response({"success": success})

    async def _handle_update_ignore_text(self, request: web.Request) -> web.Response:
        """Update an ignore-text pattern."""
        try:
            rule_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "Invalid id"}, status=400)
        data = await _json_object(request)
        pattern = data.get("pattern", "")
        if not pattern:
            return web.json_response({"error": "Pattern is required"}, status=400)
        enabled = data.get("enabled")  # None = keep current value
        success = self.database.update_ignore_text(rule_id, pattern, enabled)
        if success:
            self._ignore_changed()
        return web.json_response({"success": success})

    async def _handle_delete_ignore_text(self, request: web.Request) -> web.Response:
        """Delete an ignore-text pattern."""
        try:
            rule_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            rule_id = 0
        success = self.database.delete_ignore_text(rule_id)
        if success:
            self._ignore_changed()
        return web.json_response({"success": success})

    async def _handle_delete_all_ignore_text(self, request: web.Request) -> web.Response:
        """Delete all ignore-text patterns."""
        count = self.database.delete_all_ignore_text()
        if count:
            self._ignore_changed()
        return web.json_response({"success": True, "deleted": count})

    async def _handle_list_ignore_capcodes(self, request: web.Request) -> web.Response:
        """List ignored capcodes with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        result = self.database.list_ignore_capcodes(page, per_page, search)
        return web.json_response(result)

    async def _handle_add_ignore_capcode(self, request: web.Request) -> web.Response:
        """Add an ignored capcode."""
        data = await _json_object(request)
        capcode = data.get("capcode", "")
        if not capcode:
            return web.json_response({"error": "Capcode is required"}, status=400)
        success = self.database.add_ignore_capcode(capcode)
        if success:
            self._ignore_changed()
        return web.json_response({"success": success})

    async def _handle_update_ignore_capcode(self, request: web.Request) -> web.Response:
        """Update an ignored capcode."""
        try:
            rule_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "Invalid id"}, status=400)
        data = await _json_object(request)
        capcode = data.get("capcode", "")
        if not capcode:
            return web.json_response({"error": "Capcode is required"}, status=400)
        enabled = data.get("enabled")  # None = keep current value
        success = self.database.update_ignore_capcode(rule_id, capcode, enabled)
        if success:
            self._ignore_changed()
        return web.json_response({"success": success})

    async def _handle_delete_ignore_capcode(self, request: web.Request) -> web.Response:
        """Delete an ignored capcode."""
        try:
            rule_id = int(request.match_info.get("id", 0))
        except (TypeError, ValueError):
            rule_id = 0
        success = self.database.delete_ignore_capcode(rule_id)
        if success:
            self._ignore_changed()
        return web.json_response({"success": success})

    async def _handle_delete_all_ignore_capcodes(self, request: web.Request) -> web.Response:
        """Delete all ignored capcodes."""
        count = self.database.delete_all_ignore_capcodes()
        if count:
            self._ignore_changed()
        return web.json_response({"success": True, "deleted": count})

    async def _handle_map_tile(self, request: web.Request) -> web.Response:
        """Serve an OpenStreetMap raster tile via Home Assistant's map tile proxy."""
        z, x, y = (int(request.match_info[k]) for k in ("z", "x", "y"))
        if z > MAP_MAX_ZOOM or x >= 2**z or y >= 2**z:
            return web.Response(status=404)
        try:
            body = await self.ha_client.get_map_tile(z, x, y)
        except HAClientError as e:
            logger.debug(f"Map tile {z}/{x}/{y} failed: {e}")
            return web.Response(status=502)
        return web.Response(
            body=body,
            content_type="image/png",
            headers={"Cache-Control": f"private, max-age={MAP_TILE_MAX_AGE}"},
        )

    async def _handle_resolve_stock_photo(self, request: web.Request) -> web.Response:
        """Which bundled stock photo fits a vehicle category (?category=), with its credit."""
        name = vehicle_types.stock_image_for(request.query.get("category", ""))
        if not name:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response({"name": name, **vehicle_types.STOCK_IMAGES[name]})

    async def _handle_get_stock_photo(self, request: web.Request) -> web.Response:
        """Serve a bundled stock photo."""
        name = request.match_info.get("name", "")
        if name not in vehicle_types.STOCK_IMAGES:
            return web.json_response({"error": "Not found"}, status=404)
        return web.FileResponse(
            Path(vehicle_types.STOCK_IMAGES_DIR) / f"{name}.webp",
            headers={"Content-Type": "image/webp", "Cache-Control": "public, max-age=86400"},
        )

    async def _handle_get_vehicle_photo(self, request: web.Request) -> web.Response:
        """Serve a stored vehicle photo's raw image bytes."""
        key_type = request.match_info.get("key_type", "")
        key_value = request.match_info.get("key_value", "")
        photo = self.database.get_vehicle_photo(key_type, key_value)
        if not photo:
            return web.json_response({"error": "Not found"}, status=404)
        # Rows stored before upload validation may hold an SVG or a parameterised type
        mime_type = photo["mime_type"].split(";", 1)[0].strip().lower()
        if mime_type not in PHOTO_MIME_TYPES:
            mime_type = "application/octet-stream"
        return web.Response(
            body=photo["image"],
            content_type=mime_type,
            headers={"Cache-Control": "no-cache, must-revalidate", "X-Content-Type-Options": "nosniff"},
        )

    async def _handle_set_vehicle_photo(self, request: web.Request) -> web.Response:
        """Upload/replace a vehicle photo (multipart form: key_type, key_value, label, file)."""
        try:
            reader = await request.multipart()
            fields: dict[str, str] = {}
            image_bytes = b""
            async for part in reader:
                if not isinstance(part, aiohttp.BodyPartReader):
                    continue
                if part.name == "file":
                    image_bytes = await self._read_part_limited(part, MAX_PHOTO_SIZE)
                    if image_bytes is None:
                        return web.json_response({"error": "Image too large (max 5MB)"}, status=400)
                elif part.name:
                    value = await self._read_part_limited(part, MAX_FORM_FIELD_SIZE)
                    if value is None:
                        return web.json_response({"error": f"Field '{part.name}' too large"}, status=400)
                    fields[part.name] = value.decode("utf-8", errors="replace")
        except Exception as e:
            return web.json_response({"error": f"Invalid upload: {e}"}, status=400)

        key_type = fields.get("key_type", "")
        key_value = fields.get("key_value", "").strip()
        label = fields.get("label", "").strip()

        if key_type not in ("number", "category"):
            return web.json_response({"error": "key_type must be 'number' or 'category'"}, status=400)
        if not key_value:
            return web.json_response({"error": "key_value is required"}, status=400)
        if not image_bytes:
            return web.json_response({"error": "file is required"}, status=400)
        # Trust the file's magic bytes, not the client's Content-Type: SVG (script)
        # or a parameterised type must never be stored and served back same-origin
        mime_type = _sniff_image_type(image_bytes)
        if mime_type not in PHOTO_MIME_TYPES:
            return web.json_response({"error": "file must be a JPEG, PNG, WebP or GIF image"}, status=400)

        success = self.database.set_vehicle_photo(key_type, key_value, mime_type, image_bytes, label)
        return web.json_response({"success": success})

    @staticmethod
    async def _read_part_limited(part: aiohttp.BodyPartReader, limit: int) -> bytes | None:
        """Read a multipart part in chunks, returning None once it exceeds limit bytes."""
        chunks = []
        size = 0
        while chunk := await part.read_chunk():
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    async def _handle_delete_vehicle_photo(self, request: web.Request) -> web.Response:
        """Delete a vehicle photo."""
        key_type = request.match_info.get("key_type", "")
        key_value = request.match_info.get("key_value", "")
        success = self.database.delete_vehicle_photo(key_type, key_value)
        return web.json_response({"success": success})

    async def _handle_list_vehicles(self, request: web.Request) -> web.Response:
        """List known vehicles with pagination and search."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        return web.json_response(self.database.list_vehicles(page, per_page, request.query.get("search", "")))

    async def _save_vehicle(self, number: str, data: dict) -> web.Response:
        category = str(data.get("category", "")).strip()
        if category and category not in vehicle_types.CATEGORY_ICONS:
            return web.json_response({"error": f"Unknown category '{category}'"}, status=400)
        success = self.database.set_vehicle(number, category, str(data.get("description", "")).strip())
        if success:
            self._vehicles_changed()
        return web.json_response({"success": success})

    async def _handle_set_vehicle(self, request: web.Request) -> web.Response:
        """Add or replace a vehicle (number, category, description)."""
        data = await _json_object(request)
        number = str(data.get("number", "")).strip()
        if not number:
            return web.json_response({"error": "Number is required"}, status=400)
        return await self._save_vehicle(number, data)

    async def _handle_update_vehicle(self, request: web.Request) -> web.Response:
        """Edit a vehicle's category/description (the number is the key)."""
        return await self._save_vehicle(request.match_info.get("number", ""), await _json_object(request))

    async def _handle_delete_vehicle(self, request: web.Request) -> web.Response:
        """Delete a vehicle."""
        success = self.database.delete_vehicle(request.match_info.get("number", ""))
        if success:
            self._vehicles_changed()
        return web.json_response({"success": success})

    async def _handle_list_vehicle_categories(self, request: web.Request) -> web.Response:
        """List known vehicle categories (for the standalone photo-upload dropdown)."""
        categories = [{"category": k, "icon": v} for k, v in vehicle_types.CATEGORY_ICONS.items()]
        return web.json_response({"categories": categories})

    async def _handle_list_vehicle_photos(self, request: web.Request) -> web.Response:
        """List vehicle photos with pagination and search, for the Settings tab."""
        page = _int_param(request, "page", 1, maximum=MAX_PAGE)
        per_page = _int_param(request, "per_page", 50)
        search = request.query.get("search", "")
        result = self.database.list_vehicle_photos(page, per_page, search)
        return web.json_response(result)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meldkamer</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
    <style>
        /* Dark palette by default; light when Home Assistant (or, outside it,
           the system) uses a light theme - see applyTheme() */
        :root {
            --ha-card-background: #12161c;
            --ha-background: #0b0e12;
            --primary-text-color: #e7ebf0;
            --secondary-text-color: #9ba5b3;
            --primary-color: #4c8dff;
            --accent-color: #f5a524;
            --divider-color: #242b35;
            --card-radius: 8px;
            --card-border: 1px solid var(--divider-color);
            --prio-1: #ff6159;
            --prio-1-text: #1a0706;
            --prio-2: #f5a524;
            --prio-2-text: #1e1402;
            --prio-3: #2a3340;
            --prio-3-text: #c8cfd8;
            --disc-ambulance: #f2c94c;
            --disc-brandweer: #ff7a6b;
            --disc-politie: #6ea8ff;
            --disc-other: #a0aab8;
            --success-color: #5fd68a;
            --danger-color: #ff6159;
            --row-alt: #0e1217;
            --selected-bg: #1c2c4a;
            --incident-bg: #3a1d1d;
            --incident-text: #ffb4ae;
            --tag-bg: rgba(76,141,255,0.14);
            --tag-text: #b9d3ff;
            --map-bg: #10151b;
            --shadow-sm: 0 1px 2px rgba(0,0,0,0.25);
            --shadow-lg: 0 8px 30px rgba(0,0,0,0.45);
            --hover-bg: rgba(140,150,160,0.10);
            --mono: 'IBM Plex Mono', ui-monospace, monospace;
            color-scheme: dark;
        }
        :root[data-theme="light"] {
            --ha-card-background: #ffffff;
            --ha-background: #eef1f4;
            --primary-text-color: #1a1f26;
            --secondary-text-color: #5b6573;
            --primary-color: #2457c5;
            --divider-color: #d5dbe2;
            --prio-1: #d93a32;
            --prio-1-text: #ffffff;
            --prio-2-text: #2a1a00;
            --prio-3: #e2e7ed;
            --prio-3-text: #3d4652;
            --disc-ambulance: #c99700;
            --disc-brandweer: #e0483b;
            --disc-politie: #2f6fe0;
            --disc-other: #8a94a3;
            --success-color: #1e7a44;
            --danger-color: #d93a32;
            --row-alt: #fafbfc;
            --selected-bg: #e3edff;
            --incident-bg: #fde7e5;
            --incident-text: #a6241c;
            --tag-bg: #e8effc;
            --tag-text: #1f4aa8;
            --map-bg: #e8ece6;
            --shadow-sm: 0 1px 2px rgba(16,24,40,0.06);
            --shadow-lg: 0 20px 60px rgba(16,24,40,0.25);
            --hover-bg: rgba(16,24,40,0.045);
            color-scheme: light;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; }
        body {
            font-family: 'IBM Plex Sans', Roboto, -apple-system, 'Segoe UI', sans-serif;
            background: var(--ha-background);
            color: var(--primary-text-color);
            line-height: 1.45;
            font-size: 13px;
            -webkit-font-smoothing: antialiased;
            display: flex; flex-direction: column; overflow: hidden;
        }
        button, select, input { font-family: inherit; }
        .mono { font-family: var(--mono); }
        .container { max-width: 1500px; margin: 0 auto; padding: 16px 20px 32px; }

        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-thumb { background: rgba(140,150,160,0.35); border-radius: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }

        /* ---------- Top bar ---------- */
        .topbar {
            flex-shrink: 0; min-height: 44px; display: flex; align-items: center; gap: 14px;
            padding: 6px 14px; background: var(--ha-card-background); border-bottom: var(--card-border);
            white-space: nowrap;
        }
        .topbar > * { flex-shrink: 0; }
        @media (max-width: 1600px) { .brand-sub { display: none; } }
        .brand { display: flex; align-items: center; gap: 8px; }
        .brand svg { width: 20px; height: 20px; color: var(--primary-color); }
        .brand-name { font-weight: 600; letter-spacing: 0.04em; }
        .brand-sub { font-family: var(--mono); font-size: 12px; color: var(--secondary-text-color); }

        .tabs { display: flex; gap: 2px; padding: 2px; border-radius: 6px; background: var(--ha-background); border: var(--card-border); }
        .tab {
            height: 28px; padding: 0 12px; border: none; border-radius: 4px; cursor: pointer;
            display: inline-flex; align-items: center; justify-content: center; line-height: 1; white-space: nowrap;
            background: transparent; color: var(--secondary-text-color); font-size: 12.5px; font-weight: 500;
        }
        .tab:hover { color: var(--primary-text-color); }
        .tab.active { background: var(--primary-color); color: #fff; }
        .tab-sep { width: 1px; margin: 4px 4px; background: var(--divider-color); }

        .kpis { display: flex; align-items: center; gap: 16px; font-family: var(--mono); font-size: 12px; color: var(--secondary-text-color); }
        .kpis b { color: var(--primary-text-color); font-weight: 500; }
        .rx { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; }
        .rx::before { content: ''; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
        .rx.online { color: var(--success-color); }
        .rx.offline { color: var(--prio-2); }
        .rx.stopped { color: var(--danger-color); }
        .spacer { flex: 1; }

        /* Sensor filter: the view tabs' segmented control, scrolling sideways when space runs out */
        .topbar > .filter-tabs { flex-shrink: 1; min-width: 0; align-items: center; overflow-x: auto; scrollbar-width: none; }
        .topbar > .filter-tabs:empty { display: none; }
        /* On narrower screens the sensor buttons become a dropdown */
        .filter-select {
            display: none; height: 28px; padding: 0 6px 0 10px; border: 0; border-radius: 4px; cursor: pointer;
            background: var(--primary-color); color: #fff; font: inherit; font-size: 12.5px; font-weight: 500;
        }
        .filter-select option { background: var(--ha-card-background); color: var(--primary-text-color); }
        .icon-tab { width: 32px; padding: 0; font-size: 14px; }
        .icon-tab.off { opacity: 0.45; }
        .filter-label {
            padding: 0 8px 0 8px; font-size: 11px; font-weight: 600; letter-spacing: 0.07em;
            text-transform: uppercase; color: var(--secondary-text-color);
        }

        .icon-btn {
            width: 28px; height: 28px; border-radius: 4px; border: var(--card-border);
            background: var(--ha-card-background); color: var(--primary-text-color);
            display: inline-flex; align-items: center; justify-content: center;
            cursor: pointer; font-size: 13px; transition: background 0.15s, opacity 0.15s;
        }
        .icon-btn:hover { background: var(--hover-bg); }
        .icon-btn.off { opacity: 0.4; }
        .icon-btn.paused { background: rgba(255,152,0,0.18); color: #ff9800; }
        .clock { font-family: var(--mono); font-size: 22px; font-weight: 600; letter-spacing: 0.04em; line-height: 1; }

        /* ---------- Status bar ---------- */
        .statusbar {
            flex-shrink: 0; height: 26px; display: flex; align-items: center; gap: 18px; padding: 0 14px;
            background: var(--ha-card-background); border-top: var(--card-border);
            font-family: var(--mono); font-size: 11px; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden;
        }
        .conn { display: inline-flex; align-items: center; gap: 6px; }
        .conn .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--secondary-text-color); }
        .conn.live { color: var(--success-color); }
        .conn.live .dot { background: var(--success-color); }
        .conn.reconnecting { color: var(--prio-2); }
        .conn.reconnecting .dot { background: var(--prio-2); }
        .decoder-ok { color: var(--success-color); }
        .decoder-warn { color: var(--prio-2); }
        .decoder-bad { color: var(--danger-color); }

        /* ---------- Views ---------- */
        .views { flex: 1; min-height: 0; position: relative; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .view.active { display: flex; height: 100%; }
        .pages { height: 100%; overflow-y: auto; }
        .pages:not(:has(.tab-content.active)) { display: none; }

        .panel-label {
            height: 34px; flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; gap: 8px;
            padding: 0 14px; border-bottom: var(--card-border); background: var(--ha-card-background);
            color: var(--secondary-text-color); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase;
        }
        .empty-state { color: var(--secondary-text-color); text-align: center; padding: 40px 20px; font-size: 12px; }

        .prio {
            display: inline-block; min-width: 30px; padding: 1px 4px; border-radius: 3px; text-align: center;
            font-family: var(--mono); font-size: 11.5px; font-weight: 600;
            background: var(--prio-3); color: var(--prio-3-text);
        }
        .prio.p1 { background: var(--prio-1); color: var(--prio-1-text); }
        .prio.p2 { background: var(--prio-2); color: var(--prio-2-text); }
        .disc { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; overflow: hidden; }
        .disc::before { content: ''; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--disc-other); }
        .disc.ambulance::before { background: var(--disc-ambulance); }
        .disc.brandweer::before { background: var(--disc-brandweer); }
        .disc.politie::before { background: var(--disc-politie); }
        .badge { padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; white-space: nowrap; background: var(--tag-bg); color: var(--tag-text); }
        .badge.incident { background: var(--incident-bg); color: var(--incident-text); }
        .pill { padding: 5px 12px; border-radius: 999px; font-size: 12px; font-weight: 600; }

        /* Console: message table | map + docked detail */
        .console { flex: 1; min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) var(--console-side, minmax(520px, 44%)); gap: 1px; background: var(--divider-color); }
        .feed { display: flex; flex-direction: column; min-height: 0; background: var(--ha-card-background); }
        .feed-head, .feed-row {
            display: grid; grid-template-columns: 66px 42px minmax(0, 104px) minmax(140px, 1fr) minmax(0, 170px) minmax(0, 66px);
            gap: 10px; padding: 0 14px; align-items: center;
        }
        .feed-head {
            height: 30px; flex-shrink: 0; background: var(--ha-background); border-bottom: var(--card-border);
            color: var(--secondary-text-color); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase;
        }
        .feed-rows { flex: 1; min-height: 0; overflow-y: auto; }
        .feed-row {
            width: 100%; height: 30px; border: 0; border-bottom: 1px solid var(--divider-color); text-align: left;
            background: transparent; color: inherit; font-size: 13px; cursor: pointer;
        }
        .feed-row:nth-child(even) { background: var(--row-alt); }
        .feed-row:hover { background: var(--hover-bg); }
        .feed-row.selected { background: var(--selected-bg); box-shadow: inset 3px 0 0 var(--primary-color); }
        .feed-row .time, .feed-row .unit { font-family: var(--mono); color: var(--secondary-text-color); }
        .feed-row .text { font-family: var(--mono); font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .feed-row .text .badge { margin-left: 8px; font-family: inherit; }
        .feed-row .place, .feed-row .unit { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .side { position: relative; display: flex; flex-direction: column; min-height: 0; background: var(--ha-card-background); }
        .side .map-slot { flex: 0 0 var(--console-map, 38%); min-height: 120px; }
        .detail-dock { flex: 1; min-height: 0; overflow-y: auto; border-top: var(--card-border); }

        /* Wallboard: big map + incident list, ticker below */
        .wallboard { flex: 1; min-width: 0; display: flex; flex-direction: column; }
        .wall-main { flex: 1; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr) var(--wall-list, 400px); }
        .wall-map { position: relative; min-height: 0; }
        .wall-map .map-slot { position: absolute; inset: 0; }
        .wall-kpis {
            position: absolute; top: 12px; left: 56px; z-index: 900; display: flex; gap: 1px;
            background: var(--divider-color); border: var(--card-border); border-radius: 4px; overflow: hidden;
        }
        .wall-kpi { padding: 7px 14px; background: var(--ha-card-background); }
        .wall-kpi .v { font-family: var(--mono); font-size: 22px; font-weight: 600; line-height: 1.2; }
        .wall-kpi .l { font-size: 10.5px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--secondary-text-color); }
        .wall-kpi .v.p1 { color: var(--prio-1); }
        .wall-kpi .v.grip { color: var(--prio-2); }
        .wall-list { position: relative; display: flex; flex-direction: column; min-height: 0; background: var(--ha-card-background); border-left: var(--card-border); }
        .wall-items { flex: 1; min-height: 0; overflow-y: auto; }
        .incident-card {
            width: 100%; display: flex; flex-direction: column; gap: 5px; padding: 10px 14px; text-align: left;
            border: 0; border-bottom: var(--card-border); background: transparent; color: inherit; cursor: pointer; font-size: 13px;
        }
        .incident-card:hover { background: var(--hover-bg); }
        .incident-card.selected { background: var(--selected-bg); }
        .incident-card.stale { opacity: 0.6; }
        .incident-card .top { display: flex; align-items: center; gap: 8px; }
        .incident-card .title { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .incident-card .age { margin-left: auto; font-family: var(--mono); font-size: 12px; color: var(--secondary-text-color); }
        .incident-card .place { color: var(--secondary-text-color); font-size: 12.5px; }
        .incident-card .tags { display: flex; gap: 6px; flex-wrap: wrap; }
        .ticker { height: 132px; flex-shrink: 0; display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 1px; background: var(--divider-color); border-top: var(--card-border); }
        .tick {
            min-width: 0; padding: 12px 16px; display: flex; flex-direction: column; gap: 7px; text-align: left;
            border: 0; background: var(--ha-card-background); color: inherit; cursor: pointer;
        }
        .tick:hover { background: var(--hover-bg); }
        .tick .meta { display: flex; gap: 10px; align-items: center; font-family: var(--mono); font-size: 14px; color: var(--secondary-text-color); }
        .tick .meta .prio { font-size: 13px; padding: 2px 6px; }
        .tick .text {
            font-family: var(--mono); font-size: 15px; line-height: 1.4; overflow: hidden;
            display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
        }

        /* Lanes: one column per discipline + map */
        .lanes { flex: 1; min-width: 0; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)) var(--lanes-side, max(440px, calc(100% - 1200px))); gap: 1px; background: var(--divider-color); }
        .lane { display: flex; flex-direction: column; min-height: 0; background: var(--ha-background); }
        .lane-head {
            height: 38px; flex-shrink: 0; display: flex; align-items: center; gap: 8px; padding: 0 12px;
            background: var(--ha-card-background); border-bottom: 2px solid var(--disc-other); font-weight: 600;
        }
        .lane-head.ambulance { border-bottom-color: var(--disc-ambulance); }
        .lane-head.brandweer { border-bottom-color: var(--disc-brandweer); }
        .lane-head.politie { border-bottom-color: var(--disc-politie); }
        .lane-head .count { margin-left: auto; font-family: var(--mono); font-size: 12px; font-weight: 400; color: var(--secondary-text-color); }
        .lane-items { flex: 1; min-height: 0; overflow-y: auto; padding: 6px; display: flex; flex-direction: column; gap: 6px; }
        .lane-card {
            width: 100%; display: flex; flex-direction: column; gap: 3px; padding: 8px 10px; text-align: left;
            border: 1px solid transparent; border-radius: 4px; background: var(--ha-card-background); color: inherit; cursor: pointer; font-size: 13px;
        }
        .lane-card:hover { border-color: var(--divider-color); }
        .lane-card.selected { background: var(--selected-bg); border-color: var(--primary-color); }
        .lane-card .meta { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 12px; color: var(--secondary-text-color); }
        .lane-card .meta .unit { margin-left: auto; }
        .lane-card .text { font-family: var(--mono); font-size: 12px; line-height: 1.4; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
        .lane-card .place { color: var(--secondary-text-color); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .lanes-side { position: relative; display: flex; flex-direction: column; min-height: 0; background: var(--ha-card-background); }
        .lanes-side .map-slot { flex: 1; min-height: 0; }

        /* ---------- Draggable dividers between panes ---------- */
        .splitter { z-index: 1100; touch-action: none; transition: background 0.15s; }
        .splitter.col { position: absolute; top: 0; bottom: 0; left: -3px; width: 7px; cursor: col-resize; }
        .splitter.row { flex: 0 0 5px; cursor: row-resize; background: var(--divider-color); }
        .splitter:hover, .splitter.dragging { background: var(--primary-color); }

        /* ---------- Map (one Leaflet map, moved into the active view's slot) ---------- */
        .map-slot { position: relative; background: var(--map-bg); }
        #map-root { position: absolute; inset: 0; }
        #map { position: absolute; inset: 0; background: var(--map-bg); }
        :root:not([data-theme="light"]) .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(0.9) contrast(0.9); }
        .map-history {
            position: absolute; top: 8px; right: 8px; z-index: 1000;
            display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end;
            padding: 4px 8px; border-radius: 4px; max-width: calc(100% - 70px);
            background: var(--ha-card-background); border: var(--card-border); box-shadow: var(--shadow-sm);
            font-size: 12px;
        }
        .map-history select { padding: 2px 6px; font-size: 12px; }
        .map-history input[type=range] { width: 160px; }
        .map-history .until { color: var(--secondary-text-color); white-space: nowrap; }
        .leaflet-container { font-family: inherit; }

        /* ---------- Message detail (docked in Console, popup elsewhere) ---------- */
        #detail { display: flex; flex-direction: column; }
        #detail-content { padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }
        .detail-head { display: flex; align-items: center; gap: 4px 10px; flex-wrap: wrap; }
        .detail-head h2 { font-size: 15px; font-weight: 600; }
        .detail-head .when { margin-left: auto; font-family: var(--mono); font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
        .detail-body {
            padding: 9px 11px; border-radius: 4px; background: var(--ha-background); border: var(--card-border);
            font-family: var(--mono); font-size: 12.5px; line-height: 1.5; word-break: break-word;
        }
        .abbrev-tags { display: flex; gap: 6px; flex-wrap: wrap; }
        .abbrev-tag { padding: 2px 8px; border-radius: 999px; font-size: 11.5px; background: var(--tag-bg); color: var(--tag-text); }
        #detail { container: detail / inline-size; }
        .detail-main { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; align-items: start; }
        .detail-left { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
        .detail-nav { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 10px 16px; border-top: var(--card-border); }
        .history-section[hidden], .detail-nav[hidden] { display: none; }
        .badge.escalated { margin-left: 8px; text-transform: none; letter-spacing: 0; }
        @container detail (min-width: 640px) {
            .detail-fields { grid-template-columns: 96px minmax(0, 1fr) 96px minmax(0, 1fr); }
            .detail-fields dt.wide { grid-column: 1; }
            .detail-fields dd.wide { grid-column: 2 / -1; }
            .vehicle-side { width: 210px; }
            .vehicle-visual { height: 150px; font-size: 56px; }
        }
        @container detail (max-width: 460px) {
            .detail-head .when { margin-left: 0; flex-basis: 100%; }
            .detail-main { grid-template-columns: 1fr; }
            .vehicle-side { width: auto; }
        }
        .detail-fields { display: grid; grid-template-columns: 130px minmax(0, 1fr); gap: 6px 10px; font-size: 12.5px; }
        .detail-fields dt { color: var(--secondary-text-color); }
        .detail-fields dd { word-break: break-word; }
        .raw-label { color: var(--secondary-text-color); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }
        .raw-block pre {
            margin-top: 6px; padding: 8px 10px; background: var(--ha-background); border-radius: 4px;
            font-family: var(--mono); font-size: 11px; white-space: pre-wrap; word-break: break-all;
        }

        .vehicle-side { width: 150px; display: flex; flex-direction: column; align-items: stretch; gap: 6px; text-align: center; }
        .vehicle-visual {
            height: 112px; border-radius: 6px; cursor: pointer; overflow: hidden;
            background: var(--ha-background); border: 1px dashed var(--divider-color);
            display: flex; align-items: center; justify-content: center; font-size: 44px;
        }
        .vehicle-visual:hover { opacity: 0.85; }
        .vehicle-visual img { width: 100%; height: 100%; object-fit: cover; }
        /* Stock photos are cut out: show the whole vehicle on the slot's background */
        .vehicle-visual img.stock { object-fit: contain; padding: 6px; }
        .vehicle-credit { font-size: 10px; color: var(--secondary-text-color); margin-top: -2px; }
        .vehicle-credit[hidden] { display: none; }
        .vehicle-credit a { color: inherit; }
        .vehicle-category-label { font-size: 12px; font-weight: 600; }
        .vehicle-number-label { font-size: 11px; color: var(--secondary-text-color); font-family: var(--mono); }
        .vehicle-side .btn { justify-content: center; padding: 4px 10px; font-size: 11.5px; }

        .history-section { padding: 10px 16px 14px; border-top: var(--card-border); display: flex; flex-direction: column; gap: 6px; }
        .history-section h4 { color: var(--secondary-text-color); font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.06em; }
        .history-item { display: grid; grid-template-columns: 104px minmax(0, 1fr); gap: 8px; font-size: 12px; }
        .disc-tag { padding: 1px 7px; border-radius: 3px; font-size: 11.5px; border: 1px solid currentColor; color: var(--disc-other); }
        .disc-tag.ambulance { color: var(--disc-ambulance); }
        .disc-tag.brandweer { color: var(--disc-brandweer); }
        .disc-tag.politie { color: var(--disc-politie); }
        .disc-tag.grip { color: var(--prio-2); }
        .history-item .time { font-family: var(--mono); color: var(--secondary-text-color); }
        .history-item .text { font-family: var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        /* Wallboard and Lanes: the detail floats in the map's left corner (top on the
           Wallboard, below its counts; bottom on Lanes) */
        .detail-float {
            position: absolute; z-index: 950; top: 84px; left: 12px;
            width: min(560px, calc(100% - 24px)); max-height: calc(100% - 96px);
            display: none; flex-direction: column; overflow: hidden;
            background: var(--ha-card-background); border: var(--card-border); border-radius: 8px; box-shadow: var(--shadow-lg);
        }
        .detail-float.open { display: flex; }
        .detail-float.bottom { top: auto; bottom: 12px; }
        .detail-float-head {
            display: flex; align-items: center; justify-content: space-between; padding: 6px 8px 6px 16px;
            border-bottom: var(--card-border); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase;
            color: var(--secondary-text-color);
        }
        .detail-float-body { flex: 1; min-height: 0; overflow-y: auto; }

        /* Settings & Info pages */
        .panel { background: var(--ha-card-background); border: var(--card-border); border-radius: 6px; }
        .panel-head {
            display: flex; justify-content: space-between; align-items: center;
            padding: 12px 14px; border-bottom: 1px solid var(--divider-color);
        }
        .panel-title { font-size: 13px; font-weight: 600; }
        .panel-sub { color: var(--secondary-text-color); font-size: 12px; }
        .panel-body { padding: 16px 18px; }

        /* ---------- Buttons & inputs ---------- */
        .btn {
            display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 12px;
            border-radius: 4px; border: var(--card-border); white-space: nowrap;
            background: var(--ha-card-background); color: var(--primary-text-color);
            font-size: 13px; font-weight: 500; cursor: pointer;
            transition: background 0.12s, opacity 0.12s, filter 0.12s;
        }
        .btn:hover { background: var(--hover-bg); }
        .btn:disabled { opacity: 0.45; cursor: default; }
        .btn-primary { background: var(--primary-color); border-color: var(--primary-color); color: #fff; }
        .btn-primary:hover { background: var(--primary-color); filter: brightness(1.1); }
        .btn-danger { background: transparent; color: var(--danger-color); border-color: var(--danger-color); }
        .btn-danger:hover { background: var(--incident-bg); }
        .btn-danger-solid { background: var(--danger-color); border-color: var(--danger-color); color: #fff; }
        .btn-danger-solid:hover { background: var(--danger-color); filter: brightness(1.1); }

        .input {
            height: 30px; padding: 0 10px; border-radius: 4px; border: var(--card-border);
            background: var(--ha-background); color: var(--primary-text-color);
            font-size: 13px; outline: none; font-family: inherit;
            transition: border-color 0.12s;
        }
        .input:focus { border-color: var(--primary-color); }
        textarea.input { height: auto; padding: 8px 10px; resize: vertical; line-height: 1.4; font-family: var(--mono); }

        /* ---------- Info tab ---------- */
        .info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
        .info-card h3, .section-title {
            font-size: 11.5px; font-weight: 600; color: var(--secondary-text-color);
            text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 4px;
            display: flex; align-items: center; gap: 8px;
        }
        .info-row {
            display: flex; justify-content: space-between; gap: 12px;
            padding: 9px 0; border-bottom: 1px solid var(--divider-color); font-size: 14px;
        }
        .info-row:last-child { border-bottom: none; }
        .reception { display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 12px 32px; }
        .info-label { color: var(--secondary-text-color); }
        .info-value { color: var(--primary-text-color); font-family: var(--mono); font-size: 13.5px; text-align: right; }

        .info-value a { color: var(--primary-color); text-decoration: none; }
        .info-value a:hover { text-decoration: underline; }
        .support-links { display: flex; gap: 8px; flex-wrap: wrap; }
        .support-links .btn { text-decoration: none; }

        .stats-panel { margin-top: 14px; }
        .stats-charts-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 28px; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--divider-color);
        }
        .chart-title {
            display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
            font-size: 11.5px; font-weight: 600; color: var(--secondary-text-color);
            text-transform: uppercase; letter-spacing: 0.07em;
        }
        .chart-row { display: flex; align-items: center; gap: 20px; }
        .chart-legend { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 7px; }
        .chart-legend-item {
            display: flex; align-items: center; gap: 8px; font-size: 13.5px;
            color: var(--primary-text-color); overflow: hidden;
        }
        .chart-legend-item .swatch { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
        .chart-legend-item .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .chart-legend-item .count { font-family: var(--mono); color: var(--secondary-text-color); margin-left: auto; padding-left: 8px; flex-shrink: 0; }

        /* ---------- Settings tab ---------- */
        .db-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
        .db-toolbar-left { display: flex; align-items: center; gap: 10px; }
        .db-toolbar-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .db-table-title { font-size: 12px; font-weight: 600; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.07em; }

        .db-table-wrap { max-height: 480px; overflow-y: auto; }
        .db-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
        .db-table th {
            text-align: left; padding: 9px 12px; font-size: 11px; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.06em;
            color: var(--secondary-text-color); border-bottom: 1px solid var(--divider-color);
            position: sticky; top: 0; background: var(--ha-card-background); z-index: 1;
        }
        .db-table td { padding: 9px 12px; border-bottom: 1px solid var(--divider-color); color: var(--primary-text-color); }
        .db-table tbody tr:hover { background: var(--hover-bg); }
        .row-del {
            border: none; background: transparent; color: var(--secondary-text-color);
            cursor: pointer; padding: 4px 7px; border-radius: 4px; font-size: 13px; line-height: 1;
        }
        .row-del:hover { color: var(--danger-color); background: var(--incident-bg); }

        .db-pagination {
            display: flex; justify-content: space-between; align-items: center;
            padding: 12px 14px; border-top: 1px solid var(--divider-color);
            font-family: var(--mono); font-size: 12.5px; color: var(--secondary-text-color);
        }

        .progress { height: 8px; border-radius: 999px; background: var(--divider-color); overflow: hidden; }
        .progress > div { height: 100%; background: var(--primary-color); border-radius: 999px; transition: width 0.3s; }

        /* ---------- Modal ---------- */
        .modal-overlay {
            position: fixed; inset: 0;
            display: none; align-items: center; justify-content: center;
            z-index: 3000; padding: 20px;
        }
        .modal-overlay.open { display: flex; }
        .modal {
            background: var(--ha-card-background); border: var(--card-border);
            border-radius: 6px; width: min(520px, 100%); max-height: 85vh;
            overflow-y: auto; box-shadow: var(--shadow-lg);
        }
        .modal-head {
            display: flex; justify-content: space-between; align-items: center;
            padding: 14px 18px; border-bottom: 1px solid var(--divider-color);
        }
        .modal-title { font-size: 14px; font-weight: 600; }
        .modal-close {
            border: none; background: transparent; color: var(--secondary-text-color);
            font-size: 18px; cursor: pointer; line-height: 1; padding: 2px 6px; border-radius: 4px;
        }
        .modal-close:hover { background: var(--hover-bg); color: var(--primary-text-color); }
        .modal-body { padding: 16px 18px; display: flex; flex-direction: column; gap: 12px; font-size: 13px; }
        .modal-foot {
            padding: 14px 18px; border-top: 1px solid var(--divider-color);
            display: flex; justify-content: flex-end; gap: 8px;
        }
        .form-field label {
            display: block; font-size: 11px; font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.05em; color: var(--secondary-text-color); margin-bottom: 5px;
        }
        .form-field .input { width: 100%; }
        .form-check { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }

        /* ---------- Toasts ---------- */
        #toast-root {
            position: fixed; right: 18px; bottom: 18px; z-index: 4000;
            display: flex; flex-direction: column; gap: 8px;
        }
        .toast {
            padding: 10px 14px; border-radius: 8px; max-width: 340px;
            background: var(--ha-card-background); border: var(--card-border);
            border-left: 3px solid var(--primary-color); box-shadow: var(--shadow-lg);
            font-size: 13px; animation: toast-in 0.18s ease-out;
        }
        .toast.success { border-left-color: var(--success-color); }
        .toast.error { border-left-color: var(--danger-color); }
        @keyframes toast-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

        /* ---------- Settings landing cards ---------- */
        .db-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 12px; }
        .db-card {
            border: var(--card-border); border-radius: 4px;
            background: var(--ha-background); padding: 14px 16px;
            display: flex; flex-direction: column; gap: 9px;
        }
        .db-card-head { display: flex; align-items: center; gap: 8px; }
        .db-step {
            width: 22px; height: 22px; border-radius: 3px; flex-shrink: 0;
            background: var(--tag-bg); color: var(--tag-text);
            font-family: var(--mono); font-size: 12px; font-weight: 600;
            display: inline-flex; align-items: center; justify-content: center;
        }
        .db-card-title { font-size: 14px; font-weight: 600; flex: 1; }
        .db-card-status { font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 3px; }
        .db-card-status.ready { color: var(--success-color); border: 1px solid currentColor; }
        .db-card-status.empty { color: var(--prio-2); border: 1px solid currentColor; }
        .db-card-desc { font-size: 13px; color: var(--secondary-text-color); line-height: 1.5; }
        .db-card-count { font-size: 12.5px; color: var(--secondary-text-color); }
        .db-card-count span { font-family: var(--mono); color: var(--primary-text-color); font-size: 15px; font-weight: 500; }
        .db-card-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: auto; }

        /* Smaller screens (laptops, 1280-1400 px wide or up to 800 px high) */
        @media (max-width: 1400px) {
            .brand-name, .kpis .kpi-extra, .sensor-filter-pill { display: none; }
            .filter-select { display: block; }
            .clock { font-size: 18px; }
            .console { grid-template-columns: minmax(0, 1fr) var(--console-side, minmax(460px, 40%)); }
            .feed-head, .feed-row { grid-template-columns: 62px 42px minmax(0, 96px) minmax(160px, 1fr) minmax(0, 150px); }
            .feed-head .unit, .feed-row .unit { display: none; }
        }
        @media (max-height: 800px) {
            .ticker { height: 108px; }
            .tick { padding: 10px 14px; gap: 5px; }
            .tick .text { font-size: 14px; -webkit-line-clamp: 2; }
        }
        @media (max-width: 1200px) {
            .console { grid-template-columns: minmax(0, 1fr) var(--console-side, 420px); }
            .lanes { grid-template-columns: repeat(4, minmax(0, 1fr)) var(--lanes-side, 380px); }
            .wall-main { grid-template-columns: minmax(0, 1fr) var(--wall-list, 320px); }
            .feed-head, .feed-row { grid-template-columns: 62px 42px minmax(0, 96px) minmax(140px, 1fr) minmax(0, 130px); }
            .feed-head .unit, .feed-row .unit { display: none; }
        }
        @media (max-width: 800px) {
            body { overflow: auto; }
            .view.active { height: auto; }
            .console, .wall-main { grid-template-columns: 1fr; }
            .feed-rows { max-height: 60vh; }
            .wall-map { height: 50vh; }
            .lanes { grid-template-columns: 1fr; }
            .ticker { grid-template-columns: repeat(2, minmax(0, 1fr)); height: auto; }
            .feed-head, .feed-row { grid-template-columns: 58px 42px minmax(0, 1fr); }
            .feed-head .disc-col, .feed-row .disc, .feed-head .place-col, .feed-row .place { display: none; }
        }
        /* Phones: the top bar wraps into two rows - view tabs, then status, filter and toggles */
        .tab-icon { display: none; }
        @media (max-width: 700px) {
            .topbar { flex-wrap: wrap; gap: 6px 8px; padding: 6px 8px; }
            .topbar > .brand, .topbar > .spacer, .clock { display: none; }
            .topbar > nav.tabs { order: 1; flex: 1 1 100%; }
            nav.tabs .tab { flex: 1; padding: 0 6px; }
            nav.tabs .tab[data-tab="settings"], nav.tabs .tab[data-tab="info"] { flex: 0 0 36px; }
            .tab-label { display: none; }
            nav.tabs .tab:not([data-tab="settings"]):not([data-tab="info"]) .tab-label { display: inline; }
            .tab-icon { display: inline; font-size: 15px; }
            .kpis { order: 2; gap: 8px; }
            .rx { font-size: 0; gap: 0; }  /* the coloured dot only; the status bar spells it out */
            .topbar > .filter-tabs { order: 3; flex: 1 1 0; }
            .topbar > .filter-tabs .filter-label { display: none; }
            .topbar > .filter-tabs .filter-select { width: 100%; min-width: 0; }
            .topbar > .tabs[role="group"] { order: 4; }
        }
    </style>
</head>
<body>
    <header class="topbar">
        <div class="brand">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14M8 8a6 6 0 0 0 0 8M16 8a6 6 0 0 1 0 8"/><circle cx="12" cy="12" r="1.6"/></svg>
            <span class="brand-name">MELDKAMER</span>
            <span class="brand-sub"><span id="network-name">P2000 FLEX</span> · <span id="receiver-type"></span></span>
        </div>
        <nav class="tabs" aria-label="Views">
            <button type="button" class="tab" data-tab="console" onclick="switchTab('console')">Console</button>
            <button type="button" class="tab" data-tab="wallboard" onclick="switchTab('wallboard')">Wallboard</button>
            <button type="button" class="tab" data-tab="lanes" onclick="switchTab('lanes')">Lanes</button>
            <span class="tab-sep" aria-hidden="true"></span>
            <button type="button" class="tab" data-tab="settings" onclick="switchTab('settings')" title="Settings"><span class="tab-label">Settings</span><span class="tab-icon" aria-hidden="true">⚙</span></button>
            <button type="button" class="tab" data-tab="info" onclick="switchTab('info')" title="Info"><span class="tab-label">Info</span><span class="tab-icon" aria-hidden="true">ⓘ</span></button>
        </nav>
        <div class="kpis">
            <span id="rx-status" class="rx stopped">DECODER STOPPED</span>
            <span>MSG <b id="kpi-msgs-hour">0</b>/h</span>
            <span class="kpi-extra">FRAMES <b id="kpi-frames">0</b>/min</span>
            <span class="kpi-extra">INCIDENTS <b id="kpi-incidents">0</b></span>
        </div>
        <span class="spacer"></span>
        <div id="sensor-filter-bar" class="tabs filter-tabs" aria-label="Feed filter"></div>
        <div class="tabs" role="group" aria-label="Sound, speech, map focus and theme">
            <button type="button" id="sound-toggle" class="tab icon-tab" onclick="toggleSound()" title="Toggle notification sound">
                <span id="sound-icon">🔔</span>
            </button>
            <button type="button" id="tts-toggle" class="tab icon-tab" onclick="toggleTts()" title="Toggle speech output">
                <span id="tts-icon">🔇</span>
            </button>
            <button type="button" id="automap-toggle" class="tab icon-tab" onclick="toggleAutoMap()" title="Auto-show new messages on map">
                <span id="automap-icon">📍</span>
            </button>
            <button type="button" id="theme-toggle" class="tab icon-tab" onclick="cycleTheme()" title="Theme">
                <span id="theme-icon">🌓</span>
            </button>
        </div>
        <span id="clock" class="clock"></span>
    </header>

    <div class="views">
        <section id="tab-console" class="tab-content view" aria-label="Console">
            <div class="console">
                <div class="feed">
                    <div class="feed-head"><span>Time</span><span>Prio</span><span class="disc-col">Discipline</span><span>Message</span><span class="place-col">Location</span><span class="unit">Unit</span></div>
                    <div id="console-rows" class="feed-rows"><div class="empty-state">No messages yet</div></div>
                </div>
                <div id="console-side" class="side">
                    <div class="splitter col" data-size="--console-side" data-pane="console-side" data-dir="-1" role="separator" aria-orientation="vertical" aria-label="Resize message list" title="Drag to resize, double-click to reset"></div>
                    <div id="slot-console" class="map-slot"></div>
                    <div class="splitter row" data-size="--console-map" data-pane="slot-console" data-dir="1" role="separator" aria-orientation="horizontal" aria-label="Resize map" title="Drag to resize, double-click to reset"></div>
                    <div id="detail-dock" class="detail-dock"></div>
                </div>
            </div>
        </section>

        <section id="tab-wallboard" class="tab-content view" aria-label="Wallboard">
            <div class="wallboard">
                <div class="wall-main">
                    <div class="wall-map">
                        <div id="slot-wallboard" class="map-slot"></div>
                        <div id="wall-kpis" class="wall-kpis"></div>
                    </div>
                    <div id="wall-list" class="wall-list">
                        <div class="splitter col" data-size="--wall-list" data-pane="wall-list" data-dir="-1" role="separator" aria-orientation="vertical" aria-label="Resize incident list" title="Drag to resize, double-click to reset"></div>
                        <div class="panel-label"><span>Incidents</span><span>by priority</span></div>
                        <div id="wall-items" class="wall-items"><div class="empty-state">No messages yet</div></div>
                    </div>
                </div>
                <div id="wall-ticker" class="ticker"></div>
            </div>
        </section>

        <section id="tab-lanes" class="tab-content view" aria-label="Lanes">
            <div class="lanes">
                <div id="lane-brandweer" class="lane"></div>
                <div id="lane-ambulance" class="lane"></div>
                <div id="lane-politie" class="lane"></div>
                <div id="lane-other" class="lane"></div>
                <div id="lanes-side" class="lanes-side">
                    <div class="splitter col" data-size="--lanes-side" data-pane="lanes-side" data-dir="-1" role="separator" aria-orientation="vertical" aria-label="Resize lanes" title="Drag to resize, double-click to reset"></div>
                    <div id="slot-lanes" class="map-slot"></div>
                </div>
            </div>
        </section>

    <div class="pages">
    <div class="container">
        <div id="tab-info" class="tab-content">
            <div class="info-grid">
                <div class="panel info-card">
                    <div class="panel-body">
                        <h3>Receiver</h3>
                        <div class="info-row"><span class="info-label">Network</span><span class="info-value" id="info-network">-</span></div>
                        <div class="info-row"><span class="info-label">Frequency</span><span class="info-value" id="info-frequency">-</span></div>
                        <div class="info-row"><span class="info-label">Sample rate</span><span class="info-value" id="info-samplerate">-</span></div>
                        <div class="info-row"><span class="info-label">Decoder</span><span class="info-value" id="info-decoder">-</span></div>
                    </div>
                </div>

                <div class="panel info-card">
                    <div class="panel-body">
                        <h3>Device</h3>
                        <div class="info-row"><span class="info-label">Type</span><span class="info-value" id="info-device">-</span></div>
                        <div class="info-row"><span class="info-label">Driver</span><span class="info-value" id="info-driver">-</span></div>
                        <div class="info-row"><span class="info-label">Status</span><span class="info-value" id="info-status">-</span></div>
                        <div class="info-row"><span class="info-label">Geocoding</span><span class="info-value" id="info-geocoding">-</span></div>
                        <div class="info-row"><span class="info-label">OpenCage quota</span><span class="info-value" id="info-geocode-quota">-</span></div>
                        <div class="info-row"><span class="info-label">Geocoding skipped</span><span class="info-value" id="info-geocode-skipped">-</span></div>
                    </div>
                </div>

                <div class="panel info-card">
                    <div class="panel-body">
                        <h3>Database</h3>
                        <div class="info-row"><span class="info-label">Places</span><span class="info-value" id="info-places">0</span></div>
                        <div class="info-row"><span class="info-label">Streets</span><span class="info-value" id="info-streets">0</span></div>
                        <div class="info-row"><span class="info-label">Capcodes</span><span class="info-value" id="info-capcodes">0</span></div>
                        <div class="info-row"><span class="info-label">Texts</span><span class="info-value" id="info-texts">0</span></div>
                        <div class="info-row"><span class="info-label">Geocodes</span><span class="info-value" id="info-geocodes">0</span></div>
                    </div>
                </div>
            </div>

            <div class="info-grid" style="margin-top: 14px;">
                <div class="panel info-card">
                    <div class="panel-body">
                        <h3>Reception</h3>
                        <div class="reception">
                        <div>
                        <div class="info-row"><span class="info-label">Active receiver</span><span class="info-value" id="rx-source">-</span></div>
                        <div class="info-row"><span class="info-label">Last FLEX frame</span><span class="info-value" id="rx-last">-</span></div>
                        <div class="info-row"><span class="info-label">Frames last minute</span><span class="info-value" id="rx-frames-min">-</span></div>
                        <div class="info-row"><span class="info-label">Messages last hour</span><span class="info-value" id="rx-msgs-hour">-</span></div>
                        <div class="info-row"><span class="info-label">Corrected bit errors per frame</span><span class="info-value" id="rx-corrected">-</span></div>
                        <div class="info-row"><span class="info-label">Failed decodes last hour</span><span class="info-value" id="rx-failed">-</span></div>
                        <div class="info-row"><span class="info-label">Sync lost last hour</span><span class="info-value" id="rx-sync">-</span></div>
                        </div>
                        <div>
                        <div class="chart-title">FLEX frames per minute, last hour</div>
                        <div id="frames-chart"></div>
                        <div class="panel-sub" style="margin-top: 8px;">Fewer corrected bit errors and failed decodes mean a cleaner signal - use them when tuning gain and ppm. rtl_fm and rx_fm don't report an RF signal level.</div>
                        </div>
                        </div>
                    </div>
                </div>
            </div>

            <div class="panel info-card stats-panel">
                <div class="panel-body">
                    <h3>Statistics</h3>
                    <div class="info-row"><span class="info-label">Total messages</span><span class="info-value" id="info-total">0</span></div>
                    <div class="info-row"><span class="info-label">Session uptime</span><span class="info-value" id="info-uptime">-</span></div>
                    <div class="stats-charts-grid">
                        <div>
                            <div class="chart-title">By region
                                <button id="charts-pause-toggle" class="icon-btn" style="width:24px;height:24px;font-size:12px;margin-left:auto;" onclick="toggleChartsPause()" title="Pause chart updates to investigate without refreshes">
                                    <span id="charts-pause-icon">⏸️</span>
                                </button>
                            </div>
                            <div id="region-chart"></div>
                        </div>
                        <div>
                            <div class="chart-title">By discipline</div>
                            <div id="discipline-chart"></div>
                        </div>
                    </div>
                </div>
            </div>

            <div class="info-grid" style="margin-top: 14px;">
                <div class="panel info-card">
                    <div class="panel-body">
                        <h3>Support</h3>
                        <div class="info-row"><span class="info-label">Documentation</span><span class="info-value"><a href="https://github.com/cyberjunky/addon-meldkamer#readme" target="_blank" rel="noopener">README on GitHub</a></span></div>
                        <div class="info-row"><span class="info-label">Problem or idea</span><span class="info-value"><a href="https://github.com/cyberjunky/addon-meldkamer/issues" target="_blank" rel="noopener">Open an issue</a></span></div>
                        <div class="info-row"><span class="info-label">Version</span><span class="info-value">v__ADDON_VERSION__ · <a href="https://github.com/cyberjunky/addon-meldkamer/blob/main/CHANGELOG.md" target="_blank" rel="noopener">changelog</a></span></div>
                    </div>
                </div>

                <div class="panel info-card">
                    <div class="panel-body">
                        <h3>Donate</h3>
                        <div class="panel-sub" style="margin-bottom: 12px;">Meldkamer is free and open source. If it's useful to you, please consider supporting its development.</div>
                        <div class="support-links">
                            <a class="btn btn-primary" href="https://www.paypal.me/cyberjunkynl/" target="_blank" rel="noopener">Donate via PayPal</a>
                            <a class="btn" href="https://github.com/sponsors/cyberjunky" target="_blank" rel="noopener">Sponsor on GitHub</a>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div id="tab-settings" class="tab-content">
            <div class="panel">
                <div class="panel-body">
                    <!-- Landing: guided setup flow -->
                    <div id="db-landing">
                        <div class="db-toolbar">
                            <span class="db-table-title">Database setup</span>
                            <button class="btn btn-danger" onclick="dbResetDatabase()">Reset database</button>
                        </div>
                        <div class="panel-sub" style="margin-bottom: 14px;">Load the reference data in order - each step improves message parsing, badges and map pins.</div>

                        <div class="db-cards">
                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-step">1</span>
                                    <span class="db-card-title">Capcodes</span>
                                    <span class="db-card-status empty" id="db-status-capcodes">-</span>
                                </div>
                                <div class="db-card-desc">Pager addresses with discipline, region and receiver info. Powers the badges and receiver details on every message.</div>
                                <div class="db-card-count"><span id="db-count-capcodes">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn btn-primary" onclick="dbImportBommel(this)">Import Bommel</button>
                                    <button class="btn" onclick="dbOpenTable('capcodes')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-step">2</span>
                                    <span class="db-card-title">Places</span>
                                    <span class="db-card-status empty" id="db-status-places">-</span>
                                </div>
                                <div class="db-card-desc">All ~2600 Dutch places with coordinates. Enables city detection in messages and city-level map pins.</div>
                                <div class="db-card-count"><span id="db-count-places">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn btn-primary" onclick="importAllPlaces(this)">Import All Places</button>
                                    <button class="btn" onclick="dbOpenTable('places')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-step">3</span>
                                    <span class="db-card-title">Streets</span>
                                    <span class="db-card-status empty" id="db-status-streets">-</span>
                                </div>
                                <div class="db-card-desc">Official BAG street names per province. Enables full street-address extraction for precise geocoding.</div>
                                <div class="db-card-count"><span id="db-count-streets">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn btn-primary" onclick="dbImportBAG(this)">Import BAG</button>
                                    <button class="btn" onclick="dbOpenTable('streets')">Manage</button>
                                </div>
                            </div>

                        </div>

                        <div class="panel-sub" style="margin: 18px 0 10px;">No import needed - built-in, auto-filled, or managed directly from the dashboard</div>
                        <div class="db-cards">
                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-card-title">Texts</span>
                                    <span class="db-card-status ready" id="db-status-texts">-</span>
                                </div>
                                <div class="db-card-desc">300+ P2000 abbreviations with their full meanings, shown as tags in message details. Seeded automatically on startup - your own edits are never overwritten by that.</div>
                                <div class="db-card-count"><span id="db-count-texts">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn" onclick="dbOpenTable('texts')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-card-title">Messages</span>
                                    <span class="db-card-status ready" id="db-status-messages">-</span>
                                </div>
                                <div class="db-card-desc">Decoded P2000 messages with parsed location and receiver info. Filled automatically by the decoder.</div>
                                <div class="db-card-count"><span id="db-count-messages">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn" onclick="dbOpenTable('messages')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-card-title">Geocodes</span>
                                    <span class="db-card-status ready" id="db-status-geocodes">-</span>
                                </div>
                                <div class="db-card-desc">OpenCage address lookup cache. Fills automatically when geocoding is enabled; saves API quota.</div>
                                <div class="db-card-count"><span id="db-count-geocodes">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn" onclick="dbOpenTable('geocodes')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-card-title">TTS Replacements</span>
                                    <span class="db-card-status ready" id="db-status-tts">-</span>
                                </div>
                                <div class="db-card-desc">Regex rules that make messages speakable, used by browser speech and Home Assistant sensors.</div>
                                <div class="db-card-count"><span id="db-count-tts">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn" onclick="dbOpenTable('tts')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-card-title">Ignore Text</span>
                                    <span class="db-card-status ready" id="db-status-ignore_text">-</span>
                                </div>
                                <div class="db-card-desc">Wildcard patterns (e.g. *TESTOPROEP*) - messages matching any of these are dropped entirely.</div>
                                <div class="db-card-count"><span id="db-count-ignore_text">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn" onclick="dbOpenTable('ignore_text')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-card-title">Ignore Capcodes</span>
                                    <span class="db-card-status ready" id="db-status-ignore_capcodes">-</span>
                                </div>
                                <div class="db-card-desc">Capcodes to drop. A message is only dropped when every one of its capcodes is on this list.</div>
                                <div class="db-card-count"><span id="db-count-ignore_capcodes">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn" onclick="dbOpenTable('ignore_capcodes')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-card-title">Vehicle Photos</span>
                                    <span class="db-card-status empty" id="db-status-vehicle_photos">-</span>
                                </div>
                                <div class="db-card-desc">Photos you've uploaded per vehicle number (e.g. "07-1782", "18-187") or vehicle category (e.g. "ambulance", "politie"), shown on matching messages.</div>
                                <div class="db-card-count"><span id="db-count-vehicle_photos">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn" onclick="dbOpenTable('vehicle_photos')">Manage</button>
                                </div>
                            </div>

                            <div class="db-card">
                                <div class="db-card-head">
                                    <span class="db-card-title">Vehicles</span>
                                    <span class="db-card-status empty" id="db-status-vehicles">-</span>
                                </div>
                                <div class="db-card-desc">What each vehicle number is (e.g. "11-0293" → brandweer_officier). Filled from the capcode list and from messages that name a vehicle with its type ("TS 07-1782"); add, edit or import your own. Only vehicles listed here get a type and photo.</div>
                                <div class="db-card-count"><span id="db-count-vehicles">0</span> rows</div>
                                <div class="db-card-actions">
                                    <button class="btn" onclick="dbOpenTable('vehicles')">Manage</button>
                                </div>
                            </div>
                        </div>

                    </div>
                    <!-- Table view -->
                    <div id="db-table-view" style="display: none;">
                        <div class="db-toolbar">
                            <div class="db-toolbar-left">
                                <button class="btn" onclick="dbBackToLanding()">← Back</button>
                                <span id="db-table-title" class="db-table-title">Table</span>
                            </div>
                            <div class="db-toolbar-right">
                                <input type="text" id="db-search" class="input" placeholder="Search…" onkeyup="dbSearch()">
                                <div id="db-buttons" style="display: flex; gap: 6px; flex-wrap: wrap;"></div>
                            </div>
                        </div>

                        <div class="db-table-wrap">
                            <table class="db-table">
                                <thead id="db-thead"></thead>
                                <tbody id="db-tbody"></tbody>
                            </table>
                        </div>

                        <div class="db-pagination">
                            <span id="db-count">0 items</span>
                            <div style="display: flex; align-items: center; gap: 10px;">
                                <button id="db-prev" class="btn" onclick="dbPrev()">← Prev</button>
                                <span id="db-page">Page 1</span>
                                <button id="db-next" class="btn" onclick="dbNext()">Next →</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    </div>
    </div>

    <footer class="statusbar">
        <span id="decoder-status" class="decoder-bad">● DECODER STOPPED</span>
        <span id="device-info"></span>
        <span id="stream-status" class="conn" title="Live data connection"><span class="dot"></span><span id="stream-status-text">Connecting…</span></span>
        <span class="spacer"></span>
        <span><span id="total-messages">0</span> messages</span>
        <span>uptime <span id="uptime">0m</span></span>
        <span>v__ADDON_VERSION__</span>
    </footer>

    <!-- The map and the message detail are single elements, moved into the active view -->
    <div id="parking" hidden>
        <div id="map-root">
            <div id="map"></div>
            <div class="map-history">
                <label for="map-history-hours">History</label>
                <select id="map-history-hours" class="input" onchange="loadMapHistory()">
                    <option value="">Off</option>
                    <option value="24">24 hours</option>
                    <option value="168">7 days</option>
                    <option value="720">30 days</option>
                    <option value="8760">1 year</option>
                </select>
                <input id="map-history-until" type="range" style="display:none;" oninput="renderMapHistory()" title="Show incidents up to this moment">
                <span id="map-history-label" class="until"></span>
            </div>
            <div id="detail-float" class="detail-float" role="dialog" aria-label="Message details">
                <div class="detail-float-head">
                    <span>Message details</span>
                    <button class="modal-close" onclick="closeDetailFloat()" aria-label="Close">✕</button>
                </div>
                <div id="detail-float-body" class="detail-float-body"></div>
            </div>
        </div>
        <div id="detail">
            <div id="detail-content"><div class="empty-state">Click a message to view details</div></div>
            <div id="incident-section" class="history-section" hidden>
                <h4 id="incident-title">Incident</h4>
                <div id="incident-list"></div>
            </div>
            <div id="history-section" class="history-section" hidden>
                <h4>Earlier at this address</h4>
                <div id="history-list"></div>
            </div>
            <div id="detail-nav" class="detail-nav" hidden>
                <button type="button" class="btn" onclick="stepDetail(-1)">← Previous</button>
                <span id="detail-pos" class="panel-sub"></span>
                <button type="button" class="btn" onclick="stepDetail(1)">Next →</button>
            </div>
        </div>
    </div>

    <div id="modal-root"></div>
    <datalist id="capcode-suggestions"></datalist>
    <div id="toast-root"></div>

    <script>
        const basePath = window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/';

        // =====================================================================
        // Theme: follows Home Assistant's dark mode (this page runs in its
        // ingress iframe, same origin), the system outside Home Assistant,
        // or a manual light/dark choice
        // =====================================================================
        const THEME_ICONS = {auto: '🌓', light: '☀️', dark: '🌙'};
        let themeChoice = localStorage.getItem('p2000_theme');
        if (!THEME_ICONS[themeChoice]) themeChoice = 'auto';

        function haDarkMode() {
            try {
                const ha = window.parent !== window && window.parent.document.querySelector('home-assistant');
                return ha && ha.hass && ha.hass.themes ? Boolean(ha.hass.themes.darkMode) : null;
            } catch (e) {
                return null;  // Not inside Home Assistant's frontend
            }
        }

        function applyTheme() {
            let dark = themeChoice === 'dark';
            if (themeChoice === 'auto') {
                const ha = haDarkMode();
                dark = ha === null ? window.matchMedia('(prefers-color-scheme: dark)').matches : ha;
            }
            document.documentElement.dataset.theme = dark ? 'dark' : 'light';
        }

        function initThemeToggle() {
            document.getElementById('theme-icon').textContent = THEME_ICONS[themeChoice];
            document.getElementById('theme-toggle').title = themeChoice === 'auto'
                ? 'Theme follows Home Assistant - click for light'
                : `Theme: ${themeChoice} - click to change`;
        }

        function cycleTheme() {
            themeChoice = {auto: 'light', light: 'dark', dark: 'auto'}[themeChoice];
            localStorage.setItem('p2000_theme', themeChoice);
            initThemeToggle();
            applyTheme();
        }

        applyTheme();
        // Home Assistant doesn't tell the iframe when its theme changes
        setInterval(applyTheme, 2000);
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);

        // =====================================================================
        // Map: one Leaflet map, moved into the active view's map slot
        // =====================================================================
        // The last view (and the zoom used to focus incidents) survives reloads;
        // focusing a new incident only moves the map, it keeps the zoom
        const FOCUS_ZOOM = 13;
        let savedView = null;
        try { savedView = JSON.parse(localStorage.getItem('p2000_map_view')); } catch (e) {}
        const map = savedView
            ? L.map('map').setView([savedView.lat, savedView.lng], savedView.zoom)
            : L.map('map').setView([52.2, 5.3], 8);
        let focusZoom = parseInt(localStorage.getItem('p2000_map_zoom'), 10) || FOCUS_ZOOM;
        map.on('zoomend', () => {
            focusZoom = map.getZoom();
            localStorage.setItem('p2000_map_zoom', focusZoom);
        });
        map.on('moveend', () => {
            const c = map.getCenter();
            localStorage.setItem('p2000_map_view', JSON.stringify({lat: c.lat, lng: c.lng, zoom: map.getZoom()}));
        });
        // Tiles come through the add-on (and Home Assistant's built-in map tile
        // proxy); OSM blocks browsers requesting them directly from here.
        L.tileLayer(basePath + 'api/map-tiles/{z}/{x}/{y}.png', {
            attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors',
            maxZoom: 19
        }).addTo(map);

        // Heatmap of past incidents from the message history; the slider
        // replays them up to the chosen moment
        let historyLayer = null;
        let historyPoints = [];

        function loadMapHistory() {
            const hours = document.getElementById('map-history-hours').value;
            const slider = document.getElementById('map-history-until');
            const label = document.getElementById('map-history-label');
            if (!hours) {
                if (historyLayer) { map.removeLayer(historyLayer); historyLayer = null; }
                slider.style.display = 'none';
                label.textContent = '';
                return;
            }
            fetch(basePath + `api/map-history?hours=${hours}`)
                .then(r => r.json())
                .then(points => {
                    historyPoints = points;
                    if (!points.length) {
                        slider.style.display = 'none';
                        label.textContent = 'No located messages';
                    } else {
                        slider.min = points[0].time;
                        slider.max = Math.max(points[points.length - 1].time, points[0].time + 1);
                        slider.value = slider.max;
                        slider.style.display = '';
                    }
                    renderMapHistory();
                })
                .catch(() => toast('Failed to load map history', 'error'));
        }

        function renderMapHistory() {
            const slider = document.getElementById('map-history-until');
            const until = Number(slider.value);
            const shown = historyPoints.filter(p => p.time <= until);
            // Urgent (P1) incidents weigh more in the heatmap
            const latlngs = shown.map(p => [p.latitude, p.longitude, p.priority === 1 ? 1 : 0.5]);
            if (historyLayer) {
                historyLayer.setLatLngs(latlngs);
            } else {
                historyLayer = L.heatLayer(latlngs, { radius: 20, blur: 15, maxZoom: 12 }).addTo(map);
            }
            if (historyPoints.length) {
                document.getElementById('map-history-label').textContent =
                    `${shown.length} until ${new Date(until * 1000).toLocaleString([], {dateStyle: 'short', timeStyle: 'short'})}`;
            }
        }

        let currentMarker = null;
        let selectedMsg = null;
        let selectedMsgKey = null;
        // Stable identity for a message: list positions shift as new ones arrive
        function msgKey(msg) {
            return msg.id !== undefined && msg.id !== null ? 'id:' + msg.id : msg.timestamp + '|' + (msg.body || '');
        }
        function highlightSelectedMessage() {
            document.querySelectorAll('[data-msg-index]').forEach(el => {
                const m = currentMessages[Number(el.dataset.msgIndex)];
                el.classList.toggle('selected', selectedMsgKey !== null && Boolean(m) && msgKey(m) === selectedMsgKey);
            });
        }
        const markers = [];

        // Home Assistant zones that sensors filter on. They are read from Home
        // Assistant after startup, so redraw only when the set changes.
        const sensorZoneLayer = L.layerGroup().addTo(map);
        let sensorZonesSig = null;
        function renderSensorZones(zones) {
            const sig = JSON.stringify(zones || []);
            if (sig === sensorZonesSig) return;
            sensorZonesSig = sig;
            sensorZoneLayer.clearLayers();
            (zones || []).forEach(z => {
                L.circle([z.center_lat, z.center_lon], {
                    radius: z.radius_km * 1000,
                    color: '#03a9f4', weight: 1.5, fillColor: '#03a9f4', fillOpacity: 0.06, dashArray: '4,4'
                }).addTo(sensorZoneLayer).bindTooltip(`${escapeHtml(z.name)} (${z.radius_km.toFixed(1)} km)`, { sticky: true });
            });
        }

        const totalEl = document.getElementById('total-messages');
        const uptimeEl = document.getElementById('uptime');
        const decoderEl = document.getElementById('decoder-status');
        const regionChartEl = document.getElementById('region-chart');
        const disciplineChartEl = document.getElementById('discipline-chart');
        const detailContent = document.getElementById('detail-content');
        const historyList = document.getElementById('history-list');
        const streamStatusEl = document.getElementById('stream-status');
        const streamStatusText = document.getElementById('stream-status-text');

        // =====================================================================
        // Toasts & modals
        // =====================================================================
        function toast(message, type) {
            const root = document.getElementById('toast-root');
            const el = document.createElement('div');
            el.className = 'toast' + (type ? ' ' + type : '');
            el.textContent = message;
            root.appendChild(el);
            setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 0.3s'; }, 3600);
            setTimeout(() => el.remove(), 4000);
        }

        function openModal(opts) {
            closeModal();
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay open';
            overlay.innerHTML = `
                <div class="modal">
                    <div class="modal-head">
                        <span class="modal-title">${escapeHtml(opts.title || '')}</span>
                        <button class="modal-close" data-close>✕</button>
                    </div>
                    <div class="modal-body">${opts.bodyHTML || ''}</div>
                    <div class="modal-foot">${opts.footHTML || ''}</div>
                </div>`;
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay || e.target.hasAttribute('data-close')) closeModal();
            });
            document.getElementById('modal-root').appendChild(overlay);
            return overlay;
        }

        function closeModal() {
            const root = document.getElementById('modal-root');
            root.innerHTML = '';
        }

        function confirmDialog(opts) {
            return new Promise((resolve) => {
                const overlay = openModal({
                    title: opts.title || 'Are you sure?',
                    bodyHTML: `<div>${opts.message || ''}</div>`,
                    footHTML: `
                        <button class="btn" data-close>Cancel</button>
                        <button class="btn ${opts.danger ? 'btn-danger-solid' : 'btn-primary'}" data-confirm>${escapeHtml(opts.confirmLabel || 'Confirm')}</button>`
                });
                overlay.querySelector('[data-confirm]').addEventListener('click', () => {
                    closeModal();
                    resolve(true);
                });
                overlay.addEventListener('click', (e) => {
                    if (e.target === overlay || e.target.hasAttribute('data-close')) resolve(false);
                });
            });
        }

        // =====================================================================
        // Sound notifications
        // =====================================================================
        let soundEnabled = localStorage.getItem('p2000_sound_enabled') !== 'false';
        let audioContext = null;

        function initSoundToggle() {
            const btn = document.getElementById('sound-toggle');
            const icon = document.getElementById('sound-icon');
            btn.classList.toggle('active', soundEnabled);
            btn.classList.toggle('off', !soundEnabled);
            icon.textContent = soundEnabled ? '🔔' : '🔕';
            btn.title = soundEnabled ? 'Sound notifications ON - click to disable' : 'Sound notifications OFF - click to enable';
        }

        function toggleSound() {
            soundEnabled = !soundEnabled;
            localStorage.setItem('p2000_sound_enabled', soundEnabled);
            initSoundToggle();
            if (soundEnabled) {
                unlockAudio();
                playNotificationSound();
            }
        }

        function unlockAudio() {
            if (!audioContext) {
                audioContext = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (audioContext.state === 'suspended') {
                audioContext.resume();
            }
        }

        document.addEventListener('click', unlockAudio, { once: true });
        document.addEventListener('touchstart', unlockAudio, { once: true });

        function playNotificationSound() {
            if (!soundEnabled) return;
            try {
                if (!audioContext) {
                    audioContext = new (window.AudioContext || window.webkitAudioContext)();
                }
                if (audioContext.state === 'suspended') {
                    audioContext.resume();
                }
                const oscillator = audioContext.createOscillator();
                const gainNode = audioContext.createGain();
                oscillator.connect(gainNode);
                gainNode.connect(audioContext.destination);
                oscillator.frequency.setValueAtTime(880, audioContext.currentTime);
                oscillator.frequency.setValueAtTime(1100, audioContext.currentTime + 0.1);
                gainNode.gain.setValueAtTime(0.3, audioContext.currentTime);
                gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.3);
                oscillator.start(audioContext.currentTime);
                oscillator.stop(audioContext.currentTime + 0.3);
            } catch (e) {
                console.log('Audio playback failed:', e);
            }
        }

        // =====================================================================
        // Auto-map toggle
        // =====================================================================
        let autoMapEnabled = localStorage.getItem('p2000_automap_enabled') !== 'false';

        function initAutoMapToggle() {
            const btn = document.getElementById('automap-toggle');
            const icon = document.getElementById('automap-icon');
            btn.classList.toggle('active', autoMapEnabled);
            btn.classList.toggle('off', !autoMapEnabled);
            icon.textContent = autoMapEnabled ? '📍' : '📌';
            btn.title = autoMapEnabled ? 'Auto-focus ON - new messages show on map' : 'Auto-focus OFF - click to enable';
        }

        function toggleAutoMap() {
            autoMapEnabled = !autoMapEnabled;
            localStorage.setItem('p2000_automap_enabled', autoMapEnabled);
            initAutoMapToggle();
        }

        function autoShowLatestOnMap(latest) {
            // Uses the already-filtered list passed in - re-fetching from
            // /api/messages here (as this used to) ignores the current
            // sensor filter and shows the newest message regardless of it.
            const target = (latest || []).find(m => m.latitude && m.longitude);
            if (target) {
                showMessageDetails(target);
            }
        }

        // =====================================================================
        // Browser text-to-speech
        // =====================================================================
        let ttsEnabled = localStorage.getItem('p2000_tts_enabled') === 'true';
        function initTtsToggle() {
            const btn = document.getElementById('tts-toggle');
            const icon = document.getElementById('tts-icon');
            btn.classList.toggle('active', ttsEnabled);
            btn.classList.toggle('off', !ttsEnabled);
            icon.textContent = ttsEnabled ? '🗣️' : '🔇';
            btn.title = ttsEnabled ? 'Speech ON - new messages are read aloud' : 'Speech OFF - click to enable';
        }

        function toggleTts() {
            ttsEnabled = !ttsEnabled;
            localStorage.setItem('p2000_tts_enabled', ttsEnabled);
            initTtsToggle();
            if (!ttsEnabled && window.speechSynthesis) {
                speechSynthesis.cancel();
            }
        }

        function speakMessage(msg) {
            if (!ttsEnabled || !window.speechSynthesis || !msg || !msg.tts) return;
            // msg.tts is the spoken form built server-side with the TTS
            // replacement rules - the same text Home Assistant speakers get
            const utterance = new SpeechSynthesisUtterance(msg.tts);
            utterance.lang = 'nl-NL';
            speechSynthesis.cancel();  // Newest message takes priority
            speechSynthesis.speak(utterance);
        }

        // Tracks the top-of-list message's own timestamp, not the global
        // message count - the global count increases for every decoded
        // message regardless of the active sensor filter, which previously
        // caused TTS/notifications to re-fire (and re-speak the same,
        // unchanged filtered top message) whenever any non-matching message
        // arrived, instead of only on a genuinely new matching one.
        let lastSeenTopTimestamp = null;

        function checkForNewMessages(latest) {
            const top = (latest || [])[0];
            const topTimestamp = top ? top.timestamp : null;
            if (lastSeenTopTimestamp === null) {
                lastSeenTopTimestamp = topTimestamp;
                return;
            }
            if (topTimestamp && topTimestamp !== lastSeenTopTimestamp) {
                playNotificationSound();
                speakMessage(top);
                const earlier = top.incident_id && currentMessages.filter(m => m.incident_id === top.incident_id);
                if (earlier && earlier.length) {
                    toast(`Incident escalated: ${top.address || top.city || ''} (${earlier.length + 1} messages)`, 'error');
                }
                if (autoMapEnabled) {
                    autoShowLatestOnMap(latest);
                }
            }
            lastSeenTopTimestamp = topTimestamp;
        }

        initSoundToggle();
        initAutoMapToggle();
        initTtsToggle();
        initThemeToggle();

        // =====================================================================
        // Formatting helpers
        // =====================================================================
        function formatTime(ts) {
            return new Date(ts).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
        }
        function formatDate(ts) {
            return new Date(ts).toLocaleString([], {
                day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
            });
        }

        // Safe for element content AND quoted attribute values
        const HTML_ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
        function escapeHtml(text) {
            if (text === null || text === undefined) return '';
            return String(text).replace(/[&<>"']/g, ch => HTML_ESCAPES[ch]);
        }

        function formatShortDate(ts) {
            return new Date(ts).toLocaleString([], {day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false});
        }

        // Minutes or hours since a timestamp, for incident ages
        function ago(ts) {
            const mins = Math.max(0, Math.floor((Date.now() - new Date(ts)) / 60000));
            return mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h`;
        }

        // =====================================================================
        // Views: Console, Wallboard and Lanes
        // =====================================================================
        const VIEWS = ['console', 'wallboard', 'lanes'];
        let activeView = 'console';

        function switchTab(tabName) {
            document.querySelectorAll('.tab[data-tab]').forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + tabName));
            localStorage.setItem('p2000_tab', tabName);
            if (tabName === 'info') loadReceiver();
            if (VIEWS.includes(tabName)) {
                activeView = tabName;
                document.getElementById('slot-' + tabName).appendChild(document.getElementById('map-root'));
                placeDetail();
                setTimeout(() => map.invalidateSize(), 50);
                if (evtSource) resyncIfStale();
            }
        }

        // The detail panel is docked under the map in Console, and floats over
        // the map's left corner on the Wallboard (top) and Lanes (bottom)
        function placeDetail() {
            closeDetailFloat();
            document.getElementById('detail-float').classList.toggle('bottom', activeView === 'lanes');
            const home = activeView === 'console' ? 'detail-dock' : 'detail-float-body';
            document.getElementById(home).appendChild(document.getElementById('detail'));
        }

        function openDetailFloat() {
            document.getElementById('detail-float').classList.add('open');
        }

        function closeDetailFloat() {
            document.getElementById('detail-float').classList.remove('open');
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeDetailFloat();
        });

        // Draggable dividers: each sets a CSS size variable, kept per browser;
        // double-click restores the default
        function initSplitters() {
            const root = document.documentElement.style;
            document.querySelectorAll('.splitter').forEach(handle => {
                const name = handle.dataset.size;
                const saved = localStorage.getItem('p2000_size' + name);
                if (saved) root.setProperty(name, saved);

                handle.addEventListener('pointerdown', (e) => {
                    e.preventDefault();
                    const pane = document.getElementById(handle.dataset.pane);
                    const row = handle.classList.contains('row');
                    const dir = Number(handle.dataset.dir);
                    const start = row ? e.clientY : e.clientX;
                    const startSize = row ? pane.offsetHeight : pane.offsetWidth;
                    const room = row ? pane.parentElement.offsetHeight : pane.parentElement.offsetWidth;
                    handle.setPointerCapture(e.pointerId);
                    handle.classList.add('dragging');
                    const move = (ev) => {
                        const size = startSize + dir * ((row ? ev.clientY : ev.clientX) - start);
                        root.setProperty(name, Math.round(Math.max(200, Math.min(room - 200, size))) + 'px');
                        map.invalidateSize();
                    };
                    const up = () => {
                        handle.classList.remove('dragging');
                        handle.removeEventListener('pointermove', move);
                        handle.removeEventListener('pointerup', up);
                        localStorage.setItem('p2000_size' + name, root.getPropertyValue(name));
                    };
                    handle.addEventListener('pointermove', move);
                    handle.addEventListener('pointerup', up);
                });

                handle.addEventListener('dblclick', () => {
                    root.removeProperty(name);
                    localStorage.removeItem('p2000_size' + name);
                    map.invalidateSize();
                });
            });
        }
        initSplitters();

        function updateClock() {
            document.getElementById('clock').textContent = formatTime(Date.now());
        }
        updateClock();
        setInterval(updateClock, 1000);

        // The live stream can silently die (dropped by a proxy, or the browser
        // tab/this in-app tab being away for a while) with no visible error -
        // reconnect + refetch on return instead of leaving it frozen until a
        // manual page reload.
        function resyncIfStale() {
            if (!evtSource || evtSource.readyState === EventSource.CLOSED) {
                connectStream();
            }
            loadMessagesList();
        }

        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) resyncIfStale();
        });

        // Uptime is recomputed client-side so the SSE stream can stay quiet
        // when nothing changed.
        let lastStartTime = null;
        function renderUptime() {
            if (!lastStartTime) return;
            const start = new Date(lastStartTime);
            const totalMinutes = Math.max(0, Math.floor((Date.now() - start) / 60000));
            const hours = Math.floor(totalMinutes / 60);
            const mins = totalMinutes % 60;
            const text = hours > 0 ? `${hours}h ${mins}m` : `${mins}m`;
            uptimeEl.textContent = text;
            document.getElementById('info-uptime').textContent = text;
        }
        setInterval(renderUptime, 30000);

        // =====================================================================
        // Reception quality (Info tab)
        // =====================================================================
        function formatAgo(epochSeconds) {
            if (!epochSeconds) return 'never';
            const secs = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
            if (secs < 60) return `${secs}s ago`;
            if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
            return `${Math.floor(secs / 3600)}h ${Math.floor(secs % 3600 / 60)}m ago`;
        }

        // Single-series bar chart: one bar per minute, hover shows the details
        function renderFramesChart(history) {
            const el = document.getElementById('frames-chart');
            if (!history.length) { el.innerHTML = '<div class="empty-state" style="padding: 20px;">No data yet</div>'; return; }
            const width = 600, height = 120, slot = width / 60;
            const max = Math.max(1, ...history.map(b => b.frames));
            const now = Math.floor(Date.now() / 60000) * 60;
            const bars = history.map(b => {
                const x = width - (now - b.minute) / 60 * slot;
                const h = Math.max(b.frames ? 2 : 0, b.frames / max * (height - 4));
                const tip = `${new Date(b.minute * 1000).toLocaleTimeString([], {timeStyle: 'short'})}: ${b.frames} frames, ${b.messages} messages, ${b.corrected_bits} corrected bits, ${b.failed} failed`;
                // Rounded top, square base on the axis; the full-height rect is the hover target
                const r = Math.min(2, h);
                return `<g><title>${escapeHtml(tip)}</title>` +
                    `<rect x="${x}" y="0" width="${slot}" height="${height}" fill="transparent"/>` +
                    `<path d="M${x + 1},${height} v${r - h} q0,${-r} ${r},${-r} h${slot - 2 - 2 * r} q${r},0 ${r},${r} v${h - r} z" fill="var(--primary-color)"/></g>`;
            }).join('');
            el.innerHTML = `<svg viewBox="0 0 ${width} ${height + 16}" style="width:100%;height:auto;display:block;">` +
                `<line x1="0" y1="${height}" x2="${width}" y2="${height}" stroke="var(--divider-color)"/>${bars}` +
                `<text x="0" y="${height + 13}" font-size="10" fill="var(--secondary-text-color)">-60 min</text>` +
                `<text x="${width}" y="${height + 13}" font-size="10" text-anchor="end" fill="var(--secondary-text-color)">now · max ${max}/min</text></svg>`;
        }

        let receiverTimer = null;
        function loadReceiver() {
            clearTimeout(receiverTimer);
            if (!document.querySelector('#tab-info.active')) return;
            fetch(basePath + 'api/receiver').then(r => r.json()).then(rx => {
                const active = rx.sources.find(src => src.active);
                document.getElementById('rx-source').textContent = active
                    ? `${active.label} (${active.type}, ${rx.ppm} ppm)` + (rx.sources.length > 1 ? ' · failover on' : '')
                    : '-';
                document.getElementById('rx-last').textContent = rx.silent_for === null ? 'never' : formatAgo(Date.now() / 1000 - rx.silent_for);
                document.getElementById('rx-frames-min').textContent = rx.frames_last_minute;
                document.getElementById('rx-msgs-hour').textContent = rx.messages_last_hour;
                document.getElementById('rx-corrected').textContent = rx.corrected_bits_per_frame ?? '-';
                document.getElementById('rx-failed').textContent = rx.failed_last_hour;
                document.getElementById('rx-sync').textContent = rx.sync_lost_last_hour;
                renderFramesChart(rx.history);
            }).catch(() => {}).finally(() => { receiverTimer = setTimeout(loadReceiver, 5000); });
        }

        // =====================================================================
        // Dashboard rendering
        // =====================================================================
        function updateInfo(stats) {
            const set = (id, value) => { document.getElementById('info-' + id).textContent = value; };
            set('network', stats.network_name);
            set('frequency', stats.frequency);
            set('samplerate', stats.sample_rate + ' Hz');
            set('decoder', stats.decoder);
            set('device', stats.device_type);
            set('driver', stats.device_driver);
            set('total', stats.total_messages);
            set('status', stats.decoder_running ? 'Running' : (stats.decoder_error || 'Stopped'));
            set('geocoding', !stats.geocoding_configured
                ? 'Disabled'
                : (stats.geocoding_enabled ? 'Enabled' : 'Invalid API key (disabled)'));
            set('geocode-quota', !(stats.geocoding_configured && stats.geocoding_enabled)
                ? '-'
                : (stats.geocode_rate_limited ? 'Exhausted - resets at midnight UTC' : stats.geocode_rate_remaining + ' left today'));
            set('geocode-skipped', !(stats.geocoding_configured && stats.geocoding_enabled && stats.geocode_only_sensor_matches)
                ? '-'
                : stats.geocode_skipped + ' (no sensor match)');
            set('places', stats.db_places);
            set('streets', stats.db_streets);
            set('capcodes', stats.db_capcodes);
            set('texts', stats.db_texts);
            set('geocodes', stats.db_geocodes);
        }

        function showMessageDetails(msg, idx) {
            selectedMsg = msg;
            selectedMsgKey = (typeof idx === 'number') ? msgKey(msg) : null;
            highlightSelectedMessage();

            // Filter capcodes to only show unmatched ones
            const matchedCapcodes = new Set();
            if (msg.receivers) {
                const matches = msg.receivers.match(/\\((\\d+)\\)/g);
                if (matches) {
                    matches.forEach(m => matchedCapcodes.add(m.replace(/[()]/g, '')));
                }
            }
            const unmatchedCapcodes = (msg.capcodes || []).filter(c => !matchedCapcodes.has(c));

            const geocodeLevel = (msg.latitude && msg.longitude)
                ? (msg.location_accuracy === 'street' ? 'Street' : (msg.location_accuracy === 'city' ? 'City' : 'Unknown'))
                : '—';
            const hasDistance = msg.distance_km !== null && msg.distance_km !== undefined;

            // [label, value, wide]: wide rows span the full width in the two-column layout
            const fields = [
                ['Discipline', msg.discipline || '—'],
                ['Region', msg.region || '—'],
                ['Geocode', geocodeLevel],
                msg.repeats ? ['Repeats', `resent ${msg.repeats} times`] : null,
                ['Address', msg.address || msg.city || '—', true],
                unmatchedCapcodes.length > 0 ? ['Capcodes', unmatchedCapcodes.join(' '), true] : null,
                ['Receivers', msg.receivers || '—', true],
            ].filter(Boolean);

            const abbrevTags = (msg.abbreviations || []).map(a =>
                `<span class="abbrev-tag"><strong>${escapeHtml(a.abbreviation)}</strong> ${escapeHtml(a.full_text)}</span>`
            ).join('');

            const rawBlock = msg.raw_message ? `
                <div class="raw-block">
                    <span class="raw-label">Raw message</span>
                    <pre>${escapeHtml(msg.raw_message)}</pre>
                </div>` : '';

            // The photo slot is always there: the recognised vehicle (photo
            // uploadable), or an unknown unit. All alerted units are listed, the
            // shown vehicle in bold.
            const category = msg.vehicle_category;
            const hasVehicle = Boolean(category || msg.vehicle_number);
            const units = (msg.vehicle_units && msg.vehicle_units.length) ? msg.vehicle_units : (msg.vehicle_number ? [msg.vehicle_number] : []);
            const unitsHTML = units.map(u => units.length > 1 && u === msg.vehicle_number ? `<b>${escapeHtml(u)}</b>` : escapeHtml(u)).join(', ');
            const vehicleBlock = `
                <div class="vehicle-side">
                    <div class="vehicle-visual" id="vehicle-visual"${hasVehicle ? ' onclick="openVehiclePhotoUpload()"' : ''}>
                        <span id="vehicle-icon-fallback">${escapeHtml(msg.vehicle_icon || '📻')}</span>
                    </div>
                    <div class="vehicle-credit" id="vehicle-credit" hidden></div>
                    <div class="vehicle-category-label">${escapeHtml(category ? prettyVehicleCategory(category) : (msg.vehicle_number ? 'Vehicle type unknown' : 'Unknown unit'))}</div>
                    ${msg.vehicle_description ? `<div class="vehicle-number-label">${escapeHtml(msg.vehicle_description)}</div>` : ''}
                    ${units.length ? `<div class="vehicle-number-label">${unitsHTML}</div>` : ''}
                    ${hasVehicle ? '<button class="btn" onclick="openVehiclePhotoUpload()">Change photo</button>' : ''}
                </div>`;

            const title = [msg.discipline || 'P2000', placeOf(msg)].filter(Boolean).join(' · ');
            const when = formatDate(msg.timestamp) + (hasDistance ? ` · ${msg.distance_km} km from home` : '');
            detailContent.innerHTML = `
                <div class="detail-head">
                    ${prioChip(msg)}
                    <h2>${escapeHtml(title)}</h2>
                    <span class="when">${escapeHtml(when)}</span>
                </div>
                <div class="detail-main">
                    <div class="detail-left">
                        <div class="detail-body">${escapeHtml(msg.body || 'No message')}</div>
                        ${abbrevTags ? `<div class="abbrev-tags">${abbrevTags}</div>` : ''}
                        <dl class="detail-fields">${fields.map(([label, value, wide]) =>
                            `<dt${wide ? ' class="wide"' : ''}>${label}</dt><dd${wide ? ' class="wide"' : ''}>${escapeHtml(value)}</dd>`).join('')}</dl>
                        ${rawBlock}
                    </div>
                    ${vehicleBlock}
                </div>
            `;
            renderDetailNav();

            if (msg.vehicle_category || msg.vehicle_number) loadVehiclePhoto(msg);

            document.getElementById('history-section').hidden = false;

            if (msg.latitude && msg.longitude) {
                try {
                    const lat = parseFloat(msg.latitude);
                    const lng = parseFloat(msg.longitude);
                    if (!isNaN(lat) && !isNaN(lng)) {
                        if (currentMarker) map.removeLayer(currentMarker);
                        currentMarker = L.marker([lat, lng]).addTo(map);
                        currentMarker.bindPopup(`<b>${escapeHtml(msg.discipline || 'P2000')}</b><br><small>${escapeHtml(msg.address || '')}</small><br><div style="margin-top:6px;max-width:250px;word-wrap:break-word;">${escapeHtml(msg.body || 'No message')}</div>`);
                        map.setView([lat, lng], focusZoom, {animate: false});
                        const panel = document.getElementById('detail-float');
                        if (panel.classList.contains('open')) {
                            // Keep the location visible to the right of the floating detail
                            map.panBy([-(panel.offsetWidth + 24) / 2, 0], {animate: false});
                        } else {
                            currentMarker.openPopup();
                        }
                    }
                } catch (e) {}
            }

            renderIncident(msg);

            // Location history (exact address preferred, city as fallback)
            const renderHistory = (items, extraFilter) => {
                historyList.innerHTML = items
                    .filter(h => h.timestamp !== msg.timestamp && (!extraFilter || extraFilter(h)))
                    .slice(0, 10)
                    .map(h => `
                        <div class="history-item">
                            <span class="time">${formatShortDate(h.timestamp)}</span>
                            <span class="text" title="${escapeHtml(h.body || '')}">${escapeHtml(h.body || '-')}</span>
                        </div>
                    `).join('') || '<div class="panel-sub">Nothing earlier at this location</div>';
            };
            if (msg.address) {
                fetch(basePath + `api/history/${encodeURIComponent(msg.address)}`)
                    .then(r => r.json())
                    .then(history => renderHistory(history, h => h.address === msg.address))
                    .catch(() => {});
            } else if (msg.city) {
                fetch(basePath + `api/history/${encodeURIComponent(msg.city)}`)
                    .then(r => r.json())
                    .then(history => renderHistory(history))
                    .catch(() => {});
            } else {
                historyList.innerHTML = '<div class="panel-sub">No location in this message</div>';
            }
        }

        // Position of the shown message in the list, with previous/next buttons
        function renderDetailNav() {
            const idx = selectedMsg ? currentMessages.findIndex(m => msgKey(m) === msgKey(selectedMsg)) : -1;
            document.getElementById('detail-nav').hidden = idx < 0;
            document.getElementById('detail-pos').textContent = `Message ${idx + 1} of ${currentMessages.length}`;
        }

        function stepDetail(step) {
            const idx = currentMessages.findIndex(m => msgKey(m) === msgKey(selectedMsg)) + step;
            if (idx >= 0 && idx < currentMessages.length) handleMessageClick(idx);
        }

        function renderIncident(msg) {
            const section = document.getElementById('incident-section');
            const list = document.getElementById('incident-list');
            section.hidden = true;
            if (!msg.incident_id) return;
            fetch(basePath + `api/incident/${encodeURIComponent(msg.incident_id)}`)
                .then(r => r.json())
                .then(items => {
                    if (selectedMsg !== msg || items.length < 2) return;
                    // What later messages added to the incident's first one
                    const disciplines = m => (m.discipline || '').split(',').map(d => d.trim()).filter(Boolean);
                    const first = new Set(disciplines(items[0]));
                    const added = [...new Set(items.slice(1).flatMap(disciplines))].filter(d => !first.has(d));
                    const grip = Math.max(0, ...items.map(gripLevel));
                    const escalation = added.map(d => '+ ' + d).concat(grip ? ['GRIP ' + grip] : []);
                    document.getElementById('incident-title').innerHTML = `Incident · ${items.length} messages`
                        + (escalation.length ? `<span class="badge incident escalated">escalated: ${escapeHtml(escalation.join(', '))}</span>` : '');
                    list.innerHTML = items.map(h => `
                        <div class="history-item">
                            <span class="time">${formatTime(h.timestamp)}</span>
                            <span class="text" title="${escapeHtml(h.body || '')}"><span class="disc ${discKind(h)}"></span> ${escapeHtml(h.body || '-')}</span>
                        </div>
                    `).join('');
                    section.hidden = false;
                })
                .catch(() => {});
        }

        // ----- Vehicle photos (user-uploaded, keyed by vehicle number or category) -----
        function prettyVehicleCategory(category) {
            return (category || '').split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
        }

        function vehiclePhotoUrl(keyType, keyValue) {
            return `${basePath}api/vehicle-photo/${keyType}/${encodeURIComponent(keyValue)}`;
        }

        // Shows the bundled stock photo for the vehicle type, with its credit;
        // keeps the emoji fallback when there's none
        async function loadStockVehiclePhoto(msg, visual, fallback, credit) {
            if (!msg.vehicle_category) return;
            const query = new URLSearchParams({category: msg.vehicle_category});
            let stock;
            try {
                const response = await fetch(`${basePath}api/vehicle-stock?${query}`);
                if (!response.ok) return;
                stock = await response.json();
            } catch (e) {
                return;
            }
            const img = new Image();
            img.onload = () => {
                fallback.style.display = 'none';
                img.alt = prettyVehicleCategory(msg.vehicle_category);
                img.className = 'stock';
                visual.appendChild(img);
                if (credit) {
                    credit.innerHTML = `Photo: <a href="${escapeHtml(stock.source)}" target="_blank" rel="noopener">${escapeHtml(stock.author)}</a>, ${escapeHtml(stock.license)}`;
                    credit.hidden = false;
                }
            };
            img.src = `${basePath}api/vehicle-stock/${encodeURIComponent(stock.name)}`;
        }

        // Tries the exact vehicle number photo first, falls back to the category
        // photo, then the bundled stock photo, and finally leaves the emoji
        // fallback showing.
        function loadVehiclePhoto(msg) {
            const visual = document.getElementById('vehicle-visual');
            const fallback = document.getElementById('vehicle-icon-fallback');
            const credit = document.getElementById('vehicle-credit');
            if (!visual || !fallback) return;

            const tryUrls = [];
            if (msg.vehicle_number) tryUrls.push(['number', msg.vehicle_number]);
            if (msg.vehicle_category) tryUrls.push(['category', msg.vehicle_category]);

            const attempt = (i) => {
                if (i >= tryUrls.length) {
                    loadStockVehiclePhoto(msg, visual, fallback, credit);
                    return;
                }
                const [keyType, keyValue] = tryUrls[i];
                const img = new Image();
                img.onload = () => {
                    fallback.style.display = 'none';
                    img.alt = prettyVehicleCategory(msg.vehicle_category);
                    visual.appendChild(img);
                };
                img.onerror = () => attempt(i + 1);
                img.src = vehiclePhotoUrl(keyType, keyValue);
            };
            attempt(0);
        }

        function openVehiclePhotoUpload() {
            const msg = selectedMsg;
            if (!msg || !(msg.vehicle_category || msg.vehicle_number)) return;

            const options = [];
            if (msg.vehicle_number) {
                options.push(`<label class="form-check"><input type="radio" name="vp-key" value="number" checked> This vehicle only (#${escapeHtml(msg.vehicle_number)})</label>`);
            }
            if (msg.vehicle_category) {
                options.push(`<label class="form-check"><input type="radio" name="vp-key" value="category" ${msg.vehicle_number ? '' : 'checked'}> All "${escapeHtml(prettyVehicleCategory(msg.vehicle_category))}" vehicles (fallback)</label>`);
            }

            const overlay = openModal({
                title: 'Upload vehicle photo',
                bodyHTML: `
                    <div class="form-field">
                        <label>Photo</label>
                        <input type="file" accept="image/*" class="input" data-vp-file>
                    </div>
                    <div class="form-field">${options.join('')}</div>
                    <div class="form-field">
                        <label>Label (optional)</label>
                        <input type="text" class="input" data-vp-label placeholder="e.g. 17106 - Rotterdam-Rijnmond">
                    </div>
                `,
                footHTML: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>Upload</button>`
            });

            overlay.querySelector('[data-save]').addEventListener('click', () => {
                const fileInput = overlay.querySelector('[data-vp-file]');
                const file = fileInput.files[0];
                if (!file) { toast('Choose an image file', 'error'); return; }
                const keyType = overlay.querySelector('input[name="vp-key"]:checked').value;
                const keyValue = keyType === 'number' ? msg.vehicle_number : msg.vehicle_category;
                const label = overlay.querySelector('[data-vp-label]').value.trim();

                const form = new FormData();
                form.append('key_type', keyType);
                form.append('key_value', keyValue);
                form.append('label', label);
                form.append('file', file);

                fetch(`${basePath}api/vehicle-photo`, { method: 'POST', body: form })
                    .then(r => r.json())
                    .then(result => {
                        if (result.error || result.success === false) {
                            toast(result.error || 'Upload failed', 'error');
                        } else {
                            toast('Photo saved', 'success');
                            closeModal();
                            loadVehiclePhoto(msg);
                        }
                    })
                    .catch(() => toast('Upload failed', 'error'));
            });
        }

        // Vehicle categories for the Vehicles table's category field
        let vehicleCategories = [];
        fetch(`${basePath}api/vehicle-categories`)
            .then(r => r.json())
            .then(data => { vehicleCategories = (data.categories || []).map(c => c.category); })
            .catch(() => {});

        // Standalone upload from the Settings tab's Vehicle Photos table (no selected message).
        function dbOpenVehiclePhotoUpload() {
            fetch(`${basePath}api/vehicle-categories`)
                .then(r => r.json())
                .then(data => dbShowVehiclePhotoUploadModal(data.categories || []))
                .catch(() => dbShowVehiclePhotoUploadModal([]));
        }

        function dbShowVehiclePhotoUploadModal(categories) {
            const categoryOptions = categories.map(c =>
                `<option value="${escapeHtml(c.category)}">${c.icon} ${escapeHtml(prettyVehicleCategory(c.category))}</option>`
            ).join('');

            const overlay = openModal({
                title: 'Upload vehicle photo',
                bodyHTML: `
                    <div class="form-field">
                        <label>Photo</label>
                        <input type="file" accept="image/*" class="input" data-vp-file>
                    </div>
                    <div class="form-field">
                        <label class="form-check"><input type="radio" name="db-vp-key" value="category" checked> Vehicle category (fallback photo)</label>
                        <label class="form-check"><input type="radio" name="db-vp-key" value="number"> Specific vehicle number</label>
                    </div>
                    <div class="form-field" data-db-vp-category-field>
                        <label>Category</label>
                        <select class="input" data-db-vp-category>${categoryOptions}</select>
                    </div>
                    <div class="form-field" data-db-vp-number-field style="display:none;">
                        <label>Vehicle number</label>
                        <input type="text" class="input" data-db-vp-number placeholder="e.g. 07-1782 or 18-187">
                        <div style="font-size: 12px; color: var(--secondary-text-color); margin-top: 4px;">The RR-NNN number only, without the vehicle-type prefix - e.g. "07-1782" (fire brigade) or "18-187" (ambulance region-unit). Check the vehicle's own #tag on a matching message's detail panel for the exact value to use.</div>
                    </div>
                    <div class="form-field">
                        <label>Label (optional)</label>
                        <input type="text" class="input" data-db-vp-label placeholder="e.g. 17106 - Rotterdam-Rijnmond">
                    </div>
                `,
                footHTML: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>Upload</button>`
            });

            const categoryField = overlay.querySelector('[data-db-vp-category-field]');
            const numberField = overlay.querySelector('[data-db-vp-number-field]');
            overlay.querySelectorAll('input[name="db-vp-key"]').forEach(radio => {
                radio.addEventListener('change', () => {
                    const isNumber = radio.value === 'number' && radio.checked;
                    if (radio.checked) {
                        categoryField.style.display = radio.value === 'category' ? '' : 'none';
                        numberField.style.display = radio.value === 'number' ? '' : 'none';
                    }
                });
            });

            overlay.querySelector('[data-save]').addEventListener('click', () => {
                const fileInput = overlay.querySelector('[data-vp-file]');
                const file = fileInput.files[0];
                if (!file) { toast('Choose an image file', 'error'); return; }
                const keyType = overlay.querySelector('input[name="db-vp-key"]:checked').value;
                const keyValue = keyType === 'category'
                    ? overlay.querySelector('[data-db-vp-category]').value
                    : overlay.querySelector('[data-db-vp-number]').value.trim();
                if (!keyValue) { toast(keyType === 'category' ? 'Choose a category' : 'Enter a vehicle number', 'error'); return; }
                const label = overlay.querySelector('[data-db-vp-label]').value.trim();

                const form = new FormData();
                form.append('key_type', keyType);
                form.append('key_value', keyValue);
                form.append('label', label);
                form.append('file', file);

                fetch(`${basePath}api/vehicle-photo`, { method: 'POST', body: form })
                    .then(r => r.json())
                    .then(result => {
                        if (result.error || result.success === false) {
                            toast(result.error || 'Upload failed', 'error');
                        } else {
                            toast('Photo saved', 'success');
                            closeModal();
                            dbLoadData();
                            dbLoadStats();
                        }
                    })
                    .catch(() => toast('Upload failed', 'error'));
            });
        }

        let currentMessages = [];

        // Messages sharing an incident id (same address, close in time) are
        // shown as one card, newest message on top. Returns groups of
        // {idx, msgs} where idx is the newest message's index in `messages`.
        function groupByIncident(messages) {
            const groups = [];
            const byIncident = {};
            messages.forEach((msg, idx) => {
                const group = msg.incident_id && byIncident[msg.incident_id];
                if (group) {
                    group.msgs.push(msg);
                } else {
                    const created = {idx, msgs: [msg]};
                    groups.push(created);
                    if (msg.incident_id) byIncident[msg.incident_id] = created;
                }
            });
            return groups;
        }

        // Priority as the pager writes it (A1, B2, P1...), else P + level
        function prioLabel(msg) {
            const m = /^\\s*([ABP])\\s?([0-9])/i.exec(msg.body || '');
            if (m) return (m[1] + m[2]).toUpperCase();
            return msg.priority ? 'P' + msg.priority : '';
        }

        function prioChip(msg) {
            const label = prioLabel(msg);
            if (!label) return '';
            const cls = msg.priority === 1 ? 'p1' : (msg.priority === 2 ? 'p2' : '');
            return `<span class="prio ${cls}">${label}</span>`;
        }

        const LANES = [['brandweer', 'Brandweer'], ['ambulance', 'Ambulance'], ['politie', 'Politie'], ['other', 'Other']];

        // The lane (and colour) of a message: its first known discipline. A and
        // B urgency codes are ambulance codes, so a message that also reaches
        // fire brigade capcodes still belongs in the ambulance lane.
        function discKind(msg) {
            const disc = (msg.discipline || '').toLowerCase();
            if (disc.includes('ambulance') && /^[AB]/.test(prioLabel(msg))) return 'ambulance';
            const lane = LANES.slice(0, 3).find(([kind]) => disc.includes(kind));
            return lane ? lane[0] : 'other';
        }

        function placeOf(msg) {
            return msg.street ? `${msg.street}, ${msg.city}` : (msg.city || '');
        }

        function consoleRow(msg, idx, incidentSize) {
            return `
                <button type="button" class="feed-row" data-msg-index="${idx}" onclick="handleMessageClick(${idx})">
                    <span class="time">${formatTime(msg.timestamp)}</span>
                    <span>${prioChip(msg)}</span>
                    <span class="disc ${discKind(msg)}">${escapeHtml(msg.discipline || '—')}</span>
                    <span class="text">${escapeHtml(msg.body || '')}${incidentSize > 1 ? `<span class="badge incident" title="${incidentSize} messages for this incident">🚨 ${incidentSize}</span>` : ''}${msg.repeats ? `<span class="badge" title="Resent ${msg.repeats} times by the pager network">🔁 ${msg.repeats}</span>` : ''}</span>
                    <span class="place">${escapeHtml(placeOf(msg))}</span>
                    <span class="unit">${escapeHtml(msg.vehicle_number || '')}</span>
                </button>`;
        }

        function renderConsole(latest) {
            const el = document.getElementById('console-rows');
            const sizes = {};
            latest.forEach(m => { if (m.incident_id) sizes[m.incident_id] = (sizes[m.incident_id] || 0) + 1; });
            const shown = new Set();
            el.innerHTML = latest.map((m, idx) => {
                // The incident count goes on the incident's newest message only
                const first = m.incident_id && !shown.has(m.incident_id);
                if (first) shown.add(m.incident_id);
                return consoleRow(m, idx, first ? sizes[m.incident_id] : 0);
            }).join('') || '<div class="empty-state">No messages yet</div>';
        }

        function disciplineTags(msgs) {
            const counts = {};
            msgs.forEach(m => (m.discipline || '').split(',').map(d => d.trim()).filter(Boolean)
                .forEach(d => { counts[d] = (counts[d] || 0) + 1; }));
            return Object.entries(counts).map(([d, n]) =>
                `<span class="disc-tag ${discKind({discipline: d})}">${escapeHtml(d)}${n > 1 ? ` (${n})` : ''}</span>`
            ).join('');
        }

        function gripLevel(msg) {
            const m = /GRIP\\s*:?\\s*([1-5])/i.exec(msg.body || '');
            return m ? Number(m[1]) : 0;
        }

        // Incidents (or single messages) by priority, newest first; ones quiet
        // for longer than the incident window drop below the active ones
        function renderWallboard() {
            const latest = currentMessages;
            const windowMs = ((lastStats && lastStats.incident_window_minutes) || 15) * 60000;
            const items = groupByIncident(latest).map(g => {
                const top = g.msgs.reduce((a, b) => ((b.priority || 9) < (a.priority || 9) ? b : a));
                return {g, top, active: Date.now() - new Date(g.msgs[0].timestamp) <= windowMs};
            }).sort((a, b) => (b.active - a.active) || ((a.top.priority || 9) - (b.top.priority || 9))
                || (new Date(b.g.msgs[0].timestamp) - new Date(a.g.msgs[0].timestamp)));

            document.getElementById('wall-items').innerHTML = items.map(({g, top, active}) => {
                const grip = Math.max(0, ...g.msgs.map(gripLevel));
                return `
                    <button type="button" class="incident-card${active ? '' : ' stale'}" data-msg-index="${g.idx}" onclick="handleMessageClick(${g.idx})">
                        <span class="top">${prioChip(top)}<span class="title">${escapeHtml(top.body || '')}</span><span class="age">${ago(g.msgs[0].timestamp)}</span></span>
                        <span class="place">${escapeHtml(placeOf(top) || top.region || '—')}${g.msgs.length > 1 ? ` · ${g.msgs.length} messages` : ''}</span>
                        <span class="tags">${disciplineTags(g.msgs)}${grip ? `<span class="disc-tag grip">GRIP ${grip}</span>` : ''}</span>
                    </button>`;
            }).join('') || '<div class="empty-state">No messages yet</div>';

            const active = items.filter(i => i.active);
            const maxGrip = Math.max(0, ...active.flatMap(i => i.g.msgs).map(gripLevel));
            document.getElementById('wall-kpis').innerHTML = `
                <div class="wall-kpi"><div class="v p1">${active.filter(i => i.top.priority === 1).length}</div><div class="l">P1 active</div></div>
                <div class="wall-kpi"><div class="v">${active.length}</div><div class="l">Active</div></div>
                <div class="wall-kpi"><div class="v">${lastStats ? lastStats.messages_last_hour : 0}</div><div class="l">Msg / hour</div></div>
                <div class="wall-kpi"><div class="v${maxGrip ? ' grip' : ''}">${maxGrip ? 'GRIP ' + maxGrip : '—'}</div><div class="l">GRIP</div></div>`;

            document.getElementById('wall-ticker').innerHTML = latest.slice(0, 5).map((m, idx) => `
                <button type="button" class="tick" data-msg-index="${idx}" onclick="handleMessageClick(${idx})">
                    <span class="meta"><span>${formatTime(m.timestamp)}</span>${prioChip(m)}<span class="disc ${discKind(m)}">${escapeHtml(m.discipline || '')}</span></span>
                    <span class="text">${escapeHtml(m.body || '')}</span>
                </button>`).join('');
        }

        function renderLanes(latest) {
            LANES.forEach(([kind, name]) => {
                const items = latest.map((m, idx) => ({m, idx})).filter(({m}) => discKind(m) === kind);
                const p1 = items.filter(({m}) => m.priority === 1).length;
                document.getElementById('lane-' + kind).innerHTML = `
                    <div class="lane-head ${kind}">${name}${p1 ? `<span class="prio p1">${p1} P1</span>` : ''}<span class="count">${items.length}</span></div>
                    <div class="lane-items">${items.map(({m, idx}) => `
                        <button type="button" class="lane-card" data-msg-index="${idx}" onclick="handleMessageClick(${idx})">
                            <span class="meta">${prioChip(m)}<span>${formatTime(m.timestamp)}</span><span class="unit">${escapeHtml(m.vehicle_number || '')}</span></span>
                            <span class="text">${escapeHtml(m.body || '')}</span>
                            <span class="place">${escapeHtml(placeOf(m))}</span>
                        </button>`).join('') || '<div class="empty-state">Nothing</div>'}
                    </div>`;
            });
        }

        function handleMessageClick(idx) {
            if (!currentMessages[idx]) return;
            if (activeView !== 'console') openDetailFloat();
            showMessageDetails(currentMessages[idx], idx);
        }

        function renderPieChart(data, el) {
            const sorted = Object.entries(data || {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
            const total = sorted.reduce((sum, [, v]) => sum + v, 0);
            if (total === 0) { el.innerHTML = '<div class="empty-state" style="padding: 20px;">No data</div>'; return; }

            const colors = ['#4c8dff', '#f5a524', '#5fd68a', '#ff6159', '#b18cff', '#4fc3c8'];
            let cumulativePercent = 0;
            let paths = '';
            let legend = '';

            sorted.forEach(([label, value], i) => {
                const percent = value / total;
                const startX = Math.cos(2 * Math.PI * cumulativePercent);
                const startY = Math.sin(2 * Math.PI * cumulativePercent);
                cumulativePercent += percent;
                const endX = Math.cos(2 * Math.PI * cumulativePercent);
                const endY = Math.sin(2 * Math.PI * cumulativePercent);
                const largeArc = percent > 0.5 ? 1 : 0;
                paths += `<path d="M 0 0 L ${startX} ${startY} A 1 1 0 ${largeArc} 1 ${endX} ${endY} Z" fill="${colors[i % colors.length]}" opacity="0.85"/>`;
                const pct = Math.round(percent * 100);
                legend += `<div class="chart-legend-item" title="${escapeHtml(label)}: ${value} (${pct}%)">` +
                    `<span class="swatch" style="background:${colors[i % colors.length]}"></span>` +
                    `<span class="label">${escapeHtml(label)}</span>` +
                    `<span class="count">${value}</span>` +
                    `</div>`;
            });

            el.innerHTML = `<div class="chart-row"><svg viewBox="-1.1 -1.1 2.2 2.2" style="width:110px;height:110px;flex-shrink:0;transform:rotate(-90deg)">${paths}</svg><div class="chart-legend">${legend}</div></div>`;
        }

        function updateMarkers(messages) {
            markers.forEach(m => map.removeLayer(m));
            markers.length = 0;
            messages.filter(m => m.latitude && m.longitude).slice(0, 20).forEach(msg => {
                const m = L.circleMarker([msg.latitude, msg.longitude], {
                    radius: 6, fillColor: msg.priority === 1 ? '#ff5252' : '#00d4ff',
                    color: '#fff', weight: 1, fillOpacity: 0.8
                }).addTo(map);
                m.bindPopup(`<b>${escapeHtml(msg.discipline || 'P2000')}</b><br><div style="max-width:200px;word-wrap:break-word;">${escapeHtml(msg.body || 'No message')}</div>`);
                markers.push(m);
            });
        }

        // Signatures let us skip DOM work when the stream delivers unchanged
        // data - this keeps scroll position and selection intact.
        let lastMsgsSig = null;
        let lastChartSig = null;

        // Freezes the region/discipline charts so incoming messages don't
        // redraw them mid-investigation; resuming immediately catches up.
        let chartsPaused = false;

        function toggleChartsPause() {
            chartsPaused = !chartsPaused;
            const btn = document.getElementById('charts-pause-toggle');
            const icon = document.getElementById('charts-pause-icon');
            btn.classList.toggle('paused', chartsPaused);
            icon.textContent = chartsPaused ? '▶️' : '⏸️';
            btn.title = chartsPaused
                ? 'Charts paused - click to resume live updates'
                : 'Pause chart updates to investigate without refreshes';
        }

        function renderMessagesList(latest) {
            const sig = latest.length + '|' + latest.map(m => m.timestamp + (m.repeats || 0)).join('|');
            if (sig === lastMsgsSig) return;
            lastMsgsSig = sig;

            currentMessages = latest;
            if (!latest.length) selectedMsgKey = null;
            renderConsole(latest);
            renderWallboard();
            renderLanes(latest);
            highlightSelectedMessage();
            if (selectedMsg) renderDetailNav();
            updateMarkers(latest);
        }

        // Incident ages on the wallboard keep counting without new messages
        setInterval(() => { renderWallboard(); highlightSelectedMessage(); }, 30000);

        let lastStats = null;

        function update(data) {
            const stats = data.stats;
            lastStats = stats;

            renderSensorZones(stats.sensor_zones);
            checkForNewMessages(data.latest || []);

            totalEl.textContent = stats.total_messages;
            document.getElementById('kpi-msgs-hour').textContent = stats.messages_last_hour;
            document.getElementById('kpi-frames').textContent = stats.frames_last_minute;
            document.getElementById('kpi-incidents').textContent = stats.open_incidents;
            document.getElementById('device-info').textContent =
                `${stats.device_type || '-'} · ${stats.device_driver || '-'} · ppm ${stats.ppm}`;

            const networkEl = document.getElementById('network-name');
            if (networkEl && stats.network_name) {
                networkEl.textContent = stats.network_name;
            }
            const receiverEl = document.getElementById('receiver-type');
            if (receiverEl) {
                receiverEl.textContent = stats.device_type || '';
            }

            const rx = document.getElementById('rx-status');
            if (!stats.decoder_running) {
                rx.className = 'rx stopped';
                rx.textContent = stats.decoder_error ? 'NO SDR DEVICE' : 'DECODER STOPPED';
                rx.title = stats.decoder_error || '';
                decoderEl.className = 'decoder-bad';
                decoderEl.textContent = '● ' + (stats.decoder_error || 'Decoder stopped');
            } else if (!stats.receiver_online) {
                rx.className = 'rx offline';
                rx.textContent = 'NO FLEX FRAMES';
                rx.title = 'The decoder runs but has not decoded any FLEX frames recently - check the antenna, gain and ppm';
                decoderEl.className = 'decoder-warn';
                decoderEl.textContent = '● DECODER RUNNING · NO FRAMES';
            } else {
                rx.className = 'rx online';
                rx.textContent = 'RX ONLINE';
                rx.title = '';
                decoderEl.className = 'decoder-ok';
                decoderEl.textContent = '● DECODER RUNNING';
            }

            lastStartTime = stats.start_time;
            renderUptime();

            const chartSig = JSON.stringify(stats.by_region || {}) + '|' + JSON.stringify(stats.by_discipline || {});
            if (!chartsPaused && chartSig !== lastChartSig) {
                lastChartSig = chartSig;
                renderPieChart(stats.by_region, regionChartEl);
                renderPieChart(stats.by_discipline, disciplineChartEl);
            }

            renderMessagesList(data.latest || []);
            updateInfo(stats);
        }

        // =====================================================================
        // Live stream + initial load
        // =====================================================================
        let currentSensorFilter = '';
        let evtSource = null;

        function streamUrl() {
            return basePath + 'api/stream' + (currentSensorFilter ? `?sensor=${encodeURIComponent(currentSensorFilter)}` : '');
        }

        // The server sends data or a heartbeat at least every 15 s. When nothing
        // arrives for longer, the connection was dropped without the browser
        // noticing (e.g. by the ingress proxy): reconnect and reload.
        const STREAM_TIMEOUT_MS = 45000;
        let lastStreamActivity = Date.now();

        function connectStream() {
            if (evtSource) evtSource.close();
            lastStreamActivity = Date.now();
            evtSource = new EventSource(streamUrl());
            evtSource.onopen = () => {
                lastStreamActivity = Date.now();
                streamStatusEl.className = 'conn live';
                streamStatusText.textContent = 'Live';
            };
            evtSource.onmessage = (e) => {
                lastStreamActivity = Date.now();
                update(JSON.parse(e.data));
            };
            evtSource.addEventListener('ping', () => { lastStreamActivity = Date.now(); });
            evtSource.onerror = () => {
                streamStatusEl.className = 'conn reconnecting';
                streamStatusText.textContent = 'Reconnecting…';
                // A rejected reconnect (e.g. while Home Assistant renews the
                // ingress session) makes the browser give up for good: retry
                const source = evtSource;
                if (source.readyState === EventSource.CLOSED) {
                    setTimeout(() => {
                        if (evtSource !== source) return;
                        connectStream();
                        loadMessagesList();
                    }, 5000);
                }
            };
        }

        setInterval(() => {
            if (evtSource && Date.now() - lastStreamActivity > STREAM_TIMEOUT_MS) {
                connectStream();
                loadMessagesList();
            }
        }, 15000);

        // A failure right after waking up is usually Home Assistant's ingress
        // session being renewed: retry a few times before reporting it
        function loadMessagesList(attempt = 1) {
            const url = basePath + 'api/messages' + (currentSensorFilter ? `?sensor=${encodeURIComponent(currentSensorFilter)}` : '');
            fetch(url)
                .then(r => {
                    if (!r.ok) throw new Error(`HTTP ${r.status}`);
                    return r.json();
                })
                .then(msgs => renderMessagesList(msgs.slice(0, 50)))
                .catch(e => {
                    if (attempt < 4) setTimeout(() => loadMessagesList(attempt + 1), attempt * 2000);
                    else toast(`Failed to load messages (${e.message})`, 'error');
                });
        }

        function clearSelectedMessage() {
            // The selected message's map pin/popup and the detail panel are set
            // by clicking a message, independent of the periodic message-list/
            // marker refresh - switching filters must clear them explicitly, or
            // a message that no longer matches the new filter keeps showing.
            selectedMsg = null;
            selectedMsgKey = null;
            closeDetailFloat();
            if (currentMarker) { map.removeLayer(currentMarker); currentMarker = null; }
            detailContent.innerHTML = '<div class="empty-state">Click a message to view details</div>';
            document.getElementById('history-section').hidden = true;
            document.getElementById('incident-section').hidden = true;
            document.getElementById('detail-nav').hidden = true;
        }

        function selectSensorFilter(name) {
            if (name === currentSensorFilter) return;
            currentSensorFilter = name;
            document.querySelectorAll('.sensor-filter-pill').forEach(el => {
                el.classList.toggle('active', el.dataset.sensor === name);
            });
            document.querySelector('.filter-select').value = name;
            lastMsgsSig = null;  // force re-render even if the next batch looks identical in size/timestamps
            clearSelectedMessage();
            connectStream();
            loadMessagesList();
        }

        function loadSensorFilterBar() {
            fetch(basePath + 'api/sensors').then(r => r.json()).then(sensors => {
                const bar = document.getElementById('sensor-filter-bar');
                if (sensors && sensors.length > 0) {
                    // Default to "only messages matching one of your configured
                    // sensors" - the whole point of defining sensors is to narrow
                    // the feed, so showing everything by default defeats that.
                    currentSensorFilter = '__any__';
                    const pills = [
                        `<button type="button" class="tab sensor-filter-pill active" data-sensor="__any__">Sensors</button>`,
                        `<button type="button" class="tab sensor-filter-pill" data-sensor="">All</button>`,
                    ].concat(sensors.map(s => `<button type="button" class="tab sensor-filter-pill" data-sensor="${escapeHtml(s.name)}">${escapeHtml(s.name)}</button>`));
                    const options = [['__any__', 'Sensors'], ['', 'All']].concat(sensors.map(s => [s.name, s.name]))
                        .map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`);
                    bar.innerHTML = '<span class="filter-label">Filter</span>' + pills.join('')
                        + `<select class="filter-select" aria-label="Feed filter">${options.join('')}</select>`;
                    bar.addEventListener('click', (e) => {
                        const pill = e.target.closest('.sensor-filter-pill');
                        if (pill) selectSensorFilter(pill.dataset.sensor);
                    });
                    bar.querySelector('.filter-select').addEventListener('change', (e) => selectSensorFilter(e.target.value));
                }
                connectStream();
                loadMessagesList();
            }).catch(() => {
                connectStream();
                loadMessagesList();
            });
        }

        const savedTab = localStorage.getItem('p2000_tab');
        switchTab(VIEWS.concat(['settings', 'info']).includes(savedTab) ? savedTab : 'console');
        loadSensorFilterBar();
        fetch(basePath + 'api/stats').then(r => r.json()).then(stats => update({stats, latest: []}))
            .catch(() => toast('Failed to load stats', 'error'));

        // =====================================================================
        // Settings tab: database tables and imports
        // =====================================================================
        let dbCurrentTable = null;
        let dbCurrentPage = 1;
        let dbTotalPages = 1;
        let dbSearchTimeout = null;
        let dbCurrentItems = [];

        const dbColumns = {
            capcodes: ['capcode', 'discipline', 'region', 'location', 'description'],
            places: ['city', 'abbreviation', 'province'],
            streets: ['street', 'city_name', 'postalcode'],
            messages: ['timestamp', 'discipline', 'city', 'street', 'body'],
            texts: ['abbreviation', 'full_text'],
            geocodes: ['query', 'city', 'street', 'latitude', 'longitude'],
            tts: ['pattern', 'replacement', 'enabled'],
            ignore_text: ['pattern', 'enabled'],
            ignore_capcodes: ['capcode', 'enabled'],
            vehicle_photos: ['thumbnail', 'key_type', 'key_value', 'label'],
            vehicles: ['number', 'category', 'description', 'source']
        };

        const dbTitles = {
            capcodes: 'Capcodes',
            places: 'Places',
            streets: 'Streets',
            messages: 'Messages',
            texts: 'Texts',
            geocodes: 'Geocodes',
            tts: 'TTS Replacements',
            ignore_text: 'Ignore Text',
            ignore_capcodes: 'Ignore Capcodes',
            vehicle_photos: 'Vehicle Photos',
            vehicles: 'Vehicles'
        };

        const dbTableButtons = {
            capcodes: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbAdd'},
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Import CSV', cls: '', fn: 'dbImportDialog'},
                {label: 'Import Bommel', cls: '', fn: 'dbImportBommel'},
                {label: 'Clear table', cls: 'btn-danger', fn: 'dbClearTable'}
            ],
            places: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbAdd'},
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Import CSV', cls: '', fn: 'dbImportDialog'},
                {label: 'Import All Places', cls: '', fn: 'importAllPlaces'},
                {label: 'Import BAG', cls: '', fn: 'dbImportBAG'},
                {label: 'Clear table', cls: 'btn-danger', fn: 'dbClearTable'}
            ],
            streets: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbAdd'},
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Import CSV', cls: '', fn: 'dbImportDialog'},
                {label: 'Import BAG', cls: '', fn: 'dbImportBAG'},
                {label: 'Clear table', cls: 'btn-danger', fn: 'dbClearTable'}
            ],
            messages: [
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Delete all', cls: 'btn-danger', fn: 'dbDeleteAllMessages'}
            ],
            texts: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbAdd'},
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Import Texts', cls: '', fn: 'dbImportAbbreviations'},
                {label: 'Clear table', cls: 'btn-danger', fn: 'dbClearTable'}
            ],
            geocodes: [
                {label: 'Clear all', cls: 'btn-danger', fn: 'dbClearGeocodes'}
            ],
            tts: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbAdd'},
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Delete all', cls: 'btn-danger', fn: 'dbDeleteAllTts'}
            ],
            ignore_text: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbAdd'},
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Delete all', cls: 'btn-danger', fn: 'dbDeleteAllIgnoreText'}
            ],
            ignore_capcodes: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbAdd'},
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Delete all', cls: 'btn-danger', fn: 'dbDeleteAllIgnoreCapcodes'}
            ],
            vehicle_photos: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbOpenVehiclePhotoUpload'},
                {label: 'Clear table', cls: 'btn-danger', fn: 'dbClearTable'}
            ],
            vehicles: [
                {label: '+ Add', cls: 'btn-primary', fn: 'dbAdd'},
                {label: 'Export', cls: 'btn-primary', fn: 'dbExport'},
                {label: 'Import CSV', cls: '', fn: 'dbImportDialog'},
                {label: 'Clear table', cls: 'btn-danger', fn: 'dbClearTable'}
            ]
        };

        // Fields shown in the "Add" form per table
        const dbAddFields = {
            capcodes: ['capcode', 'discipline', 'region', 'location', 'description', 'remark'],
            places: ['city', 'abbreviation', 'province', 'latitude', 'longitude'],
            streets: ['street', 'city_id', 'postalcode'],
            texts: ['abbreviation', 'full_text'],
            tts: ['pattern', 'replacement'],
            ignore_text: ['pattern'],
            ignore_capcodes: ['capcode'],
            vehicles: ['number', 'category', 'description']
        };

        function getApiTable(table) {
            if (table === 'texts') return 'abbreviations';
            if (table === 'tts') return 'tts_replacements';
            return table;
        }

        function dbLoadStats() {
            const tables = [
                ['capcodes', 'capcodes'],
                ['places', 'places'],
                ['streets', 'streets'],
                ['messages', 'messages'],
                ['abbreviations', 'texts'],
                ['geocodes', 'geocodes'],
                ['tts_replacements', 'tts'],
                ['ignore_text', 'ignore_text'],
                ['ignore_capcodes', 'ignore_capcodes'],
                ['vehicle_photos', 'vehicle_photos'],
                ['vehicles', 'vehicles']
            ];
            tables.forEach(([api, card]) => {
                fetch(`${basePath}api/db/${api}?page=1&per_page=1`)
                    .then(r => r.json())
                    .then(data => {
                        const total = data.total || 0;
                        const countEl = document.getElementById('db-count-' + card);
                        if (countEl) countEl.textContent = total.toLocaleString();
                        const statusEl = document.getElementById('db-status-' + card);
                        if (statusEl) {
                            statusEl.textContent = total > 0 ? 'Ready' : 'Empty';
                            statusEl.className = 'db-card-status ' + (total > 0 ? 'ready' : 'empty');
                        }
                    })
                    .catch(() => {});
            });
        }

        function dbOpenTable(table) {
            dbCurrentTable = table;
            dbCurrentPage = 1;
            document.getElementById('db-search').value = '';
            document.getElementById('db-table-title').textContent = dbTitles[table];

            const buttons = dbTableButtons[table] || [];
            document.getElementById('db-buttons').innerHTML = buttons.map(b =>
                `<button class="btn ${b.cls}" onclick="${b.fn}(this)">${b.label}</button>`
            ).join('');

            document.getElementById('db-landing').style.display = 'none';
            document.getElementById('db-table-view').style.display = 'block';

            dbLoadData();
        }

        function dbBackToLanding() {
            dbCurrentTable = null;
            document.getElementById('db-landing').style.display = 'block';
            document.getElementById('db-table-view').style.display = 'none';
            dbLoadStats();
        }

        function dbLoadData() {
            const apiTable = getApiTable(dbCurrentTable);
            const search = document.getElementById('db-search').value;
            fetch(`${basePath}api/db/${apiTable}?page=${dbCurrentPage}&per_page=50&search=${encodeURIComponent(search)}`)
                .then(r => r.json())
                .then(data => dbRenderTable(data))
                .catch(e => console.error('Failed to load database:', e));
        }

        function dbRenderTable(data) {
            const cols = dbColumns[dbCurrentTable];
            const thead = document.getElementById('db-thead');
            const tbody = document.getElementById('db-tbody');
            dbCurrentItems = data.items || [];
            const editable = ['capcodes', 'places', 'streets', 'tts', 'ignore_text', 'ignore_capcodes', 'vehicles'].includes(dbCurrentTable);

            thead.innerHTML = '<tr>' + cols.map(c => `<th>${c}</th>`).join('') + `<th style="width: ${editable ? 76 : 40}px;"></th></tr>`;

            if (dbCurrentItems.length === 0) {
                tbody.innerHTML = `<tr><td colspan="${cols.length + 1}" class="empty-state">No data found</td></tr>`;
            } else {
                tbody.innerHTML = dbCurrentItems.map((item, i) =>
                    '<tr>' +
                    cols.map(c => c === 'thumbnail'
                        ? `<td><img src="${vehiclePhotoUrl(item.key_type, item.key_value)}" alt="" style="width:36px;height:36px;object-fit:cover;border-radius:6px;" onerror="this.style.display='none'"></td>`
                        : `<td>${escapeHtml(item[c] ?? '')}</td>`).join('') +
                    `<td style="white-space: nowrap;">${editable ? `<button class="row-del" title="Edit row" onclick="dbEditRow(${i})">✏️</button>` : ''}<button class="row-del" title="Delete row" onclick="dbDeleteRow(${i})">🗑️</button></td>` +
                    '</tr>'
                ).join('');
            }

            document.getElementById('db-count').textContent = `${(data.total || 0).toLocaleString()} items`;
            dbCurrentPage = data.page || 1;
            dbTotalPages = Math.max(1, Math.ceil((data.total || 0) / (data.per_page || 50)));
            document.getElementById('db-page').textContent = `Page ${dbCurrentPage} / ${dbTotalPages}`;
            document.getElementById('db-prev').disabled = dbCurrentPage <= 1;
            document.getElementById('db-next').disabled = dbCurrentPage >= dbTotalPages;
        }

        function dbSearch() {
            clearTimeout(dbSearchTimeout);
            dbSearchTimeout = setTimeout(() => {
                dbCurrentPage = 1;
                dbLoadData();
            }, 300);
        }

        function dbPrev() { if (dbCurrentPage > 1) { dbCurrentPage--; dbLoadData(); } }
        function dbNext() { if (dbCurrentPage < dbTotalPages) { dbCurrentPage++; dbLoadData(); } }

        function dbRowId(item) {
            if (dbCurrentTable === 'capcodes') return item.capcode;
            if (dbCurrentTable === 'geocodes') return item.query;
            if (dbCurrentTable === 'vehicles') return item.number;
            return item.id;
        }

        async function dbDeleteRow(i) {
            const item = dbCurrentItems[i];
            if (!item) return;
            const ok = await confirmDialog({
                title: 'Delete row',
                message: 'Delete this item? This cannot be undone.',
                confirmLabel: 'Delete',
                danger: true
            });
            if (!ok) return;
            const url = dbCurrentTable === 'vehicle_photos'
                ? `${basePath}api/vehicle-photo/${item.key_type}/${encodeURIComponent(item.key_value)}`
                : `${basePath}api/db/${getApiTable(dbCurrentTable)}/${encodeURIComponent(dbRowId(item))}`;
            fetch(url, {method: 'DELETE'})
                .then(r => r.json())
                .then(result => {
                    if (result.success === false) toast('Delete failed', 'error');
                    else toast('Row deleted', 'success');
                    dbLoadData();
                })
                .catch(() => toast('Delete failed', 'error'));
        }

        // ----- Known-value suggestions (e.g. capcodes already in the database) -----
        let capcodeSuggestTimeout = null;
        function updateCapcodeSuggestions(query) {
            clearTimeout(capcodeSuggestTimeout);
            capcodeSuggestTimeout = setTimeout(() => {
                fetch(`${basePath}api/db/capcodes?page=1&per_page=15&search=${encodeURIComponent(query || '')}`)
                    .then(r => r.json())
                    .then(data => {
                        const datalist = document.getElementById('capcode-suggestions');
                        if (!datalist) return;
                        datalist.innerHTML = (data.items || [])
                            .map(c => `<option value="${escapeHtml(c.capcode)}" label="${escapeHtml(c.description || '')}">`)
                            .join('');
                    })
                    .catch(() => {});
            }, 250);
        }

        // Builds the <input> for one field in the Add/Edit forms - fields with
        // known values (like capcodes already in the database) get suggestions,
        // gracefully offering nothing if that table hasn't been imported yet.
        function dbFieldInputHTML(f, value) {
            const valueAttr = value !== undefined ? ` value="${escapeHtml(value)}"` : '';
            if (dbCurrentTable === 'ignore_capcodes' && f === 'capcode') {
                return `<input type="text" class="input" data-field="${f}" list="capcode-suggestions" oninput="updateCapcodeSuggestions(this.value)"${valueAttr}>`;
            }
            if (dbCurrentTable === 'vehicles' && f === 'category') {
                const options = ['', ...vehicleCategories].map(c =>
                    `<option value="${escapeHtml(c)}"${c === (value || '') ? ' selected' : ''}>${c ? escapeHtml(prettyVehicleCategory(c)) : '(unknown)'}</option>`).join('');
                return `<select class="input" data-field="${f}">${options}</select>`;
            }
            // Editing a capcode or vehicle row: the key is in the URL and can't change
            const readonly = value !== undefined && ((dbCurrentTable === 'capcodes' && f === 'capcode') || (dbCurrentTable === 'vehicles' && f === 'number')) ? ' readonly' : '';
            return `<input type="text" class="input" data-field="${f}"${valueAttr}${readonly}>`;
        }

        // ----- Add item (modal form) -----
        function dbAdd() {
            const fields = dbAddFields[dbCurrentTable];
            if (!fields) {
                toast('Rows cannot be added to this table', 'error');
                return;
            }
            if (dbCurrentTable === 'ignore_capcodes') updateCapcodeSuggestions('');
            const bodyHTML = fields.map(f => `
                <div class="form-field">
                    <label>${f}</label>
                    ${dbFieldInputHTML(f)}
                </div>`).join('');
            const overlay = openModal({
                title: `Add to ${dbTitles[dbCurrentTable]}`,
                bodyHTML,
                footHTML: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>Add</button>`
            });
            overlay.querySelector('[data-save]').addEventListener('click', () => {
                const values = {};
                overlay.querySelectorAll('[data-field]').forEach(inp => { values[inp.dataset.field] = inp.value.trim(); });
                const required = fields[0];
                if (!values[required]) {
                    toast(`${required} is required`, 'error');
                    return;
                }
                fetch(`${basePath}api/db/${getApiTable(dbCurrentTable)}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(values)
                }).then(r => r.json()).then(result => {
                    if (result.error) toast(result.error, 'error');
                    else {
                        toast('Item added', 'success');
                        closeModal();
                        dbLoadData();
                    }
                }).catch(() => toast('Add failed', 'error'));
            });
        }

        // ----- Edit row (modal form, pre-filled) -----
        function dbEditRow(i) {
            const item = dbCurrentItems[i];
            if (!item) return;
            const fields = dbAddFields[dbCurrentTable] || [];
            if (dbCurrentTable === 'ignore_capcodes') updateCapcodeSuggestions('');
            const bodyHTML = fields.map(f => `
                <div class="form-field">
                    <label>${f}</label>
                    ${dbFieldInputHTML(f, item[f] ?? '')}
                </div>`).join('') +
                (['tts', 'ignore_text', 'ignore_capcodes'].includes(dbCurrentTable) ? `
                <label class="form-check"><input type="checkbox" data-field-enabled ${item.enabled ? 'checked' : ''}> Enabled</label>` : '');
            const overlay = openModal({
                title: `Edit ${dbTitles[dbCurrentTable]}`,
                bodyHTML,
                footHTML: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>Save</button>`
            });
            overlay.querySelector('[data-save]').addEventListener('click', () => {
                const values = {};
                overlay.querySelectorAll('[data-field]').forEach(inp => { values[inp.dataset.field] = inp.value.trim(); });
                const enabledBox = overlay.querySelector('[data-field-enabled]');
                if (enabledBox) values.enabled = enabledBox.checked;
                fetch(`${basePath}api/db/${getApiTable(dbCurrentTable)}/${encodeURIComponent(dbRowId(item))}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(values)
                }).then(r => r.json()).then(result => {
                    if (result.error || result.success === false) toast(result.error || 'Update failed', 'error');
                    else {
                        toast('Row updated', 'success');
                        closeModal();
                        dbLoadData();
                    }
                }).catch(() => toast('Update failed', 'error'));
            });
        }

        // ----- CSV export/import -----
        function dbExport() {
            const apiTable = getApiTable(dbCurrentTable);
            window.location.href = `${basePath}api/db/export/${apiTable}`;
        }

        function dbImportDialog() {
            const overlay = openModal({
                title: `Import CSV into ${dbTitles[dbCurrentTable]}`,
                bodyHTML: `
                    <div class="form-field">
                        <label>Choose a .csv file</label>
                        <input type="file" accept=".csv,text/csv" class="input" data-file>
                    </div>
                    <div class="form-field">
                        <label>…or paste CSV content</label>
                        <textarea class="input" rows="8" data-csv placeholder="header1,header2&#10;value1,value2"></textarea>
                    </div>
                    <label class="form-check"><input type="checkbox" data-replace> Replace all existing data (default: merge)</label>`,
                footHTML: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-import>Import</button>`
            });
            const textarea = overlay.querySelector('[data-csv]');
            overlay.querySelector('[data-file]').addEventListener('change', (e) => {
                const file = e.target.files[0];
                if (!file) return;
                const reader = new FileReader();
                reader.onload = () => { textarea.value = reader.result; };
                reader.readAsText(file);
            });
            overlay.querySelector('[data-import]').addEventListener('click', () => {
                const csv = textarea.value.trim();
                if (!csv) { toast('No CSV content provided', 'error'); return; }
                const replace = overlay.querySelector('[data-replace]').checked;
                fetch(`${basePath}api/db/import/${getApiTable(dbCurrentTable)}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({csv, replace})
                }).then(r => r.text().then(text => {
                    let result = null;
                    try { result = JSON.parse(text); } catch (e) { /* plain-text error page */ }
                    if (!r.ok) {
                        const msg = (result && result.error) || (r.status === 413 ? 'CSV file too large (max 50 MB)' : text);
                        throw new Error(msg || `HTTP ${r.status}`);
                    }
                    return result || {};
                })).then(result => {
                    if (result.error) toast(result.error, 'error');
                    else {
                        toast(`Imported ${result.imported} items, skipped ${result.skipped}`, 'success');
                        closeModal();
                        dbLoadData();
                    }
                }).catch(e => toast('Import failed: ' + e.message, 'error'));
            });
        }

        // ----- BAG import -----
        let bagProgressInterval = null;

        function dbImportBAG(btn) {
            fetch(`${basePath}api/db/bag/provinces`)
                .then(r => r.json())
                .then(data => {
                    const provinces = data.provinces || [];
                    const overlay = openModal({
                        title: 'Import BAG address data',
                        bodyHTML: `
                            <div class="form-field">
                                <label>Province</label>
                                <select class="input" data-province style="width:100%">
                                    ${provinces.map(p => `<option>${escapeHtml(p)}</option>`).join('')}
                                </select>
                            </div>
                            <div class="panel-sub">Imports cities and streets for the selected province. This may take 5-10 minutes for large provinces.</div>`,
                        footHTML: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-start>Start import</button>`
                    });
                    overlay.querySelector('[data-start]').addEventListener('click', () => {
                        const province = overlay.querySelector('[data-province]').value;
                        closeModal();

                        // Progress in the button itself, same as Import All Places
                        const origLabel = btn ? btn.textContent : '';
                        if (btn) { btn.disabled = true; btn.textContent = '⏳ Starting…'; }
                        const resetBtn = () => { if (btn) { btn.disabled = false; btn.textContent = origLabel; } };

                        const stopPolling = () => {
                            if (bagProgressInterval) { clearInterval(bagProgressInterval); bagProgressInterval = null; }
                        };

                        // Poll only once the server has reset progress for this run,
                        // or the first poll can read the previous run's result
                        fetch(`${basePath}api/db/bag/import/${encodeURIComponent(province)}`, {method: 'POST'})
                            .then(r => r.json())
                            .then(result => {
                                if (result.error) {
                                    toast(result.error, 'error');
                                    resetBtn();
                                    return;
                                }
                                stopPolling();
                                bagProgressInterval = setInterval(() => {
                                    fetch(`${basePath}api/db/bag/progress`)
                                        .then(r => r.json())
                                        .then(p => {
                                            if (btn) btn.textContent = `⏳ ${p.percent}%`;
                                            if (!p.running) {
                                                stopPolling();
                                                if (p.percent >= 100) {
                                                    toast(`BAG import complete: ${p.cities} cities, ${p.streets} streets`, 'success');
                                                } else {
                                                    toast('BAG import failed: ' + (p.status || 'unknown error'), 'error');
                                                }
                                                resetBtn();
                                                dbLoadStats();
                                                if (dbCurrentTable) dbLoadData();
                                            }
                                        })
                                        .catch(() => {
                                            stopPolling();
                                            toast('Lost track of BAG import progress', 'error');
                                            resetBtn();
                                        });
                                }, 500);
                            })
                            .catch(() => {
                                stopPolling();
                                toast('BAG import failed', 'error');
                                resetBtn();
                            });
                    });
                })
                .catch(() => toast('Failed to load provinces', 'error'));
        }

        function importAllPlaces(btn) {
            confirmDialog({
                title: 'Import all places',
                message: 'Import ~2600 Dutch places from PDOK?',
                confirmLabel: 'Import'
            }).then(ok => {
                if (!ok) return;
                if (btn) { btn.disabled = true; btn.textContent = '⏳ Importing…'; }
                const resetBtn = () => { if (btn) { btn.disabled = false; btn.textContent = '🌍 Import All Places'; } };

                fetch(`${basePath}api/db/bag/import-all-places`, {method: 'POST'})
                    .then(r => r.json())
                    .then(result => {
                        if (result.error) {
                            toast(result.error, 'error');
                            resetBtn();
                            return;
                        }
                        const pollInterval = setInterval(() => {
                            fetch(`${basePath}api/db/bag/progress`)
                                .then(r => r.json())
                                .then(p => {
                                    if (btn) btn.textContent = `⏳ ${p.percent}%`;
                                    if (!p.running) {
                                        clearInterval(pollInterval);
                                        if (p.percent >= 100) {
                                            toast(`Import complete: ${p.cities} places`, 'success');
                                        } else {
                                            toast('Import failed: ' + (p.status || 'unknown error'), 'error');
                                        }
                                        resetBtn();
                                        dbLoadStats();
                                    }
                                })
                                .catch(() => {
                                    clearInterval(pollInterval);
                                    toast('Lost track of import progress', 'error');
                                    resetBtn();
                                });
                        }, 500);
                    })
                    .catch(() => {
                        toast('Import failed', 'error');
                        resetBtn();
                    });
            });
        }

        // ----- Bommel capcode import -----
        let capcodeProgressInterval = null;

        function dbImportBommel(btn) {
            confirmDialog({
                title: 'Import capcodes',
                message: 'Import all capcodes from p2000.bommel.net?<br><br>This fetches ~10,000 capcodes from all 25 regions and takes about 1-2 minutes.',
                confirmLabel: 'Import'
            }).then(ok => {
                if (!ok) return;
                const origLabel = btn ? btn.textContent : '';
                if (btn) { btn.disabled = true; btn.textContent = '⏳ Starting…'; }
                const resetBtn = () => { if (btn) { btn.disabled = false; btn.textContent = origLabel; } };

                const stopPolling = () => {
                    if (capcodeProgressInterval) { clearInterval(capcodeProgressInterval); capcodeProgressInterval = null; }
                };

                // Poll only once the server has reset progress for this run
                fetch(`${basePath}api/db/capcodes/import-bommel`, {method: 'POST'})
                    .then(r => r.json())
                    .then(result => {
                        if (result.error) {
                            toast(result.error, 'error');
                            resetBtn();
                            return;
                        }
                        stopPolling();
                        capcodeProgressInterval = setInterval(() => {
                            fetch(`${basePath}api/db/capcodes/import-progress`)
                                .then(r => r.json())
                                .then(p => {
                                    if (btn) btn.textContent = `⏳ ${p.percent}%`;
                                    if (!p.running) {
                                        stopPolling();
                                        if (p.percent >= 100) {
                                            toast(`Capcode import complete: ${(p.imported || 0).toLocaleString()} capcodes`, 'success');
                                        } else {
                                            toast('Capcode import failed: ' + (p.status || 'unknown error'), 'error');
                                        }
                                        resetBtn();
                                        dbLoadStats();
                                        if (dbCurrentTable) dbLoadData();
                                    }
                                })
                                .catch(() => {
                                    stopPolling();
                                    toast('Lost track of capcode import progress', 'error');
                                    resetBtn();
                                });
                        }, 300);
                    })
                    .catch(() => {
                        stopPolling();
                        toast('Capcode import failed', 'error');
                        resetBtn();
                    });
            });
        }

        function dbImportAbbreviations(btn) {
            confirmDialog({
                title: 'Import abbreviations',
                message: 'Import the P2000 abbreviations database?<br><br>This includes 300+ common abbreviations used by Dutch emergency services.',
                confirmLabel: 'Import'
            }).then(ok => {
                if (!ok) return;
                const origLabel = btn ? btn.textContent : '';
                if (btn) { btn.disabled = true; btn.textContent = '⏳ Importing…'; }
                const resetBtn = () => { if (btn) { btn.disabled = false; btn.textContent = origLabel; } };
                fetch(`${basePath}api/db/abbreviations/import`, {method: 'POST'})
                    .then(r => r.json())
                    .then(result => {
                        if (result.error) toast(result.error, 'error');
                        else {
                            toast(`Imported ${result.count} abbreviations`, 'success');
                            dbLoadStats();
                            if (dbCurrentTable) dbLoadData();
                        }
                        resetBtn();
                    })
                    .catch(() => { toast('Import failed', 'error'); resetBtn(); });
            });
        }

        // ----- Destructive actions -----
        function dbClearTable() {
            const table = dbCurrentTable;
            confirmDialog({
                title: `Clear ${dbTitles[table]}`,
                message: `Delete ALL rows from ${dbTitles[table]}? This cannot be undone.`,
                confirmLabel: 'Delete everything',
                danger: true
            }).then(ok => {
                if (!ok) return;
                fetch(`${basePath}api/db/clear/${getApiTable(table)}`, {method: 'DELETE'})
                    .then(r => r.json())
                    .then(result => {
                        if (result.error) toast(result.error, 'error');
                        else toast(`Cleared ${result.deleted} rows`, 'success');
                        dbLoadStats();
                        dbLoadData();
                    })
                    .catch(() => toast('Clear failed', 'error'));
            });
        }

        function dbDeleteAllMessages() {
            confirmDialog({
                title: 'Delete all messages',
                message: 'Delete ALL messages from history? This cannot be undone.',
                confirmLabel: 'Delete everything',
                danger: true
            }).then(ok => {
                if (!ok) return;
                fetch(`${basePath}api/db/messages`, {method: 'DELETE'})
                    .then(r => r.json())
                    .then(result => {
                        toast(`Deleted ${result.deleted} messages`, 'success');
                        dbLoadData();
                    })
                    .catch(() => toast('Delete failed', 'error'));
            });
        }

        function dbDeleteAllTts() {
            confirmDialog({
                title: 'Delete all TTS replacements',
                message: 'Delete ALL TTS replacements? This cannot be undone.',
                confirmLabel: 'Delete everything',
                danger: true
            }).then(ok => {
                if (!ok) return;
                fetch(`${basePath}api/db/tts_replacements`, {method: 'DELETE'})
                    .then(r => r.json())
                    .then(result => {
                        toast(`Deleted ${result.deleted} TTS replacements`, 'success');
                        dbLoadData();
                    })
                    .catch(() => toast('Delete failed', 'error'));
            });
        }

        function dbDeleteAllIgnoreText() {
            confirmDialog({
                title: 'Delete all ignore-text patterns',
                message: 'Delete ALL ignore-text patterns? This cannot be undone.',
                confirmLabel: 'Delete everything',
                danger: true
            }).then(ok => {
                if (!ok) return;
                fetch(`${basePath}api/db/ignore_text`, {method: 'DELETE'})
                    .then(r => r.json())
                    .then(result => {
                        toast(`Deleted ${result.deleted} ignore-text patterns`, 'success');
                        dbLoadData();
                    })
                    .catch(() => toast('Delete failed', 'error'));
            });
        }

        function dbDeleteAllIgnoreCapcodes() {
            confirmDialog({
                title: 'Delete all ignored capcodes',
                message: 'Delete ALL ignored capcodes? This cannot be undone.',
                confirmLabel: 'Delete everything',
                danger: true
            }).then(ok => {
                if (!ok) return;
                fetch(`${basePath}api/db/ignore_capcodes`, {method: 'DELETE'})
                    .then(r => r.json())
                    .then(result => {
                        toast(`Deleted ${result.deleted} ignored capcodes`, 'success');
                        dbLoadData();
                    })
                    .catch(() => toast('Delete failed', 'error'));
            });
        }

        function dbClearGeocodes() {
            confirmDialog({
                title: 'Clear geocode cache',
                message: 'Clear all cached geocodes? This removes all address lookup cache entries.',
                confirmLabel: 'Clear cache',
                danger: true
            }).then(ok => {
                if (!ok) return;
                fetch(`${basePath}api/db/geocodes`, {method: 'DELETE'})
                    .then(r => r.json())
                    .then(result => {
                        if (result.error) toast(result.error, 'error');
                        else {
                            toast(`Cleared ${result.deleted} geocodes from cache`, 'success');
                            dbLoadData();
                        }
                    })
                    .catch(() => toast('Clear failed', 'error'));
            });
        }

        function dbResetDatabase() {
            confirmDialog({
                title: 'Reset database',
                message: 'Delete ALL database data - messages, capcodes, places, streets, geocodes, texts and TTS replacements?<br><br><strong>This cannot be undone.</strong>',
                confirmLabel: 'Reset database',
                danger: true
            }).then(ok => {
                if (!ok) return;
                fetch(`${basePath}api/db/reset`, {method: 'DELETE'})
                    .then(r => r.json())
                    .then(result => {
                        if (result.error) toast(result.error, 'error');
                        else {
                            toast('Database reset complete. Use Import Bommel and Import BAG to restore data.', 'success');
                            dbLoadStats();
                            if (dbCurrentTable) dbLoadData();
                        }
                    })
                    .catch(() => toast('Reset failed', 'error'));
            });
        }

        // Load database node counts on page load
        setTimeout(dbLoadStats, 100);
    </script>
</body>
</html>
"""
