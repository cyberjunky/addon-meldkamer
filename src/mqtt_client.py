"""MQTT publisher for P2000 sensor updates.

This is an optional, additive publishing path alongside the HA REST API
sensors (ha_client.py / sensor_manager.py).

Topic layout:
- The availability topic uses the configurable base_topic: "{base_topic}/status".
- State, attribute and HA-discovery-config topics are published under the
  fixed prefix "homeassistant/sensor/p2000_rtlsdr/...", independent of the
  configured base_topic/ha_autodiscovery_topic.
- The attribute JSON payload uses descriptive key names (with spaces, e.g.
  "time received") for readability in Lovelace cards/templates.
"""

import asyncio
import json
import logging
import os
import ssl
import time
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt

from .config import Config
from .message import P2000Message
from .sensor_manager import SensorConfig

logger = logging.getLogger(__name__)

# Fixed topic prefix, independent of the configured base_topic / ha_autodiscovery_topic.
_MQTT_TOPIC_PREFIX = "homeassistant/sensor/p2000_rtlsdr"


class MQTTClient:
    """Publishes P2000 sensor updates to MQTT."""

    def __init__(self, config: Config):
        self.config = config
        self.enabled = config.mqtt_enabled
        self._client: mqtt.Client | None = None
        self._connected = False
        self._discovered: set = set()
        self.base_topic = config.mqtt_base_topic or "p2000_rtlsdr"
        self.availability_topic = f"{self.base_topic}/status"

    async def start(self) -> None:
        """Connect to the MQTT broker. No-op when MQTT is disabled."""
        if not self.enabled:
            return

        host = self.config.mqtt_host
        port = self.config.mqtt_port
        user = self.config.mqtt_user
        password = self.config.mqtt_password

        if not host:
            fetched = await self._fetch_supervisor_mqtt()
            if fetched:
                host = fetched.get("host") or host
                port = fetched.get("port") or port
                user = fetched.get("user") or user
                password = fetched.get("password") or password

        if not host:
            logger.warning("MQTT enabled but no broker host configured or discovered - MQTT disabled")
            self.enabled = False
            return

        client_id = self.config.mqtt_client_id or "p2000_rtlsdr"
        self._client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        if user:
            self._client.username_pw_set(user, password or None)

        if self.config.mqtt_tls_enabled:
            cert_reqs = ssl.CERT_NONE if self.config.mqtt_tls_insecure else ssl.CERT_REQUIRED
            self._client.tls_set(
                ca_certs=self.config.mqtt_tls_ca or None,
                certfile=self.config.mqtt_tls_cert or None,
                keyfile=self.config.mqtt_tls_keyfile or None,
                cert_reqs=cert_reqs,
            )
            if self.config.mqtt_tls_insecure:
                self._client.tls_insecure_set(True)

        self._client.will_set(self.availability_topic, payload="offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        try:
            self._client.connect_async(host, int(port or 1883), keepalive=60)
            self._client.loop_start()
            logger.info(f"MQTT client connecting to {host}:{port} (base_topic={self.base_topic})")
        except Exception as e:
            logger.error(f"Failed to start MQTT client: {e}")
            self.enabled = False

    async def _fetch_supervisor_mqtt(self) -> dict[str, Any] | None:
        """Ask Supervisor for the MQTT add-on's broker details."""
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            return None
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    "http://supervisor/services/mqtt",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp,
            ):
                if resp.status != 200:
                    return None
                data = (await resp.json()).get("data", {})
                return {
                    "host": data.get("host"),
                    "port": data.get("port"),
                    "user": data.get("username"),
                    "password": data.get("password"),
                }
        except Exception as e:
            logger.warning(f"Could not fetch MQTT service info from Supervisor: {e}")
            return None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            logger.info("MQTT connected")
            client.publish(self.availability_topic, payload="online", qos=0, retain=True)
        else:
            logger.error(f"MQTT connect failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            logger.warning(f"MQTT disconnected unexpectedly (rc={rc})")

    def publish_for_sensor(
        self,
        sensor: SensorConfig,
        msg: P2000Message,
        tts_text: str,
        opencage_info: str,
    ) -> None:
        """Publish state/attributes (and HA discovery, once) for one matched sensor."""
        if not self.enabled or not self._client or not self._connected:
            return

        sensor_key = sensor.mqtt_id or sensor.entity_id
        self._ensure_discovery(sensor, sensor_key)

        state_topic = f"{_MQTT_TOPIC_PREFIX}/{sensor_key}/state"
        attribute_topic = f"{_MQTT_TOPIC_PREFIX}/{sensor_key}/attributes"

        attributes = {
            "time received": time.strftime("%a %b %d %H:%M:%S %Y", msg.timestamp.astimezone().timetuple()),
            "group id": msg.group_id,
            "receivers": msg.receivers,
            "capcodes": msg.capcodes,
            "priority": msg.priority,
            "disciplines": msg.discipline,
            "raw message": msg.raw_message,
            "region": msg.region,
            "location": msg.location,
            "postal code": msg.postalcode,
            "city": msg.city,
            "address": msg.address,
            "street": msg.street,
            "remarks": msg.remarks,
            "longitude": msg.longitude,
            "latitude": msg.latitude,
            "opencage": opencage_info,
            "mapurl": msg.mapurl,
            "distance": msg.distance,
            "tts": tts_text,
        }

        retain = self.config.mqtt_retain
        try:
            self._client.publish(attribute_topic, payload=json.dumps(attributes), retain=retain)
            self._client.publish(state_topic, payload=msg.body, retain=retain)
        except Exception as e:
            logger.error(f"Failed to publish MQTT message for sensor '{sensor.name}': {e}")

    def _ensure_discovery(self, sensor: SensorConfig, sensor_key: str) -> None:
        """Send the HA MQTT discovery config payload once per sensor."""
        if not self.config.mqtt_ha_autodiscovery or sensor_key in self._discovered:
            return

        autodiscovery_topic = self.config.mqtt_ha_autodiscovery_topic or "homeassistant"
        discover_topic = f"{autodiscovery_topic}/sensor/p2000_rtlsdr/{sensor_key}/config"
        payload = {
            "name": sensor.name,
            "unique_id": str(sensor_key),
            "icon": sensor.icon,
            "availability_topic": self.availability_topic,
            "force_update": True,
            "state_topic": f"{_MQTT_TOPIC_PREFIX}/{sensor_key}/state",
            "json_attributes_topic": f"{_MQTT_TOPIC_PREFIX}/{sensor_key}/attributes",
        }
        try:
            self._client.publish(discover_topic, payload=json.dumps(payload), qos=1, retain=True)
            self._discovered.add(sensor_key)
            logger.debug(f"Sent MQTT autodiscovery for sensor '{sensor.name}' ({sensor_key})")
        except Exception as e:
            logger.error(f"Failed to publish MQTT discovery for sensor '{sensor.name}': {e}")

    async def stop(self) -> None:
        """Announce offline and disconnect."""
        if not self._client:
            return
        try:
            self._client.publish(self.availability_topic, payload="offline", qos=1, retain=True)
            await asyncio.sleep(0.2)  # give the network loop a moment to flush
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as e:
            logger.debug(f"Error stopping MQTT client: {e}")
