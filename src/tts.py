"""Text-to-speech: turn a P2000 message into a speakable sentence.

The same spoken text is used for Home Assistant sensors, MQTT, speaker
announcements and the dashboard's browser speech.
"""

import logging
import re

from .database import Database
from .message import P2000Message

logger = logging.getLogger(__name__)

# Seeded into the database on startup (only new patterns, user edits are kept)
DEFAULT_TTS_REPLACEMENTS = [
    # Priority codes - real messages use e.g. "A1"/"P1" with no space, but
    # allow an optional one since the parser itself sees both forms
    {"pattern": r"\bA\s?1\b", "replacement": "Ambulance met spoed"},
    {"pattern": r"\bA\s?2\b", "replacement": "Ambulance zonder spoed"},
    {"pattern": r"\bB\s?1\b", "replacement": "Ambulance besteld vervoer"},
    {"pattern": r"\bB\s?2\b", "replacement": "Ambulance besteld vervoer"},
    {"pattern": r"\bP\s?1\b", "replacement": "Prio 1"},
    {"pattern": r"\bP\s?2\b", "replacement": "Prio 2"},
    # Meldkamer gespreksgroep codes - short spoken forms
    {"pattern": r"BDH\-[0-9]+", "replacement": "Brandweer Den Haag"},
    {"pattern": r"BOB\-[0-9]+", "replacement": "Brandweer Oost-Brabant"},
    {"pattern": r"BON\-[0-9]+", "replacement": "Brandweer Oost-Nederland"},
    {"pattern": r"BNN\-[0-9]+", "replacement": "Brandweer Noord-Nederland"},
    {"pattern": r"BNH\-[0-9]+", "replacement": "Brandweer Noord-Holland"},
    {"pattern": r"BLB\-[0-9]+", "replacement": "Brandweer Limburg"},
    {"pattern": r"BAD\-[0-9]+", "replacement": "Brandweer Amsterdam-Amstelland"},
    {"pattern": r"BMD\-[0-9]+", "replacement": "Brandweer Midden-Nederland"},
    {"pattern": r"BRT\-[0-9]+", "replacement": "Brandweer Rotterdam"},
    {"pattern": r"BZB\-[0-9]+", "replacement": "Brandweer Zeeland"},
    # Ambulance dienstposten - short spoken form
    {"pattern": r"DP[0-9]+", "replacement": "Dienstpost"},
    {"pattern": r"\bAMBU\b", "replacement": "Ambulance"},
    # City abbreviations - short spoken forms (see city_abbreviations.py
    # for the full list used for address/city matching; only ones worth
    # a distinct spoken form belong here)
    {"pattern": r"\bSGRAVH\b", "replacement": "Den Haag"},
    # Status flags - drop the "(... : ja)" wrapper, just speak the label
    {"pattern": r"\(Directe inzet:\s*ja\)", "replacement": "Directe inzet"},
    {"pattern": r"\(DIA:\s*ja\)", "replacement": "Directe inzet ambulance"},
    # Reference numbers - not useful spoken aloud
    {"pattern": r"\s*\bbon:?\s*\d+", "replacement": ""},
    {"pattern": r"\s*\brit:?\s*\d+", "replacement": ""},
    {"pattern": r"\s*\bicnum:?\s*\d+", "replacement": ""},
    # Common role/discipline abbreviations - spoken in full
    {"pattern": r"\bGHOR\b", "replacement": "Geneeskundige hulpverlening bij ongevallen en rampen"},
    {"pattern": r"\bMMT\b", "replacement": "Mobiel medisch team"},
    {"pattern": r"\bSEH\b", "replacement": "Spoedeisende Hulp"},
    {"pattern": r"\bOVD-B\b", "replacement": "Officier van Dienst Brandweer"},
    {"pattern": r"\bOVD-G\b", "replacement": "Officier van Dienst Geneeskundig"},
    {"pattern": r"\bOVD-P\b", "replacement": "Officier van Dienst Politie"},
    {"pattern": r"\bOvD\b", "replacement": "Officier van Dienst"},
    {"pattern": r"\bPOL\b", "replacement": "Politie"},
    {"pattern": r"\bRAV\b", "replacement": "Regionale ambulancevoorziening"},
    {"pattern": r"\bBR\b", "replacement": "Brand"},
    # Long standalone numbers (capcodes, other reference numbers) - postal
    # codes are 4 digits and ambulance unit numbers are 5 (e.g. "17341"),
    # so both are left alone; bon/rit/icnum numbers are already stripped
    # above regardless of length
    {"pattern": r"\s*\b\d{6,}\b", "replacement": ""},
]

# B1/B2 mean "Brandweer ... spoed" for fire, but an unrelated, always
# non-urgent "besteld vervoer" code for ambulance - consumed before the rules
# run, so the generic fire-meaning rule never sees a B1/B2 on an ambulance message
_AMBULANCE_B_CODE = re.compile(r"\bB\s?[12]\b", re.IGNORECASE)
# A1/A2/B1/B2 already speak "Ambulance ... spoed"; collapse a following bare
# "Ambulance" (e.g. from the AMBU rule) so it isn't said twice
_DOUBLE_AMBULANCE = re.compile(r"\bAmbulance\b((?:\s+(?:met|zonder)\s+spoed)?)\s+Ambulance\b", re.IGNORECASE)
# Same for Brand - e.g. "(Zeer gr. BR) BR bijeenkomst" both expand to "Brand"
_DOUBLE_BRAND = re.compile(r"\bBrand\b(\W{0,3})Brand\b\s*", re.IGNORECASE)


class SpeechRules:
    """TTS replacement rules from the database, applied to messages."""

    def __init__(self, database: Database):
        self.database = database
        imported = database.import_tts_replacements(DEFAULT_TTS_REPLACEMENTS)
        if imported:
            logger.info(f"Seeded {imported} default TTS replacement(s) into database")
        self._rules: list[dict] = []
        self.reload()

    def reload(self) -> None:
        """Reload the enabled rules from the database (after edits in the web UI)."""
        self._rules = self.database.get_all_tts_replacements()
        logger.info(f"Loaded {len(self._rules)} TTS replacement(s)")

    def speak(self, msg: P2000Message) -> str:
        """Return the spoken form of a message.

        Case-insensitive, like the patterns are edited in the web UI (an inline
        (?i) can't be relied on, so the flag is always applied).
        """
        text = msg.speakable_body or msg.body
        if "ambulance" in msg.discipline.lower():
            text = _AMBULANCE_B_CODE.sub("Ambulance besteld vervoer", text)
        for rule in self._rules:
            try:
                text = re.sub(rule["pattern"], rule["replacement"], text, flags=re.IGNORECASE)
            except re.error as e:
                logger.warning(f"Invalid TTS regex pattern '{rule['pattern']}': {e}")
        text = _DOUBLE_AMBULANCE.sub(r"Ambulance\1", text)
        return _DOUBLE_BRAND.sub(r"Brand\1", text).strip()
