"""P2000 message parsing."""

import logging
import re
from datetime import UTC, datetime

from . import vehicle_types
from .city_abbreviations import get_city_from_abbreviation
from .database import Database
from .message import P2000Message

logger = logging.getLogger(__name__)

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
        Parse a message line from multimon-ng.

        FLEX format:
        FLEX|YYYY-MM-DD HH:MM:SS|baud/frame/type|groupid|capcodes|msgtype|body

        POCSAG format:
        POCSAG1200: Address: 1234567 Function: 0 Alpha: Message text here
        """
        if line.startswith("FLEX"):
            return self._parse_flex(line)
        elif line.startswith("POCSAG"):
            return self._parse_pocsag(line)
        return None

    def _parse_flex(self, line: str) -> P2000Message | None:
        """Parse FLEX format message."""
        # Only process ALN (alphanumeric) messages
        if "|ALN|" not in line:
            logger.debug(f"Skipping non-ALN FLEX message: {line[:50]}")
            return None

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

        return msg

    def _parse_pocsag(self, line: str) -> P2000Message | None:
        """Parse POCSAG format message."""
        # Format: POCSAG1200: Address: 1234567 Function: 0 Alpha: Message text
        import re

        match = re.match(r"POCSAG\d+:\s*Address:\s*(\d+)\s*Function:\s*(\d+)\s*(?:Alpha|Numeric):\s*(.*)", line)
        if not match:
            logger.debug(f"Invalid POCSAG format: {line[:50]}")
            return None

        capcode = match.group(1)
        function = match.group(2)
        body = match.group(3).strip()

        if not body:
            logger.debug(f"Skipping empty POCSAG message: {line[:50]}")
            return None

        logger.debug(f"POCSAG: capcode={capcode}, body={body[:50]}...")

        msg = P2000Message(
            timestamp=datetime.now(UTC).astimezone(),
            body=body,
            raw_message=line.strip(),
            capcodes=[capcode],
            group_id=f"F{function}",
            message_type="POCSAG",
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
        words = text.replace("'", "").split()
        for i in range(len(words) - 1, max(0, len(words) - 8), -1):
            word = words[i]
            if re.match(r"^\d+$", word):  # Skip pure numbers (capcodes)
                continue
            if len(word) < 4:  # Skip short words (likely abbreviations)
                continue
            if word.lower() in skip_city_words:
                continue
            # Check if this word is a known city in our database
            if self.database.check_city(word):
                # Before accepting, check if preceding words form a multi-word city
                # e.g., "Koog aan de Zaan" instead of just "Zaan"
                multi_word_city = None
                for j in range(max(0, i - 4), i):
                    candidate = " ".join(words[j : i + 1])
                    if self.database.check_city(candidate):
                        multi_word_city = candidate
                        break

                if multi_word_city:
                    found_city = multi_word_city
                    msg.city = multi_word_city
                    search_text = text.replace(multi_word_city, " ", 1)
                    excluded_words.extend(words[j : i + 1])
                    logger.debug(f"Multi-word city found: {multi_word_city}")
                else:
                    found_city = word
                    msg.city = word
                    search_text = text.replace(word, " ", 1)
                    excluded_words.append(word)
                    logger.debug(f"City found at end of message: {word}")
                break
            # Check if this is a city abbreviation (e.g., OEGSTG -> Oegstgeest)
            expanded_city = get_city_from_abbreviation(word)
            if expanded_city:
                found_city = expanded_city
                msg.city = expanded_city
                search_text = text.replace(word, " ", 1)
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

        # STEP 3: Try to match a known street from BAG data in remaining text
        street_match = self.database.match_street_in_text(search_text, excluded_words)
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

            street_suffixes = [
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
            ]

            # Look for words ending with street suffixes
            for i, word in enumerate(words):
                if word.upper() in skip_words or word in skip_words:
                    continue
                if re.match(r"^\d{5,}$", word):  # Skip capcodes
                    continue

                # Check if word ends with street suffix
                for suffix in street_suffixes:
                    if word.lower().endswith(suffix) and len(word) > len(suffix) + 2:
                        # Found potential street, get preceding words too
                        street_parts = []
                        for j in range(max(0, i - 3), i + 1):
                            w = words[j]
                            if (
                                w[0].isupper()
                                or w.lower() in ["van", "de", "den", "het", "ter", "ten", "op"]
                                or (w.isdigit() and len(w) <= 4)
                            ):
                                street_parts.append(w)

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
        """Best-effort vehicle/unit category and voertuignummer, from a known abbreviation set."""
        if not msg.body:
            return
        msg.vehicle_category = vehicle_types.classify(msg.abbreviations)
        msg.vehicle_number = vehicle_types.extract_vehicle_number(msg.body)
        msg.vehicle_icon = vehicle_types.icon_for(msg.vehicle_category)
