from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app import dhw
from conftest import FIXTURES

TZ = ZoneInfo("Europe/Berlin")
LOG_CSV = FIXTURES / "trendlog_2026-11-11_15.csv"


def test_read_trendlog_csv():
    recs = dhw.read_trendlog(LOG_CSV, TZ)
    assert len(recs) == 7200
    total = sum(q for _, q in recs) / 60
    # DHW heat 11.–15.11. from the analysis: ≈ 236 kWh (valve open, defrost excluded)
    assert 200 < total < 260


def test_profile_from_trendlog_complete_days():
    acc = dhw.accumulate_trendlogs(str(FIXTURES / "*.xlsx"), TZ)  # also picks up *.csv
    prof = dhw.build_profile(acc, "logfile")
    assert prof is not None and prof.days_workday == 5 and prof.weekend is None
    assert 35 < prof.daily_kwh() < 55
    # morning charge around 07:00 local is the peak
    assert int(np.argmax(prof.workday)) // 4 in (6, 7, 8)


def test_profile_from_hourly_stats_and_merge():
    start = datetime(2026, 11, 2, 23, tzinfo=timezone.utc)  # 00:00 local
    rows = [(start + timedelta(hours=h), 2.0 if (h % 24) == 7 else 0.5) for h in range(24 * 8)]
    acc = dhw.accumulate_hourly(rows, TZ)
    prof = dhw.build_profile(acc, "ha_statistics")
    assert prof.weekend is None or prof.days_weekend >= 3
    assert abs(prof.daily_kwh() - (23 * 0.5 + 2.0)) < 1e-6
    assert prof.kw_at(datetime(2026, 11, 20, 7, 30, tzinfo=TZ)) == pytest.approx(2.0)


def test_missing_columns_raise(tmp_path: Path):
    f = tmp_path / "x.csv"
    f.write_text("Zeitstempel;foo\n11.11.2026 00:00:00;1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        dhw.read_trendlog(f, TZ)


def test_original_xlsx_if_available():
    p = Path("/mnt/user-data/uploads/ab58d549-heatpump_log_2026-11-11_bis_2026-11-15.xlsx")
    if not p.exists():
        pytest.skip("Original-Trendlog nicht vorhanden")
    recs = dhw.read_trendlog(p, TZ)
    assert len(recs) == 7200
