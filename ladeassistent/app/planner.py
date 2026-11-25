"""Planning: cheapest hours in the horizon to charge a given amount, and the advice built on it."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass
class Hour:
    t: datetime                 # UTC hour start
    surplus_kw: float = 0.0     # forecast PV surplus (≥ 0)
    price_ct: float = 30.0      # grid price ct/kWh
    present: float = 1.0        # probability the car is at home (1 when plugged)


@dataclass
class Slot:
    t: datetime
    kwh_surplus: float = 0.0
    kwh_grid: float = 0.0


@dataclass
class Plan:
    slots: list[Slot] = field(default_factory=list)
    kwh: float = 0.0
    kwh_surplus: float = 0.0
    cost_eur: float = 0.0

    @property
    def kwh_grid(self) -> float:
        return self.kwh - self.kwh_surplus


def _tiers(hours: list[Hour], charge_kw: float, min_kw: float, feedin_ct: float,
           presence_min: float):
    """(cost ct/kWh, time, hour index, kind, kWh capacity) per usable tier."""
    out = []
    for i, h in enumerate(hours):
        if h.present < presence_min:
            continue
        sur = min(h.surplus_kw, charge_kw) if h.surplus_kw >= min_kw else 0.0
        if sur > 0:
            out.append((feedin_ct, h.t, i, "s", sur))
        if charge_kw - sur > 0:
            out.append((h.price_ct, h.t, i, "g", charge_kw - sur))
    return out


def _fill(hours, tiers, need_kwh) -> Plan:
    slots: dict[int, Slot] = {}
    left = need_kwh
    cost = 0.0
    for c, _t, i, kind, cap in tiers:
        if left <= 1e-9:
            break
        take = min(cap, left)
        s = slots.setdefault(i, Slot(hours[i].t))
        if kind == "s":
            s.kwh_surplus += take
        else:
            s.kwh_grid += take
        cost += take * c / 100
        left -= take
    p = Plan(slots=[slots[i] for i in sorted(slots)])
    p.kwh = need_kwh - max(left, 0.0)
    p.kwh_surplus = sum(s.kwh_surplus for s in p.slots)
    p.cost_eur = cost
    return p


def cheapest(hours: list[Hour], need_kwh: float, charge_kw: float, min_kw: float,
             feedin_ct: float, presence_min: float = 0.5) -> Plan:
    """Fill ``need_kwh`` from the cheapest tiers; surplus is valued at the feed-in tariff."""
    tiers = sorted(_tiers(hours, charge_kw, min_kw, feedin_ct, presence_min), key=lambda x: (x[0], x[1]))
    return _fill(hours, tiers, need_kwh)


def immediate(hours: list[Hour], need_kwh: float, charge_kw: float, min_kw: float,
              feedin_ct: float) -> Plan:
    """Baseline: plug in now and charge at full power from the first hour on."""
    tiers = sorted(_tiers(hours, charge_kw, min_kw, feedin_ct, 0.0), key=lambda x: (x[1], x[3] != "s"))
    return _fill(hours, tiers, need_kwh)


def surplus_window(plan: Plan) -> tuple[datetime, datetime, float] | None:
    """Largest contiguous block of hours that charge from surplus: (start, end, kWh)."""
    best, cur = None, None
    for s in plan.slots:
        if s.kwh_surplus <= 0:
            continue
        if cur and s.t == cur[1]:
            cur = (cur[0], s.t + timedelta(hours=1), cur[2] + s.kwh_surplus)
        else:
            cur = (s.t, s.t + timedelta(hours=1), s.kwh_surplus)
        if best is None or cur[2] > best[2]:
            best = cur
    return best


# ---------------------------------------------------------------- advice
@dataclass
class Advice:
    status: str                       # urgent | opportunity | plugged | ok | unknown
    title: str
    message: str
    days_left: float | None = None
    need_kwh: float = 0.0
    window: dict | None = None
    plan: Plan | None = None
    baseline: Plan | None = None
    key: str = ""                     # dedup key for notifications

    def as_dict(self, tz: ZoneInfo) -> dict:
        def p(plan):
            if not plan:
                return None
            return {"kwh": round(plan.kwh, 1), "kwh_surplus": round(plan.kwh_surplus, 1),
                    "kwh_grid": round(plan.kwh_grid, 1), "cost_eur": round(plan.cost_eur, 2)}
        return {"status": self.status, "title": self.title, "message": self.message,
                "days_left": self.days_left, "need_kwh": round(self.need_kwh, 1),
                "window": self.window, "plan": p(self.plan), "baseline": p(self.baseline),
                "slots": [{"t": s.t.astimezone(tz).isoformat(), "pv": round(s.kwh_surplus, 1),
                           "grid": round(s.kwh_grid, 1)} for s in (self.plan.slots if self.plan else [])]}


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _when(t: datetime, now: datetime, tz: ZoneInfo) -> str:
    lt, ln = t.astimezone(tz), now.astimezone(tz)
    delta = (lt.date() - ln.date()).days
    day = {0: "heute", 1: "morgen", 2: "übermorgen"}.get(delta, WEEKDAYS[lt.weekday()])
    return day


def _num(x, nd=0) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def advise(*, now: datetime, tz: ZoneInfo, soc: float | None, capacity_kwh: float, target_soc: float,
           reserve_soc: float, days_left: float | None, plugged: bool, hours: list[Hour],
           charge_kw: float, min_kw: float, feedin_ct: float, urgent_days: float,
           min_savings_eur: float, min_kwh: float, vehicle: str) -> Advice:
    if soc is None:
        return Advice("unknown", "Ladestand unbekannt", "Kein aktueller Ladestand vom Fahrzeug.")
    need = max(0.0, (target_soc - soc) / 100 * capacity_kwh)
    plan = cheapest(hours, need, charge_kw, min_kw, feedin_ct, 0.0 if plugged else 0.5) if need > 0 else None
    base = immediate(hours, need, charge_kw, min_kw, feedin_ct) if need > 0 else None
    win = surplus_window(plan) if plan else None
    window = None
    if win:
        window = {"start": win[0].astimezone(tz).isoformat(), "end": win[1].astimezone(tz).isoformat(),
                  "kwh_surplus": round(win[2], 1)}
    dl = f"{_num(days_left, 1)} Tage" if days_left is not None else "unbekannt"
    if plugged:
        msg = f"{vehicle} ist angesteckt, Ladestand {_num(soc)} %."
        if win:
            msg += (f" Bester Zeitraum {_when(win[0], now, tz)} {win[0].astimezone(tz):%H}–"
                    f"{win[1].astimezone(tz):%H} Uhr mit ca. {_num(win[2])} kWh PV-Überschuss.")
        return Advice("plugged", "Angesteckt", msg, days_left, need, window, plan, base)
    if days_left is not None and days_left < urgent_days:
        return Advice("urgent", f"{vehicle}: bald anstecken",
                      f"Ladestand {_num(soc)} %, reicht bei üblicher Nutzung noch etwa {dl}. "
                      "Bitte an der Wallbox anstecken.",
                      days_left, need, window, plan, base,
                      key=f"urgent:{now.astimezone(tz).date().isoformat()}")
    if plan and win and need >= min_kwh:
        savings = (base.cost_eur - plan.cost_eur) if base else 0.0
        if win[2] >= min(min_kwh, need) and savings >= min_savings_eur:
            s, e = win[0].astimezone(tz), win[1].astimezone(tz)
            title = f"Günstig laden: {_when(win[0], now, tz)} {s:%H}–{e:%H} Uhr"
            msg = (f"{vehicle}: Ladestand {_num(soc)} % (reicht noch ca. {dl}). "
                   f"Im Zeitraum stehen etwa {_num(win[2])} kWh PV-Überschuss bereit, "
                   f"Ersparnis ca. {_num(savings, 2)} € gegenüber sofortigem Laden. "
                   "Rechtzeitig vorher anstecken.")
            a = Advice("opportunity", title, msg, days_left, need, window, plan, base,
                       key=f"window:{win[0].isoformat()}")
            a.window["savings_eur"] = round(savings, 2)
            return a
    return Advice("ok", "Kein Handlungsbedarf",
                  f"{vehicle}: Ladestand {_num(soc)} %, reicht noch ca. {dl}. "
                  "Kein lohnendes Ladefenster in den nächsten Stunden.",
                  days_left, need, window, plan, base)
