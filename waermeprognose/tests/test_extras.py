from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from app import extras
from app.model import FixedFlow

TZ = ZoneInfo("Europe/Berlin")


def test_pv_parse_and_align():
    resp = {"forecast": [
        {"datetime": "2026-11-18T11:00:00+01:00", "watts": 6081.43, "p10": 2683.48, "p90": 8380.6},
        {"datetime": "2026-11-18T11:15:00+01:00", "watts": 5563.36, "p10": None, "p90": None},
        {"datetime": "kaputt", "watts": 1}]}
    pv = extras.parse_pv_forecast(resp)
    assert len(pv) == 2
    slots = [datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=15 * i) for i in range(3)]
    kw, lo, hi = extras.pv_for_slots(pv, slots)
    assert abs(kw[0] - 6.08143) < 1e-9 and abs(lo[0] - 2.68348) < 1e-9
    assert lo[1] == kw[1] == hi[1]          # missing band -> mean value
    assert np.isnan(kw[2])


def _days(values_by_date):
    out = {}
    for d, kw in values_by_date.items():
        start = datetime(d.year, d.month, d.day, tzinfo=TZ).astimezone(timezone.utc)
        rows = [(start + timedelta(minutes=5 * i), kw) for i in range(288)]
        out.update(extras.daily_slots(rows, TZ, 5))
    return out


def test_household_profile_split_and_aggregate():
    # Mon 9.11. .. Mon 16.11.2026; weekend days 14./15.11.
    vals = {date(2026, 11, d): (2.0 if date(2026, 11, d).weekday() < 5 else 3.0) for d in range(9, 17)}
    vals[date(2026, 11, 13)] = 8.0          # one-off outlier on Friday
    days = _days(vals)
    p = extras.build_household_profile(days, today=date(2026, 11, 17), n_days=3)
    assert p.days_workday == ["2026-11-16", "2026-11-13", "2026-11-12"]
    assert abs(p.workday[0] - 4.0) < 1e-9                  # mean (2+8+2)/3
    assert p.days_weekend == ["2026-11-15", "2026-11-14"] and p.weekend[10] == 3.0
    med = extras.build_household_profile(days, today=date(2026, 11, 17), n_days=3, aggregate="median")
    assert med.workday[0] == 2.0                            # median ignores the outlier
    assert p.kw_at(datetime(2026, 11, 21, 12, 0, tzinfo=TZ)) == 3.0   # Saturday
    assert abs(p.summary()["daily_kwh_workday"] - 96.0) < 0.1


def test_incomplete_and_today_excluded():
    days = _days({date(2026, 11, 16): 2.0, date(2026, 11, 17): 9.0})
    s, c = days[date(2026, 11, 16)]
    c[:50] = 0                                               # drop half a day
    days[date(2026, 11, 16)] = (s, c)
    p = extras.build_household_profile(days, today=date(2026, 11, 17))
    assert p.workday is None and np.isnan(p.kw_at(datetime(2026, 11, 18, 8, tzinfo=TZ)))


def test_hourly_rows_fill_four_slots():
    start = datetime(2026, 11, 15, 23, tzinfo=timezone.utc)  # 16.11. 00:00 local
    rows = [(start + timedelta(hours=h), 1.5) for h in range(24)]
    d = extras.daily_slots(rows, TZ, 60)[date(2026, 11, 16)]
    assert np.allclose(d[0], 1.5) and d[1].sum() == 1440


def test_fixed_flow():
    assert FixedFlow(30.0).flow(np.array([1.0, 2.0])).tolist() == [30.0, 30.0]
