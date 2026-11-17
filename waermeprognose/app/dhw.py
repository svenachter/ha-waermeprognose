"""Domestic hot water profile from Lambda trend logs and/or HA long-term statistics.

Profile = mean DHW heat (kW) per 15-min slot of the local day. With at least
``MIN_DAYS_SPLIT`` days of both workdays and weekend days, separate profiles are kept.
"""
from __future__ import annotations

import csv
import glob
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

log = logging.getLogger(__name__)
SLOTS = 96
MIN_DAYS_SPLIT = 3
KEY_VALVE = 'key:"boiler.3"'        # Brauchwasser: charging pump / 3-way valve
KEY_HEAT = 'key:"heatpump.17"'      # Wärmepumpe: thermal heating power (kW)
KEY_STATE = 'key:"heatpump.21"'     # Wärmepumpe: state (10 = defrost)

# Fallback, derived from the trend log 11.–15.11.2026 (kW per hour of day)
DEFAULT_HOURLY_KW = [1.45, 1.32, 1.58, 1.89, 1.21, 2.13, 5.36, 8.91, 5.22, 1.91, 0.0, 1.51,
                     0.63, 1.84, 1.07, 0.51, 1.72, 1.95, 1.07, 1.84, 0.03, 1.98, 0.75, 1.31]


@dataclass
class DhwProfile:
    workday: np.ndarray = field(default_factory=lambda: np.repeat(DEFAULT_HOURLY_KW, 4).astype(float))
    weekend: np.ndarray | None = None
    days_workday: float = 0.0
    days_weekend: float = 0.0
    source: str = "default"

    def kw_at(self, local_dt: datetime) -> float:
        slot = local_dt.hour * 4 + local_dt.minute // 15
        prof = self.weekend if (self.weekend is not None and local_dt.weekday() >= 5) else self.workday
        return float(prof[slot])

    def daily_kwh(self) -> float:
        return float(self.workday.sum() * 0.25)

    def summary(self) -> dict:
        return {"source": self.source, "days_workday": self.days_workday,
                "days_weekend": self.days_weekend, "split_weekend": self.weekend is not None,
                "daily_kwh_workday": round(self.daily_kwh(), 1),
                "daily_kwh_weekend": None if self.weekend is None else round(float(self.weekend.sum() * 0.25), 1)}


class _Accumulator:
    """Sums slot energy per local day; complete days only."""

    def __init__(self):
        self.energy = defaultdict(lambda: np.zeros(SLOTS))   # date -> kWh per slot
        self.coverage = defaultdict(lambda: np.zeros(SLOTS))  # date -> minutes covered

    def add(self, local_dt: datetime, kwh: float, minutes: float):
        d = local_dt.date()
        s = local_dt.hour * 4 + local_dt.minute // 15
        self.energy[d][s] += kwh
        self.coverage[d][s] += minutes

    def days(self, min_cover=0.9):
        for d, cov in self.coverage.items():
            if cov.sum() >= min_cover * 1440:
                yield d, self.energy[d]


def build_profile(acc: _Accumulator, source: str) -> DhwProfile | None:
    wd, we = [], []
    for d, e in acc.days():
        (we if d.weekday() >= 5 else wd).append(e / 0.25)  # kWh/slot -> kW
    if not wd and not we:
        return None
    allp = np.mean(wd + we, axis=0)
    if len(wd) >= MIN_DAYS_SPLIT and len(we) >= MIN_DAYS_SPLIT:
        return DhwProfile(np.mean(wd, axis=0), np.mean(we, axis=0), len(wd), len(we), source)
    return DhwProfile(allp, None, len(wd) + len(we), 0, source)


# ---------- Lambda trend log -------------------------------------------------

def _find_col(header: list[str], key: str) -> int | None:
    for i, h in enumerate(header):
        if h and key in str(h):
            return i
    return None


def _num(v) -> float:
    if v is None or v == "":
        return np.nan
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return np.nan


def _parse_ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _rows_from_file(path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        for row in ws.iter_rows(values_only=True):
            yield list(row)
        wb.close()
    else:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            yield from csv.reader(fh, dialect)


def read_trendlog(path: str | Path, tz: ZoneInfo) -> list[tuple[datetime, float]]:
    """Return (local timestamp, DHW heat kW) per log row. Defrost rows count as 0."""
    rows = _rows_from_file(Path(path))
    header = [str(h) if h is not None else "" for h in next(rows)]
    i_ts = 0
    i_valve, i_heat = _find_col(header, KEY_VALVE), _find_col(header, KEY_HEAT)
    i_state = _find_col(header, KEY_STATE)
    if i_valve is None or i_heat is None:
        raise ValueError(f"{path}: Spalten {KEY_VALVE} / {KEY_HEAT} nicht gefunden")
    out = []
    for r in rows:
        ts = _parse_ts(r[i_ts]) if r else None
        if ts is None:
            continue
        valve, q = _num(r[i_valve]), _num(r[i_heat])
        state = _num(r[i_state]) if i_state is not None else np.nan
        if not np.isfinite(q) or state == 10:
            q = 0.0
        out.append((ts.replace(tzinfo=tz) if ts.tzinfo is None else ts.astimezone(tz),
                    max(q, 0.0) if valve == 1 else 0.0))
    return out


def accumulate_trendlogs(pattern: str, tz: ZoneInfo, acc: _Accumulator | None = None) -> _Accumulator:
    acc = acc or _Accumulator()
    seen: set[datetime] = set()
    for f in sorted(glob.glob(pattern)) + sorted(glob.glob(re.sub(r"\.xlsx$", ".csv", pattern))):
        try:
            recs = read_trendlog(f, tz)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the app
            log.warning("Trendlog %s übersprungen: %s", f, e)
            continue
        if len(recs) < 2:
            continue
        step_min = max(1.0, (recs[1][0] - recs[0][0]).total_seconds() / 60)
        for ts, q in recs:
            if ts in seen:
                continue
            seen.add(ts)
            acc.add(ts, q * step_min / 60, step_min)
        log.info("Trendlog %s: %d Zeilen", f, len(recs))
    return acc


def accumulate_hourly(hourly: list[tuple[datetime, float]], tz: ZoneInfo,
                      acc: _Accumulator | None = None, skip_days: set | None = None) -> _Accumulator:
    """Hourly DHW heat (kWh, hour start UTC) spread evenly over 4 slots."""
    acc = acc or _Accumulator()
    for t, kwh in hourly:
        if kwh is None or not np.isfinite(kwh):
            continue
        local = t.astimezone(tz)
        if skip_days and local.date() in skip_days:
            continue
        for k in range(4):
            acc.add(local + timedelta(minutes=15 * k), max(kwh, 0.0) / 4, 15)
    return acc
