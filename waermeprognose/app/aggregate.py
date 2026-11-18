"""Hourly aggregation of the 15-min forecast rows (used by API and HA sensor)."""
from __future__ import annotations

AGG_MEAN = ("t_out", "t_damped", "gti", "q_heat_kw", "q_dhw_kw", "q_total_kw", "flow_temp",
            "p_el_kw", "p_backup_kw", "q_heat_p10", "q_heat_p90", "p_el_p10", "p_el_p90",
            "pv_kw", "pv_p10", "pv_p90", "household_kw", "net_kw")
AGG_ANY = ("frost_risk", "below_min_modulation")


def aggregate_hourly(rows: list[dict]) -> list[dict]:
    out, cur, key = [], [], None
    for r in rows:
        k = r["t"][:13]
        if key is not None and k != key:
            out.append(_agg(cur))
            cur = []
        key = k
        cur.append(r)
    if cur:
        out.append(_agg(cur))
    return out


def _agg(rows: list[dict]) -> dict:
    t = rows[0]["t"]
    res = {"t": t[:14] + "00" + t[16:], "slots": len(rows)}
    for k in AGG_MEAN:
        vals = [r[k] for r in rows if r.get(k) is not None]
        res[k] = round(sum(vals) / len(vals), 3) if vals else None
    for k in AGG_ANY:
        res[k] = any(r.get(k) for r in rows)
    q = (res["q_total_kw"] or 0)
    res["cop_system"] = round(q / res["p_el_kw"], 3) if res["p_el_kw"] else None
    return res
