"""Orchestration: fetch inputs, run model, persist, publish, calibrate, backtest."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from . import dhw as dhw_mod
from .calibration import fit_flow_offset, fit_heat_model
from .ha import HomeAssistant
from .model import CurvePoints, ForecastInputs, ema, run
from .settings import ModelParams, Settings
from .store import Store
from .weather import OpenMeteo, WeatherSeries

log = logging.getLogger(__name__)
VERSION = "0.1.2"
CALIBRATED_FIELDS = ("q_base_kw", "q_slope_kw_per_k", "knee_slope_kw_per_k",
                     "solar_gain_kw_per_wm2", "uncertainty", "flow_offset_hp")
BIAS_DECAY_H = 6.0
WEATHER_MAX_AGE = timedelta(hours=6)


def _r(x, nd=3):
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    x = float(x)
    return None if not np.isfinite(x) else round(x, nd)


class ForecastService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient,
                 ha: HomeAssistant | None = None, store: Store | None = None):
        self.s = settings
        self.client = client
        self.ha = ha or HomeAssistant(client)
        self.store = store or Store(Path(settings.data_dir) / "waermeprognose.sqlite")
        self.params: ModelParams = replace(settings.model)
        self.tz = ZoneInfo("Europe/Berlin")
        self.lat = settings.latitude
        self.lon = settings.longitude
        self.profile = dhw_mod.DhwProfile()
        self.result: dict | None = None
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self.calibration_info: dict | None = None
        self._weather: WeatherSeries | None = None
        self._weather_at: datetime | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- setup
    async def startup(self):
        try:
            cfg = await self.ha.config()
            self.tz = ZoneInfo(cfg.get("time_zone") or "Europe/Berlin")
            self.lat = self.lat if self.lat is not None else cfg.get("latitude")
            self.lon = self.lon if self.lon is not None else cfg.get("longitude")
        except Exception as e:  # noqa: BLE001
            log.warning("HA-Konfiguration nicht lesbar (%s); nutze Optionen", e)
        if self.lat is None or self.lon is None:
            raise RuntimeError("Keine Koordinaten: latitude/longitude in den Optionen setzen")
        saved = self.store.latest_params()
        if saved:
            data, info, created = saved
            self.params = replace(self.params, **{k: data[k] for k in CALIBRATED_FIELDS if k in data})
            self.calibration_info = {**info, "created": created.isoformat()}
            log.info("Kalibrierte Parameter vom %s geladen", created)
        await self.refresh_dhw_profile()

    @property
    def om(self) -> OpenMeteo:
        return OpenMeteo(self.client, self.lat, self.lon, self.s.facades, self.s.weather_model)

    # ---------------------------------------------------------------- DHW
    async def refresh_dhw_profile(self):
        acc = dhw_mod._Accumulator()
        src = self.s.dhw.source
        log_days: set = set()
        if src in ("logfile", "both"):
            acc = await asyncio.to_thread(dhw_mod.accumulate_trendlogs, self.s.dhw.logfile_glob, self.tz, acc)
            log_days = {d for d, _ in acc.days()}
        if src in ("ha_statistics", "both"):
            try:
                end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                start = end - timedelta(days=self.s.dhw.history_days)
                st = await self.ha.statistics([self.s.entities.dhw_thermal], start, end, types=("change",))
                rows = [(r["start"], r.get("change")) for r in st.get(self.s.entities.dhw_thermal, [])]
                acc = dhw_mod.accumulate_hourly(rows, self.tz, acc, skip_days=log_days)
            except Exception as e:  # noqa: BLE001
                log.warning("WW-Statistik nicht abrufbar: %s", e)
        prof = dhw_mod.build_profile(acc, src)
        if prof:
            self.profile = prof
        log.info("WW-Profil: %s", self.profile.summary())

    # ---------------------------------------------------------------- forecast
    async def _weather_now(self, past_h: int) -> WeatherSeries:
        try:
            w = await self.om.forecast_15min(self.s.horizon_hours, past_h)
            self._weather, self._weather_at = w, datetime.now(timezone.utc)
            return w
        except Exception as e:  # noqa: BLE001
            if self._weather and datetime.now(timezone.utc) - self._weather_at < WEATHER_MAX_AGE:
                log.warning("open-meteo nicht erreichbar (%s) – nutze Prognose von %s", e, self._weather_at)
                return self._weather
            raise

    async def _curves(self) -> list[CurvePoints]:
        out = []
        for c in self.s.circuits:
            vals = [await self.ha.float_state(e) for e in (c.curve_cold, c.curve_mid, c.curve_warm)]
            if any(v is None for v in vals):
                log.warning("Heizkurve %s unvollständig, übersprungen", c.name)
                continue
            off = await self.ha.float_state(c.offset) if c.offset else 0.0
            out.append(CurvePoints(*vals, offset=off or 0.0))
        return out

    async def run_once(self) -> dict:
        async with self._lock:
            try:
                res = await self._run()
                self.result, self.last_run, self.last_error = res, datetime.now(timezone.utc), None
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Prognoselauf fehlgeschlagen")
                if self.s.publish_sensors:
                    await self._publish_status()
                raise
            if self.s.publish_sensors:
                try:
                    await self._publish(res)
                except Exception as e:  # noqa: BLE001
                    log.warning("Sensoren nicht veröffentlicht: %s", e)
            return res

    async def _run(self) -> dict:
        now = datetime.now(timezone.utc)
        past_h = int(max(48, 3 * self.params.damping_hours))
        w = await self._weather_now(past_h)
        times = w.times
        t0 = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        i0 = next((i for i, t in enumerate(times) if t >= t0), None)
        if i0 is None:
            raise RuntimeError("Wetterdaten enthalten keine Zukunft")
        n = min(self.s.horizon_hours * 4, len(times) - i0)

        temp = np.interp(np.arange(len(w.temp)), np.flatnonzero(~np.isnan(w.temp)), w.temp[~np.isnan(w.temp)])
        local = await self.ha.float_state(self.s.entities.outdoor_temp)
        bias = 0.0
        if local is not None and np.isfinite(temp[i0]):
            bias = float(np.clip(local - temp[i0], -5, 5))
        lead_h = np.maximum(0, np.arange(len(temp)) - i0) * 0.25
        temp_c = temp + np.where(np.arange(len(temp)) >= i0, bias * np.exp(-lead_h / BIAS_DECAY_H), bias)
        t_damp = ema(temp_c, 0.25, self.params.damping_hours)

        sl = slice(i0, i0 + n)
        ftimes = times[sl]
        q_dhw = np.array([self.profile.kw_at(t.astimezone(self.tz)) for t in ftimes])
        curves = await self._curves()
        out = run(ForecastInputs(ftimes, temp_c[sl], t_damp[sl], w.rh[sl], w.gti_sum()[sl], q_dhw, curves),
                  self.params)

        rows = []
        for k, t in enumerate(ftimes):
            row = {"t": t.astimezone(self.tz).isoformat()}
            for key, arr in out.items():
                row[key] = _r(arr[k], 2 if key in ("t_out", "t_damped", "flow_temp", "gti") else 3)
            rows.append(row)

        hourly = self._hourly(ftimes, out)
        self.store.save_forecast(now, [(h["start"], h["q_heat_kwh"], h["q_dhw_kwh"], h["p_el_kwh"]) for h in hourly])
        return {
            "created_at": now.astimezone(self.tz).isoformat(),
            "model_version": VERSION,
            "weather": {"model": self.s.weather_model, "fetched_at":
                        self._weather_at.astimezone(self.tz).isoformat() if self._weather_at else None,
                        "local_bias_k": round(bias, 2)},
            "location": {"latitude": self.lat, "longitude": self.lon},
            "params": {k: _r(v, 5) if not isinstance(v, list) else v for k, v in asdict(self.params).items()},
            "heating_curves": [asdict(c) for c in curves],
            "dhw_profile": self.profile.summary(),
            "units": {"power": "kW (Mittel je Slot)", "temperature": "°C", "gti": "W/m²", "slot": "15 min"},
            "series": rows,
            "summary": self._summary(ftimes, out),
        }

    def _hourly(self, ftimes, out) -> list[dict]:
        buckets: dict[datetime, list[int]] = {}
        for k, t in enumerate(ftimes):
            buckets.setdefault(t.replace(minute=0), []).append(k)
        res = []
        for h, idx in buckets.items():
            res.append({"start": h,
                        "q_heat_kwh": float(out["q_heat_kw"][idx].sum() * 0.25),
                        "q_dhw_kwh": float(out["q_dhw_kw"][idx].sum() * 0.25),
                        "p_el_kwh": float(out["p_el_kw"][idx].sum() * 0.25),
                        "slots": len(idx)})
        return res

    def _summary(self, ftimes, out) -> dict:
        days: dict[str, dict] = {}
        for k, t in enumerate(ftimes):
            d = t.astimezone(self.tz).date().isoformat()
            e = days.setdefault(d, {"q_heat_kwh": 0.0, "q_dhw_kwh": 0.0, "p_el_kwh": 0.0,
                                    "p_backup_kwh": 0.0, "slots": 0, "frost_slots": 0})
            e["q_heat_kwh"] += out["q_heat_kw"][k] * 0.25
            e["q_dhw_kwh"] += out["q_dhw_kw"][k] * 0.25
            e["p_el_kwh"] += out["p_el_kw"][k] * 0.25
            e["p_backup_kwh"] += out["p_backup_kw"][k] * 0.25
            e["slots"] += 1
            e["frost_slots"] += int(out["frost_risk"][k])
        for e in days.values():
            q = e["q_heat_kwh"] + e["q_dhw_kwh"]
            e["cop_system"] = round(q / e["p_el_kwh"], 2) if e["p_el_kwh"] > 0 else None
            e["complete_day"] = e["slots"] == 96
            for k in ("q_heat_kwh", "q_dhw_kwh", "p_el_kwh", "p_backup_kwh"):
                e[k] = round(e[k], 2)
        nxt = slice(0, 4)
        return {"days": days,
                "next_hour": {"q_total_kw": _r(out["q_total_kw"][nxt].mean()),
                              "p_el_kw": _r(out["p_el_kw"][nxt].mean())}}

    # ---------------------------------------------------------------- HA sensors
    async def _publish(self, res: dict):
        days = list(res["summary"]["days"].items())
        nh = res["summary"]["next_hour"]
        base = {"attribution": "Wärmeprognose (open-meteo.com)", "created_at": res["created_at"]}

        def energy(name, val, day):
            return (f"sensor.waermeprognose_{name}", val,
                    {**base, "unit_of_measurement": "kWh", "device_class": "energy", "day": day,
                     "friendly_name": f"Wärmeprognose {name.replace('_', ' ')}", "icon": "mdi:chart-bell-curve"})

        items = []
        labels = ["heute_rest", "morgen", "uebermorgen"]
        for (d, e), lab in zip(days, labels):
            items.append(energy(f"waerme_{lab}", round(e["q_heat_kwh"] + e["q_dhw_kwh"], 1), d))
            items.append(energy(f"strom_{lab}", round(e["p_el_kwh"], 1), d))
        items.append(("sensor.waermeprognose_waerme_naechste_stunde", nh["q_total_kw"],
                      {**base, "unit_of_measurement": "kW", "device_class": "power",
                       "friendly_name": "Wärmeprognose Wärme nächste Stunde"}))
        items.append(("sensor.waermeprognose_strom_naechste_stunde", nh["p_el_kw"],
                      {**base, "unit_of_measurement": "kW", "device_class": "power",
                       "friendly_name": "Wärmeprognose Strom nächste Stunde"}))
        for eid, val, attrs in items:
            await self.ha.set_state(eid, val, attrs)
        await self._publish_status()

    async def _publish_status(self):
        await self.ha.set_state("sensor.waermeprognose_status", "ok" if not self.last_error else "error", {
            "friendly_name": "Wärmeprognose Status", "icon": "mdi:heat-pump",
            "last_run": self.last_run.astimezone(self.tz).isoformat() if self.last_run else None,
            "last_error": self.last_error, "calibration": self.calibration_info,
        })

    # ---------------------------------------------------------------- calibration
    async def calibrate(self) -> dict:
        days = self.s.dhw.history_days
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        e = self.s.entities
        st = await self.ha.statistics([e.heat_thermal, e.outdoor_temp, e.hp_flow, e.dhw_thermal],
                                      start, end, types=("change", "mean"))
        hours = [start + timedelta(hours=i) for i in range(int((end - start).total_seconds() // 3600))]
        idx = {h: i for i, h in enumerate(hours)}

        def series(sid, key):
            a = np.full(len(hours), np.nan)
            for r in st.get(sid, []):
                if r["start"] in idx and r.get(key) is not None:
                    a[idx[r["start"]]] = r[key]
            return a

        q, t = series(e.heat_thermal, "change"), series(e.outdoor_temp, "mean")
        flow, q_dhw = series(e.hp_flow, "mean"), series(e.dhw_thermal, "change")
        g = np.zeros(len(hours))
        try:
            hist = await self.om.history_hourly(days + 1)
            for h, v in hist.items():
                if h in idx:
                    g[idx[h]] = v["gti"]
                    if np.isnan(t[idx[h]]):
                        t[idx[h]] = v["temp"]
        except Exception as ex:  # noqa: BLE001
            log.warning("open-meteo-Historie nicht verfügbar (%s) – Solargewinne nicht kalibriert", ex)
        res = await asyncio.to_thread(fit_heat_model, hours, q, t, g, self.params,
                                      self.s.calibration.prior_hours)
        info = {**res.as_dict(), "created": datetime.now(timezone.utc).isoformat(), "applied": False}
        new_params = res.params if res.n_blocks >= 8 else self.params
        # flow offset: actual HP flow vs. highest heating-curve set point
        curves = await self._curves()
        if curves and np.isfinite(t).any():
            t_f = np.interp(np.arange(len(t)), np.flatnonzero(~np.isnan(t)), t[~np.isnan(t)])
            td = ema(t_f, 1.0, self.params.damping_hours)
            curve = np.max(np.vstack([c.flow(td) for c in curves]), axis=0)
            off, n_flow = fit_flow_offset(flow, curve, q, q_dhw)
            info["flow_offset_hours"] = n_flow
            if off is not None:
                new_params = replace(new_params, flow_offset_hp=off)
                info["flow_offset_hp"] = round(off, 2)
        if res.n_blocks >= 8 or "flow_offset_hp" in info:
            info["applied"] = True
            self.params = new_params
            self.store.save_params({k: getattr(new_params, k) for k in CALIBRATED_FIELDS}, info)
        self.calibration_info = info
        log.info("Kalibrierung: %s", info)
        return info

    # ---------------------------------------------------------------- backtest
    async def backtest(self, days: int = 7, lead_h: int = 24) -> dict:
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        e = self.s.entities
        ids = [e.heat_thermal, e.dhw_thermal, e.heat_electric, e.dhw_electric]
        st = await self.ha.statistics(ids, start, end, types=("change",))
        actual: dict[datetime, dict] = {}
        for sid, key in zip(ids, ("q", "q", "p", "p")):
            for r in st.get(sid, []):
                a = actual.setdefault(r["start"], {"q": 0.0, "p": 0.0, "n": 0})
                a[key] += r.get("change") or 0.0
                a["n"] += 1
        fc: dict[int, tuple] = {}
        for issued, target, qh, qd, pe in self.store.forecasts_for(start, end):
            if issued <= target - lead_h * 3600 + 900:
                fc[target] = (qh + qd, pe)          # rows ordered by issued -> keep latest
        per_day: dict[str, dict] = {}
        for h, a in actual.items():
            f = fc.get(int(h.timestamp()))
            if f is None or a["n"] < 4:
                continue
            d = per_day.setdefault(h.astimezone(self.tz).date().isoformat(),
                                   {"q_actual": 0.0, "q_fc": 0.0, "p_actual": 0.0, "p_fc": 0.0, "hours": 0})
            d["q_actual"] += a["q"]; d["q_fc"] += f[0]
            d["p_actual"] += a["p"]; d["p_fc"] += f[1]; d["hours"] += 1
        for d in per_day.values():
            for k in list(d):
                if k != "hours":
                    d[k] = round(d[k], 2)
            d["q_err_pct"] = round((d["q_fc"] / d["q_actual"] - 1) * 100, 1) if d["q_actual"] else None
            d["p_err_pct"] = round((d["p_fc"] / d["p_actual"] - 1) * 100, 1) if d["p_actual"] else None
        return {"lead_hours": lead_h, "days": per_day,
                "note": "Prognosen werden erst ab Installation gespeichert; Abweichung = Prognose/Ist − 1"}


class Scheduler:
    """Runs the forecast every 15 min (+offset) and calibration once per night."""

    def __init__(self, svc: ForecastService, offset_s: int = 60):
        self.svc, self.offset_s = svc, offset_s
        self._task: asyncio.Task | None = None
        self._last_cal_date = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    @staticmethod
    def next_quarter(now: datetime, offset_s: int) -> datetime:
        base = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        nxt = base + timedelta(seconds=offset_s)
        while nxt <= now:
            nxt += timedelta(minutes=15)
        return nxt

    async def _loop(self):
        try:
            await self.svc.run_once()
        except Exception:  # noqa: BLE001
            pass
        while True:
            now = datetime.now(timezone.utc)
            await asyncio.sleep((self.next_quarter(now, self.offset_s) - now).total_seconds())
            local = datetime.now(self.svc.tz)
            cal = self.svc.s.calibration
            if cal.enabled and local.hour == cal.hour and self._last_cal_date != local.date():
                self._last_cal_date = local.date()
                try:
                    await self.svc.calibrate()
                    await self.svc.refresh_dhw_profile()
                except Exception:  # noqa: BLE001
                    log.exception("Kalibrierung fehlgeschlagen")
            try:
                await self.svc.run_once()
            except Exception:  # noqa: BLE001
                pass
