"""Configuration management for Meldkamer."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

# P2000 FLEX fixed settings
P2000_FREQUENCY = "169.65M"
P2000_SAMPLE_RATE = 22050
P2000_DECODER = "FLEX"
P2000_NETWORK_NAME = "P2000 FLEX"


@dataclass
class Config:
    """Application configuration."""

    # Receiver settings
    receiver_type: str  # auto, rtl-sdr, hackrf, soapysdr, network
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

    # Sensor publishing via HA REST API
    sensors_enabled: bool = False

    # Sensor definitions (loaded from config)
    sensors: list = None  # List of sensor configs

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

    @classmethod
    def from_environment(cls) -> "Config":
        return cls(
            receiver_type=os.environ.get("P2000_RECEIVER_TYPE", "auto"),
            network_host=os.environ.get("P2000_NETWORK_HOST", ""),
            network_port=int(os.environ.get("P2000_NETWORK_PORT", "1234")),
            opencage_enabled=os.environ.get("P2000_OPENCAGE_ENABLED", "false").lower() == "true",
            opencage_token=os.environ.get("P2000_OPENCAGE_TOKEN", ""),
            log_level=os.environ.get("P2000_LOG_LEVEL", "info"),
            gain=int(os.environ.get("P2000_GAIN", "0")),
            ppm_correction=int(os.environ.get("P2000_PPM", "0")),
            hackrf_lna_gain=int(os.environ.get("P2000_HACKRF_LNA", "32")),
            hackrf_vga_gain=int(os.environ.get("P2000_HACKRF_VGA", "20")),
            hackrf_amp_enable=os.environ.get("P2000_HACKRF_AMP", "false").lower() == "true",
            mqtt_enabled=os.environ.get("P2000_MQTT_ENABLED", "false").lower() == "true",
            mqtt_host=os.environ.get("P2000_MQTT_HOST", ""),
            mqtt_port=int(os.environ.get("P2000_MQTT_PORT", "1883")),
            mqtt_user=os.environ.get("P2000_MQTT_USER", ""),
            mqtt_password=os.environ.get("P2000_MQTT_PASSWORD", ""),
            mqtt_client_id=os.environ.get("P2000_MQTT_CLIENT_ID", "p2000_rtlsdr"),
            mqtt_base_topic=os.environ.get("P2000_MQTT_BASE_TOPIC", "p2000_rtlsdr"),
        )

    @classmethod
    def from_options_file(cls, path: str = "/data/options.json") -> "Config":
        options_path = Path(path)
        if options_path.exists():
            with open(options_path) as f:
                data = json.load(f)

            receiver = data.get("receiver", {})
            geocoding = data.get("geocoding", {})
            mqtt = data.get("mqtt", {})

            return cls(
                receiver_type=receiver.get("type", "auto"),
                network_host=receiver.get("network_host", ""),
                network_port=receiver.get("network_port", 1234),
                opencage_enabled=geocoding.get("enabled", False),
                opencage_token=geocoding.get("opencage_token", ""),
                log_level=data.get("log_level", "info"),
                gain=receiver.get("sdr_gain", 0),
                ppm_correction=receiver.get("ppm_correction", 0),
                hackrf_lna_gain=receiver.get("hackrf_lna_gain", 32),
                hackrf_vga_gain=receiver.get("hackrf_vga_gain", 20),
                hackrf_amp_enable=receiver.get("hackrf_amp_enable", False),
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
        return cls.from_environment()
