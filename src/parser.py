"""P2000 message parsing."""

import logging
import re
from datetime import UTC, datetime

from . import message_fields, vehicle_types
from .city_abbreviations import get_city_from_abbreviation
from .database import Database
from .message import P2000Message
from .postcode import full_postcode

logger = logging.getLogger(__name__)

# Common endings of Dutch street names
STREET_SUFFIXES = (
    "straat",
    "weg",
    "laan",
    "plein",
    "singel",
    "kade",
    "gracht",
    "steeg",
    "pad",
    "dreef",
    "hof",
    "dijk",
    "park",
)


STREET_NAME_CONNECTORS = {"van", "de", "den", "der", "het", "ter", "ten", "op", "aan", "in", "t", "'t", "s", "'s"}
# A name word: a capital followed by a lowercase letter ("Burgemeester", "Jan", "Kortenaer-Noord"),
# an ordinal ("2e") or dotted initials ("J.P.", "H.J.E.")
_STREET_NAME_WORD_RE = re.compile(
    r"^(?:[A-Z][a-z\u00e0-\u00ff][\w'.]*(?:-[A-Za-z][\w'.]*)*|\d{1,2}e|(?:[A-Z]\.){1,4})$"
)
# Capitalised words that P2000 texts put before an address but that are no part of it
_NOT_STREET_NAME_WORDS = {
    "ambulance",
    "ambulancepost",
    "ambu",
    "brandweer",
    "brandweerkazerne",
    "kazerne",
    "politie",
    "melding",
    "ongeval",
    "nacontrole",
    "buitensluiting",
    "binnensluiting",
    "afhijsen",
    "reanimatie",
    "prio",
    "test",
    "oefening",
}


def is_street_name_word(word: str) -> bool:
    """Whether a word can be part of a street name before its last word ("Burgemeester van ...straat").

    Message codes are not: priorities ("A1", "P", "2"), talk groups ("BAD-01"), capitals ("CO-melder", "BR"),
    and neither are incident words such as "Woningbrand" or "Assistentie".
    """
    if word.lower() in STREET_NAME_CONNECTORS:
        return True
    if not _STREET_NAME_WORD_RE.match(word) or word.lower() in _NOT_STREET_NAME_WORDS:
        return False
    return not message_fields.is_incident_word(word)


def looks_like_street(name: str) -> bool:
    """Whether a street name is clearly a street, not an ordinary word that happens to name one.

    Some BAG streets are named after everyday words ("Dienst", "Contact", "Inzet", "Oost"). Without a
    town in the message such a word must not pick a street (and with it a town) somewhere in the country.
    """
    name = name.strip()
    if " " in name:
        return True
    lower = name.lower()
    return any(lower.endswith(suffix) and len(lower) > len(suffix) + 2 for suffix in STREET_SUFFIXES)


def city_shortname_tags(body: str) -> list[dict[str, str]]:
    """Town shortnames in a message ("SGRAVH" for 's-Gravenhage) as abbreviation tags for the detail.

    Same rule as the town search in _extract_address: one of the last 8 words,
    4+ characters, not a number. The message text itself is shown as received.
    """
    tags: list[dict[str, str]] = []
    for word in body.split()[-8:]:
        if len(word) < 4 or word.isdigit():
            continue
        city = get_city_from_abbreviation(word)
        if city and word.lower() != city.lower() and all(t["abbreviation"] != word for t in tags):
            tags.append({"abbreviation": word, "full_text": city})
    return tags


# Priority detection patterns
PRIO_PATTERNS = [
    (1, r"^A\s?1|\bA\s?1|PRIO\s?1|^P\s?1"),
    (2, r"^A\s?2|\bA\s?2|PRIO\s?2|^P\s?2"),
    (3, r"^B\s?[123]|PRIO\s?3|^P\s?3"),
    (4, r"PRIO\s?4|^P\s?4"),
]


class Parser:
    """Parse P2000 FLEX messages."""

    def __init__(self, database: Database):
        self.database = database
        self._skip_keywords: set = set()

    def parse(self, line: str) -> P2000Message | None:
        """
        Parse an alphanumeric FLEX message line from multimon-ng:
        FLEX|YYYY-MM-DD HH:MM:SS|baud/frame/type|groupid|capcodes|ALN|body
        """
        parts = line.split("|")
        if len(parts) < 7:
            logger.debug(f"Invalid FLEX format (need 7+ parts, got {len(parts)}): {line[:50]}")
            return None

        try:
            # multimon-ng FLEX timestamps are UTC - parse as tz-aware, then convert to local time
            timestamp = datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).astimezone()
        except ValueError:
            timestamp = datetime.now(UTC).astimezone()

        group_id = parts[3].strip()
        capcodes_str = parts[4].strip()
        # Body may itself contain '|' characters - rejoin everything after part 6
        body = "|".join(parts[6:]).strip()

        if not body:
            logger.debug(f"Skipping empty FLEX message: {line[:50]}")
            return None

        capcodes = [c.strip() for c in capcodes_str.split() if c.strip()]

        logger.debug(f"FLEX: capcodes={capcodes}, body={body[:50]}...")

        msg = P2000Message(
            timestamp=timestamp,
            body=body,
            raw_message=line.strip(),
            capcodes=capcodes,
            group_id=group_id,
            message_type="FLEX",
        )

        # Extract priority
        msg.priority = self._extract_priority(body)

        # Extract address components
        self._extract_address(msg)

        # Lookup capcode information
        self._enrich_from_capcodes(msg)

        # Expand abbreviations in message body
        self._expand_abbreviations(msg)

        # Best-effort vehicle/unit classification (icon, photo lookup key)
        self._classify_vehicle(msg)

        # A full postcode in the text ("3068HA") gives a precise position (see postcode.py)
        if postcode := full_postcode(body):
            msg.postalcode = postcode
        # Incident type, reference numbers, talk group
        for name, value in message_fields.derived_fields(body).items():
            setattr(msg, name, value)
        # Without a region from the capcodes, a vehicle named with its type ("ambu 16-107")
        # has the region in its number (not the bare trailing numbers, which may be anything)
        if not msg.region:
            named = vehicle_types.named_vehicles(body)
            if named:
                msg.region = message_fields.region_of_vehicle(named[0][0])
        return msg

    def _extract_priority(self, text: str) -> int:
        """Extract priority level from message text."""
        for priority, pattern in PRIO_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return priority
        return 0

    def _speakable_city_replace(self, speakable_body: str, token: str, expanded_city: str) -> str:
        """Swap a city abbreviation token for its full name in speakable text.

        P2000 messages sometimes already spell the city out in full AND append
        the abbreviated code right after (e.g. "... Sliedrecht SLIEDR bon ...").
        In that case, drop the abbreviation instead of announcing the city name
        twice in a row.
        """
        if token.lower() == expanded_city.lower():
            return speakable_body  # not actually an abbreviation - nothing to expand
        if re.search(rf"\b{re.escape(expanded_city)}\b", speakable_body, re.IGNORECASE):
            return re.sub(rf"\s*\b{re.escape(token)}\b", "", speakable_body, count=1)
        return speakable_body.replace(token, expanded_city, 1)

    def _extract_address(self, msg: P2000Message) -> None:
        """Extract address components from message body for geocoding.

        Order:
        1. Search for city from END of message (where P2000 typically places them)
        2. Fall back to find_city_in_text if not found
        3. Match streets in remaining text
        """
        text = msg.body
        msg.speakable_body = text
        found_city = None
        search_text = text  # Text to search for streets (after removing city)
        excluded_words = []  # Words to exclude from street matching

        # STEP 1: Search for city from END of message first (typical P2000 format)
        # Messages usually end with: "...Street City 123456" or "...Street City CAPCODE"
        # Skip common P2000 words that aren't cities
        skip_city_words = {
            "bon",
            "rit",
            "ambu",
            "prio",
            "icnum",
            "regio",
            "alert",
            "melding",
            "dienst",
            "post",
            "dhn",
            "dmt",
            "nhn",
            "hgl",
        }
        words = text.split()
        for i in range(len(words) - 1, max(0, len(words) - 8), -1):
            word = words[i]
            if re.match(r"^\d+$", word):  # Skip pure numbers (capcodes)
                continue
            if len(word) < 4:  # Skip short words (likely abbreviations)
                continue
            if word.lower() in skip_city_words:
                continue
            # Check if this word is a known city in our database
            city_name = self.database.find_city_name(word)
            if city_name:
                # Before accepting, check if preceding words form a multi-word city
                # e.g., "Koog aan de Zaan" instead of just "Zaan"
                start = i
                for j in range(max(0, i - 4), i):
                    candidate = self.database.find_city_name(" ".join(words[j : i + 1]))
                    if candidate:
                        city_name, start = candidate, j
                        break

                found_city = city_name
                msg.city = city_name
                # Remove the matched tokens (not the first substring hit, which
                # could be inside a street name like "Delftweg")
                search_text = " ".join(words[:start] + words[i + 1 :])
                excluded_words.extend(words[start : i + 1])
                logger.debug(f"City found at end of message: {city_name}")
                break
            # Check if this is a city abbreviation (e.g., OEGSTG -> Oegstgeest)
            expanded_city = get_city_from_abbreviation(word)
            if expanded_city:
                found_city = expanded_city
                msg.city = expanded_city
                search_text = " ".join(words[:i] + words[i + 1 :])
                excluded_words.append(word)
                msg.speakable_body = self._speakable_city_replace(msg.speakable_body, word, expanded_city)
                logger.debug(f"City abbreviation expanded: {word} -> {expanded_city}")
                break

        # STEP 2: If no city found at end, try find_city_in_text (scans full message)
        if not found_city:
            city_match = self.database.find_city_in_text(text)
            if city_match:
                found_city = city_match["city"]
                search_text = city_match["remaining_text"]
                excluded_words.append(city_match["matched_text"])
                msg.city = found_city
                if city_match["matched_text"] != found_city:
                    msg.speakable_body = self._speakable_city_replace(
                        msg.speakable_body, city_match["matched_text"], found_city
                    )
                logger.debug(f"City match via find_city_in_text: {found_city}")

        # STEP 3: Try to match a known street from BAG data in remaining text.
        # With a known town only that town's streets count; without one, only names that are
        # clearly a street, so a word like "dienst" or "contact" doesn't place the message
        # in the one town that has a street of that name. A town that isn't a stored place
        # (e.g. "Rijswijk ZH" from the abbreviation list, stored as "Rijswijk") can't filter
        # the streets, so those are searched in every town as before
        street_match = self.database.match_street_in_text(
            search_text, excluded_words, city=self.database.find_city_name(msg.city)
        )
        if street_match and not msg.city and not looks_like_street(street_match["street"]):
            street_match = None
        if street_match:
            msg.street = street_match["street"]

            # Use street's associated city only if we still haven't found one
            # Don't overwrite expanded city abbreviations (e.g., Oegstgeest) with raw abbreviations from database (e.g., OEGSTG)
            if not found_city and not msg.city and street_match.get("city"):
                msg.city = street_match["city"]

            # Use postal code from BAG data if available
            if street_match.get("postalcode"):
                msg.postalcode = street_match["postalcode"]

            # Build address with postal code if available
            if msg.postalcode and msg.city:
                msg.address = f"{msg.street}, {msg.postalcode} {msg.city}, Netherlands"
            elif msg.city:
                msg.address = f"{msg.street}, {msg.city}, Netherlands"
            else:
                msg.address = f"{msg.street}, Netherlands"
            msg.location_accuracy = "street"
            logger.debug(f"BAG match: street={msg.street}, postalcode={msg.postalcode}, city={msg.city}")
            return

        # Try full address pattern with postal code: Street 123, 1234AB City
        match = re.search(
            r"([A-Za-z][A-Za-z\s]+?)\s+(\d+)[,\s]+(\d{4}\s?[A-Z]{2})\s+([A-Za-z][A-Za-z\s]+)", search_text
        )
        if match:
            msg.street = match.group(1).strip()
            msg.postalcode = match.group(3).strip()
            if not found_city:
                msg.city = match.group(4).strip()
            msg.address = f"{msg.street}, {msg.postalcode} {msg.city}"
            msg.location_accuracy = "street"
            return

        # Try street + postal + city pattern: Streetname 1234AB Cityname
        match = re.search(r"([A-Za-z][A-Za-z\s]+?)\s+(\d{4}\s?[A-Z]{2})\s+([A-Za-z]+)", search_text)
        if match:
            msg.street = match.group(1).strip()
            msg.postalcode = match.group(2).strip()
            if not found_city:
                msg.city = match.group(3).strip()
            msg.address = f"{msg.street}, {msg.postalcode} {msg.city}"
            msg.location_accuracy = "street"
            return

        # If we found a city but no street, try to find street in remaining text
        if found_city:
            # Try to extract street from remaining words (heuristic approach)
            words = search_text.replace("'", "").split()
            skip_words = {
                "AMBU",
                "BRAND",
                "PRIO",
                "A1",
                "A2",
                "B1",
                "B2",
                "P1",
                "P2",
                "bon",
                "Rit",
                "Regio",
                "Ambu",
                "Brandweer",
            }

            # Look for words ending with street suffixes
            for i, word in enumerate(words):
                if word.upper() in skip_words or word in skip_words:
                    continue
                if re.match(r"^\d{5,}$", word):  # Skip capcodes
                    continue

                # Check if word ends with street suffix
                for suffix in STREET_SUFFIXES:
                    if word.lower().endswith(suffix) and len(word) > len(suffix) + 2:
                        # Found potential street: add the name words right before it ("Burgemeester",
                        # "van", "2e"), stopping at the first word that isn't one, so codes such as
                        # "A1", "P 2", "BAD-01" or "CO-melder" don't end up in the street name
                        street_parts = [word]
                        for j in range(i - 1, max(-1, i - 4), -1):
                            if not is_street_name_word(words[j]):
                                break
                            street_parts.insert(0, words[j])

                        if street_parts:
                            msg.street = " ".join(street_parts)
                            msg.address = f"{msg.street}, {msg.city}, Netherlands"
                            msg.location_accuracy = "street"
                            return

            # No street found, just use city
            msg.address = f"{msg.city}, Netherlands"
            msg.location_accuracy = "city"
            return

        # Last resort: find abbreviations that map to cities (if still no city)
        if not found_city:
            abbrevs = re.findall(r"\b[A-Z]{3,}\b", text)
            for abbrev in abbrevs:
                if abbrev in self._skip_keywords:
                    continue

                city = self.database.find_city_by_abbreviation(abbrev)
                if city:
                    msg.city = city
                    msg.address = f"{msg.city}, Netherlands"
                    msg.location_accuracy = "city"
                    return
                else:
                    self._skip_keywords.add(abbrev)

    def _enrich_from_capcodes(self, msg: P2000Message) -> None:
        """Add region/discipline info from capcode database."""
        receivers = []
        disciplines = set()
        remarks = []

        for capcode in msg.capcodes:
            info = self.database.find_capcode(capcode)
            if info:
                if info.get("description"):
                    receivers.append(f"{info['description']} ({capcode})")
                if info.get("discipline"):
                    disciplines.add(info["discipline"])
                if info.get("region") and not msg.region:
                    msg.region = info["region"]
                if info.get("location") and not msg.location:
                    msg.location = info["location"]
                if info.get("remark"):
                    remarks.append(info["remark"])

        msg.receivers = ", ".join(receivers)
        msg.discipline = ", ".join(sorted(disciplines))
        msg.remarks = ", ".join(remarks)

    def _expand_abbreviations(self, msg: P2000Message) -> None:
        """Find and expand abbreviations in message body."""
        if not msg.body:
            return

        # Use database to find abbreviations in the message text
        found = self.database.find_abbreviations_in_text(msg.body, msg.discipline)
        if found:
            msg.abbreviations = found

    def _classify_vehicle(self, msg: P2000Message) -> None:
        """Best-effort vehicle/unit category and vehicle number, from a known abbreviation set."""
        if not msg.body:
            return
        # Vehicles the message names with their type ("TS 07-1782") are real data: remember them
        named = vehicle_types.named_vehicles(msg.body)
        if named:
            self.database.learn_vehicles([(number, category, "") for number, category in named], "message")
        info = self.database.resolve_vehicle(msg.body, msg.discipline, msg.abbreviations)
        msg.vehicle_category = info["vehicle_category"]
        msg.vehicle_number = info["vehicle_number"]
        msg.vehicle_units = info["vehicle_units"]
        msg.vehicle_description = info["vehicle_description"]
        msg.vehicle_icon = info["vehicle_icon"]
