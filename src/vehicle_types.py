"""Best-effort vehicle/unit classification and voertuignummer extraction.

Maps the small set of vehicle-type abbreviations already in
abbreviation_import.py to a broad category, used to pick a fallback icon and
as a lookup key for user-uploaded vehicle photos (see database.py's
vehicle_photos table). This is deliberately a coarse classification, not a
decode of the official "Nationaal Nummerplan Brandweer Nederland" numbering
scheme - real per-vehicle photos aren't available from any open/licensed
source, so the practical path is category-level defaults plus optional
user-uploaded photos keyed by the exact voertuignummer token.
"""

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
    # Police
    "POL": "politie",
    "AE": "politie",
    "AT": "politie",
    "ME": "politie",
    # Water rescue
    "RB": "reddingboot",
}

# Category -> fallback emoji shown when no user-uploaded photo exists for
# either the exact voertuignummer or the category itself.
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
    "politie": "🚓",
    "reddingboot": "🛟",
}

DEFAULT_ICON = "📻"

# Matches a known vehicle-type token directly followed by a voertuignummer,
# either the official "RR-XXXX" fire brigade format (e.g. "TS 07-1782") or a
# bare run of digits as commonly seen for ambulance units (e.g. "AMBU 17106").
# Longer codes (AL-42, ...) are tried first so e.g. "AL-42" isn't matched as
# bare "AL", and the dashed number form is tried before the bare-digits one.
_VEHICLE_NUMBER_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(k) for k in sorted(VEHICLE_CATEGORIES, key=len, reverse=True))
    + r")\s+(\d{2,4}-\d{3,4}|\d{3,6})\b"
)

# Fallback for ambulance "ritmelding" messages that carry the voertuignummer
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


def classify(abbreviations: list[dict[str, str]]) -> str:
    """Pick the first recognized vehicle category from a message's found abbreviations."""
    for item in abbreviations:
        category = VEHICLE_CATEGORIES.get(item.get("abbreviation", ""))
        if category:
            return category
    return ""


def extract_vehicle_number(text: str) -> str:
    """Extract a voertuignummer following a known vehicle-type token, if present."""
    match = _VEHICLE_NUMBER_RE.search(text)
    if match:
        number = match.group(1)
        # Bare 5-digit ambulance numbers (e.g. "AMBU 18187") are RRNNN - the
        # same 2-digit region + 3-digit sequence scheme confirmed for the
        # "<RRNNN> - <plaats> Rit <ritnummer>" format below, just written
        # without a dash here. Reformat for consistency. Numbers that already
        # contain a dash (fire brigade "RR-XXXX") or aren't 5 digits are left
        # as-is - unverified for those cases.
        if "-" not in number and len(number) == 5:
            number = f"{number[:2]}-{number[2:]}"
        return number
    match = _AMBULANCE_UNIT_RE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return ""


def icon_for(category: str) -> str:
    """Fallback emoji for a vehicle category ("" gives a generic radio icon)."""
    return CATEGORY_ICONS.get(category, DEFAULT_ICON)
