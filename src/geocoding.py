"""OpenCage geocoding client with rate limiting and caching."""

import logging
from datetime import UTC, datetime

import requests

from .database import Database

logger = logging.getLogger(__name__)


class Geocoder:
    """OpenCage geocoding with database cache."""

    API_URL = "https://api.opencagedata.com/geocode/v1/json"

    def __init__(self, token: str, database: Database):
        self.token = token
        self.database = database
        self.enabled = bool(token)
        self.rate_limited = False
        self.rate_remaining = 9999  # Unknown until first API response
        self.rate_reset: int | None = None  # Reset epoch reported by OpenCage
        self.geocoded = False  # Whether the last geocode() call produced coordinates
        self._rate_limited_on = None  # UTC date when the rate limit was hit

    def geocode(self, address: str) -> dict | None:
        """
        Geocode an address. Returns dict with lat, lon, mapurl or None.

        First checks database cache, then calls OpenCage API if needed.
        """
        logger.debug(f"Geocode request: enabled={self.enabled}, address='{address}'")
        self.geocoded = False
        if not self.enabled or not address:
            logger.debug(f"Geocode skipped: enabled={self.enabled}, has_address={bool(address)}")
            return None

        # Check cache first
        cached = self.database.find_geocode(address)
        if cached:
            lat = float(cached["latitude"])
            lon = float(cached["longitude"])
            # Validate cached coords are within Netherlands
            if 50.75 <= lat <= 53.55 and 3.36 <= lon <= 7.21:
                logger.debug(f"Geocode cache hit: {address}")
                self.geocoded = True
                return {"latitude": lat, "longitude": lon, "mapurl": cached["mapurl"]}
            else:
                logger.warning(f"Cached geocode outside Netherlands, ignoring: {lat}, {lon}")

        # Clear the rate limit flag if the daily quota has reset (midnight UTC)
        self._check_rate_limit_reset()

        # Skip if rate limited
        if self.rate_limited:
            return None

        # Call OpenCage API
        return self._call_api(address)

    def _call_api(self, address: str) -> dict | None:
        """Call OpenCage API."""
        try:
            response = requests.get(
                self.API_URL,
                params={"q": address, "key": self.token, "limit": 1, "country": "nl", "language": "nl"},
                timeout=5,
            )

            if response.status_code == 402 or response.status_code == 429:
                logger.warning("OpenCage rate limit exceeded (daily quota) - geocoding paused until quota resets")
                self.rate_limited = True
                self._rate_limited_on = datetime.now(UTC).date()
                # The error response may still carry rate info (remaining/reset)
                try:
                    rate = response.json().get("rate")
                    if rate:
                        self.rate_remaining = rate.get("remaining", 0)
                        self.rate_reset = rate.get("reset")
                except ValueError:
                    pass
                return None

            if response.status_code == 401:
                logger.error(
                    "OpenCage API key unauthorized (HTTP 401) - check opencage_token; "
                    "geocoding permanently disabled until addon restart"
                )
                self.enabled = False
                return None

            response.raise_for_status()
            data = response.json()

            if data.get("rate"):
                self.rate_remaining = data["rate"].get("remaining", 9999)
                self.rate_reset = data["rate"].get("reset")

            if data.get("total_results", 0) == 0:
                logger.debug(f"No geocode results for: {address}")
                return None

            result = data["results"][0]
            components = result.get("components", {})
            geometry = result.get("geometry", {})

            # Verify result is in Netherlands (via components)
            city = (
                components.get("city")
                or components.get("town")
                or components.get("village")
                or components.get("municipality")
            )

            # Accept result if it's in Netherlands and looks reasonable
            country = components.get("country_code", "").lower()
            if country != "nl":
                logger.debug(f"Geocode not in Netherlands: {country}")
                return None

            latitude = geometry.get("lat")
            longitude = geometry.get("lng")
            mapurl = result.get("annotations", {}).get("OSM", {}).get("url", "")

            # Validate coordinates are within Netherlands bounding box
            # Netherlands bounds: lat 50.75 to 53.55, lon 3.36 to 7.21
            NL_LAT_MIN, NL_LAT_MAX = 50.75, 53.55
            NL_LON_MIN, NL_LON_MAX = 3.36, 7.21

            if latitude and longitude:
                if not (NL_LAT_MIN <= latitude <= NL_LAT_MAX and NL_LON_MIN <= longitude <= NL_LON_MAX):
                    logger.warning(f"Geocode result outside Netherlands: {latitude}, {longitude} for '{address}'")
                    return None

                # Store in cache
                self.database.store_geocode(
                    query=address,
                    datatype=components.get("_type", ""),
                    latitude=latitude,
                    longitude=longitude,
                    postalcode=components.get("postcode", ""),
                    street=components.get("road", ""),
                    city=city or "",
                    address=result.get("formatted", ""),
                    mapurl=mapurl,
                )

                self.geocoded = True
                return {"latitude": latitude, "longitude": longitude, "mapurl": mapurl}

        except requests.exceptions.Timeout:
            logger.warning("OpenCage API timeout")
        except requests.exceptions.RequestException as e:
            logger.error(f"OpenCage API error: {e}")
        except (KeyError, IndexError) as e:
            logger.error(f"OpenCage parse error: {e}")

        return None

    def _check_rate_limit_reset(self) -> None:
        """Clear the rate-limit flag once the OpenCage daily quota has reset.

        OpenCage resets the daily quota at midnight UTC. Prefer the reset epoch
        reported by the API; fall back to a new UTC day (checked on each call).
        """
        if not self.rate_limited:
            return

        now = datetime.now(UTC)

        if self.rate_reset and now.timestamp() >= self.rate_reset:
            logger.info("OpenCage rate limit reset (per API reset time) - geocoding resumed")
            self.reset_rate_limit()
            return

        if self._rate_limited_on and now.date() > self._rate_limited_on:
            logger.info("OpenCage rate limit reset (new UTC day) - geocoding resumed")
            self.reset_rate_limit()

    def reset_rate_limit(self) -> None:
        """Reset rate limit flag (quota resets daily at midnight UTC)."""
        self.rate_limited = False
        self.rate_reset = None
        self._rate_limited_on = None
