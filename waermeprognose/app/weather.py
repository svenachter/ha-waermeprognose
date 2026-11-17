"""open-meteo client (15-min forecast + hourly history for calibration)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import numpy as np

from .settings import Facade

log = logging.getLogger(__name__)
API = "https://api.open-meteo.com/v1/forecast"
BASE_VARS = "temperature_2m,relative_humidity_2m,wind_speed_10m,shortwave_radiation"


@dataclass
class WeatherSeries:
    times: list[datetime]                 # slot start (UTC)
    temp: np.ndarray
    rh: np.ndarray
    wind: np.ndarray
    ghi: np.ndarray
    gti: dict[str, np.ndarray]
    model_run: str = ""

    def gti_sum(self) -> np.ndarray:
        if not self.gti:
            return np.zeros(len(self.times))
        return np.nansum(np.vstack(list(self.gti.values())), axis=0)


def _arr(block: dict, key: str, n: int) -> np.ndarray:
    vals = block.get(key)
    if vals is None:
        return np.full(n, np.nan)
    return np.array([np.nan if v is None else float(v) for v in vals], dtype=float)


def _to_times(ts: list[int], interval_s: int) -> list[datetime]:
    # open-meteo time stamps mark the END of the averaging interval for radiation
    # and the instant for temperature. We label slots by their start.
    return [datetime.fromtimestamp(t - interval_s, tz=timezone.utc) for t in ts]


def parse_minutely(base: dict, facade_payloads: dict[str, dict]) -> WeatherSeries:
    m = base["minutely_15"]
    n = len(m["time"])
    gti = {}
    for name, payload in facade_payloads.items():
        fm = payload.get("minutely_15", {})
        if fm.get("time") == m["time"]:
            gti[name] = _arr(fm, "global_tilted_irradiance", n)
        else:
            log.warning("GTI time axis for facade %s does not match, ignored", name)
    return WeatherSeries(
        times=_to_times(m["time"], 900),
        temp=_arr(m, "temperature_2m", n),
        rh=_arr(m, "relative_humidity_2m", n),
        wind=_arr(m, "wind_speed_10m", n),
        ghi=_arr(m, "shortwave_radiation", n),
        gti=gti,
        model_run=str(base.get("generationtime_ms", "")),
    )


class OpenMeteo:
    def __init__(self, client: httpx.AsyncClient, lat: float, lon: float,
                 facades: list[Facade], model: str = "best_match"):
        self.client, self.lat, self.lon = client, lat, lon
        self.facades, self.model = facades, model

    def _common(self) -> dict:
        p = {"latitude": self.lat, "longitude": self.lon, "timezone": "UTC",
             "timeformat": "unixtime"}
        if self.model and self.model != "best_match":
            p["models"] = self.model
        return p

    async def _get(self, params: dict) -> dict:
        r = await self.client.get(API, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(data.get("reason", "open-meteo error"))
        return data

    async def forecast_15min(self, horizon_h: int, past_h: int) -> WeatherSeries:
        span = {"forecast_minutely_15": horizon_h * 4 + 8, "past_minutely_15": past_h * 4}
        base = await self._get({**self._common(), **span, "minutely_15": BASE_VARS})
        fac = {}
        for f in self.facades:
            fac[f.name] = await self._get({**self._common(), **span,
                                           "minutely_15": "global_tilted_irradiance",
                                           "tilt": f.tilt, "azimuth": f.azimuth})
        return parse_minutely(base, fac)

    async def history_hourly(self, past_days: int) -> dict[datetime, dict[str, float]]:
        """Hourly temperature + summed GTI for the past days (hour start, UTC)."""
        past_days = int(max(1, min(92, past_days)))
        span = {"past_days": past_days, "forecast_days": 1}
        base = await self._get({**self._common(), **span, "hourly": "temperature_2m"})
        h = base["hourly"]
        times = _to_times(h["time"], 3600)
        temp = _arr(h, "temperature_2m", len(times))
        gti = np.zeros(len(times))
        for f in self.facades:
            d = await self._get({**self._common(), **span, "hourly": "global_tilted_irradiance",
                                 "tilt": f.tilt, "azimuth": f.azimuth})
            gti += np.nan_to_num(_arr(d["hourly"], "global_tilted_irradiance", len(times)))
        return {t: {"temp": float(temp[i]), "gti": float(gti[i])} for i, t in enumerate(times)}
