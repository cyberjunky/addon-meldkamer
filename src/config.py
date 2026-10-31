"""Configuration management for Meldkamer."""

import json
from dataclasses import dataclass, field
from pathlib import Path

# P2000 FLEX fixed settings
P2000_FREQUENCY = "169.65M"
P2000_SAMPLE_RATE = 22050
P2000_DECODER = "FLEX"
P2000_NETWORK_NAME = "P2000 FLEX"

OPTIONS_FILE = "/data/options.json"


@dataclass
class Config:
    """Application configuration, read from the add-on options."""

    # Receiver settings
    receiver_type: str  # rtl-sdr, hackrf, soapysdr, network
    network_host: str
    network_port: int

    # Geocoding settings
    opencage_enabled: bool
    opencage_token: str

    # System settings
    log_level: str
    gain: int = 0  # 0 = automatic gain control (RTL-SDR only)
    ppm_correction: int = 0  # Frequency correction in PPM

    # HackRF-specific gain stages
    hackrf_lna_gain: int = 32  # LNA gain 0-40 dB (8 dB steps)
    hackrf_vga_gain: int = 20  # VGA gain 0-62 dB (2 dB steps)
    hackrf_amp_enable: bool = False  # 14dB RF amplifier

    # Power an active antenna/LNA over the coax (rtl-sdr only, rtl_fm -T)
    bias_tee: bool = False

    # The receiver counts as offline after this long without any FLEX frame; the
    # decoder then switches to the backup receiver (or restarts the active one)
    offline_after_minutes: int = 10

    # Optional backup receiver: a remote rtl_tcp server, used when the active one goes quiet
    backup_host: str = ""
    backup_port: int = 1234
    backup_ppm_correction: int = 0

    # Identical messages resent within this window are counted as repeats, not new messages
    repeat_window_seconds: int = 60

    # Messages to the same address within this window are grouped into one incident
    incident_window_minutes: int = 15

    # Fire meldkamer_message / meldkamer_incident_escalated on the Home Assistant event bus
    ha_events: bool = True

    # Announce messages of sensors with `announce: true` on Home Assistant speakers
    announce_tts_entity: str = ""
    announce_media_players: list[str] = field(default_factory=list)
    announce_language: str = ""

    # Only call OpenCage for messages a sensor can match; others use the cache or city-level coordinates
    geocode_only_sensor_matches: bool = False

    # Sensor publishing via HA REST API
    sensors_enabled: bool = False

    # Sensor definitions (loaded from config)
    sensors: list = field(default_factory=list)

    # MQTT publishing (optional, additive to the HA REST API sensors above)
    mqtt_enabled: bool = False
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_client_id: str = "p2000_rtlsdr"
    mqtt_base_topic: str = "p2000_rtlsdr"
    mqtt_ha_autodiscovery: bool = True
    mqtt_ha_autodiscovery_topic: str = "homeassistant"
    mqtt_retain: bool = False
    mqtt_tls_enabled: bool = False
    mqtt_tls_ca: str = "/etc/ssl/certs/ca-certificates.crt"
    mqtt_tls_cert: str = ""
    mqtt_tls_keyfile: str = ""
    mqtt_tls_insecure: bool = True

    @property
    def frequency(self) -> str:
        return P2000_FREQUENCY

    @property
    def sample_rate(self) -> int:
        return P2000_SAMPLE_RATE

    @property
    def decoder(self) -> str:
        return P2000_DECODER

    @property
    def network_name(self) -> str:
        return P2000_NETWORK_NAME

    @property
    def announce_enabled(self) -> bool:
        return bool(self.announce_tts_entity and self.announce_media_players)

    @classmethod
    def from_options_file(cls, path: str = OPTIONS_FILE) -> "Config":
        try:
            with open(Path(path)) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError(f"Cannot read add-on options from {path}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"Invalid add-on options in {path}: expected a JSON object")

        receiver = data.get("receiver", {})
        geocoding = data.get("geocoding", {})
        announce = data.get("announce", {})
        mqtt = data.get("mqtt", {})

        return cls(
            receiver_type=receiver.get("type", "rtl-sdr"),
            network_host=receiver.get("network_host", ""),
            network_port=receiver.get("network_port", 1234),
            opencage_enabled=geocoding.get("enabled", False),
            opencage_token=geocoding.get("opencage_token", ""),
            geocode_only_sensor_matches=geocoding.get("only_sensor_matches", False),
            log_level=data.get("log_level", "info"),
            gain=receiver.get("sdr_gain", 0),
            ppm_correction=receiver.get("ppm_correction", 0),
            hackrf_lna_gain=receiver.get("hackrf_lna_gain", 32),
            hackrf_vga_gain=receiver.get("hackrf_vga_gain", 20),
            hackrf_amp_enable=receiver.get("hackrf_amp_enable", False),
            bias_tee=receiver.get("bias_tee", False),
            offline_after_minutes=receiver.get("offline_after_minutes", 10),
            backup_host=receiver.get("backup_host", ""),
            backup_port=receiver.get("backup_port", 1234),
            backup_ppm_correction=receiver.get("backup_ppm_correction", 0),
            repeat_window_seconds=data.get("repeat_window_seconds", 60),
            incident_window_minutes=data.get("incident_window_minutes", 15),
            ha_events=data.get("ha_events", True),
            announce_tts_entity=announce.get("tts_entity", ""),
            announce_media_players=[p.strip() for p in announce.get("media_players", "").split(",") if p.strip()],
            announce_language=announce.get("language", ""),
            sensors_enabled=data.get("sensors_enabled", False),
            sensors=data.get("sensors", []),
            mqtt_enabled=mqtt.get("enabled", False),
            mqtt_host=mqtt.get("host", ""),
            mqtt_port=mqtt.get("port", 1883),
            mqtt_user=mqtt.get("user", ""),
            mqtt_password=mqtt.get("password", ""),
            mqtt_client_id=mqtt.get("client_id", "p2000_rtlsdr"),
            mqtt_base_topic=mqtt.get("base_topic", "p2000_rtlsdr"),
            mqtt_ha_autodiscovery=mqtt.get("ha_autodiscovery", True),
            mqtt_ha_autodiscovery_topic=mqtt.get("ha_autodiscovery_topic", "homeassistant"),
            mqtt_retain=mqtt.get("retain", False),
            mqtt_tls_enabled=mqtt.get("tls_enabled", False),
            mqtt_tls_ca=mqtt.get("tls_ca", "/etc/ssl/certs/ca-certificates.crt"),
            mqtt_tls_cert=mqtt.get("tls_cert", ""),
            mqtt_tls_keyfile=mqtt.get("tls_keyfile", ""),
            mqtt_tls_insecure=mqtt.get("tls_insecure", True),
        )
