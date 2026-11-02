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

    # Distance from Home Assistant's home zone (km, 2 decimals; None without coordinates or home zone)
    distance_km: float | None = None

    # Pager protocol
    message_type: str = "FLEX"

    # Expanded abbreviations found in body
    abbreviations: list[dict[str, str]] = field(default_factory=list)

    # Vehicle/unit identification (vehicle numbers in the text, what they are from
    # the vehicles table - see vehicle_types.py)
    vehicle_category: str = ""  # e.g. "ambulance", "helikopter", "" if unknown
    vehicle_number: str = ""  # the vehicle shown, e.g. "17-106" - "" if none found
    vehicle_units: list[str] = field(default_factory=list)  # all vehicle numbers in the message
    vehicle_description: str = ""  # from the vehicles table
    vehicle_icon: str = ""  # fallback emoji for vehicle_category, shown until a photo is uploaded

    # Same text as `body`, but with any city abbreviation token (e.g. "SGRAVH")
    # replaced by the resolved full city name (e.g. "'s-Gravenhage") - used for
    # TTS so speech reads the same place name shown in `city`/`address` instead
    # of the raw abbreviation from the pager text.
    speakable_body: str = ""

    # Spoken form of the message (speakable_body with the TTS replacement rules applied)
    tts: str = ""

    # What the message is about (see message_fields.py): "fire", "traffic", "medical", ... and its label
    incident_type: str = ""
    incident_label: str = ""
    # Reference numbers and codes in the text
    ride_number: str = ""  # ambulance "rit: 194288"
    bon_number: str = ""  # "bon 170616"
    incident_number: str = ""  # "icnum 123456"
    talk_group: str = ""  # meldkamer gespreksgroep, e.g. "BAD-02"
    direct_deployment: bool = False  # "(Directe inzet: ja)"
    # Database row id, set once the message is stored
    id: int | None = None

    # Messages to the same address within the incident window share an incident id
    incident_id: str = ""

    # How often the pager network resent this exact message within the repeat window
    repeats: int = 0

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
            "distance_km": self.distance_km,
            "message_type": self.message_type,
            "abbreviations": self.abbreviations,
            "vehicle_category": self.vehicle_category,
            "vehicle_number": self.vehicle_number,
            "vehicle_units": self.vehicle_units,
            "vehicle_description": self.vehicle_description,
            "vehicle_icon": self.vehicle_icon,
            "tts": self.tts,
            "incident_type": self.incident_type,
            "incident_label": self.incident_label,
            "ride_number": self.ride_number,
            "bon_number": self.bon_number,
            "incident_number": self.incident_number,
            "talk_group": self.talk_group,
            "direct_deployment": self.direct_deployment,
            "id": self.id,
            "incident_id": self.incident_id,
            "repeats": self.repeats,
        }
