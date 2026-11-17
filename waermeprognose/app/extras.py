"""PV forecast (Helios) and household load profile."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

log = logging.getLogger(__name__)
SLOTS = 96


# ---------------------------------------------------------------- PV (Helios)

def parse_pv_forecast(resp: dict) -> dict[datetime, tuple[float, float | None, float | None]]:
    """Helios get_forecast response -> {slot start (aware): (kW, p10 kW, p90 kW)}."""
    out = {}
    for p in (resp or {}).get("forecast", []):
        try:
            t = datetime.fromisoformat(p["datetime"])
        except (KeyError, ValueError, TypeError):
            continue
        w = p.get("watts")
        if w is None:
            continue
        p10, p90 = p.get("p10"), p.get("p90")
        out[t] = (float(w) / 1000, None if p10 is None else float(p10) / 1000,
                  None if p90 is None else float(p90) / 1000)
    return out


def pv_for_slots(pv: dict, slots: list[datetime]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Align to the forecast grid; missing slots -> NaN, missing bands -> mean value."""
    by_ts = {int(t.timestamp()): v for t, v in pv.items()}
    kw, lo, hi = (np.full(len(slots), np.nan) for _ in range(3))
    for i, t in enumerate(slots):
        v = by_ts.get(int(t.timestamp()))
        if v is None:
            continue
        kw[i] = v[0]
        lo[i] = v[1] if v[1] is not None else v[0]
        hi[i] = v[2] if v[2] is not None else v[0]
    return kw, lo, hi


# ---------------------------------------------------------------- household profile

@dataclass
class HouseholdProfile:
    workday: np.ndarray | None = None
    weekend: np.ndarray | None = None
    days_workday: list[str] = field(default_factory=list)
    days_weekend: list[str] = field(default_factory=list)
    aggregate: str = "mean"

    def kw_at(self, local_dt: datetime) -> float:
        slot = local_dt.hour * 4 + local_dt.minute // 15
        prof = self.weekend if local_dt.weekday() >= 5 and self.weekend is not None else self.workday
        if prof is None:
            prof = self.weekend
        return float("nan") if prof is None else float(prof[slot])

    def summary(self) -> dict:
        def kwh(p):
            return None if p is None else round(float(np.nansum(p) * 0.25), 1)
        return {"aggregate": self.aggregate, "days_workday": self.days_workday,
                "days_weekend": self.days_weekend, "daily_kwh_workday": kwh(self.workday),
                "daily_kwh_weekend": kwh(self.weekend)}


def daily_slots(rows: list[tuple[datetime, float]], tz: ZoneInfo, step_min: int,
                ) -> dict[date, tuple[np.ndarray, np.ndarray]]:
    """Mean power rows (start UTC, kW) -> per local day: (kW per 15-min slot, minutes covered)."""
    acc: dict[date, list[np.ndarray]] = {}
    for t, kw in rows:
        if kw is None or not np.isfinite(kw):
            continue
        local = t.astimezone(tz)
        sums, cov = acc.setdefault(local.date(), [np.zeros(SLOTS), np.zeros(SLOTS)])
        # a row may span several slots (hourly) or part of one (5-min)
        m = 0
        while m < step_min:
            lt = local + timedelta(minutes=m)
            if lt.date() != local.date():
                break
            s = lt.hour * 4 + lt.minute // 15
            span = min(15 - lt.minute % 15, step_min - m)
            sums[s] += kw * span
            cov[s] += span
            m += span
    return {d: (np.where(c > 0, s / np.maximum(c, 1e-9), np.nan), c) for d, (s, c) in acc.items()}


def build_household_profile(days: dict[date, tuple[np.ndarray, np.ndarray]], today: date,
                            n_days: int = 3, aggregate: str = "mean", split_weekend: bool = True,
                            min_coverage: float = 0.9) -> HouseholdProfile:
    complete = sorted((d for d, (_, cov) in days.items()
                       if d < today and cov.sum() >= min_coverage * 1440), reverse=True)
    agg = np.nanmedian if aggregate == "median" else np.nanmean

    def pick(pred):
        sel = [d for d in complete if pred(d)][:n_days]
        if not sel:
            return None, []
        arr = np.vstack([days[d][0] for d in sel])
        return agg(arr, axis=0), [d.isoformat() for d in sel]

    if split_weekend:
        wd, wd_days = pick(lambda d: d.weekday() < 5)
        we, we_days = pick(lambda d: d.weekday() >= 5)
    else:
        wd, wd_days = pick(lambda d: True)
        we, we_days = None, []
    return HouseholdProfile(wd, we, wd_days, we_days, aggregate)
