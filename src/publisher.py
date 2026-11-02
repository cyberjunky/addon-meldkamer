"""Publishes messages and receiver health to Home Assistant (REST API and MQTT)."""

import asyncio
import logging

from .config import Config
from .ha_client import HAClient, HAClientError
from .incidents import Incident
from .message import P2000Message
from .mqtt_client import MQTTClient
from .sensor_manager import SensorConfig, SensorManager

logger = logging.getLogger(__name__)

MESSAGE_EVENT = "meldkamer_message"
ESCALATION_EVENT = "meldkamer_incident_escalated"
RECEIVER_ONLINE_ENTITY = "binary_sensor.meldkamer_receiver_online"
MESSAGES_PER_HOUR_ENTITY = "sensor.meldkamer_messages_per_hour"


class Publisher:
    """Sensor states, events and speaker announcements for Home Assistant."""

    def __init__(self, config: Config, ha_client: HAClient, sensor_manager: SensorManager):
        self.config = config
        self.ha_client = ha_client
        self.sensor_manager = sensor_manager
        self.mqtt_client: MQTTClient | None = MQTTClient(config) if config.mqtt_enabled else None
        # Announcements play one after another without holding up message processing
        self._announcements: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
        self._tasks: list[asyncio.Task] = []
        # Sensors that already got a message, so registration won't reset them to "Waiting for data..."
        self._published: set[str] = set()
        # The dashboard's path in Home Assistant, for the dashboard_url in events (read from the Supervisor at start)
        self.dashboard_path = ""

        if config.sensors_enabled and not ha_client.available and not config.mqtt_enabled:
            logger.warning("SUPERVISOR_TOKEN not found and MQTT disabled - sensors will not be published anywhere")
        if config.announce_enabled and not any(s.announce for s in sensor_manager.sensors):
            logger.warning("Announcements are configured, but no sensor has 'announce: true'")

    @property
    def _mqtt_active(self) -> bool:
        return bool(self.mqtt_client and self.mqtt_client.enabled)

    async def start(self) -> None:
        if self.mqtt_client:
            await self.mqtt_client.start()
        if self.ha_client.available:
            slug = await self.ha_client.get_addon_slug()
            if slug:
                self.dashboard_path = f"/hassio/ingress/{slug}"
        if self.config.sensors_enabled and self.sensor_manager.sensors and self.ha_client.available:
            self._tasks.append(asyncio.create_task(self._register_sensors()))
        if self.config.announce_enabled and self.ha_client.available:
            self._tasks.append(asyncio.create_task(self._announce_worker()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.mqtt_client:
            await self.mqtt_client.stop()

    async def _register_sensors(self) -> None:
        """Pre-create sensors in Home Assistant so they appear immediately.

        Doesn't wipe the state/attributes of sensors that already exist, and
        retries with backoff while Home Assistant is unreachable (e.g. Core
        still starting, Supervisor answering 502).
        """
        pending = list(self.sensor_manager.sensors)
        logger.info(f"Registering {len(pending)} sensor(s) in Home Assistant...")
        backoff = 5
        while True:
            failed = []
            for sensor in pending:
                if sensor.entity_id in self._published:
                    continue
                try:
                    existing = await self.ha_client.get_state(f"sensor.{sensor.entity_id}")
                except HAClientError:
                    # Unknown whether it exists - don't overwrite a possibly existing state
                    failed.append(sensor)
                    continue
                if existing is not None:
                    logger.debug(f"sensor.{sensor.entity_id} already exists, keeping current state")
                    continue
                if sensor.entity_id in self._published:
                    continue  # A message arrived while checking
                created = await self.ha_client.set_state(
                    f"sensor.{sensor.entity_id}",
                    state="Waiting for data...",
                    attributes={"friendly_name": sensor.name, "icon": sensor.icon},
                )
                if not created:
                    failed.append(sensor)
            if not failed:
                logger.info("Sensors registered in Home Assistant")
                return
            pending = failed
            logger.warning(f"Could not register {len(pending)} sensor(s) in Home Assistant, retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def publish_message(
        self,
        msg: P2000Message,
        sensors: list[SensorConfig],
        opencage_info: str,
        incident: Incident | None,
        escalation: list[str],
    ) -> None:
        """Publish a new message to its matching sensors, the event bus and the speakers."""
        if self.config.sensors_enabled:
            for sensor in sensors:
                await self._publish_sensor(sensor, msg, opencage_info)

        if self.config.ha_events and self.ha_client.available:
            event = {**msg.to_dict(), "sensors": [s.name for s in sensors]}
            if self.dashboard_path and msg.id is not None:
                event["dashboard_url"] = f"{self.dashboard_path}#msg={msg.id}"
            await self.ha_client.fire_event(MESSAGE_EVENT, event)
            if escalation:
                await self.ha_client.fire_event(
                    ESCALATION_EVENT, {**incident.to_dict(), "reasons": escalation, "body": msg.body}
                )

        if self.config.announce_enabled and any(s.announce for s in sensors) and msg.tts:
            if self.config.quiet_now():
                logger.debug(f"Quiet hours ({self.config.announce_quiet_hours}): not announcing {msg.body[:40]}")
                return
            try:
                self._announcements.put_nowait(msg.tts)
            except asyncio.QueueFull:
                logger.warning("Too many announcements queued, skipping one")

    async def _publish_sensor(self, sensor: SensorConfig, msg: P2000Message, opencage_info: str) -> None:
        zone_distance = self.sensor_manager.zone_distance(msg, sensor)
        self._published.add(sensor.entity_id)
        if self.ha_client.available:
            attributes = msg.to_dict()
            attributes["friendly_name"] = sensor.name
            attributes["icon"] = sensor.icon
            attributes["distance"] = zone_distance
            attributes["opencage"] = opencage_info
            # HA rejects states over 255 chars; the full spoken text is in the tts attribute
            await self.ha_client.set_state(f"sensor.{sensor.entity_id}", msg.tts[:255], attributes)
        if self._mqtt_active:
            self.mqtt_client.publish_for_sensor(sensor, msg, zone_distance, opencage_info)

    async def _announce_worker(self) -> None:
        players = self.config.announce_media_players
        while True:
            text = await self._announcements.get()
            data = {"entity_id": self.config.announce_tts_entity, "media_player_entity_id": players, "message": text}
            if self.config.announce_language:
                data["language"] = self.config.announce_language
            await self.ha_client.call_service("tts", "speak", data)

    async def publish_health(self, online: bool, messages_per_hour: int) -> None:
        """Publish whether the receiver is online and how many messages it decoded in the last hour."""
        if not self.config.sensors_enabled:
            return
        if self.ha_client.available:
            await self.ha_client.set_state(
                RECEIVER_ONLINE_ENTITY,
                "on" if online else "off",
                {"friendly_name": "Meldkamer receiver online", "device_class": "connectivity"},
            )
            await self.ha_client.set_state(
                MESSAGES_PER_HOUR_ENTITY,
                str(messages_per_hour),
                {
                    "friendly_name": "Meldkamer messages per hour",
                    "icon": "mdi:message-flash",
                    "unit_of_measurement": "msg/h",
                    "state_class": "measurement",
                },
            )
        if self._mqtt_active:
            self.mqtt_client.publish_health(online, messages_per_hour)
