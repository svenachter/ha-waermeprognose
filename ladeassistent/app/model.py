"""Learning part: daily energy use per weekday, presence at home, consumption per km.

Pure functions on recorded samples, no Home Assistant access. All learned values decay
with a half-life so that changed habits take over within a few weeks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass
class Sample:
    ts: datetime                 # UTC
    soc: float | None = None     # %
    odo: float | None = None     # km
    home: bool | None = None
    plugged: bool | None = None
    charge_kw: float | None = None


def _w(age_days: float, halflife: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / halflife)


def weighted_quantile(values: list[float], weights: list[float], q: float) -> float | None:
    pairs = sorted((v, w) for v, w in zip(values, weights) if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * total - 1e-12:
            return v
    return pairs[-1][0]


# ---------------------------------------------------------------- consumption per km
def estimate_kwh_per_km(samples: list[Sample], capacity_kwh: float, fallback: float,
                        min_km: float = 50.0) -> tuple[float, dict]:
    """ΔSoC over Δkm per stretch between two charges. Falls back below ``min_km`` of evidence.

    Whole stretches instead of sample pairs, because car clouds often report the SoC later
    than the odometer (km and SoC drop land in different samples)."""
    km = kwh = 0.0

    def close(first, last):
        nonlocal km, kwh
        if first is None or last is None:
            return
        dkm, dsoc = last.odo - first.odo, first.soc - last.soc
        if 0 < dkm < 3000 and dsoc > 0:
            km += dkm
            kwh += dsoc / 100 * capacity_kwh

    first = prev = None
    for s in samples:
        charging = bool(s.charge_kw and s.charge_kw > 0.3)
        if charging or s.soc is None or s.odo is None:
            if charging:
                close(first, prev)
                first = prev = None
            continue
        if prev is not None and s.soc > prev.soc + 2:          # charged elsewhere
            close(first, prev)
            first = None
        if first is None:
            first = s
        prev = s
    close(first, prev)
    if km < min_km or kwh <= 0:
        return fallback, {"source": "fallback", "km": round(km, 1)}
    v = min(max(kwh / km, 0.10), 0.45)
    return v, {"source": "learned", "km": round(km, 1), "kwh": round(kwh, 1)}


# ---------------------------------------------------------------- daily use
def daily_km(samples: list[Sample], tz: ZoneInfo) -> dict[date, float]:
    """Kilometres per local day from the odometer (last reading per day vs. previous day)."""
    last: dict[date, float] = {}
    for s in samples:
        if s.odo is not None:
            d = s.ts.astimezone(tz).date()
            last[d] = max(last.get(d, s.odo), s.odo)
    days = sorted(last)
    out = {}
    for a, b in zip(days, days[1:]):
        gap = (b - a).days
        delta = max(last[b] - last[a], 0.0)
        if gap == 1:
            out[b] = delta
        elif gap > 1:                      # missing days: spread evenly (no data != no driving)
            for i in range(1, gap + 1):
                out[a + timedelta(days=i)] = delta / gap
    return out


@dataclass
class UsageModel:
    """Daily energy use (kWh) per weekday as weighted quantiles; pooled when data is thin."""
    by_weekday: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    pooled: list[tuple[float, float]] = field(default_factory=list)
    min_days: int = 3
    default_kwh: float = 5.0

    @classmethod
    def build(cls, kwh_per_day: dict[date, float], today: date, halflife_days: float = 28.0,
              min_days: int = 3, default_kwh: float = 5.0) -> "UsageModel":
        m = cls(min_days=min_days, default_kwh=default_kwh)
        for d, v in kwh_per_day.items():
            if d >= today:
                continue
            w = _w((today - d).days, halflife_days)
            m.by_weekday.setdefault(d.weekday(), []).append((v, w))
            m.pooled.append((v, w))
        return m

    def days_of_data(self) -> int:
        return len(self.pooled)

    def quantile(self, weekday: int, q: float) -> float:
        rows = self.by_weekday.get(weekday, [])
        if len(rows) < self.min_days:
            rows = self.pooled
        if len(rows) < self.min_days:
            return self.default_kwh
        return weighted_quantile([v for v, _ in rows], [w for _, w in rows], q) or 0.0

    def mean(self) -> float:
        if len(self.pooled) < self.min_days:
            return self.default_kwh
        tw = sum(w for _, w in self.pooled)
        return sum(v * w for v, w in self.pooled) / tw

    def days_left(self, start: datetime, tz: ZoneInfo, energy_kwh: float, q: float,
                  max_days: int = 30) -> float:
        """Days until ``energy_kwh`` is used up at the ``q`` quantile (rest of today pro rata)."""
        if energy_kwh <= 0:
            return 0.0
        local = start.astimezone(tz)
        left = energy_kwh
        frac_today = 1 - (local.hour * 60 + local.minute) / 1440
        d = local.date()
        days = 0.0
        for i in range(max_days):
            need = self.quantile(d.weekday(), q) * (frac_today if i == 0 else 1.0)
            span = frac_today if i == 0 else 1.0
            if need >= left and need > 0:
                return round(days + span * left / need, 2)
            left -= need
            days += span
            d += timedelta(days=1)
        return float(max_days)

    def summary(self, q: float) -> dict:
        names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        return {"days_of_data": self.days_of_data(),
                "mean_kwh_per_day": round(self.mean(), 1),
                f"p{int(q * 100)}_kwh": {names[i]: round(self.quantile(i, q), 1) for i in range(7)}}


# ---------------------------------------------------------------- presence
@dataclass
class PresenceModel:
    """Probability that the car is at home per weekday and hour (local time)."""
    grid: dict[tuple[int, int], tuple[float, float]] = field(default_factory=dict)  # (sum w·home, sum w)
    prior: float = 0.9
    prior_weight: float = 2.0

    @classmethod
    def build(cls, samples: list[Sample], tz: ZoneInfo, now: datetime, halflife_days: float = 28.0,
              prior: float = 0.9, prior_weight: float = 2.0) -> "PresenceModel":
        m = cls(prior=prior, prior_weight=prior_weight)
        # one vote per hour slot: the last known state in that hour
        per_hour: dict[datetime, bool] = {}
        for s in samples:
            home = s.home if s.home is not None else (True if s.plugged else None)
            if home is None:
                continue
            per_hour[s.ts.replace(minute=0, second=0, microsecond=0)] = home
        for h, home in per_hour.items():
            lt = h.astimezone(tz)
            k = (lt.weekday(), lt.hour)
            w = _w((now - h).total_seconds() / 86400, halflife_days)
            a, b = m.grid.get(k, (0.0, 0.0))
            m.grid[k] = (a + w * (1.0 if home else 0.0), b + w)
        return m

    def prob(self, t: datetime, tz: ZoneInfo) -> float:
        lt = t.astimezone(tz)
        a, b = self.grid.get((lt.weekday(), lt.hour), (0.0, 0.0))
        return (a + self.prior * self.prior_weight) / (b + self.prior_weight)

    def summary(self, tz: ZoneInfo) -> dict:
        if not self.grid:
            return {"hours_of_data": 0}
        n = sum(1 for _ in self.grid)
        avg = sum(a / b for a, b in self.grid.values() if b > 0) / max(n, 1)
        return {"slots": n, "mean_home": round(avg, 2)}


def is_finite(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))
