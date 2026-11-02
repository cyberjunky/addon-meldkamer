"""Vehicle/unit recognition: vehicle numbers in a message and what they are.

What a vehicle is comes only from real data: the vehicles table (filled by
the user, CSV import, the capcode list and messages that name a vehicle, like
"TS 07-1782") or the type code the message itself names. Nothing is derived
from the number. The broad category picks the fallback icon, a bundled stock
photo (see STOCK_IMAGES) and is a lookup key for user-uploaded photos (see
database.py's vehicle_photos table).
"""

import os
import re

# Abbreviation -> broad vehicle category. Only codes that identify a
# *vehicle/unit type* belong here (not e.g. priority codes or region codes).
VEHICLE_CATEGORIES: dict[str, str] = {
    # Ambulance
    "AMBU": "ambulance",
    "MUG": "ambulance",
    # Helicopters (air ambulance / trauma team)
    "MUH": "helikopter",
    "MAA": "helikopter",
    "HTT": "helikopter",
    # Fire - pumping/engine appliances
    "TS": "brandweer_tankautospuit",
    "TAS": "brandweer_tankautospuit",
    "TS-HV": "brandweer_tankautospuit",
    # Fire - aerial appliances
    "AL": "brandweer_ladder",
    "AL-42": "brandweer_ladder",
    "AL-36": "brandweer_ladder",
    "AL-32": "brandweer_ladder",
    "HW": "brandweer_hoogwerker",
    "HW-36": "brandweer_hoogwerker",
    "HW-24": "brandweer_hoogwerker",
    # Fire - specialized appliances
    "SL": "brandweer_slangenwagen",
    "SL-4": "brandweer_slangenwagen",
    "SL-6": "brandweer_slangenwagen",
    "WO": "brandweer_waterongevallen",
    "PB": "brandweer_poederblus",
    "SB": "brandweer_schuimblus",
    "MS": "brandweer_motorspuit",
    "DPA": "brandweer_dompelpomp",
    "DPU": "brandweer_dompelpomp",
    "RIV": "brandweer_riv",
    "HVH": "brandweer_haakarmbak",
    "GSH": "brandweer_haakarmbak",
    "VZH": "brandweer_haakarmbak",
    "LHH": "brandweer_haakarmbak",
    "CHH": "brandweer_haakarmbak",
    "HV": "brandweer_hulpverlening",
    "OvD": "brandweer_officier",
    "HOvD": "brandweer_officier",
    "CvD": "brandweer_officier",
    # Police
    "POL": "politie",
    "AE": "politie",
    "AT": "politie",
    "ME": "politie",
    # Water rescue
    "RB": "reddingboot",
}

# Category -> fallback emoji shown when no user-uploaded photo exists for
# either the exact vehicle number or the category itself.
CATEGORY_ICONS: dict[str, str] = {
    "ambulance": "🚑",
    "helikopter": "🚁",
    "brandweer_tankautospuit": "🚒",
    "brandweer_ladder": "🚒",
    "brandweer_hoogwerker": "🚒",
    "brandweer_slangenwagen": "🚒",
    "brandweer_waterongevallen": "🚤",
    "brandweer_poederblus": "🚒",
    "brandweer_schuimblus": "🚒",
    "brandweer_motorspuit": "🚒",
    "brandweer_dompelpomp": "🚒",
    "brandweer_riv": "🚒",
    "brandweer_haakarmbak": "🚛",
    "brandweer_hulpverlening": "🚒",
    "brandweer_officier": "🚒",
    "brandweer_dienstbus": "🚐",
    "brandweer_ogs": "🚐",
    "politie": "🚓",
    "reddingboot": "🛟",
}

DEFAULT_ICON = "📻"

# Matches a known vehicle-type token directly followed by a vehicle number,
# either the official "RR-XXXX" fire brigade format (e.g. "TS 07-1782") or a
# bare run of digits as commonly seen for ambulance units (e.g. "AMBU 17106").
# Longer codes (AL-42, ...) are tried first so e.g. "AL-42" isn't matched as
# bare "AL", and the dashed number form is tried before the bare-digits one.
# Ambulance ritmeldingen write the type in lower case or capitalised ("ambu 16-107", "Ambu")
_VEHICLE_WORDS = dict(VEHICLE_CATEGORIES)
for _key, _category in VEHICLE_CATEGORIES.items():
    if _category == "ambulance":
        _VEHICLE_WORDS[_key.lower()] = _category
        _VEHICLE_WORDS[_key.capitalize()] = _category
_VEHICLE_NUMBER_RE = re.compile(
    r"\b("
    + "|".join(re.escape(k) for k in sorted(_VEHICLE_WORDS, key=len, reverse=True))
    + r")\s+(?:-\s+)?(\d{2,4}-\d{3,4}|\d{3,6})\b"
)

# Fallback for ambulance "ritmelding" messages that carry the vehicle number
# without any AMBU/MUG-style prefix right next to it, as a bare 5-digit
# RRNNN token - 2-digit RAV region + 3-digit unit sequence, dash omitted to
# save space in the pager text, e.g. "07205 - Arnhem Rit 264112" is unit
# 07-205 (Gelderland-Midden, which includes Arnhem). Confirmed against
# AmbuMedia's public vehicle registry (ambumedia.nl/07-gelderland-midden),
# which lists 07-205 as an active Mercedes Sprinter mid-complexity-care
# unit. The trailing "Rit <ritnummer>" anchors the match so this doesn't
# misfire on unrelated numbers elsewhere in the body. Reassembled with a
# dash on the way out to match the official RR-NNN format.
_AMBULANCE_UNIT_RE = re.compile(r"\b(\d{2})(\d{3})\s*-\s*[A-Za-zÀ-ÿ'\-]+(?:\s+[A-Za-zÀ-ÿ'\-]+)*\s+Rit\s+\d+\b")


# Fire brigade messages end with the alerted units' numbers, dash omitted:
# "P 1 BNN-01 Ongeval wegvervoer Stationsstraat Scheemda 013693 012933"
# alerts 01-3693 and 01-2933.
_TRAILING_UNITS_RE = re.compile(r"((?:\s+\d{6})+)\s*$")
_FIRE_PRIORITY_RE = re.compile(r"^\s*(?:P\s?[123]|PRIO\s?[123])\b", re.IGNORECASE)

# Vehicle-type tokens written as words in the message text, for messages
# whose found abbreviations aren't at hand (history read back from the database)
_VEHICLE_TOKEN_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(VEHICLE_CATEGORIES, key=len, reverse=True)) + r")\b"
)


def classify(abbreviations: list[dict[str, str]]) -> str:
    """Pick the first recognized vehicle category from a message's found abbreviations."""
    for item in abbreviations:
        category = VEHICLE_CATEGORIES.get(item.get("abbreviation", ""))
        if category:
            return category
    return ""


def classify_text(text: str) -> str:
    """Pick the first vehicle-type token in the message text."""
    match = _VEHICLE_TOKEN_RE.search(text or "")
    return VEHICLE_CATEGORIES[match.group(1)] if match else ""


def is_fire_message(text: str, discipline: str = "") -> bool:
    """Fire brigade message: by its capcodes' discipline or its "P 1"-style priority."""
    return "brandweer" in (discipline or "").lower() or bool(_FIRE_PRIORITY_RE.match(text or ""))


def fire_units(text: str) -> list[str]:
    """Unit numbers listed at the end of a fire brigade message, as "RR-NNNN"."""
    match = _TRAILING_UNITS_RE.search(text or "")
    if not match:
        return []
    # Region codes run 01-25
    return [f"{t[:2]}-{t[2:]}" for t in match.group(1).split() if 1 <= int(t[:2]) <= 25]


_FULL_NUMBER_RE = re.compile(r"\b\d{2}-\d{3,4}\b")


# Vehicle types written out in capcode descriptions ("Ambulance-07-101",
# "Zorgambulance-07-401", "Reddingsboot 01-2616"); only unambiguous words
_DESCRIPTION_CATEGORIES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ambulance|sololance|rapid responder", re.IGNORECASE), "ambulance"),
    (re.compile(r"\breddings?boot\b", re.IGNORECASE), "reddingboot"),
]


def classify_description(text: str) -> str:
    """Vehicle category a capcode description spells out in words, "" if none."""
    for pattern, category in _DESCRIPTION_CATEGORIES:
        if pattern.search(text or ""):
            return category
    return ""


def is_full_number(number: str) -> bool:
    """Region-prefixed vehicle number, e.g. "11-0293" or ambulance "17-106"."""
    return bool(_FULL_NUMBER_RE.fullmatch(number or ""))


def full_numbers(text: str) -> list[str]:
    """Region-prefixed vehicle numbers written in a text."""
    return _FULL_NUMBER_RE.findall(text or "")


def _normalize_number(number: str) -> str:
    # Bare 5-digit ambulance numbers (e.g. "AMBU 18187") are RRNNN - the same
    # 2-digit region + 3-digit sequence scheme as the "<RRNNN> - <plaats> Rit"
    # format below, just written without a dash. Numbers that already contain
    # a dash or aren't 5 digits are left as-is.
    if "-" not in number and len(number) == 5:
        return f"{number[:2]}-{number[2:]}"
    return number


def named_vehicles(text: str) -> list[tuple[str, str]]:
    """(number, category) for each vehicle the text names with its type, e.g. "TS 07-1782"."""
    return [
        (_normalize_number(m.group(2)), _VEHICLE_WORDS[m.group(1)]) for m in _VEHICLE_NUMBER_RE.finditer(text or "")
    ]


def message_units(text: str, discipline: str = "") -> list[str]:
    """All vehicle numbers in a message, in the order they appear."""
    units = [number for number, _ in named_vehicles(text)]
    match = _AMBULANCE_UNIT_RE.search(text or "")
    if match:
        units.append(f"{match.group(1)}-{match.group(2)}")
    if is_fire_message(text, discipline):
        units += fire_units(text)
    return list(dict.fromkeys(units))


def resolve_vehicle(
    text: str,
    discipline: str = "",
    abbreviations: list[dict[str, str]] | None = None,
    known: dict[str, dict] | None = None,
) -> dict:
    """The message's vehicle fields, from the vehicles table (`known`) and the text.

    The shown vehicle is the first unit found in the vehicles table, else the
    first unit the message names with its type, else the first unit (no type).
    A message without any unit number keeps the type code it names, if any.
    """
    known = known or {}
    units = message_units(text, discipline)
    named = dict(named_vehicles(text))
    number = next((u for u in units if u in known), "")
    category = description = ""
    if number:
        category = known[number].get("category", "")
        description = known[number].get("description", "")
    elif named:
        number = next(iter(named))
        category = named[number]
    elif units:
        number = units[0]
    else:
        category = classify(abbreviations) if abbreviations is not None else classify_text(text)
    return {
        "vehicle_category": category,
        "vehicle_number": number,
        "vehicle_units": units,
        "vehicle_description": description,
        "vehicle_icon": icon_for(category),
    }


def extract_vehicle_number(text: str) -> str:
    """Extract a vehicle number following a known vehicle-type token, if present."""
    named = named_vehicles(text)
    if named:
        return named[0][0]
    match = _AMBULANCE_UNIT_RE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return ""


def icon_for(category: str) -> str:
    """Fallback emoji for a vehicle category ("" gives a generic radio icon)."""
    return CATEGORY_ICONS.get(category, DEFAULT_ICON)


# Bundled stock photos (vehicle_images/<name>.webp): freely licensed photos
# from Wikimedia Commons, background removed and licence plates replaced.
# Shown when the user hasn't uploaded a photo for the vehicle or category.
STOCK_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vehicle_images")
STOCK_IMAGES: dict[str, dict[str, str]] = {
    "tankautospuit": {
        "author": "Alf van Beem",
        "license": "CC0",
        "source": "https://commons.wikimedia.org/wiki/File:Volvo_FE_Ziegler_Brandweer_Midden-_en_West_Brabant,_Gilze_en_Rijen.JPG",
    },
    "ladder": {
        "author": "Alf van Beem",
        "license": "CC0",
        "source": "https://commons.wikimedia.org/wiki/File:Ladderwagen_Brandweer_Velsen_Unit_6602.JPG",
    },
    "hulpverlening": {
        "author": "Alf van Beem",
        "license": "CC0",
        "source": "https://commons.wikimedia.org/wiki/File:Brandweer_Kennemerland_Hoofddorp_12-4270_Volvo_FM_Ziegler_foto1.jpg",
    },
    "haakarm": {
        "author": "Alf van Beem",
        "license": "Public domain",
        "source": "https://commons.wikimedia.org/wiki/File:Volvo_FM,_Unit_17-1381_%26_Unit_17-1362,_Gezamenlijke_Brandweer_Kazerne_Prinses_M%C3%A1ximaweg_pic1.JPG",
    },
    "schuimblus": {
        "author": "S.J. de Waard",
        "license": "CC BY-SA 4.0",
        "source": "https://commons.wikimedia.org/wiki/File:Schuimblusaanhanger_17-1865_van_Brandweer_Rotterdam-Rijnmond_(01).jpg",
    },
    "waterongevallen": {
        "author": "Alf van Beem",
        "license": "CC0",
        "source": "https://commons.wikimedia.org/wiki/File:Brandweer_Kennemerland_Hoofddorp_Mercedes_416_BS-GJ-48_car_354_Waterongevallen.JPG",
    },
    "officier": {
        "author": "Alf van Beem",
        "license": "Public domain",
        "source": "https://commons.wikimedia.org/wiki/File:Nissan_Brandweer_Kennemerland_Officier_van_Dienst_unit_12-1293_foto_2.JPG",
    },
    "ambulance": {
        "author": "Joost J. Bakker IJmuiden",
        "license": "CC BY 2.0",
        "source": "https://commons.wikimedia.org/wiki/File:Kennemerland_Mercedes_Benz_Sprinter_Ambulance_12-511.jpg",
    },
    "helikopter": {
        "author": "BoasB112",
        "license": "CC BY-SA 4.0",
        "source": "https://commons.wikimedia.org/wiki/File:Lifeliner_2_na_een_inzet.jpg",
    },
    "reddingboot": {
        "author": "Joost J. Bakker IJmuiden",
        "license": "CC BY 2.0",
        "source": "https://commons.wikimedia.org/wiki/File:KNRM_Christien.jpg",
    },
    "dienstbus": {
        "author": "Alf van Beem",
        "license": "CC0",
        "source": "https://commons.wikimedia.org/wiki/File:VW_Brandweer_35-GTK-7_Zwanenburg.JPG",
    },
    "politie": {
        "author": "Joost J. Bakker",
        "license": "CC BY 2.0",
        "source": "https://commons.wikimedia.org/wiki/File:BMW_X1_Politie.jpg",
    },
}

# Vehicle category -> stock photo
CATEGORY_STOCK_IMAGES: dict[str, str] = {
    "ambulance": "ambulance",
    "helikopter": "helikopter",
    "brandweer_tankautospuit": "tankautospuit",
    "brandweer_ladder": "ladder",
    "brandweer_hoogwerker": "ladder",
    "brandweer_slangenwagen": "hulpverlening",
    "brandweer_waterongevallen": "waterongevallen",
    "brandweer_poederblus": "schuimblus",
    "brandweer_schuimblus": "schuimblus",
    "brandweer_motorspuit": "tankautospuit",
    "brandweer_dompelpomp": "haakarm",
    "brandweer_riv": "officier",
    "brandweer_haakarmbak": "haakarm",
    "brandweer_hulpverlening": "hulpverlening",
    "brandweer_officier": "officier",
    "brandweer_dienstbus": "dienstbus",
    "brandweer_ogs": "dienstbus",
    "politie": "politie",
    "reddingboot": "reddingboot",
}


def stock_image_for(category: str) -> str:
    """Name of the bundled photo for a vehicle category ("" if none fits)."""
    return CATEGORY_STOCK_IMAGES.get(category, "")
