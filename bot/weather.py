"""Weather for the morning brief (plan Section 7).

Open-Meteo needs no API key and no account, which is why it's the default —
one less credential to manage for a self-hosted single-user bot.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from bot.errors import AssistantError, E

ENDPOINT = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class Forecast:
    high_c: float
    low_c: float
    feels_like_high_c: float
    feels_like_low_c: float
    wind_kph: float
    precipitation_chance_pct: int | None = None

    def summary(self) -> str:
        """One line, the way it appears in the brief."""
        parts = [f"{self.low_c:.0f} to {self.high_c:.0f}°C"]
        # Only mention "feels like" when it actually differs enough to matter.
        if abs(self.feels_like_high_c - self.high_c) >= 3:
            parts.append(f"feels like {self.feels_like_high_c:.0f}°")
        parts.append(f"wind {self.wind_kph:.0f} km/h")
        if self.precipitation_chance_pct is not None and self.precipitation_chance_pct >= 30:
            parts.append(f"{self.precipitation_chance_pct}% chance of precipitation")
        return ", ".join(parts)


def fetch(
    latitude: float, longitude: float, timezone: str, *, timeout: float = 10.0
) -> Forecast:
    """Today's forecast. Raises AssistantError(E205) on any failure."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone,
        "forecast_days": 1,
        "daily": ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "apparent_temperature_max",
                "apparent_temperature_min",
                "wind_speed_10m_max",
                "precipitation_probability_max",
            ]
        ),
    }
    try:
        response = httpx.get(ENDPOINT, params=params, timeout=timeout)
        response.raise_for_status()
        daily = response.json()["daily"]
        return Forecast(
            high_c=daily["temperature_2m_max"][0],
            low_c=daily["temperature_2m_min"][0],
            feels_like_high_c=daily["apparent_temperature_max"][0],
            feels_like_low_c=daily["apparent_temperature_min"][0],
            wind_kph=daily["wind_speed_10m_max"][0],
            precipitation_chance_pct=(daily.get("precipitation_probability_max") or [None])[0],
        )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise AssistantError(
            E.WEATHER, "Couldn't get the forecast.", cause=exc
        ) from exc
