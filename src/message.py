"""P2000 message data model."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class P2000Message:
    """Represents a decoded P2000 message with all enriched data."""

    # Core message data
    timestamp: datetime
    body: str
    raw_message: str

    # Capcodes
    capcodes: list[str] = field(default_factory=list)
    group_id: str = ""

    # Extracted location data
    address: str = ""
    street: str = ""
    city: str = ""
    postalcode: str = ""

    # Capcode database lookups
    region: str = ""
    discipline: str = ""
    location: str = ""
    receivers: str = ""
    remarks: str = ""

    # Priority (1=highest)
    priority: int = 0

    # Geocoding results
    latitude: float | None = None
    longitude: float | None = None
    mapurl: str = ""
    location_accuracy: str = ""  # "street", "city", or empty

    # Distance to the evaluating sensor's center point (km, 2 decimals; "" when not computed)
    distance: float | str = ""

    # Message type (FLEX, POCSAG, etc.)
    message_type: str = "FLEX"

    # Expanded abbreviations found in body
    abbreviations: list[dict[str, str]] = field(default_factory=list)

    # Vehicle/unit identification (best-effort, from a small known abbreviation
    # vocabulary - see vehicle_types.py)
    vehicle_category: str = ""  # e.g. "ambulance", "helikopter", "" if unrecognized
    vehicle_number: str = ""  # the "voertuignummer" token, e.g. "17106" - "" if none found
    vehicle_icon: str = ""  # fallback emoji for vehicle_category, shown until a photo is uploaded

    # Same text as `body`, but with any city abbreviation token (e.g. "SGRAVH")
    # replaced by the resolved full city name (e.g. "'s-Gravenhage") - used for
    # TTS so speech reads the same place name shown in `city`/`address` instead
    # of the raw abbreviation from the pager text. Falls back to `body` when
    # no substitution was needed.
    speakable_body: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.astimezone().isoformat(),
            "body": self.body,
            "raw_message": self.raw_message,
            "capcodes": self.capcodes,
            "group_id": self.group_id,
            "address": self.address,
            "street": self.street,
            "city": self.city,
            "postalcode": self.postalcode,
            "region": self.region,
            "discipline": self.discipline,
            "location": self.location,
            "receivers": self.receivers,
            "remarks": self.remarks,
            "priority": self.priority,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "mapurl": self.mapurl,
            "location_accuracy": self.location_accuracy,
            "distance": self.distance,
            "message_type": self.message_type,
            "abbreviations": self.abbreviations,
            "vehicle_category": self.vehicle_category,
            "vehicle_number": self.vehicle_number,
            "vehicle_icon": self.vehicle_icon,
            "speakable_body": self.speakable_body or self.body,
        }
