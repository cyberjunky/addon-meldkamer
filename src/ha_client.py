"""Home Assistant REST API client for sensor updates."""

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class HAClient:
    """Publishes sensor state to Home Assistant via Supervisor REST API."""

    def __init__(self):
        self._token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._base_url = "http://supervisor/core/api"
        self._session: aiohttp.ClientSession | None = None

    @property
    def available(self) -> bool:
        """Check if Supervisor token is available."""
        return bool(self._token)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """Get the current state of a sensor in Home Assistant.

        Args:
            entity_id: Sensor entity ID (without 'sensor.' prefix).

        Returns the state dict, or None if the entity does not exist yet (404).
        """
        try:
            session = await self._get_session()
            url = f"{self._base_url}/states/sensor.{entity_id}"

            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                logger.error(f"HA API error {resp.status} for sensor.{entity_id}: {text}")
                return None
        except Exception as e:
            logger.error(f"Failed to get state for sensor.{entity_id}: {e}")
            return None

    async def update_sensor(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, Any],
    ) -> bool:
        """Create or update a sensor in Home Assistant.

        Args:
            entity_id: Sensor entity ID (without 'sensor.' prefix).
            state: Sensor state value.
            attributes: Sensor attributes dict.
        """
        try:
            session = await self._get_session()
            url = f"{self._base_url}/states/sensor.{entity_id}"
            payload = {"state": state, "attributes": attributes}

            async with session.post(url, json=payload) as resp:
                if resp.status in (200, 201):
                    logger.debug(f"Updated sensor: sensor.{entity_id}")
                    return True
                else:
                    text = await resp.text()
                    logger.error(f"HA API error {resp.status} for sensor.{entity_id}: {text}")
                    return False
        except Exception as e:
            logger.error(f"Failed to update sensor.{entity_id}: {e}")
            return False

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
