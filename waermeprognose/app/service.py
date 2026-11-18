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
from . import extras
from .aggregate import aggregate_hourly
from .calibration import fit_flow_offset, fit_heat_model
from .ha import HomeAssistant
from .model import CurvePoints, FixedFlow, ForecastInputs, ema, run
from .settings import ModelParams, Settings
from .store import Store
from .weather import OpenMeteo, WeatherSeries

log = logging.getLogger(__name__)
VERSION = "0.3.0"
CALIBRATED_FIELDS = ("q_base_kw", "q_slope_kw_per_k", "knee_slope_kw_per_k",
                     "solar_gain_kw_per_wm2", "uncertainty", "flow_offset_hp")
BIAS_DECAY_H = 6.0
WEATHER_MAX_AGE = timedelta(hours=6)


def _sum_or_none(a) -> float | None:
    a = np.asarray(a, float)
    return None if not np.isfinite(a).any() else float(np.nansum(a) * 0.25)


def _dev_pct(fc, ist, floor):
    if fc is None or ist is None or abs(ist) < floor:
        return None
    return round((fc / ist - 1) * 100, 1)


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
        self.household = extras.HouseholdProfile()
        self._household_date = None
        self.pv_status: dict = {"enabled": settings.pv.enabled}
        self.result: dict | None = None
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self.calibration_info: dict | None = None
        self._weather: WeatherSeries | None = None
        self._weather_at: datetime | None = None
        self._compare_hour: datetime | None = None
        self.compare_result: dict | None = None
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
        await self.refresh_household_profile()

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

    # ---------------------------------------------------------------- household
    async def refresh_household_profile(self):
        hs, e = self.s.household, self.s.entities
        if not hs.enabled:
            return
        today = datetime.now(self.tz).date()
        ids = [e.household_power] + ([e.hp_power] if hs.correct_hp_share else [])
        corr = max(self.params.power_correction - 1.0, 0.0) if hs.correct_hp_share else 0.0
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        days: dict = {}
        # hourly long-term statistics (kept forever) first, then 5-min short-term (≈10 days)
        for period, step, back in (("hour", 60, 21), ("5minute", 5, 10)):
            try:
                st = await self.ha.statistics(ids, end - timedelta(days=back), end,
                                              period=period, types=("mean",))
            except Exception as ex:  # noqa: BLE001
                log.warning("Haushalts-Statistik (%s) nicht abrufbar: %s", period, ex)
                continue
            hp = {r["start"]: r.get("mean") for r in st.get(e.hp_power, [])}
            rows = []
            for r in st.get(e.household_power, []):
                w = r.get("mean")
                if w is None:
                    continue
                w = w - corr * (hp.get(r["start"]) or 0.0)
                rows.append((r["start"], max(w, 0.0) / 1000))
            for d, v in extras.daily_slots(rows, self.tz, step).items():
                if period == "5minute" and v[1].sum() < 0.9 * 1440 and d in days:
                    continue          # keep the complete hourly day
                days[d] = v
        self.household = extras.build_household_profile(
            days, today, hs.days, hs.aggregate, hs.split_weekend)
        self._household_date = today
        log.info("Haushaltsprofil: %s", self.household.summary())

    async def _pv(self, ftimes: list[datetime]):
        n = len(ftimes)
        if not self.s.pv.enabled:
            return np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
        data = {"config_entry_id": self.s.pv.config_entry_id} if self.s.pv.config_entry_id else {}
        try:
            resp = await self.ha.call_service(self.s.pv.service, data)
            pv = extras.parse_pv_forecast(resp)
            kw, lo, hi = extras.pv_for_slots(pv, ftimes)
            self.pv_status = {"enabled": True, "ok": True, "points": len(pv),
                              "covered_slots": int(np.isfinite(kw).sum()), "slots": n}
            return kw, lo, hi
        except Exception as ex:  # noqa: BLE001
            log.warning("PV-Prognose nicht abrufbar: %s", ex)
            self.pv_status = {"enabled": True, "ok": False, "error": str(ex)}
            return np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)

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

    async def _circuit_states(self) -> tuple[list[CurvePoints | FixedFlow], list[dict]]:
        """Flow set point providers of all active circuits + info for the API."""
        providers, info = [], []
        inactive = {x.upper() for x in self.s.inactive_states}
        for c in self.s.circuits:
            row = {"name": c.name, "mode": c.mode, "active": False}
            state = None
            if c.state:
                st = await self.ha.state(c.state)
                state = (st or {}).get("state")
                row["state"] = state
            if state and str(state).upper() in inactive:
                row["reason"] = "Betriebsstatus inaktiv"
                info.append(row)
                continue
            target = await self.ha.float_state(c.target_flow) if c.target_flow else None
            row["target_flow_now"] = target
            prov = None
            if c.mode == "target":
                if target is not None and target > 0:
                    prov = FixedFlow(target)
                else:
                    row["reason"] = "kein Sollwert"
            else:
                vals = [await self.ha.float_state(e) if e else None
                        for e in (c.curve_cold, c.curve_mid, c.curve_warm)]
                if all(v is not None for v in vals):
                    off = await self.ha.float_state(c.offset) if c.offset else 0.0
                    prov = CurvePoints(*vals, offset=off or 0.0)
                elif target is not None and target > 0:
                    prov = FixedFlow(target)
                    row["reason"] = "Heizkurve unvollständig, nutze aktuellen Sollwert"
                else:
                    row["reason"] = "Heizkurve unvollständig"
            if prov is not None:
                row["active"] = True
                row["setpoint"] = asdict(prov)
                providers.append(prov)
            info.append(row)
        if not providers:
            log.warning("Kein aktiver Heizkreis – Vorlauf fällt auf 35 °C zurück")
        return providers, info

    async def _curves(self) -> list[CurvePoints | FixedFlow]:
        return (await self._circuit_states())[0]

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
            hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            if self._compare_hour != hour:
                try:
                    self.compare_result = await self.compare_hourly()
                    self._compare_hour = hour
                    if self.s.publish_sensors:
                        await self._publish_compare(self.compare_result)
                except Exception as e:  # noqa: BLE001
                    log.warning("Abgleich Prognose/Ist fehlgeschlagen: %s", e)
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
        curves, circuit_info = await self._circuit_states()
        out = run(ForecastInputs(ftimes, temp_c[sl], t_damp[sl], w.rh[sl], w.gti_sum()[sl], q_dhw, curves),
                  self.params)
        # electricity balance: PV (Helios) and household base load
        if self.s.household.enabled and self._household_date != datetime.now(self.tz).date():
            try:
                await self.refresh_household_profile()
            except Exception as ex:  # noqa: BLE001
                log.warning("Haushaltsprofil nicht aktualisiert: %s", ex)
        pv_kw, pv_lo, pv_hi = await self._pv(ftimes)
        hh = (np.array([self.household.kw_at(t.astimezone(self.tz)) for t in ftimes])
              if self.s.household.enabled else np.full(len(ftimes), np.nan))
        out["pv_kw"], out["pv_p10"], out["pv_p90"] = pv_kw, pv_lo, pv_hi
        out["household_kw"] = hh
        out["net_kw"] = out["p_el_kw"] + hh - pv_kw     # > 0 grid/battery/CHP needed, < 0 surplus

        rows = []
        for k, t in enumerate(ftimes):
            row = {"t": t.astimezone(self.tz).isoformat()}
            for key, arr in out.items():
                row[key] = _r(arr[k], 2 if key in ("t_out", "t_damped", "flow_temp", "gti") else 3)
            rows.append(row)

        hourly = self._hourly(ftimes, out)
        self.store.save_forecast(now, [(h["start"], h["q_heat_kwh"], h["q_dhw_kwh"], h["p_el_kwh"],
                                        h["pv_kwh"], h["hh_kwh"]) for h in hourly])
        return {
            "created_at": now.astimezone(self.tz).isoformat(),
            "model_version": VERSION,
            "weather": {"model": self.s.weather_model, "fetched_at":
                        self._weather_at.astimezone(self.tz).isoformat() if self._weather_at else None,
                        "local_bias_k": round(bias, 2)},
            "location": {"latitude": self.lat, "longitude": self.lon},
            "params": {k: _r(v, 5) if not isinstance(v, list) else v for k, v in asdict(self.params).items()},
            "circuits": circuit_info,
            "dhw_profile": self.profile.summary(),
            "household_profile": self.household.summary(),
            "pv": self.pv_status,
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
                        "pv_kwh": _sum_or_none(out["pv_kw"][idx]),
                        "hh_kwh": _sum_or_none(out["household_kw"][idx]),
                        "slots": len(idx)})
        return res

    def _summary(self, ftimes, out) -> dict:
        days: dict[str, dict] = {}
        for k, t in enumerate(ftimes):
            d = t.astimezone(self.tz).date().isoformat()
            e = days.setdefault(d, {"q_heat_kwh": 0.0, "q_dhw_kwh": 0.0, "p_el_kwh": 0.0,
                                    "p_backup_kwh": 0.0, "pv_kwh": 0.0, "household_kwh": 0.0,
                                    "net_import_kwh": 0.0, "surplus_kwh": 0.0,
                                    "slots": 0, "frost_slots": 0, "pv_missing_slots": 0})
            e["q_heat_kwh"] += out["q_heat_kw"][k] * 0.25
            e["q_dhw_kwh"] += out["q_dhw_kw"][k] * 0.25
            e["p_el_kwh"] += out["p_el_kw"][k] * 0.25
            e["p_backup_kwh"] += out["p_backup_kw"][k] * 0.25
            pv, hh, net = out["pv_kw"][k], out["household_kw"][k], out["net_kw"][k]
            e["pv_kwh"] += 0.0 if np.isnan(pv) else pv * 0.25
            e["pv_missing_slots"] += int(np.isnan(pv))
            e["household_kwh"] += 0.0 if np.isnan(hh) else hh * 0.25
            if np.isfinite(net):
                e["net_import_kwh"] += max(net, 0.0) * 0.25
                e["surplus_kwh"] += max(-net, 0.0) * 0.25
            e["slots"] += 1
            e["frost_slots"] += int(out["frost_risk"][k])
        for e in days.values():
            q = e["q_heat_kwh"] + e["q_dhw_kwh"]
            e["cop_system"] = round(q / e["p_el_kwh"], 2) if e["p_el_kwh"] > 0 else None
            e["complete_day"] = e["slots"] == 96
            for k in ("q_heat_kwh", "q_dhw_kwh", "p_el_kwh", "p_backup_kwh", "pv_kwh",
                      "household_kwh", "net_import_kwh", "surplus_kwh"):
                e[k] = round(e[k], 2)
        nxt = slice(0, 4)
        return {"days": days,
                "next_hour": {"q_total_kw": _r(out["q_total_kw"][nxt].mean()),
                              "p_el_kw": _r(out["p_el_kw"][nxt].mean()),
                              "pv_kw": _r(np.nanmean(out["pv_kw"][nxt])) if np.isfinite(out["pv_kw"][nxt]).any() else None,
                              "household_kw": _r(np.nanmean(out["household_kw"][nxt])) if np.isfinite(out["household_kw"][nxt]).any() else None,
                              "net_kw": _r(np.nanmean(out["net_kw"][nxt])) if np.isfinite(out["net_kw"][nxt]).any() else None}}

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
            if self.s.household.enabled:
                items.append(energy(f"haushalt_{lab}", round(e["household_kwh"], 1), d))
            if self.s.pv.enabled:
                items.append(energy(f"pv_{lab}", round(e["pv_kwh"], 1), d))
            items.append(energy(f"netzbedarf_{lab}", round(e["net_import_kwh"], 1), d))
        items.append(("sensor.waermeprognose_waerme_naechste_stunde", nh["q_total_kw"],
                      {**base, "unit_of_measurement": "kW", "device_class": "power",
                       "friendly_name": "Wärmeprognose Wärme nächste Stunde"}))
        items.append(("sensor.waermeprognose_strom_naechste_stunde", nh["p_el_kw"],
                      {**base, "unit_of_measurement": "kW", "device_class": "power",
                       "friendly_name": "Wärmeprognose Strom nächste Stunde"}))
        for eid, val, attrs in items:
            await self.ha.set_state(eid, val, attrs)
        await self.ha.set_state("sensor.waermeprognose_zeitreihe", res["created_at"], {
            "friendly_name": "Wärmeprognose Zeitreihe", "icon": "mdi:table-clock",
            "device_class": "timestamp", "resolution": "60min",
            "forecast": compact_hourly(res["series"], hours=48)})
        await self._publish_status()

    async def _publish_compare(self, cmp: dict):
        rows = [{"t": r["t"], "lead": r["lead_h"],
                 **{f"{k}{suf}": r[f"{k}_{suf2}"] for k in ("q", "p", "pv", "hh")
                    for suf, suf2 in (("f", "fc"), ("i", "ist"), ("d", "dev"))}}
                for r in cmp["rows"]]
        q = cmp["summary"]["q"]
        await self.ha.set_state("sensor.waermeprognose_abgleich",
                                q["dev_pct"] if q["dev_pct"] is not None else "unknown", {
            "friendly_name": "Wärmeprognose Abgleich Wärme", "icon": "mdi:scale-balance",
            "unit_of_measurement": "%", "created_at": cmp["created_at"],
            "hours": cmp["hours"], "lead_hours_preferred": cmp["lead_hours_preferred"],
            "summary": cmp["summary"], "rows": rows})

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

    # ---------------------------------------------------------------- forecast vs. actual
    async def compare_hourly(self, hours: int | None = None, lead_h: float | None = None) -> dict:
        """Hour by hour: stored forecast (lead ≈ lead_h) vs. measured values for the last hours."""
        c, e = self.s.compare, self.s.entities
        hours = int(hours or c.hours)
        lead_h = c.lead_hours if lead_h is None else float(lead_h)
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=hours)
        ids = [e.heat_thermal, e.dhw_thermal, e.heat_electric, e.dhw_electric,
               e.pv_power, e.household_power, e.hp_power]
        st = await self.ha.statistics(ids, start, end, period="hour", types=("change", "mean"))

        def col(sid, key):
            return {r["start"]: r.get(key) for r in st.get(sid, []) if r.get(key) is not None}

        q_h, q_w = col(e.heat_thermal, "change"), col(e.dhw_thermal, "change")
        p_h, p_w = col(e.heat_electric, "change"), col(e.dhw_electric, "change")
        pv_w, hh_w, hp_w = col(e.pv_power, "mean"), col(e.household_power, "mean"), col(e.hp_power, "mean")
        corr = self.params.power_correction
        hh_corr = (corr - 1.0) if self.s.household.correct_hp_share else 0.0

        best: dict[int, tuple] = {}
        for row in self.store.forecasts_for(start, end):
            issued, target = row[0], row[1]
            lead = (target - issued) / 3600
            if lead < c.min_lead_hours:
                continue
            if target not in best or abs(lead - lead_h) < abs(best[target][0] - lead_h):
                best[target] = (lead, row)

        rows = []
        sums = {k: [0.0, 0.0, 0] for k in ("q", "p", "pv", "hh")}
        devs = {k: [] for k in sums}
        floors = {"q": 0.2, "p": 0.05, "pv": 0.05, "hh": 0.05}
        for i in range(hours):
            h = start + timedelta(hours=i)
            ist = {
                "q": (q_h[h] + q_w.get(h, 0.0)) if h in q_h else None,
                "p": (p_h[h] + p_w.get(h, 0.0)) * corr if h in p_h else None,
                "pv": pv_w[h] / 1000 if h in pv_w else None,
                "hh": max(hh_w[h] - hh_corr * hp_w.get(h, 0.0), 0.0) / 1000 if h in hh_w else None,
            }
            b = best.get(int(h.timestamp()))
            fc = {"q": None, "p": None, "pv": None, "hh": None}
            issued = lead = None
            if b:
                lead, (iss, _t, qh, qd, pe, pv, hh) = b
                issued = datetime.fromtimestamp(iss, tz=timezone.utc).astimezone(self.tz).isoformat()
                fc = {"q": qh + qd, "p": pe, "pv": pv, "hh": hh}
            row = {"t": h.astimezone(self.tz).isoformat(), "issued": issued,
                   "lead_h": None if lead is None else round(lead, 1)}
            for k in sums:
                row[f"{k}_fc"] = None if fc[k] is None else round(fc[k], 2)
                row[f"{k}_ist"] = None if ist[k] is None else round(ist[k], 2)
                d = _dev_pct(fc[k], ist[k], floors[k])
                row[f"{k}_dev"] = d
                if fc[k] is not None and ist[k] is not None:
                    sums[k][0] += fc[k]; sums[k][1] += ist[k]; sums[k][2] += 1
                    if d is not None:
                        devs[k].append(abs(d))
            rows.append(row)
        summary = {}
        for k, (f, a, n) in sums.items():
            summary[k] = {"hours": n, "fc_kwh": round(f, 1), "ist_kwh": round(a, 1),
                          "dev_pct": _dev_pct(f, a, floors[k] * max(n, 1)) if n else None,
                          "mean_abs_dev_pct": round(float(np.mean(devs[k])), 1) if devs[k] else None}
        return {"created_at": datetime.now(self.tz).isoformat(), "hours": hours,
                "lead_hours_preferred": lead_h, "min_lead_hours": c.min_lead_hours,
                "units": "kWh je Stunde; Abweichung = Prognose/Ist − 1 in %",
                "note": "WP-Strom Ist = Reglerzähler × power_correction; PV-Ist = entities.pv_power",
                "summary": summary, "rows": rows}

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
        for issued, target, qh, qd, pe, *_ in self.store.forecasts_for(start, end):
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


def compact_hourly(series: list[dict], hours: int = 48) -> list[dict]:
    """Hourly rows with short keys for a HA attribute (~6 kB for 48 h)."""
    def r(v, nd=1):
        return None if v is None else round(v, nd)
    out = []
    for h in aggregate_hourly(series)[:hours]:
        out.append({"t": h["t"], "ta": r(h["t_out"]), "vl": r(h["flow_temp"]),
                    "q": r(h["q_total_kw"]), "qh": r(h["q_heat_kw"]), "qw": r(h["q_dhw_kw"]),
                    "cop": r(h["cop_system"], 2), "wp": r(h["p_el_kw"], 2),
                    "hh": r(h["household_kw"], 2), "pv": r(h["pv_kw"], 2),
                    "net": r(h["net_kw"], 2), "frost": bool(h["frost_risk"])})
    return out


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
