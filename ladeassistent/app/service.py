"""Runtime: sample the car, learn, plan, publish sensors, notify."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .ha import HomeAssistant
from .model import PresenceModel, Sample, UsageModel, daily_km, estimate_kwh_per_km
from .notify import handle_action, mark_sent, payload, should_send
from .planner import Advice, Hour, advise
from .settings import Settings
from .store import Store

log = logging.getLogger(__name__)
UTC = timezone.utc
VERSION = "0.1.1"
EVENT_ACTION = "mobile_app_notification_action"
EVENT_TEST = "ladeassistent_test"

ON = {"on", "true", "home", "connected", "plugged", "plugged_in", "1"}
OFF = {"off", "false", "not_home", "disconnected", "unplugged", "0", "away"}


def to_bool(state) -> bool | None:
    if state is None:
        return None
    s = str(state).strip().lower()
    if s in ON:
        return True
    if s in OFF:
        return False
    return None


def location_home(state) -> bool | None:
    if state is None or str(state).lower() in ("unknown", "unavailable", ""):
        return None
    return str(state).lower() == "home"


def num(x) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def forecast_hours(attr_rows, s: Settings, now: datetime, price_ct: float,
                   presence, tz: ZoneInfo) -> list[Hour]:
    """Hourly list over the horizon from a forecast attribute; empty surplus where missing."""
    e = s.energy
    by_hour: dict[datetime, list[float]] = {}
    for r in attr_rows or []:
        try:
            t = datetime.fromisoformat(str(r[e.surplus_time_key])).astimezone(UTC)
            v = float(r[e.surplus_key])
        except (KeyError, TypeError, ValueError):
            continue
        if e.surplus_unit.lower() == "w":
            v /= 1000
        v = max(0.0, -v) if e.surplus_invert else max(0.0, v)
        by_hour.setdefault(t.replace(minute=0, second=0, microsecond=0), []).append(v)
    start = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    hours = []
    for i in range(s.policy.horizon_hours):
        t = start + timedelta(hours=i)
        vals = by_hour.get(t, [])
        sur = sum(vals) / len(vals) if vals else 0.0
        if i == 0:                         # only the rest of the current hour is usable
            sur *= max(0.0, 1 - now.minute / 60)
        hours.append(Hour(t, sur, price_ct, presence(t)))
    return hours


class Service:
    def __init__(self, settings: Settings):
        self.s = settings
        self.store = Store(Path(settings.data_dir) / "ladeassistent.sqlite")
        self.client = httpx.AsyncClient()
        self.ha = HomeAssistant(self.client)
        self.tz = ZoneInfo("Europe/Berlin")
        self.last: Advice | None = None
        self.info: dict = {}
        self.prev_plugged: bool | None = None

    # ------------------------------------------------------------ sampling
    async def _state(self, eid: str):
        if not eid:
            return None
        st = await self.ha.state(eid)
        return st.get("state") if st else None

    async def plugged_now(self) -> bool | None:
        p = to_bool(await self._state(self.s.charger.plugged))
        if p is None and self.s.vehicle.plugged:
            p = to_bool(await self._state(self.s.vehicle.plugged))
        return p

    async def sample_now(self) -> Sample:
        v, c = self.s.vehicle, self.s.charger
        kw = num(await self._state(c.power))
        if kw is not None and c.power_unit.lower() == "w":
            kw /= 1000
        return Sample(datetime.now(UTC), soc=num(await self._state(v.soc)),
                      odo=num(await self._state(v.odometer)),
                      home=location_home(await self._state(v.location)),
                      plugged=await self.plugged_now(), charge_kw=kw)

    async def bootstrap(self) -> None:
        """Fill the store from HA long-term statistics (hourly) and short-term history."""
        v, c = self.s.vehicle, self.s.charger
        end = datetime.now(UTC)
        start = end - timedelta(days=self.s.bootstrap_days)
        rows: dict[datetime, Sample] = {}

        def at(t):
            return rows.setdefault(t, Sample(t))

        ids = [x for x in (v.soc, v.odometer, c.power) if x]
        try:
            st = await self.ha.statistics(ids, start, end, types=("mean", "max", "state"))
        except Exception as ex:
            log.warning("Statistik-Abfrage fehlgeschlagen: %s", ex)
            st = {}
        for r in st.get(v.soc, []):
            if r.get("mean") is not None:
                at(r["start"] + timedelta(hours=1)).soc = r["mean"]
        for r in st.get(v.odometer, []):
            val = r.get("max") if r.get("max") is not None else r.get("state")
            if val is not None:
                at(r["start"] + timedelta(hours=1)).odo = val
        for r in st.get(c.power, []):
            if r.get("mean") is not None:
                kw = r["mean"] / 1000 if c.power_unit.lower() == "w" else r["mean"]
                at(r["start"] + timedelta(hours=1)).charge_kw = kw
        try:
            hist = await self.ha.history([v.location, c.plugged], start, end)
        except Exception as ex:
            log.warning("Verlauf nicht lesbar: %s", ex)
            hist = {}
        for eid, conv, attr in ((v.location, location_home, "home"), (c.plugged, to_bool, "plugged")):
            series = hist.get(eid, [])
            if not series:
                continue
            # hourly: state at the end of each hour
            i, cur = 0, None
            t = series[0][0].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            while t <= end:
                while i < len(series) and series[i][0] <= t:
                    cur = conv(series[i][1])
                    i += 1
                if cur is not None:
                    setattr(at(t), attr, cur)
                t += timedelta(hours=1)
        self.store.add(list(rows.values()))
        log.info("Bootstrap: %d Stunden-Samples aus HA übernommen", len(rows))

    # ------------------------------------------------------------ planning
    async def price_ct(self) -> float:
        e = self.s.energy
        p = num(await self._state(e.price_entity))
        if p is None:
            return e.price_ct
        return p * 100 if e.price_unit.lower() == "eur" else p

    async def compute(self) -> Advice:
        s, now = self.s, datetime.now(UTC)
        samples = self.store.samples(now - timedelta(days=180))
        kpk, kpk_info = estimate_kwh_per_km(samples, s.vehicle.capacity_kwh, s.vehicle.kwh_per_km)
        kwh_day = {d: km * kpk for d, km in daily_km(samples, self.tz).items()}
        usage = UsageModel.build(kwh_day, now.astimezone(self.tz).date(), s.policy.halflife_days)
        presence = PresenceModel.build(samples, self.tz, now, s.policy.halflife_days)
        recent = [x for x in samples if x.ts > now - timedelta(hours=12)]
        soc = next((x.soc for x in reversed(recent) if x.soc is not None), None)
        plugged = bool(recent[-1].plugged) if recent else False
        days_left = None
        if soc is not None:
            usable = max(0.0, (soc - s.policy.reserve_soc) / 100 * s.vehicle.capacity_kwh)
            days_left = usage.days_left(now, self.tz, usable, s.policy.quantile)
        rows = None
        if s.energy.surplus_entity:
            st = await self.ha.state(s.energy.surplus_entity)
            rows = (st or {}).get("attributes", {}).get(s.energy.surplus_attribute)
        price = await self.price_ct()
        hours = forecast_hours(rows, s, now, price, lambda t: 1.0 if plugged else presence.prob(t, self.tz),
                               self.tz)
        a = advise(now=now, tz=self.tz, soc=soc, capacity_kwh=s.vehicle.capacity_kwh,
                   target_soc=s.policy.target_soc, reserve_soc=s.policy.reserve_soc, days_left=days_left,
                   plugged=plugged, hours=hours, charge_kw=s.charger.max_kw, min_kw=s.charger.min_kw,
                   feedin_ct=s.energy.feedin_ct, urgent_days=s.policy.urgent_days,
                   min_savings_eur=s.policy.min_savings_eur, min_kwh=s.policy.min_kwh,
                   vehicle=s.vehicle.name)
        self.info = {"kwh_per_km": round(kpk, 3), "kwh_per_km_info": kpk_info,
                     "usage": usage.summary(s.policy.quantile), "presence": presence.summary(self.tz),
                     "price_ct": round(price, 2), "feedin_ct": s.energy.feedin_ct,
                     "surplus_forecast_kwh": round(sum(h.surplus_kw for h in hours), 1),
                     "samples": len(samples), "version": VERSION}
        self.last = a
        return a

    async def publish(self, a: Advice) -> None:
        d = a.as_dict(self.tz)
        await self.ha.set_state("sensor.ladeassistent_status", a.status, {
            **d, **self.info, "friendly_name": "Lade-Assistent", "icon": "mdi:car-electric"})
        await self.ha.set_state("sensor.ladeassistent_reichweite_tage",
                                a.days_left if a.days_left is not None else "unknown", {
                                    "friendly_name": "Lade-Assistent Reichweite", "unit_of_measurement": "d",
                                    "state_class": "measurement", "icon": "mdi:calendar-clock"})
        w = a.window or {}
        await self.ha.set_state("sensor.ladeassistent_naechstes_fenster", w.get("start") or "unknown", {
            "friendly_name": "Lade-Assistent nächstes PV-Fenster", "device_class": "timestamp",
            "end": w.get("end"), "kwh_surplus": w.get("kwh_surplus"), "icon": "mdi:solar-power"})

    async def send(self, a: Advice, force: bool = False) -> bool:
        s, now = self.s, datetime.now(UTC)
        st = self.store.get("notify", {})
        if not force:
            ok, why = should_send(a, now, self.tz, st, quiet_start=s.policy.quiet_start,
                                  quiet_end=s.policy.quiet_end, lead_hours=s.policy.lead_hours)
            today = now.astimezone(self.tz).date().isoformat()
            if ok and st.get("day") == today and st.get("n", 0) >= s.policy.max_per_day:
                ok, why = False, "Tageslimit"
            if not ok:
                log.debug("Keine Benachrichtigung (%s): %s", a.status, why)
                return False
        if not s.notify.enabled or not s.notify.services:
            log.info("Benachrichtigung unterdrückt (keine Dienste): %s", a.title)
            return False
        for svc in s.notify.services:
            try:
                await self.ha.call_service(svc, payload(a))
            except Exception as ex:
                log.warning("Benachrichtigung über %s fehlgeschlagen: %s", svc, ex)
        log.info("Benachrichtigt: %s", a.title)
        if not force:
            mark_sent(st, a.key, now)
            today = now.astimezone(self.tz).date().isoformat()
            st["n"] = st.get("n", 0) + 1 if st.get("day") == today else 1
            st["day"] = today
            self.store.put("notify", st)
        return True

    # ------------------------------------------------------------ loops
    async def on_event(self, event_type: str, data: dict) -> None:
        if event_type == EVENT_ACTION:
            st = self.store.get("notify", {})
            if handle_action(st, str(data.get("action", "")), datetime.now(UTC), self.s.policy.snooze_hours):
                self.store.put("notify", st)
                log.info("Aktion aus der App: %s", data.get("action"))
        elif event_type == EVENT_TEST:
            a = await self.compute()
            await self.send(a, force=True)

    async def event_loop(self) -> None:
        while True:
            try:
                await self.ha.events([EVENT_ACTION, EVENT_TEST], self.on_event)
            except Exception as ex:
                log.warning("Ereignis-Verbindung getrennt: %s", ex)
            await asyncio.sleep(30)

    async def run(self) -> None:
        log.info("Lade-Assistent %s startet", VERSION)
        try:
            cfg = await self.ha.config()
            self.tz = ZoneInfo(cfg.get("time_zone") or "Europe/Berlin")
        except Exception as ex:
            log.warning("HA-Konfiguration nicht lesbar: %s", ex)
        if self.store.count() == 0 and self.s.bootstrap_days > 0:
            try:
                await self.bootstrap()
            except Exception:
                log.exception("Bootstrap fehlgeschlagen")
        asyncio.create_task(self.event_loop())
        last_plan = datetime.min.replace(tzinfo=UTC)
        while True:
            try:
                smp = await self.sample_now()
                self.store.add([smp])
                if smp.plugged is not None and smp.plugged != self.prev_plugged:
                    last_plan = datetime.min.replace(tzinfo=UTC)          # replan at once on plug change
                    self.prev_plugged = smp.plugged
                if datetime.now(UTC) - last_plan >= timedelta(minutes=self.s.plan_minutes):
                    a = await self.compute()
                    await self.publish(a)
                    await self.send(a)
                    last_plan = datetime.now(UTC)
                    log.info("Status %s · Reichweite %s d · Bedarf %.1f kWh · %s", a.status, a.days_left,
                             a.need_kwh, a.title)
            except Exception:
                log.exception("Durchlauf fehlgeschlagen")
            await asyncio.sleep(self.s.poll_minutes * 60)
