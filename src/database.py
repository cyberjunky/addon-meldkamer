"""Database operations with parameterized queries and CRUD management."""

import contextlib
import csv
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from . import vehicle_types
from .abbreviation_import import DISCIPLINE_ABBREVIATION_OVERRIDES

logger = logging.getLogger(__name__)


class _LockedCursor:
    """Cursor proxy that serializes every call through a shared RLock.

    sqlite3 cursors on a shared connection (check_same_thread=False) corrupt
    each other's result sets when used concurrently from multiple threads,
    so every call goes through the lock.
    """

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock):
        self._cursor = cursor
        self._lock = lock

    def execute(self, *args, **kwargs) -> "_LockedCursor":
        with self._lock:
            self._cursor.execute(*args, **kwargs)
        return self

    def executemany(self, *args, **kwargs) -> "_LockedCursor":
        with self._lock:
            self._cursor.executemany(*args, **kwargs)
        return self

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchall(self) -> list:
        with self._lock:
            return self._cursor.fetchall()

    def close(self) -> None:
        with self._lock:
            self._cursor.close()

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int:
        return self._cursor.lastrowid


class _LockedConnection:
    """Connection proxy that serializes calls through a shared RLock."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _nl_coordinates(latitude, longitude) -> tuple[float | None, float | None]:
    """Coordinates as floats when both are set and inside the Netherlands, else (None, None)."""
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None, None
    if 50.75 <= lat <= 53.55 and 3.36 <= lon <= 7.21:
        return lat, lon
    return None, None


class Database:
    """SQLite database for capcodes, places, streets, and geocode cache."""

    SCHEMA = """
    -- Places/Cities table
    CREATE TABLE IF NOT EXISTS places (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        city TEXT UNIQUE NOT NULL,
        abbreviation TEXT,
        province TEXT,
        latitude REAL,
        longitude REAL
    );

    -- Streets table (linked to cities for better address matching)
    CREATE TABLE IF NOT EXISTS streets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        street TEXT NOT NULL,
        city_id INTEGER,
        postalcode TEXT,
        latitude REAL,
        longitude REAL,
        FOREIGN KEY (city_id) REFERENCES places(id)
    );
    CREATE INDEX IF NOT EXISTS idx_streets_street ON streets(street);
    CREATE INDEX IF NOT EXISTS idx_streets_city ON streets(city_id);
    CREATE INDEX IF NOT EXISTS idx_streets_lookup ON streets(street, city_id);

    -- Capcodes table
    CREATE TABLE IF NOT EXISTS capcodes (
        capcode TEXT PRIMARY KEY,
        discipline TEXT,
        region TEXT,
        location TEXT,
        description TEXT,
        remark TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_capcodes_discipline ON capcodes(discipline);
    CREATE INDEX IF NOT EXISTS idx_capcodes_region ON capcodes(region);

    -- Messages table (persistent message history)
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME NOT NULL,
        body TEXT,
        raw_message TEXT,
        capcodes TEXT,
        group_id TEXT,
        message_type TEXT,
        priority INTEGER,
        discipline TEXT,
        region TEXT,
        city TEXT,
        street TEXT,
        address TEXT,
        latitude REAL,
        longitude REAL,
        receivers TEXT,
        remarks TEXT,
        distance_km REAL,
        incident_id TEXT,
        repeats INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
    CREATE INDEX IF NOT EXISTS idx_messages_city ON messages(city);
    CREATE INDEX IF NOT EXISTS idx_messages_discipline ON messages(discipline);

    -- Geocodes cache table
    CREATE TABLE IF NOT EXISTS geocodes (
        query TEXT PRIMARY KEY,
        datatype TEXT,
        latitude TEXT,
        longitude TEXT,
        postalcode TEXT,
        street TEXT,
        city TEXT,
        address TEXT,
        mapurl TEXT
    );

    -- Abbreviations table (from Bommel)
    CREATE TABLE IF NOT EXISTS abbreviations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        abbreviation TEXT UNIQUE NOT NULL,
        full_text TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_abbreviations_abbrev ON abbreviations(abbreviation);

    -- TTS replacements table (regex patterns for text-to-speech)
    CREATE TABLE IF NOT EXISTS tts_replacements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern TEXT UNIQUE NOT NULL,
        replacement TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1
    );

    -- Global message filters (wildcard text patterns / exact capcode matches)
    CREATE TABLE IF NOT EXISTS ignore_text (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern TEXT UNIQUE NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS ignore_capcodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        capcode TEXT UNIQUE NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    );

    -- Known vehicles: what a vehicle number is. source = 'user' (added or
    -- edited by hand or CSV import), 'capcode' (named in the capcode list)
    -- or 'message' (named with its type in a message, e.g. "TS 07-1782").
    -- Only 'user' rows are kept when the others are refreshed.
    CREATE TABLE IF NOT EXISTS vehicles (
        number TEXT PRIMARY KEY,
        category TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'user'
    );

    -- User-uploaded vehicle photos, keyed by exact vehicle number ('number')
    -- or by broad category ('category', e.g. "ambulance") as a fallback.
    CREATE TABLE IF NOT EXISTS vehicle_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_type TEXT NOT NULL,
        key_value TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        mime_type TEXT NOT NULL,
        image BLOB NOT NULL,
        UNIQUE(key_type, key_value)
    );
    """

    def __init__(self, db_path: str = "/data/meldkamer.sqlite3"):
        self.db_path = db_path
        self._ensure_database()
        self._lock = threading.RLock()
        self._cursors = {}
        self._text_caches = {}
        self._vehicle_cache: dict[str, dict] | None = None
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.conn = _LockedConnection(self._conn, self._lock)
        self._migrate_schema()
        # Capcodes imported before the vehicles table existed
        self.learn_vehicles_from_capcodes()

    @property
    def cursor(self) -> _LockedCursor:
        """Return a locked cursor bound to the calling thread.

        The connection is shared (check_same_thread=False), but each thread
        gets its own cursor so an execute/fetch sequence cannot be handed
        another thread's result set, and every call is serialized through
        the shared RLock (sqlite3 cursors are not concurrency-safe even
        across per-thread cursors on one connection).
        """
        ident = threading.get_ident()
        cur = self._cursors.get(ident)
        if cur is None:
            with self._lock:
                cur = _LockedCursor(self._conn.cursor(), self._lock)
                self._cursors[ident] = cur
        return cur

    def _commit(self) -> None:
        """Commit the shared connection, serialized across threads."""
        with self._lock:
            self.conn.commit()

    @contextlib.contextmanager
    def _transaction(self):
        """Run a block in one explicit transaction, rolled back on any exception.

        Holds the lock for the whole block so no other thread's statements or
        commits can interleave with (or commit) a half-finished bulk change.
        """
        with self._lock:
            if self._conn.in_transaction:
                self._conn.commit()
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # =========================================================================
    # TEXT MATCHING CACHES (places / streets / abbreviations)
    # =========================================================================

    def invalidate_text_caches(self) -> None:
        """Drop cached text-matching data after places/streets/abbreviations change."""
        with self._lock:
            self._text_caches.clear()

    @staticmethod
    def _first_word_key(name: str) -> str | None:
        """Lowercased leading word of a name, used as prefilter index key."""
        match = re.match(r"\w+", name)
        return match.group(0).lower() if match else None

    def _get_text_cache(self, name: str, table: str, builder) -> Any:
        """Return cached matching data for a table, rebuilding when stale.

        The cache is rebuilt when the table row count changes (this also
        catches bulk imports that bypass the CRUD methods) or after
        invalidate_text_caches(). Row counts are re-checked at most once
        every 2 seconds to keep per-message overhead bounded.
        """
        with self._lock:
            now = time.monotonic()
            entry = self._text_caches.get(name)
            if entry is not None and now - entry["checked_at"] < 2.0:
                return entry["data"]
            count = self.cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if entry is not None and entry["count"] == count:
                entry["checked_at"] = now
                return entry["data"]
            data = builder()
            self._text_caches[name] = {"count": count, "checked_at": now, "data": data}
            return data

    def _build_match_index(self, rows, name_of, min_length: int = 1) -> dict[str, Any]:
        """Build a first-word prefilter index over rows for word-boundary matching.

        Returns {"index": {first_word: [item, ...]}, "fallback": [item, ...]}
        where each item keeps the original row order, the source row and the
        escaped name. Patterns are compiled lazily at match time so memory
        stays bounded after large (BAG) imports.
        """
        index = {}
        fallback = []
        order = 0
        for row in rows:
            name = name_of(row)
            if not name or len(name) < min_length:
                continue
            item = {"order": order, "row": row, "escaped": re.escape(name)}
            order += 1
            key = self._first_word_key(name)
            if key:
                index.setdefault(key, []).append(item)
            else:
                fallback.append(item)
        return {"index": index, "fallback": fallback}

    @staticmethod
    def _match_candidates(cache_part: dict[str, Any], text: str) -> list[dict]:
        """Collect candidate entries whose first word appears in the text.

        Candidates keep the original row order so the first pattern hit
        matches the previous per-row scan semantics.
        """
        words = set(re.findall(r"\w+", text.lower()))
        candidates = [item for word in words for item in cache_part["index"].get(word, ())]
        candidates.extend(cache_part["fallback"])
        candidates.sort(key=lambda item: item["order"])
        return candidates

    def _build_places_cache(self) -> dict[str, Any]:
        """Load places once and build first-word prefilter indexes."""
        abbrev_rows = self.cursor.execute("SELECT city, abbreviation FROM places WHERE abbreviation != ''").fetchall()
        city_rows = self.cursor.execute("SELECT city FROM places ORDER BY LENGTH(city) DESC").fetchall()
        by_normalized = {}
        for row in city_rows:
            by_normalized.setdefault(self.normalize_city(row[0]), row[0])
        return {
            "abbrevs": self._build_match_index(abbrev_rows, lambda row: row[1]),
            "cities": self._build_match_index(city_rows, lambda row: row[0]),
            "by_normalized": by_normalized,
        }

    def _build_streets_cache(self) -> dict[str, Any]:
        """Load streets once and build a first-word prefilter index."""
        # Check if places has id column for JOIN
        try:
            self.cursor.execute("PRAGMA table_info(places)")
            has_id = "id" in {row[1] for row in self.cursor.fetchall()}
        except sqlite3.Error:
            has_id = False

        if has_id:
            rows = self.cursor.execute(
                """SELECT s.street, p.city, s.postalcode FROM streets s
                   LEFT JOIN places p ON s.city_id = p.id
                   ORDER BY LENGTH(s.street) DESC"""
            ).fetchall()
        else:
            rows = self.cursor.execute(
                "SELECT street, NULL as city, postalcode FROM streets ORDER BY LENGTH(street) DESC"
            ).fetchall()
        return self._build_match_index(rows, lambda row: row[0], min_length=3)

    def _build_abbreviations_cache(self) -> dict[str, str]:
        """Load all abbreviations once."""
        rows = self.cursor.execute("SELECT abbreviation, full_text FROM abbreviations").fetchall()
        return {row[0]: row[1] for row in rows}

    def _ensure_database(self) -> None:
        """Copy database from app directory if not exists in data."""
        if not os.path.exists(self.db_path):
            source = "/app/data/meldkamer.sqlite3"
            if os.path.exists(source):
                logger.info(f"Installing database to {self.db_path}")
                shutil.copy(source, self.db_path)
            else:
                logger.info(f"Creating new database at {self.db_path}")
                # Will be created when we connect

    def _migrate_schema(self) -> None:
        """Ensure all tables exist with proper schema."""
        try:
            # First, create any missing tables
            self.conn.executescript(self.SCHEMA)
            self._commit()

            # Then, add missing columns to existing tables (for upgrades)
            migrations = [
                # Places table migrations
                ("places", "province", "TEXT"),
                ("places", "latitude", "REAL"),
                ("places", "longitude", "REAL"),
                # Streets table migrations
                ("streets", "postalcode", "TEXT"),
                ("streets", "latitude", "REAL"),
                ("streets", "longitude", "REAL"),
                # Messages table migrations
                ("messages", "distance_km", "REAL"),
                ("messages", "incident_id", "TEXT"),
                ("messages", "repeats", "INTEGER NOT NULL DEFAULT 0"),
            ]

            for table, column, col_type in migrations:
                try:
                    self.cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
                except sqlite3.OperationalError:
                    # Column doesn't exist, add it
                    try:
                        self.cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                        logger.info(f"Added column {column} to {table}")
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Could not add column {column} to {table}: {e}")

            # Indexes on migrated columns (SCHEMA runs before the columns exist on upgrades)
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_incident ON messages(incident_id)")
            self._commit()

            # SQLite cannot ADD COLUMN a PRIMARY KEY - rebuild legacy tables
            # that predate the id column (rowid is kept as id so existing
            # streets.city_id links survive)
            self._rebuild_table_with_id("places", ["city", "abbreviation", "province", "latitude", "longitude"])
            self._rebuild_table_with_id("streets", ["street", "city_id", "postalcode", "latitude", "longitude"])
            self._ensure_unique_streets()
        except sqlite3.Error as e:
            logger.error(f"Schema migration failed: {e}")

    def _table_sql(self, table: str) -> str:
        """Return the CREATE TABLE statement for a table from SCHEMA."""
        match = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n    \);", self.SCHEMA, re.DOTALL)
        return match.group(0)

    def _rebuild_table_with_id(self, table: str, columns: list[str]) -> None:
        """Rebuild a legacy table without an id column (create, copy, drop, rename)."""
        with self._lock:
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not existing or "id" in existing:
                return
            copy_cols = [c for c in columns if c in existing]
            col_list = ", ".join(copy_cols)
            create_sql = self._table_sql(table).replace(f"EXISTS {table} (", f"EXISTS {table}_new (", 1)
            try:
                with self._transaction() as conn:
                    conn.execute(f"DROP TABLE IF EXISTS {table}_new")
                    conn.execute(create_sql)
                    # OR IGNORE: legacy rows may violate the new UNIQUE/NOT NULL constraints
                    conn.execute(
                        f"INSERT OR IGNORE INTO {table}_new (id, {col_list}) SELECT rowid, {col_list} FROM {table}"
                    )
                    conn.execute(f"DROP TABLE {table}")
                    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
                logger.info(f"Rebuilt table {table} with id column")
            except sqlite3.Error as e:
                logger.warning(f"Could not rebuild table {table} with id column: {e}")
                return
        # Recreate the indexes that were dropped together with the old table
        self.conn.executescript(self.SCHEMA)

    def _ensure_unique_streets(self) -> None:
        """Deduplicate streets and add the UNIQUE (street, city_id) index."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_streets_unique'"
            ).fetchone()
            if exists:
                return
            try:
                with self._transaction() as conn:
                    removed = conn.execute(
                        "DELETE FROM streets WHERE id NOT IN (SELECT MIN(id) FROM streets GROUP BY street, city_id)"
                    ).rowcount
                    conn.execute("CREATE UNIQUE INDEX idx_streets_unique ON streets(street, city_id)")
                if removed:
                    logger.info(f"Removed {removed} duplicate streets")
            except sqlite3.Error as e:
                logger.warning(f"Could not add unique street index: {e}")

    def get_stats(self) -> dict:
        """Get database statistics for WebUI."""
        stats = {"places": 0, "capcodes": 0, "geocodes": 0, "streets": 0, "messages": 0, "texts": 0}
        # Map stat keys to actual table names
        table_map = {"texts": "abbreviations"}
        for key in stats:
            table = table_map.get(key, key)
            try:
                count = self.cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                stats[key] = count
            except sqlite3.OperationalError:
                pass
        return stats

    # =========================================================================
    # CAPCODE CRUD OPERATIONS
    # =========================================================================

    def find_capcode(self, capcode: str) -> dict | None:
        """Find capcode information. Returns dict or None."""
        self.cursor.execute(
            "SELECT capcode, discipline, region, location, description, remark FROM capcodes WHERE capcode = ?",
            (capcode,),
        )
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def list_capcodes(self, page: int = 1, per_page: int = 50, search: str = "") -> dict[str, Any]:
        """List capcodes with pagination and search."""
        offset = (page - 1) * per_page

        if search:
            search_term = f"%{search}%"
            self.cursor.execute(
                "SELECT COUNT(*) FROM capcodes WHERE capcode LIKE ? OR discipline LIKE ? OR region LIKE ? OR description LIKE ?",
                (search_term, search_term, search_term, search_term),
            )
            total = self.cursor.fetchone()[0]

            self.cursor.execute(
                "SELECT capcode, discipline, region, location, description, remark FROM capcodes "
                "WHERE capcode LIKE ? OR discipline LIKE ? OR region LIKE ? OR description LIKE ? "
                "ORDER BY capcode LIMIT ? OFFSET ?",
                (search_term, search_term, search_term, search_term, per_page, offset),
            )
        else:
            self.cursor.execute("SELECT COUNT(*) FROM capcodes")
            total = self.cursor.fetchone()[0]

            self.cursor.execute(
                "SELECT capcode, discipline, region, location, description, remark FROM capcodes "
                "ORDER BY capcode LIMIT ? OFFSET ?",
                (per_page, offset),
            )

        rows = [dict(row) for row in self.cursor.fetchall()]
        return {"items": rows, "total": total, "page": page, "per_page": per_page}

    def add_capcode(
        self,
        capcode: str,
        discipline: str = "",
        region: str = "",
        location: str = "",
        description: str = "",
        remark: str = "",
    ) -> bool:
        """Add a new capcode."""
        try:
            self.cursor.execute(
                "INSERT INTO capcodes (capcode, discipline, region, location, description, remark) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (capcode, discipline, region, location, description, remark),
            )
            self._commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_capcode(
        self,
        capcode: str,
        discipline: str = "",
        region: str = "",
        location: str = "",
        description: str = "",
        remark: str = "",
    ) -> bool:
        """Update an existing capcode."""
        try:
            self.cursor.execute(
                "UPDATE capcodes SET discipline=?, region=?, location=?, description=?, remark=? WHERE capcode=?",
                (discipline, region, location, description, remark, capcode),
            )
            self._commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error:
            return False

    def delete_capcode(self, capcode: str) -> bool:
        """Delete a capcode."""
        self.cursor.execute("DELETE FROM capcodes WHERE capcode=?", (capcode,))
        self._commit()
        return self.cursor.rowcount > 0

    # =========================================================================
    # PLACES CRUD OPERATIONS
    # =========================================================================

    @staticmethod
    def normalize_city(city: str) -> str:
        """Normalise a city name for exact matching.

        Drops apostrophes (so 's-Hertogenbosch, s-Hertogenbosch and
        'S-HERTOGENBOSCH compare equal), surrounding punctuation, extra
        whitespace and case.
        """
        city = city.replace("'", "").replace("\u2019", "")
        return " ".join(city.split()).strip(".,;:!?()").casefold()

    def find_city_name(self, city: str) -> str | None:
        """Return the stored place name that exactly matches city (normalised), or None."""
        if not city:
            return None
        cache = self._get_text_cache("places", "places", self._build_places_cache)
        return cache["by_normalized"].get(self.normalize_city(city))

    def get_place_coordinates(self, city: str) -> dict[str, float] | None:
        """Get approximate coordinates for a city from the places table."""
        name = self.find_city_name(city)
        if not name:
            return None
        try:
            self.cursor.execute(
                "SELECT latitude, longitude FROM places "
                "WHERE city = ? AND latitude IS NOT NULL AND longitude IS NOT NULL LIMIT 1",
                (name,),
            )
            row = self.cursor.fetchone()
            if row:
                return {"latitude": float(row[0]), "longitude": float(row[1])}
        except (sqlite3.Error, TypeError, ValueError):
            pass
        return None

    def find_city_by_abbreviation(self, abbrev: str) -> str | None:
        """Find full city name from abbreviation."""
        self.cursor.execute("SELECT city FROM places WHERE abbreviation = ?", (abbrev,))
        row = self.cursor.fetchone()
        return row[0] if row else None

    def find_city_in_text(self, text: str) -> dict[str, str] | None:
        """
        Find a known city name or abbreviation in the text.
        Returns dict with 'city', 'matched_text' (what was found), and 'remaining_text' (text without city).
        """
        cache = self._get_text_cache("places", "places", self._build_places_cache)

        # First try to match city abbreviations (like HELMOND, ROTTDM, etc.)
        # These are uppercase abbreviations
        for item in self._match_candidates(cache["abbrevs"], text):
            city_name, abbrev = item["row"][0], item["row"][1]
            # Use word boundary matching for abbreviations
            pattern = re.compile(r"\b" + item["escaped"] + r"\b", re.IGNORECASE)
            if pattern.search(text):
                remaining = pattern.sub("", text)
                return {"city": city_name, "matched_text": abbrev, "remaining_text": remaining.strip()}

        # Then try to match full city names (loaded longest first)
        for item in self._match_candidates(cache["cities"], text):
            city_name = item["row"][0]
            # Use word boundary matching for city names
            pattern = re.compile(r"\b" + item["escaped"] + r"\b", re.IGNORECASE)
            if pattern.search(text):
                remaining = pattern.sub("", text)
                return {"city": city_name, "matched_text": city_name, "remaining_text": remaining.strip()}

        return None

    def match_street_in_text(self, text: str, excluded_words: list[str] | None = None) -> dict[str, str] | None:
        """
        Try to find a known street name in the text using word boundary matching.
        Returns dict with 'street' and 'city' if found.

        Args:
            text: The text to search in
            excluded_words: Words to exclude from street matching (e.g. already matched city names)
        """
        excluded_lower = [w.lower() for w in (excluded_words or [])]
        cache = self._get_text_cache("streets", "streets", self._build_streets_cache)

        for item in self._match_candidates(cache, text):
            row = item["row"]
            street_name = row[0]

            # Skip if street name is in excluded words (like a city name)
            if street_name.lower() in excluded_lower:
                continue

            # Use word boundary matching instead of substring matching
            pattern = re.compile(r"\b" + item["escaped"] + r"\b", re.IGNORECASE)
            if pattern.search(text):
                return {
                    "street": street_name,
                    "city": row[1] if row[1] else None,
                    "postalcode": row[2] if len(row) > 2 and row[2] else None,
                }

        return None

    def list_places(self, page: int = 1, per_page: int = 50, search: str = "") -> dict[str, Any]:
        """List places with pagination and search."""
        offset = (page - 1) * per_page

        # Get columns that exist in the table
        try:
            self.cursor.execute("PRAGMA table_info(places)")
            existing_cols = {row[1] for row in self.cursor.fetchall()}
        except sqlite3.Error:
            existing_cols = {"city", "abbreviation"}

        # Build SELECT based on available columns
        base_cols = ["city", "abbreviation"]
        optional_cols = ["id", "province", "latitude", "longitude"]
        select_cols = base_cols + [c for c in optional_cols if c in existing_cols]

        if search:
            search_term = f"%{search}%"
            where_clause = "WHERE city LIKE ? OR abbreviation LIKE ?"
            params = [search_term, search_term]
            if "province" in existing_cols:
                where_clause += " OR province LIKE ?"
                params.append(search_term)

            self.cursor.execute(f"SELECT COUNT(*) FROM places {where_clause}", params)
            total = self.cursor.fetchone()[0]

            self.cursor.execute(
                f"SELECT {', '.join(select_cols)} FROM places {where_clause} ORDER BY city LIMIT ? OFFSET ?",
                [*params, per_page, offset],
            )
        else:
            self.cursor.execute("SELECT COUNT(*) FROM places")
            total = self.cursor.fetchone()[0]

            self.cursor.execute(
                f"SELECT {', '.join(select_cols)} FROM places ORDER BY city LIMIT ? OFFSET ?", (per_page, offset)
            )

        rows = [dict(row) for row in self.cursor.fetchall()]
        return {"items": rows, "total": total, "page": page, "per_page": per_page}

    def add_place(
        self,
        city: str,
        abbreviation: str = "",
        province: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> bool:
        """Add a new place."""
        try:
            self.cursor.execute(
                "INSERT INTO places (city, abbreviation, province, latitude, longitude) VALUES (?, ?, ?, ?, ?)",
                (city, abbreviation, province, latitude, longitude),
            )
            self._commit()
            self.invalidate_text_caches()
            return True
        except sqlite3.IntegrityError:
            return False

    # Upsert that keeps the row id (streets.city_id points at it) and only
    # overwrites province/abbreviation/coordinates with non-empty values
    _UPSERT_PLACE_SQL = (
        "INSERT INTO places (city, abbreviation, province, latitude, longitude) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(city) DO UPDATE SET "
        "abbreviation = COALESCE(NULLIF(excluded.abbreviation, ''), places.abbreviation), "
        "province = COALESCE(NULLIF(excluded.province, ''), places.province), "
        "latitude = COALESCE(excluded.latitude, places.latitude), "
        "longitude = COALESCE(excluded.longitude, places.longitude)"
    )

    def upsert_place(
        self,
        city: str,
        abbreviation: str = "",
        province: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> tuple[int, bool]:
        """Insert a place, or fill in empty fields of an existing one.

        Returns (place_id, created).
        """
        with self._transaction() as conn:
            created = conn.execute("SELECT 1 FROM places WHERE city = ?", (city,)).fetchone() is None
            conn.execute(self._UPSERT_PLACE_SQL, (city, abbreviation, province, latitude, longitude))
            place_id = conn.execute("SELECT id FROM places WHERE city = ?", (city,)).fetchone()[0]
        self.invalidate_text_caches()
        return place_id, created

    def upsert_places(self, places: list[dict[str, Any]]) -> int:
        """Bulk upsert places (dicts with city/abbreviation/province/latitude/longitude) in one transaction.

        Returns the number of rows processed.
        """
        params = [
            (
                p["city"],
                p.get("abbreviation", ""),
                p.get("province", ""),
                p.get("latitude"),
                p.get("longitude"),
            )
            for p in places
            if p.get("city")
        ]
        with self._transaction() as conn:
            conn.executemany(self._UPSERT_PLACE_SQL, params)
        self.invalidate_text_caches()
        return len(params)

    def update_place(
        self,
        place_id: int,
        city: str,
        abbreviation: str = "",
        province: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> bool:
        """Update an existing place."""
        try:
            self.cursor.execute(
                "UPDATE places SET city=?, abbreviation=?, province=?, latitude=?, longitude=? WHERE id=?",
                (city, abbreviation, province, latitude, longitude, place_id),
            )
            self._commit()
            self.invalidate_text_caches()
            return self.cursor.rowcount > 0
        except sqlite3.Error:
            return False

    def delete_place(self, place_id: int) -> bool:
        """Delete a place and its associated streets."""
        self.cursor.execute("DELETE FROM streets WHERE city_id=?", (place_id,))
        self.cursor.execute("DELETE FROM places WHERE id=?", (place_id,))
        self._commit()
        self.invalidate_text_caches()
        return self.cursor.rowcount > 0

    # =========================================================================
    # STREETS CRUD OPERATIONS
    # =========================================================================

    def list_streets(
        self, page: int = 1, per_page: int = 50, search: str = "", city_id: int | None = None
    ) -> dict[str, Any]:
        """List streets with pagination and search."""
        offset = (page - 1) * per_page
        params = []
        where_clauses = []

        if search:
            where_clauses.append("(s.street LIKE ? OR s.postalcode LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])

        if city_id:
            where_clauses.append("s.city_id = ?")
            params.append(city_id)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # Check if places table has id column (for JOIN)
        try:
            self.cursor.execute("PRAGMA table_info(places)")
            place_cols = {row[1] for row in self.cursor.fetchall()}
            has_id = "id" in place_cols
        except sqlite3.Error:
            has_id = False

        if has_id:
            # Use JOIN if places has id
            self.cursor.execute(
                f"SELECT COUNT(*) FROM streets s LEFT JOIN places p ON s.city_id = p.id {where_sql}", params
            )
            total = self.cursor.fetchone()[0]

            self.cursor.execute(
                f"SELECT s.id, s.street, s.postalcode, s.city_id, p.city as city_name, s.latitude, s.longitude "
                f"FROM streets s "
                f"LEFT JOIN places p ON s.city_id = p.id {where_sql} "
                f"ORDER BY s.street LIMIT ? OFFSET ?",
                [*params, per_page, offset],
            )
        else:
            # Simple query without JOIN
            self.cursor.execute(f"SELECT COUNT(*) FROM streets s {where_sql}", params)
            total = self.cursor.fetchone()[0]

            self.cursor.execute(
                f"SELECT id, street, postalcode, city_id, '' as city_name, latitude, longitude FROM streets s "
                f"{where_sql} ORDER BY street LIMIT ? OFFSET ?",
                [*params, per_page, offset],
            )

        rows = [dict(row) for row in self.cursor.fetchall()]
        return {"items": rows, "total": total, "page": page, "per_page": per_page}

    def add_street(
        self,
        street: str,
        city_id: int | None = None,
        postalcode: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> bool:
        """Add a new street. Returns False if it already exists for this city or has invalid coordinates."""
        given = latitude is not None or longitude is not None
        latitude, longitude = _nl_coordinates(latitude, longitude)
        if given and latitude is None:
            return False
        try:
            self.cursor.execute(
                "INSERT OR IGNORE INTO streets (street, city_id, postalcode, latitude, longitude) VALUES (?, ?, ?, ?, ?)",
                (street, city_id, postalcode, latitude, longitude),
            )
            added = self.cursor.rowcount > 0
            self._commit()
            self.invalidate_text_caches()
            return added
        except sqlite3.Error:
            return False

    def add_streets(self, city_id: int | None, streets: list[dict[str, str]]) -> int:
        """Bulk add streets (dicts with street/postcode/latitude/longitude) for one city in one transaction.

        Existing streets are kept; an empty postal code or missing coordinates
        are filled in when the new data has them. Returns the number of newly
        added streets.
        """
        rows = [
            (s["street"], city_id, s.get("postcode", ""), *_nl_coordinates(s.get("latitude"), s.get("longitude")))
            for s in streets
            if s.get("street")
        ]
        with self._transaction() as conn:
            added = conn.executemany(
                "INSERT OR IGNORE INTO streets (street, city_id, postalcode, latitude, longitude) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            ).rowcount
            conn.executemany(
                "UPDATE streets SET postalcode = ? WHERE street = ? AND city_id IS ? "
                "AND (postalcode IS NULL OR postalcode = '')",
                [(pc, street, cid) for street, cid, pc, _, _ in rows if pc],
            )
            conn.executemany(
                "UPDATE streets SET latitude = ?, longitude = ? WHERE street = ? AND city_id IS ? AND latitude IS NULL",
                [(lat, lon, street, cid) for street, cid, _, lat, lon in rows if lat is not None],
            )
        self.invalidate_text_caches()
        return added

    def update_street(
        self,
        street_id: int,
        street: str,
        city_id: int | None = None,
        postalcode: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> bool:
        """Update an existing street. Returns False for coordinates that are incomplete or outside the Netherlands."""
        given = latitude is not None or longitude is not None
        latitude, longitude = _nl_coordinates(latitude, longitude)
        if given and latitude is None:
            return False
        try:
            self.cursor.execute(
                "UPDATE streets SET street=?, city_id=?, postalcode=?, latitude=?, longitude=? WHERE id=?",
                (street, city_id, postalcode, latitude, longitude, street_id),
            )
            self._commit()
            self.invalidate_text_caches()
            return self.cursor.rowcount > 0
        except sqlite3.Error:
            return False

    def get_street_coordinates(self, street: str, city: str) -> dict[str, float] | None:
        """Coordinates of a street in a city from the streets table (BAG centroid or your own), or None."""
        name = self.find_city_name(city)
        if not name:
            return None
        try:
            self.cursor.execute(
                "SELECT s.latitude, s.longitude FROM streets s JOIN places p ON s.city_id = p.id "
                "WHERE p.city = ? AND s.street = ? COLLATE NOCASE "
                "AND s.latitude IS NOT NULL AND s.longitude IS NOT NULL LIMIT 1",
                (name, street),
            )
            row = self.cursor.fetchone()
            if row:
                return {"latitude": float(row[0]), "longitude": float(row[1])}
        except (sqlite3.Error, TypeError, ValueError):
            pass
        return None

    def delete_street(self, street_id: int) -> bool:
        """Delete a street."""
        self.cursor.execute("DELETE FROM streets WHERE id=?", (street_id,))
        self._commit()
        self.invalidate_text_caches()
        return self.cursor.rowcount > 0

    # =========================================================================
    # MESSAGE HISTORY OPERATIONS
    # =========================================================================

    def get_history_by_address(self, address: str, limit: int = 20) -> list[dict]:
        """Get message history for a specific address."""
        self.cursor.execute(
            """SELECT * FROM messages WHERE address LIKE ? ORDER BY timestamp DESC LIMIT ?""", (f"%{address}%", limit)
        )
        rows = self.cursor.fetchall()
        return [self._message_row_to_dict(row) for row in rows]

    def _message_row_to_dict(self, row) -> dict:
        """Convert message row to dict."""
        d = dict(row)
        # Convert capcodes back to list (stored as JSON; legacy rows may
        # hold a plain comma-separated string)
        raw = d.get("capcodes")
        if raw:
            try:
                parsed = json.loads(raw)
                d["capcodes"] = parsed if isinstance(parsed, list) else [str(parsed)]
            except (json.JSONDecodeError, TypeError):
                d["capcodes"] = [c.strip() for c in raw.split(",") if c.strip()]
        else:
            d["capcodes"] = []
        # Rows stored before distances were limited to messages with a street
        # hold the distance to the city centre
        if not d.get("street"):
            d["distance_km"] = None
        # Not stored: recognised from the text, so older rows get them too
        if "body" in d:
            d.update(self.resolve_vehicle(d["body"] or "", d.get("discipline") or ""))
        return d

    def list_messages(
        self, page: int = 1, per_page: int = 50, search: str = "", discipline: str = "", city: str = ""
    ) -> dict:
        """List messages with pagination and search/filter."""
        offset = (page - 1) * per_page

        # Build WHERE clause
        conditions = []
        params = []

        if search:
            conditions.append("(body LIKE ? OR address LIKE ? OR street LIKE ? OR city LIKE ?)")
            search_pattern = f"%{search}%"
            params.extend([search_pattern] * 4)

        if discipline:
            conditions.append("discipline = ?")
            params.append(discipline)

        if city:
            conditions.append("city LIKE ?")
            params.append(f"%{city}%")

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        # Get total count
        count_query = f"SELECT COUNT(*) FROM messages {where_clause}"
        self.cursor.execute(count_query, params)
        total = self.cursor.fetchone()[0]

        # Get paginated results
        query = f"""
            SELECT id, timestamp, body, capcodes, discipline, region, city, street,
                   address, latitude, longitude, message_type, priority
            FROM messages
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        self.cursor.execute(query, [*params, per_page, offset])
        rows = self.cursor.fetchall()

        return {
            "items": [self._message_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total > 0 else 1,
        }

    def delete_message(self, message_id: int) -> bool:
        """Delete a message by ID."""
        self.cursor.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        self._commit()
        return self.cursor.rowcount > 0

    def delete_all_messages(self) -> int:
        """Delete all messages. Returns count deleted."""
        self.cursor.execute("SELECT COUNT(*) FROM messages")
        count = self.cursor.fetchone()[0]
        self.cursor.execute("DELETE FROM messages")
        self._commit()
        return count

    # =========================================================================
    # GEOCODE OPERATIONS (existing)
    # =========================================================================

    @staticmethod
    def _geocode_key(address: str) -> str:
        """Cache key for a geocode query - must be identical for lookup and store."""
        return " ".join(address.split())

    def find_geocode(self, address: str) -> dict | None:
        """Find cached geocode for address."""
        self.cursor.execute(
            "SELECT latitude, longitude, address, mapurl FROM geocodes WHERE query = ?", (self._geocode_key(address),)
        )
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def store_geocode(
        self,
        query: str,
        datatype: str,
        latitude: float,
        longitude: float,
        postalcode: str,
        street: str,
        city: str,
        address: str,
        mapurl: str,
    ) -> None:
        """Store geocode result in cache."""
        try:
            self.cursor.execute(
                "INSERT OR REPLACE INTO geocodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._geocode_key(query),
                    datatype,
                    str(latitude),
                    str(longitude),
                    postalcode,
                    street,
                    city,
                    address,
                    mapurl,
                ),
            )
            self._commit()
        except sqlite3.Error as e:
            logger.error(f"Failed to store geocode: {e}")

    def list_geocodes(self, page: int = 1, per_page: int = 50, search: str = "") -> dict[str, Any]:
        """List cached geocodes with pagination and search."""
        offset = (page - 1) * per_page
        try:
            if search:
                search_term = f"%{search}%"
                self.cursor.execute(
                    """SELECT COUNT(*) FROM geocodes
                       WHERE query LIKE ? OR city LIKE ? OR street LIKE ? OR address LIKE ?""",
                    (search_term, search_term, search_term, search_term),
                )
                total = self.cursor.fetchone()[0]
                self.cursor.execute(
                    """SELECT query, datatype, latitude, longitude, postalcode, street, city, address, mapurl
                       FROM geocodes
                       WHERE query LIKE ? OR city LIKE ? OR street LIKE ? OR address LIKE ?
                       ORDER BY city, street
                       LIMIT ? OFFSET ?""",
                    (search_term, search_term, search_term, search_term, per_page, offset),
                )
            else:
                self.cursor.execute("SELECT COUNT(*) FROM geocodes")
                total = self.cursor.fetchone()[0]
                self.cursor.execute(
                    """SELECT query, datatype, latitude, longitude, postalcode, street, city, address, mapurl
                       FROM geocodes ORDER BY city, street LIMIT ? OFFSET ?""",
                    (per_page, offset),
                )

            rows = self.cursor.fetchall()
            items = [dict(row) for row in rows]
            return {
                "items": items,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": (total + per_page - 1) // per_page,
            }
        except sqlite3.Error as e:
            logger.error(f"Failed to list geocodes: {e}")
            return {"items": [], "total": 0, "page": 1, "per_page": per_page, "pages": 0}

    def delete_geocode(self, query: str) -> bool:
        """Delete a cached geocode by query."""
        try:
            self.cursor.execute("DELETE FROM geocodes WHERE query = ?", (query,))
            self._commit()
            return self.cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Failed to delete geocode: {e}")
            return False

    def delete_all_geocodes(self) -> int:
        """Delete all cached geocodes. Returns count deleted."""
        try:
            self.cursor.execute("SELECT COUNT(*) FROM geocodes")
            count = self.cursor.fetchone()[0]
            self.cursor.execute("DELETE FROM geocodes")
            self._commit()
            return count
        except sqlite3.Error as e:
            logger.error(f"Failed to delete geocodes: {e}")
            return 0

    # =========================================================================
    # IMPORT/EXPORT OPERATIONS
    # =========================================================================

    def export_capcodes_csv(self) -> str:
        """Export all capcodes to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["capcode", "discipline", "region", "location", "description", "remark"])

        self.cursor.execute(
            "SELECT capcode, discipline, region, location, description, remark FROM capcodes ORDER BY capcode"
        )
        for row in self.cursor.fetchall():
            writer.writerow(list(row))

        return output.getvalue()

    def export_places_csv(self) -> str:
        """Export all places to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["city", "abbreviation", "province", "latitude", "longitude"])

        # Get available columns
        try:
            self.cursor.execute("PRAGMA table_info(places)")
            cols = {row[1] for row in self.cursor.fetchall()}
            select_parts = ["city", "abbreviation"]
            if "province" in cols:
                select_parts.append("province")
            else:
                select_parts.append("'' as province")
            if "latitude" in cols:
                select_parts.append("latitude")
            else:
                select_parts.append("NULL as latitude")
            if "longitude" in cols:
                select_parts.append("longitude")
            else:
                select_parts.append("NULL as longitude")

            self.cursor.execute(f"SELECT {', '.join(select_parts)} FROM places ORDER BY city")
            for row in self.cursor.fetchall():
                writer.writerow(list(row))
        except Exception as e:
            logger.error(f"Export places failed: {e}")

        return output.getvalue()

    def export_streets_csv(self) -> str:
        """Export all streets to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["street", "city", "postalcode", "latitude", "longitude"])

        # Check if places has id for JOIN
        try:
            self.cursor.execute("PRAGMA table_info(places)")
            has_id = "id" in {row[1] for row in self.cursor.fetchall()}

            if has_id:
                self.cursor.execute(
                    "SELECT s.street, p.city, s.postalcode, s.latitude, s.longitude FROM streets s "
                    "LEFT JOIN places p ON s.city_id = p.id ORDER BY s.street"
                )
            else:
                self.cursor.execute("SELECT street, '', postalcode, latitude, longitude FROM streets ORDER BY street")

            for row in self.cursor.fetchall():
                writer.writerow(list(row))
        except Exception as e:
            logger.error(f"Export streets failed: {e}")

        return output.getvalue()

    def export_messages_csv(self) -> str:
        """Export all messages to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "timestamp",
                "body",
                "capcodes",
                "discipline",
                "region",
                "city",
                "street",
                "address",
                "priority",
                "message_type",
            ]
        )

        try:
            self.cursor.execute(
                "SELECT timestamp, body, capcodes, discipline, region, city, street, "
                "address, priority, message_type FROM messages ORDER BY timestamp DESC"
            )
            for row in self.cursor.fetchall():
                writer.writerow(list(row))
        except Exception as e:
            logger.error(f"Export messages failed: {e}")

        return output.getvalue()

    def export_abbreviations_csv(self) -> str:
        """Export all abbreviations to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["abbreviation", "full_text"])

        try:
            self.cursor.execute("SELECT abbreviation, full_text FROM abbreviations ORDER BY abbreviation")
            for row in self.cursor.fetchall():
                writer.writerow(list(row))
        except Exception as e:
            logger.error(f"Export abbreviations failed: {e}")

        return output.getvalue()

    def export_tts_csv(self) -> str:
        """Export all TTS replacements to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["pattern", "replacement", "enabled"])

        try:
            self.cursor.execute("SELECT pattern, replacement, enabled FROM tts_replacements ORDER BY id")
            for row in self.cursor.fetchall():
                writer.writerow(list(row))
        except Exception as e:
            logger.error(f"Export TTS replacements failed: {e}")

        return output.getvalue()

    @staticmethod
    def _read_csv(csv_content: str, required: tuple[str, ...]) -> list[dict[str, str]]:
        """Parse and validate CSV content before anything touches the database.

        Accepts comma, semicolon or tab delimiters (picked by which one yields
        the required header columns). Header names are matched
        case-insensitively. Raises ValueError for unusable input.
        """
        if not isinstance(csv_content, str):
            raise ValueError("CSV content must be a string")
        text = csv_content.lstrip("\ufeff")
        if not text.strip():
            raise ValueError("CSV is empty")

        header_line = text.splitlines()[0]
        for delimiter in (",", ";", "\t"):
            header = [h.strip().lower() for h in next(csv.reader([header_line], delimiter=delimiter))]
            if all(col in header for col in required):
                break
        else:
            raise ValueError(f"CSV header must contain column(s): {', '.join(required)} (got: {header_line[:100]!r})")

        reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
        try:
            rows = list(reader)
        except csv.Error as e:
            raise ValueError(f"Invalid CSV near line {reader.line_num}: {e}") from e
        return [dict(zip(header, (v.strip() for v in row), strict=False)) for row in rows[1:] if any(row)]

    def import_capcodes_csv(self, csv_content: str, replace: bool = False) -> dict[str, int]:
        """Import capcodes from CSV. Returns counts of imported/skipped.

        Raises ValueError for invalid CSV; the table is left untouched then.
        """
        rows = self._read_csv(csv_content, ("capcode",))
        imported = 0
        skipped = 0

        with self._transaction() as conn:
            if replace:
                conn.execute("DELETE FROM capcodes")
            for row in rows:
                if not row.get("capcode"):
                    skipped += 1
                    continue
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO capcodes (capcode, discipline, region, location, description, remark) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row["capcode"],
                            row.get("discipline", ""),
                            row.get("region", ""),
                            row.get("location", ""),
                            row.get("description", ""),
                            row.get("remark", ""),
                        ),
                    )
                    imported += 1
                except sqlite3.Error:
                    skipped += 1

        self.invalidate_text_caches()
        return {"imported": imported, "skipped": skipped}

    def import_places_csv(self, csv_content: str, replace: bool = False) -> dict[str, int]:
        """Import places from CSV. Returns counts of imported/skipped.

        Existing places are updated in place so their id (and the streets
        linked to it) is kept; empty CSV values never wipe stored data.
        With replace=True, places missing from the CSV are removed together
        with their streets. Raises ValueError for invalid CSV; nothing is
        changed then.
        """
        rows = self._read_csv(csv_content, ("city",))
        imported = 0
        skipped = 0
        cities = set()

        with self._transaction() as conn:
            for row in rows:
                try:
                    lat = float(row["latitude"]) if row.get("latitude") else None
                    lon = float(row["longitude"]) if row.get("longitude") else None
                except ValueError:
                    skipped += 1
                    continue
                if not row.get("city"):
                    skipped += 1
                    continue
                try:
                    conn.execute(
                        self._UPSERT_PLACE_SQL,
                        (row["city"], row.get("abbreviation", ""), row.get("province", ""), lat, lon),
                    )
                    cities.add(row["city"])
                    imported += 1
                except sqlite3.Error:
                    skipped += 1

            if replace:
                stale = [(r[0],) for r in conn.execute("SELECT id, city FROM places").fetchall() if r[1] not in cities]
                conn.executemany("DELETE FROM streets WHERE city_id = ?", stale)
                conn.executemany("DELETE FROM places WHERE id = ?", stale)

        self.invalidate_text_caches()
        return {"imported": imported, "skipped": skipped}

    def import_streets_csv(self, csv_content: str, replace: bool = False) -> dict[str, int]:
        """Import streets from CSV. Returns counts of imported/skipped.

        Streets already present for the same city are skipped, unless the CSV
        has coordinates for them (latitude/longitude columns are optional):
        those are updated. Coordinates outside the Netherlands are ignored.
        Raises ValueError for invalid CSV; the table is left untouched then.
        """
        rows = self._read_csv(csv_content, ("street",))
        imported = 0
        skipped = 0

        with self._transaction() as conn:
            if replace:
                conn.execute("DELETE FROM streets")
            for row in rows:
                if not row.get("street"):
                    skipped += 1
                    continue
                lat, lon = _nl_coordinates(row.get("latitude"), row.get("longitude"))
                try:
                    # Look up city_id
                    city_id = None
                    if row.get("city"):
                        city_row = conn.execute("SELECT id FROM places WHERE city = ?", (row["city"],)).fetchone()
                        city_id = city_row[0] if city_row else None

                    cur = conn.execute(
                        "INSERT OR IGNORE INTO streets (street, city_id, postalcode, latitude, longitude) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (row["street"], city_id, row.get("postalcode", ""), lat, lon),
                    )
                    if cur.rowcount == 0 and lat is not None:
                        cur = conn.execute(
                            "UPDATE streets SET latitude = ?, longitude = ? WHERE street = ? AND city_id IS ?",
                            (lat, lon, row["street"], city_id),
                        )
                    if cur.rowcount > 0:
                        imported += 1
                    else:
                        skipped += 1
                except sqlite3.Error:
                    skipped += 1

        self.invalidate_text_caches()
        return {"imported": imported, "skipped": skipped}

    # =====================
    # Message History
    # =====================

    def save_message(self, msg) -> int | None:
        """Save a P2000Message to the database. Returns the new row id, None on failure."""
        try:
            self.cursor.execute(
                """
                INSERT INTO messages (
                    timestamp, body, raw_message, capcodes, group_id, message_type,
                    priority, discipline, region, city, street, address,
                    latitude, longitude, receivers, remarks, distance_km, incident_id, repeats
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    # Stored as ISO string; passing datetime objects straight to
                    # sqlite3 is deprecated on Python 3.12+
                    msg.timestamp.isoformat(),
                    msg.body,
                    msg.raw_message,
                    json.dumps(msg.capcodes),
                    msg.group_id,
                    msg.message_type,
                    msg.priority,
                    msg.discipline,
                    msg.region,
                    msg.city,
                    msg.street,
                    msg.address,
                    msg.latitude,
                    msg.longitude,
                    msg.receivers,
                    msg.remarks,
                    msg.distance_km,
                    msg.incident_id or None,
                    msg.repeats,
                ),
            )
            self._commit()
            return self.cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to save message: {e}")
            return None

    def set_message_repeats(self, message_id: int, repeats: int) -> None:
        """Store how often a message was resent."""
        self.cursor.execute("UPDATE messages SET repeats = ? WHERE id = ?", (repeats, message_id))
        self._commit()

    def get_messages_since(self, seconds: int) -> list[dict[str, Any]]:
        """Messages received in the last `seconds`, oldest first."""
        since = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
        self.cursor.execute(
            "SELECT * FROM messages WHERE julianday(timestamp) >= julianday(?) ORDER BY julianday(timestamp)",
            (since,),
        )
        return [self._message_row_to_dict(row) for row in self.cursor.fetchall()]

    def get_incident_messages(self, incident_id: str) -> list[dict[str, Any]]:
        """All messages of one incident, oldest first."""
        self.cursor.execute(
            "SELECT * FROM messages WHERE incident_id = ? ORDER BY julianday(timestamp)", (incident_id,)
        )
        return [self._message_row_to_dict(row) for row in self.cursor.fetchall()]

    def get_map_points(self, hours: int, limit: int = 20000) -> list[dict[str, Any]]:
        """Located messages of the last `hours`, oldest first, for the map history layer."""
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        self.cursor.execute(
            """
            SELECT CAST(strftime('%s', timestamp) AS INTEGER) AS time, latitude, longitude, priority
            FROM messages
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND julianday(timestamp) >= julianday(?)
            ORDER BY julianday(timestamp) DESC
            LIMIT ?
            """,
            (since, limit),
        )
        return [dict(row) for row in reversed(self.cursor.fetchall())]

    def get_recent_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent messages from the database (same shape as _message_row_to_dict)."""
        try:
            self.cursor.execute(
                """
                SELECT * FROM messages
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (limit,),
            )
            return [self._message_row_to_dict(row) for row in self.cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get messages: {e}")
            return []

    def get_message_count(self) -> int:
        """Get total number of stored messages."""
        try:
            self.cursor.execute("SELECT COUNT(*) FROM messages")
            return self.cursor.fetchone()[0]
        except sqlite3.Error:
            return 0

    # =====================
    # Abbreviations
    # =====================

    def find_abbreviations_in_text(self, text: str, discipline: str = "") -> list[dict[str, str]]:
        """Find all abbreviations that appear in the text.

        `discipline` (e.g. "Ambulance", or a combined "Ambulance, Brandweer")
        is used to override codes that mean different things per discipline
        - see DISCIPLINE_ABBREVIATION_OVERRIDES.
        """
        found = []
        words = text.upper().split()
        discipline_lower = (discipline or "").lower()

        def _resolve_text(code: str, default_text: str) -> str:
            if not discipline_lower:
                return default_text
            for (override_code, disc_key), override_text in DISCIPLINE_ABBREVIATION_OVERRIDES.items():
                if override_code == code and disc_key in discipline_lower:
                    return override_text
            return default_text

        try:
            # Get all abbreviations (cached, rebuilt when the table changes)
            all_abbrevs = self._get_text_cache("abbreviations", "abbreviations", self._build_abbreviations_cache)

            for word in words:
                # Clean word of punctuation
                clean = word.strip(".,;:!?()[]{}")
                if clean in all_abbrevs:
                    found.append({"abbreviation": clean, "full_text": _resolve_text(clean, all_abbrevs[clean])})
                elif "-" in clean:
                    # Codes like BDH-03 / BON-01: prefix = meldkamer
                    # gespreksgroep, number = incidentkanaal
                    prefix, _, channel = clean.partition("-")
                    if prefix in all_abbrevs and channel.isdigit():
                        base_text = _resolve_text(prefix, all_abbrevs[prefix])
                        found.append({"abbreviation": clean, "full_text": f"{base_text}, kanaal {channel}"})
                else:
                    # Codes like DP4: letters + digits, no dash
                    m = re.match(r"^([A-Z]+)(\d+)$", clean)
                    if m and m.group(1) in all_abbrevs:
                        base_text = _resolve_text(m.group(1), all_abbrevs[m.group(1)])
                        found.append({"abbreviation": clean, "full_text": f"{base_text} {m.group(2)}"})
        except sqlite3.Error as e:
            logger.error(f"Error finding abbreviations: {e}")
        return found

    def list_abbreviations(self, page: int = 1, per_page: int = 50, search: str = "") -> dict[str, Any]:
        """List abbreviations with pagination and search."""
        offset = (page - 1) * per_page
        try:
            if search:
                search_term = f"%{search}%"
                self.cursor.execute(
                    """SELECT COUNT(*) FROM abbreviations
                       WHERE abbreviation LIKE ? OR full_text LIKE ?""",
                    (search_term, search_term),
                )
                total = self.cursor.fetchone()[0]
                self.cursor.execute(
                    """SELECT id, abbreviation, full_text FROM abbreviations
                       WHERE abbreviation LIKE ? OR full_text LIKE ?
                       ORDER BY abbreviation LIMIT ? OFFSET ?""",
                    (search_term, search_term, per_page, offset),
                )
            else:
                self.cursor.execute("SELECT COUNT(*) FROM abbreviations")
                total = self.cursor.fetchone()[0]
                self.cursor.execute(
                    """SELECT id, abbreviation, full_text FROM abbreviations
                       ORDER BY abbreviation LIMIT ? OFFSET ?""",
                    (per_page, offset),
                )

            items = [{"id": row[0], "abbreviation": row[1], "full_text": row[2]} for row in self.cursor.fetchall()]
            return {"items": items, "total": total, "page": page}
        except Exception as e:
            logger.error(f"Failed to list abbreviations: {e}")
            return {"items": [], "total": 0, "page": page}

    def add_abbreviation(self, abbrev: str, full_text: str) -> bool:
        """Add a new abbreviation."""
        try:
            self.cursor.execute(
                "INSERT OR REPLACE INTO abbreviations (abbreviation, full_text) VALUES (?, ?)",
                (abbrev.upper(), full_text),
            )
            self._commit()
            self.invalidate_text_caches()
            return True
        except Exception as e:
            logger.error(f"Failed to add abbreviation: {e}")
            return False

    def delete_abbreviation(self, abbrev_id: int) -> bool:
        """Delete an abbreviation by id."""
        try:
            self.cursor.execute("DELETE FROM abbreviations WHERE id = ?", (abbrev_id,))
            self._commit()
            self.invalidate_text_caches()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete abbreviation: {e}")
            return False

    def import_abbreviations(self, abbrevs: list[dict[str, str]], replace: bool = True) -> int:
        """Bulk import abbreviations. Returns count imported.

        `replace=True` (the manual "Import Texts" button) overwrites existing
        rows by abbreviation code - lets a user deliberately refresh from the
        seed data. `replace=False` (startup auto-seed) only adds codes that
        aren't already present, so it never reverts a user's own edits.
        """
        count = 0
        sql = (
            "INSERT OR REPLACE INTO abbreviations (abbreviation, full_text) VALUES (?, ?)"
            if replace
            else "INSERT OR IGNORE INTO abbreviations (abbreviation, full_text) VALUES (?, ?)"
        )
        try:
            with self._transaction() as conn:
                for item in abbrevs:
                    abbrev = item.get("abbreviation", "").upper()
                    full_text = item.get("full_text", "")
                    if abbrev and full_text:
                        cur = conn.execute(sql, (abbrev, full_text))
                        count += cur.rowcount if not replace else 1
            self.invalidate_text_caches()
        except Exception as e:
            count = 0
            logger.error(f"Failed to import abbreviations: {e}")
        return count

    # =========================================================================
    # TTS REPLACEMENTS
    # =========================================================================

    def list_tts_replacements(self, page: int = 1, per_page: int = 50, search: str = "") -> dict:
        """List TTS replacements with pagination and search."""
        try:
            where = ""
            params = []
            if search:
                where = "WHERE pattern LIKE ? OR replacement LIKE ?"
                params = [f"%{search}%", f"%{search}%"]

            self.cursor.execute(f"SELECT COUNT(*) FROM tts_replacements {where}", params)
            total = self.cursor.fetchone()[0]

            offset = (page - 1) * per_page
            self.cursor.execute(
                f"SELECT * FROM tts_replacements {where} ORDER BY id LIMIT ? OFFSET ?", [*params, per_page, offset]
            )
            items = [dict(row) for row in self.cursor.fetchall()]
            return {"items": items, "total": total, "page": page}
        except Exception as e:
            logger.error(f"Failed to list TTS replacements: {e}")
            return {"items": [], "total": 0, "page": page}

    def add_tts_replacement(self, pattern: str, replacement: str = "", enabled: bool = True) -> bool:
        """Add or update a TTS replacement."""
        try:
            self.cursor.execute(
                "INSERT OR REPLACE INTO tts_replacements (pattern, replacement, enabled) VALUES (?, ?, ?)",
                (pattern, replacement, 1 if enabled else 0),
            )
            self._commit()
            return True
        except Exception as e:
            logger.error(f"Failed to add TTS replacement: {e}")
            return False

    def update_tts_replacement(
        self, replacement_id: int, pattern: str, replacement: str = "", enabled: bool | None = None
    ) -> bool:
        """Update a TTS replacement by id. enabled=None keeps the current value."""
        try:
            if enabled is None:
                self.cursor.execute(
                    "UPDATE tts_replacements SET pattern=?, replacement=? WHERE id=?",
                    (pattern, replacement, replacement_id),
                )
            else:
                self.cursor.execute(
                    "UPDATE tts_replacements SET pattern=?, replacement=?, enabled=? WHERE id=?",
                    (pattern, replacement, 1 if enabled else 0, replacement_id),
                )
            self._commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to update TTS replacement: {e}")
            return False

    def delete_tts_replacement(self, replacement_id: int) -> bool:
        """Delete a TTS replacement by id."""
        try:
            self.cursor.execute("DELETE FROM tts_replacements WHERE id = ?", (replacement_id,))
            self._commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete TTS replacement: {e}")
            return False

    def delete_all_tts_replacements(self) -> int:
        """Delete all TTS replacements. Returns count deleted."""
        try:
            self.cursor.execute("SELECT COUNT(*) FROM tts_replacements")
            count = self.cursor.fetchone()[0]
            self.cursor.execute("DELETE FROM tts_replacements")
            self._commit()
            return count
        except Exception as e:
            logger.error(f"Failed to delete all TTS replacements: {e}")
            return 0

    def get_all_tts_replacements(self) -> list:
        """Get all enabled TTS replacements for applying to text."""
        try:
            self.cursor.execute("SELECT pattern, replacement FROM tts_replacements WHERE enabled = 1 ORDER BY id")
            return [{"pattern": row["pattern"], "replacement": row["replacement"]} for row in self.cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get TTS replacements: {e}")
            return []

    def import_tts_replacements(self, replacements: list) -> int:
        """Bulk import TTS replacements from config. Returns count imported."""
        count = 0
        try:
            with self._transaction() as conn:
                for item in replacements:
                    pattern = item.get("pattern", "")
                    replacement = item.get("replacement", "")
                    if pattern:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO tts_replacements (pattern, replacement) VALUES (?, ?)",
                            (pattern, replacement),
                        )
                        count += cur.rowcount
        except Exception as e:
            count = 0
            logger.error(f"Failed to import TTS replacements: {e}")
        return count

    # =========================================================================
    # GLOBAL MESSAGE FILTERS (ignore_text / ignore_capcodes)
    # =========================================================================

    def list_ignore_text(self, page: int = 1, per_page: int = 50, search: str = "") -> dict:
        """List ignore-text patterns with pagination and search."""
        try:
            where = ""
            params = []
            if search:
                where = "WHERE pattern LIKE ?"
                params = [f"%{search}%"]

            self.cursor.execute(f"SELECT COUNT(*) FROM ignore_text {where}", params)
            total = self.cursor.fetchone()[0]

            offset = (page - 1) * per_page
            self.cursor.execute(
                f"SELECT * FROM ignore_text {where} ORDER BY id LIMIT ? OFFSET ?", [*params, per_page, offset]
            )
            items = [dict(row) for row in self.cursor.fetchall()]
            return {"items": items, "total": total, "page": page}
        except Exception as e:
            logger.error(f"Failed to list ignore-text patterns: {e}")
            return {"items": [], "total": 0, "page": page}

    def add_ignore_text(self, pattern: str, enabled: bool = True) -> bool:
        """Add or update an ignore-text pattern."""
        try:
            self.cursor.execute(
                "INSERT OR REPLACE INTO ignore_text (pattern, enabled) VALUES (?, ?)", (pattern, 1 if enabled else 0)
            )
            self._commit()
            return True
        except Exception as e:
            logger.error(f"Failed to add ignore-text pattern: {e}")
            return False

    def update_ignore_text(self, rule_id: int, pattern: str, enabled: bool | None = None) -> bool:
        """Update an ignore-text pattern by id. enabled=None keeps the current value."""
        try:
            if enabled is None:
                self.cursor.execute("UPDATE ignore_text SET pattern=? WHERE id=?", (pattern, rule_id))
            else:
                self.cursor.execute(
                    "UPDATE ignore_text SET pattern=?, enabled=? WHERE id=?", (pattern, 1 if enabled else 0, rule_id)
                )
            self._commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to update ignore-text pattern: {e}")
            return False

    def delete_ignore_text(self, rule_id: int) -> bool:
        """Delete an ignore-text pattern by id."""
        try:
            self.cursor.execute("DELETE FROM ignore_text WHERE id = ?", (rule_id,))
            self._commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete ignore-text pattern: {e}")
            return False

    def delete_all_ignore_text(self) -> int:
        """Delete all ignore-text patterns. Returns count deleted."""
        try:
            self.cursor.execute("SELECT COUNT(*) FROM ignore_text")
            count = self.cursor.fetchone()[0]
            self.cursor.execute("DELETE FROM ignore_text")
            self._commit()
            return count
        except Exception as e:
            logger.error(f"Failed to delete all ignore-text patterns: {e}")
            return 0

    def get_all_ignore_text(self) -> list[str]:
        """Get all enabled ignore-text patterns for filtering."""
        try:
            self.cursor.execute("SELECT pattern FROM ignore_text WHERE enabled = 1 ORDER BY id")
            return [row["pattern"] for row in self.cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get ignore-text patterns: {e}")
            return []

    def import_ignore_text(self, patterns: list[str]) -> int:
        """Bulk import ignore-text patterns from config. Returns count imported."""
        count = 0
        try:
            with self._transaction() as conn:
                for pattern in patterns:
                    if pattern:
                        cur = conn.execute("INSERT OR IGNORE INTO ignore_text (pattern) VALUES (?)", (pattern,))
                        count += cur.rowcount
        except Exception as e:
            count = 0
            logger.error(f"Failed to import ignore-text patterns: {e}")
        return count

    def export_ignore_text_csv(self) -> str:
        """Export all ignore-text patterns to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["pattern", "enabled"])
        try:
            self.cursor.execute("SELECT pattern, enabled FROM ignore_text ORDER BY id")
            for row in self.cursor.fetchall():
                writer.writerow(list(row))
        except Exception as e:
            logger.error(f"Export ignore-text patterns failed: {e}")
        return output.getvalue()

    def list_ignore_capcodes(self, page: int = 1, per_page: int = 50, search: str = "") -> dict:
        """List ignored capcodes with pagination and search."""
        try:
            where = ""
            params = []
            if search:
                where = "WHERE capcode LIKE ?"
                params = [f"%{search}%"]

            self.cursor.execute(f"SELECT COUNT(*) FROM ignore_capcodes {where}", params)
            total = self.cursor.fetchone()[0]

            offset = (page - 1) * per_page
            self.cursor.execute(
                f"SELECT * FROM ignore_capcodes {where} ORDER BY id LIMIT ? OFFSET ?", [*params, per_page, offset]
            )
            items = [dict(row) for row in self.cursor.fetchall()]
            return {"items": items, "total": total, "page": page}
        except Exception as e:
            logger.error(f"Failed to list ignored capcodes: {e}")
            return {"items": [], "total": 0, "page": page}

    def add_ignore_capcode(self, capcode: str, enabled: bool = True) -> bool:
        """Add or update an ignored capcode."""
        try:
            self.cursor.execute(
                "INSERT OR REPLACE INTO ignore_capcodes (capcode, enabled) VALUES (?, ?)",
                (capcode, 1 if enabled else 0),
            )
            self._commit()
            return True
        except Exception as e:
            logger.error(f"Failed to add ignored capcode: {e}")
            return False

    def update_ignore_capcode(self, rule_id: int, capcode: str, enabled: bool | None = None) -> bool:
        """Update an ignored capcode by id. enabled=None keeps the current value."""
        try:
            if enabled is None:
                self.cursor.execute("UPDATE ignore_capcodes SET capcode=? WHERE id=?", (capcode, rule_id))
            else:
                self.cursor.execute(
                    "UPDATE ignore_capcodes SET capcode=?, enabled=? WHERE id=?",
                    (capcode, 1 if enabled else 0, rule_id),
                )
            self._commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to update ignored capcode: {e}")
            return False

    def delete_ignore_capcode(self, rule_id: int) -> bool:
        """Delete an ignored capcode by id."""
        try:
            self.cursor.execute("DELETE FROM ignore_capcodes WHERE id = ?", (rule_id,))
            self._commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete ignored capcode: {e}")
            return False

    def delete_all_ignore_capcodes(self) -> int:
        """Delete all ignored capcodes. Returns count deleted."""
        try:
            self.cursor.execute("SELECT COUNT(*) FROM ignore_capcodes")
            count = self.cursor.fetchone()[0]
            self.cursor.execute("DELETE FROM ignore_capcodes")
            self._commit()
            return count
        except Exception as e:
            logger.error(f"Failed to delete all ignored capcodes: {e}")
            return 0

    def get_all_ignore_capcodes(self) -> list[str]:
        """Get all enabled ignored capcodes for filtering."""
        try:
            self.cursor.execute("SELECT capcode FROM ignore_capcodes WHERE enabled = 1 ORDER BY id")
            return [row["capcode"] for row in self.cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get ignored capcodes: {e}")
            return []

    def export_ignore_capcodes_csv(self) -> str:
        """Export all ignored capcodes to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["capcode", "enabled"])
        try:
            self.cursor.execute("SELECT capcode, enabled FROM ignore_capcodes ORDER BY id")
            for row in self.cursor.fetchall():
                writer.writerow(list(row))
        except Exception as e:
            logger.error(f"Export ignored capcodes failed: {e}")
        return output.getvalue()

    # =========================================================================
    # VEHICLE PHOTOS (user-uploaded, keyed by vehicle number or category)
    # =========================================================================

    def get_vehicle_photo(self, key_type: str, key_value: str) -> dict | None:
        """Get a vehicle photo's mime type, label and image bytes. None if not set."""
        try:
            self.cursor.execute(
                "SELECT mime_type, label, image FROM vehicle_photos WHERE key_type = ? AND key_value = ?",
                (key_type, key_value),
            )
            row = self.cursor.fetchone()
            if not row:
                return None
            return {"mime_type": row["mime_type"], "label": row["label"], "image": bytes(row["image"])}
        except Exception as e:
            logger.error(f"Failed to get vehicle photo: {e}")
            return None

    def set_vehicle_photo(self, key_type: str, key_value: str, mime_type: str, image: bytes, label: str = "") -> bool:
        """Add or replace a vehicle photo for a vehicle number or category."""
        try:
            self.cursor.execute(
                "INSERT OR REPLACE INTO vehicle_photos (key_type, key_value, label, mime_type, image) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_type, key_value, label, mime_type, image),
            )
            self._commit()
            return True
        except Exception as e:
            logger.error(f"Failed to set vehicle photo: {e}")
            return False

    def delete_vehicle_photo(self, key_type: str, key_value: str) -> bool:
        """Delete a vehicle photo, reverting that key to the fallback icon."""
        try:
            self.cursor.execute(
                "DELETE FROM vehicle_photos WHERE key_type = ? AND key_value = ?", (key_type, key_value)
            )
            self._commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete vehicle photo: {e}")
            return False

    def list_vehicle_photos(self, page: int = 1, per_page: int = 50, search: str = "") -> dict[str, Any]:
        """List vehicle photos with pagination and search (metadata only, no image bytes)."""
        offset = (page - 1) * per_page
        try:
            if search:
                search_term = f"%{search}%"
                self.cursor.execute(
                    "SELECT COUNT(*) FROM vehicle_photos WHERE key_value LIKE ? OR label LIKE ?",
                    (search_term, search_term),
                )
                total = self.cursor.fetchone()[0]
                self.cursor.execute(
                    """SELECT id, key_type, key_value, label, mime_type FROM vehicle_photos
                       WHERE key_value LIKE ? OR label LIKE ?
                       ORDER BY id LIMIT ? OFFSET ?""",
                    (search_term, search_term, per_page, offset),
                )
            else:
                self.cursor.execute("SELECT COUNT(*) FROM vehicle_photos")
                total = self.cursor.fetchone()[0]
                self.cursor.execute(
                    "SELECT id, key_type, key_value, label, mime_type FROM vehicle_photos ORDER BY id LIMIT ? OFFSET ?",
                    (per_page, offset),
                )
            items = [dict(row) for row in self.cursor.fetchall()]
            return {
                "items": items,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": (total + per_page - 1) // per_page,
            }
        except Exception as e:
            logger.error(f"Failed to list vehicle photos: {e}")
            return {"items": [], "total": 0, "page": 1, "per_page": per_page, "pages": 0}

    # =========================================================================
    # VEHICLES (vehicle number -> what it is)
    # =========================================================================

    def _known_vehicles(self) -> dict[str, dict]:
        """All known vehicles by number (cached; messages look them up per row)."""
        if self._vehicle_cache is None:
            self.cursor.execute("SELECT number, category, description FROM vehicles")
            self._vehicle_cache = {row["number"]: dict(row) for row in self.cursor.fetchall()}
        return self._vehicle_cache

    def resolve_vehicle(self, text: str, discipline: str = "", abbreviations: list | None = None) -> dict:
        """A message's vehicle fields (see vehicle_types.resolve_vehicle)."""
        return vehicle_types.resolve_vehicle(text, discipline, abbreviations, self._known_vehicles())

    def list_vehicles(self, page: int = 1, per_page: int = 50, search: str = "") -> dict:
        """List known vehicles with pagination and search."""
        try:
            where = ""
            params = []
            if search:
                where = "WHERE number LIKE ? OR category LIKE ? OR description LIKE ?"
                params = [f"%{search}%"] * 3
            self.cursor.execute(f"SELECT COUNT(*) FROM vehicles {where}", params)
            total = self.cursor.fetchone()[0]
            offset = (page - 1) * per_page
            self.cursor.execute(
                f"SELECT number, category, description, source FROM vehicles {where} ORDER BY number LIMIT ? OFFSET ?",
                [*params, per_page, offset],
            )
            items = [dict(row) for row in self.cursor.fetchall()]
            return {"items": items, "total": total, "page": page, "per_page": per_page}
        except Exception as e:
            logger.error(f"Failed to list vehicles: {e}")
            return {"items": [], "total": 0, "page": page, "per_page": per_page}

    def set_vehicle(self, number: str, category: str = "", description: str = "") -> bool:
        """Add or replace a vehicle by hand (source 'user', never overwritten by learning)."""
        try:
            self.cursor.execute(
                "INSERT OR REPLACE INTO vehicles (number, category, description, source) VALUES (?, ?, ?, 'user')",
                (number, category, description),
            )
            self._commit()
            self._vehicle_cache = None
            return True
        except Exception as e:
            logger.error(f"Failed to set vehicle: {e}")
            return False

    def delete_vehicle(self, number: str) -> bool:
        """Delete a vehicle by number."""
        try:
            self.cursor.execute("DELETE FROM vehicles WHERE number = ?", (number,))
            self._commit()
            self._vehicle_cache = None
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete vehicle: {e}")
            return False

    def learn_vehicles(self, vehicles: list[tuple[str, str, str]], source: str) -> int:
        """Store (number, category, description) seen in real data; existing rows are kept.

        Only complete numbers ("RR-NNNN", "RR-NNN") - short local numbers like
        "TS 303" can't be matched to the numbers in messages.
        """
        count = 0
        try:
            with self._transaction() as conn:
                for number, category, description in vehicles:
                    if not vehicle_types.is_full_number(number):
                        continue
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO vehicles (number, category, description, source) VALUES (?, ?, ?, ?)",
                        (number, category, description, source),
                    )
                    count += cur.rowcount
        except Exception as e:
            logger.error(f"Failed to store vehicles: {e}")
            return 0
        if count:
            self._vehicle_cache = None
        return count

    def learn_vehicles_from_capcodes(self) -> int:
        """Vehicles named in capcode descriptions, e.g. "TS 01-2632" or "OvD - 15-9591"."""
        self.cursor.execute("SELECT description, region, location FROM capcodes WHERE description != ''")
        found = []
        for row in self.cursor.fetchall():
            description = row["description"].strip()
            place = " ".join(p.strip() for p in (row["region"], row["location"]) if p and p.strip())
            # The type only where the description names it: a code right before
            # the number ("TS 01-2632") or spelled out ("Ambulance-07-101")
            named = dict(vehicle_types.named_vehicles(description))
            spelled = vehicle_types.classify_description(description)
            for number in vehicle_types.full_numbers(description):
                category = named.get(number) or spelled
                found.append((number, category, f"{description} ({place})" if place else description))
        return self.learn_vehicles(found, "capcode")

    def export_vehicles_csv(self) -> str:
        """Export all vehicles to CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["number", "category", "description", "source"])
        try:
            self.cursor.execute("SELECT number, category, description, source FROM vehicles ORDER BY number")
            for row in self.cursor.fetchall():
                writer.writerow(list(row))
        except Exception as e:
            logger.error(f"Export vehicles failed: {e}")
        return output.getvalue()

    def import_vehicles_csv(self, csv_content: str, replace: bool = False) -> dict[str, int]:
        """Import vehicles (number, category, description) from CSV as the user's own rows.

        With replace=True all vehicles not in the CSV are removed. Raises
        ValueError for invalid CSV; nothing is changed then.
        """
        rows = self._read_csv(csv_content, ("number",))
        imported = 0
        skipped = 0
        with self._transaction() as conn:
            if replace:
                conn.execute("DELETE FROM vehicles")
            for row in rows:
                number = (row.get("number") or "").strip()
                if not number:
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO vehicles (number, category, description, source) VALUES (?, ?, ?, 'user')",
                    (number, (row.get("category") or "").strip(), (row.get("description") or "").strip()),
                )
                imported += 1
        self._vehicle_cache = None
        return {"imported": imported, "skipped": skipped}

    def clear_table(self, table: str) -> int:
        """Delete all rows from a whitelisted table. Returns count deleted, -1 on error."""
        allowed = {
            "capcodes",
            "places",
            "streets",
            "abbreviations",
            "geocodes",
            "tts_replacements",
            "messages",
            "ignore_text",
            "ignore_capcodes",
            "vehicle_photos",
            "vehicles",
        }
        if table not in allowed:
            logger.warning(f"clear_table rejected for table: {table}")
            return -1
        try:
            self.cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = self.cursor.fetchone()[0]
            self.cursor.execute(f"DELETE FROM {table}")
            self._commit()
            if table == "abbreviations":
                self.invalidate_text_caches()
            if table == "vehicles":
                self._vehicle_cache = None
            logger.info(f"Cleared table {table}: {count} rows deleted")
            return count
        except Exception as e:
            logger.error(f"Failed to clear table {table}: {e}")
            return -1

    def close(self) -> None:
        """Close database connection."""
        with self._lock:
            for cur in self._cursors.values():
                with contextlib.suppress(sqlite3.Error):
                    cur.close()
            self._cursors.clear()
            self.conn.close()
