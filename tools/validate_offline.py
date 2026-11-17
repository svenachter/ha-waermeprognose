"""Offline check of the calculation chain against a Lambda trend log + hourly statistics export.

Usage: python tools/validate_offline.py TRENDLOG.xlsx STATISTICS.xlsx
Uses the measured outdoor temperature (1-h mean) instead of a weather forecast, so it
tests demand model, heating curve, COP and DHW profile – not the weather forecast.
"""
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "waermeprognose"))
from app import dhw  # noqa: E402
from app.model import CurvePoints, ForecastInputs, ema, run  # noqa: E402
from app.settings import ModelParams  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")


def load_log(path):
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    it = ws.iter_rows(values_only=True)
    h = [str(c) for c in next(it)]
    col = lambda k: next(i for i, c in enumerate(h) if k in c)  # noqa: E731
    i_t = col('key:"ambient.1"')
    ts, t = [], []
    for r in it:
        ts.append(r[0].replace(tzinfo=TZ)); t.append(float(r[i_t]))
    return ts, np.array(t)


def load_stats(path):
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    it = ws.iter_rows(values_only=True); next(it)
    out = {}
    from datetime import datetime
    for r in it:
        end = datetime.fromisoformat(str(r[0])).astimezone(TZ)
        out[end - timedelta(hours=1)] = (float(r[1]), float(r[2]))  # el, th
    return out


def main(log_path, stats_path):
    ts, t = load_log(log_path)
    # 15-min means
    n = len(ts) // 15
    t15 = t[: n * 15].reshape(n, 15).mean(1)
    times = [ts[i * 15] for i in range(n)]
    acc = dhw.accumulate_trendlogs(log_path, TZ)
    prof = dhw.build_profile(acc, "logfile")
    p = ModelParams()
    out = run(ForecastInputs(times, t15, ema(t15, 0.25, p.damping_hours), np.full(n, 80.0), np.zeros(n),
                             np.array([prof.kw_at(x) for x in times]), [CurvePoints(48.3, 39.0, 32.0, -1.0)]), p)
    stats = load_stats(stats_path)
    print(f"{'Tag':<11}{'Q mod':>8}{'Q ist':>8}{'P mod':>8}{'P ist*1.07':>11}{'COP mod':>9}{'COP ist':>9}")
    days = sorted({x.date() for x in times})
    for d in days:
        idx = [i for i, x in enumerate(times) if x.date() == d]
        hrs = [k for k in stats if k.date() == d]
        if len(hrs) < 24:
            continue
        qm = out["q_total_kw"][idx].sum() * 0.25; pm = out["p_el_kw"][idx].sum() * 0.25
        qi = sum(stats[k][1] for k in hrs); pi = sum(stats[k][0] for k in hrs) * 1.07
        print(f"{d!s:<11}{qm:8.1f}{qi:8.1f}{pm:8.1f}{pi:11.1f}{qm/pm:9.2f}{qi/pi:9.2f}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
