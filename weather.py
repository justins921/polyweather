"""
Weather data fetcher.

Uses NOAA/NWS API for US cities and Open-Meteo for international cities.
"""

import logging
import re
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

NWS_HEADERS = {
    "User-Agent": "(PolyweatherBot, polyweather@example.com)",
    "Accept": "application/geo+json",
}


# ── City extraction from market questions ─────────────────────────────────────


def extract_city_from_question(question: str) -> tuple[str, tuple[float, float, str]] | None:
    """
    Try to find a known city in a market question string.

    Returns (city_name, (lat, lon, country_code)) or None.
    """
    q_lower = question.lower()
    # Try longest city names first to match "new york" before "york"
    for city in sorted(config.CITY_COORDS.keys(), key=len, reverse=True):
        # Word-boundary match to avoid partial matches
        pattern = r"\b" + re.escape(city) + r"\b"
        if re.search(pattern, q_lower):
            return city, config.CITY_COORDS[city]
    return None


def is_us_city(country_code: str) -> bool:
    return country_code == "US"


# ── NOAA / NWS fetcher (US cities) ───────────────────────────────────────────


def fetch_nws_forecast(lat: float, lon: float) -> dict[str, Any] | None:
    """
    Fetch forecast data from NOAA/NWS for a US location.

    Returns a dict with keys: forecast, hourly, grid_data, observations.
    """
    result: dict[str, Any] = {}

    # Step 1: resolve lat/lon to grid endpoint
    points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
    try:
        resp = requests.get(points_url, headers=NWS_HEADERS, timeout=20)
        resp.raise_for_status()
        points = resp.json()
    except requests.RequestException as e:
        logger.error("NWS points lookup failed for (%.4f, %.4f): %s", lat, lon, e)
        return None

    props = points.get("properties", {})
    forecast_url = props.get("forecast")
    hourly_url = props.get("forecastHourly")
    grid_url = props.get("forecastGridData")
    stations_url = props.get("observationStations")

    # Step 2: 7-day forecast
    if forecast_url:
        try:
            resp = requests.get(forecast_url, headers=NWS_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            periods = data.get("properties", {}).get("periods", [])
            result["forecast"] = [
                {
                    "name": p.get("name"),
                    "temperature": p.get("temperature"),
                    "temperatureUnit": p.get("temperatureUnit"),
                    "windSpeed": p.get("windSpeed"),
                    "windDirection": p.get("windDirection"),
                    "shortForecast": p.get("shortForecast"),
                    "detailedForecast": p.get("detailedForecast"),
                    "startTime": p.get("startTime"),
                    "endTime": p.get("endTime"),
                }
                for p in periods[:14]  # up to 7 days (day+night)
            ]
        except requests.RequestException as e:
            logger.warning("NWS forecast fetch failed: %s", e)

    # Step 3: Hourly forecast (next 48h)
    if hourly_url:
        try:
            resp = requests.get(hourly_url, headers=NWS_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            periods = data.get("properties", {}).get("periods", [])
            result["hourly"] = [
                {
                    "startTime": p.get("startTime"),
                    "temperature": p.get("temperature"),
                    "temperatureUnit": p.get("temperatureUnit"),
                    "probabilityOfPrecipitation": (
                        p.get("probabilityOfPrecipitation", {}).get("value")
                    ),
                    "windSpeed": p.get("windSpeed"),
                    "shortForecast": p.get("shortForecast"),
                }
                for p in periods[:48]
            ]
        except requests.RequestException as e:
            logger.warning("NWS hourly forecast fetch failed: %s", e)

    # Step 4: Grid data (probability distributions, max/min temps, etc.)
    if grid_url:
        try:
            resp = requests.get(grid_url, headers=NWS_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            grid_props = data.get("properties", {})
            result["grid_data"] = {
                "maxTemperature": _extract_grid_values(grid_props.get("maxTemperature")),
                "minTemperature": _extract_grid_values(grid_props.get("minTemperature")),
                "temperature": _extract_grid_values(grid_props.get("temperature")),
                "probabilityOfPrecipitation": _extract_grid_values(
                    grid_props.get("probabilityOfPrecipitation")
                ),
                "quantitativePrecipitation": _extract_grid_values(
                    grid_props.get("quantitativePrecipitation")
                ),
                "snowfallAmount": _extract_grid_values(
                    grid_props.get("snowfallAmount")
                ),
            }
        except requests.RequestException as e:
            logger.warning("NWS grid data fetch failed: %s", e)

    # Step 5: Current observations from nearest station
    if stations_url:
        try:
            resp = requests.get(stations_url, headers=NWS_HEADERS, timeout=20)
            resp.raise_for_status()
            stations = resp.json().get("features", [])
            if stations:
                station_id = stations[0]["properties"]["stationIdentifier"]
                obs_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
                resp = requests.get(obs_url, headers=NWS_HEADERS, timeout=20)
                resp.raise_for_status()
                obs_props = resp.json().get("properties", {})
                result["current_observation"] = {
                    "station": station_id,
                    "temperature_c": _safe_value(obs_props.get("temperature")),
                    "humidity": _safe_value(obs_props.get("relativeHumidity")),
                    "wind_speed_kmh": _safe_value(obs_props.get("windSpeed")),
                    "description": obs_props.get("textDescription"),
                    "timestamp": obs_props.get("timestamp"),
                }
        except requests.RequestException as e:
            logger.warning("NWS observation fetch failed: %s", e)

    if not result:
        return None

    result["source"] = "NWS"
    return result


def _extract_grid_values(grid_field: dict | None) -> list[dict] | None:
    """Extract time-series values from a NWS grid data field."""
    if not grid_field:
        return None
    uom = grid_field.get("uom", "")
    values = grid_field.get("values", [])
    return [
        {
            "validTime": v.get("validTime"),
            "value": v.get("value"),
            "uom": uom,
        }
        for v in values[:50]  # cap to keep prompt size manageable
    ]


def _safe_value(obs_field: dict | None) -> float | None:
    """Extract numeric value from a NWS observation measurement."""
    if obs_field and isinstance(obs_field, dict):
        return obs_field.get("value")
    return None


# ── Open-Meteo fetcher (international cities) ────────────────────────────────


def fetch_open_meteo_forecast(lat: float, lon: float) -> dict[str, Any] | None:
    """Fetch forecast from Open-Meteo for non-US locations."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation_probability,precipitation,windspeed_10m",
        "daily": (
            "temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,precipitation_probability_max"
        ),
        "timezone": "auto",
        "forecast_days": 7,
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error("Open-Meteo fetch failed for (%.4f, %.4f): %s", lat, lon, e)
        return None

    result: dict[str, Any] = {"source": "Open-Meteo"}

    # Daily summary
    daily = data.get("daily", {})
    if daily:
        dates = daily.get("time", [])
        result["daily"] = [
            {
                "date": dates[i] if i < len(dates) else None,
                "max_temp_c": _safe_list(daily.get("temperature_2m_max"), i),
                "min_temp_c": _safe_list(daily.get("temperature_2m_min"), i),
                "precip_sum_mm": _safe_list(daily.get("precipitation_sum"), i),
                "precip_prob_max": _safe_list(daily.get("precipitation_probability_max"), i),
            }
            for i in range(len(dates))
        ]

    # Hourly (cap at 72h to keep prompt manageable)
    hourly = data.get("hourly", {})
    if hourly:
        times = hourly.get("time", [])
        result["hourly"] = [
            {
                "time": times[i] if i < len(times) else None,
                "temp_c": _safe_list(hourly.get("temperature_2m"), i),
                "precip_prob": _safe_list(hourly.get("precipitation_probability"), i),
                "precip_mm": _safe_list(hourly.get("precipitation"), i),
                "wind_kmh": _safe_list(hourly.get("windspeed_10m"), i),
            }
            for i in range(min(len(times), 72))
        ]

    return result


def _safe_list(lst: list | None, idx: int) -> Any:
    if lst and idx < len(lst):
        return lst[idx]
    return None


# ── Unified entry point ──────────────────────────────────────────────────────


def fetch_weather_for_city(
    city_name: str, lat: float, lon: float, country_code: str
) -> dict[str, Any] | None:
    """Fetch weather data for a city, choosing the right API."""
    logger.info("Fetching weather for %s (%.4f, %.4f) [%s]", city_name, lat, lon, country_code)

    if is_us_city(country_code):
        data = fetch_nws_forecast(lat, lon)
        # Fall back to Open-Meteo if NWS fails
        if data is None:
            logger.warning("NWS failed for %s, falling back to Open-Meteo", city_name)
            data = fetch_open_meteo_forecast(lat, lon)
    else:
        data = fetch_open_meteo_forecast(lat, lon)

    if data:
        data["city"] = city_name
        data["lat"] = lat
        data["lon"] = lon
        data["country_code"] = country_code

    return data


def fetch_weather_for_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """
    Given a market dict, extract the city from the question and fetch weather.

    Returns weather data dict or None if city can't be identified.
    """
    question = market.get("question", "") + " " + market.get("event_title", "")
    match = extract_city_from_question(question)
    if not match:
        logger.info("Could not extract city from question: %s", market.get("question", ""))
        return None

    city_name, (lat, lon, country_code) = match
    return fetch_weather_for_city(city_name, lat, lon, country_code)
