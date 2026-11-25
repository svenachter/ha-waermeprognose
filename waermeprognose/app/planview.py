"""Plan/actual table as HTML for a Home Assistant HTML template card.

Hour rows grouped by local day: completed hours show forecast / actual (forecast taken
with the preferred lead time), the current and future hours show the latest forecast.
Pure functions only; data gathering lives in ``service.ForecastService.plan_view``.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# hourly floors (kWh) below which a percentage deviation is not meaningful
FLOORS = {"q": 0.2, "qw": 0.1, "p": 0.05, "pv": 0.05, "hh": 0.05}
DEV_KEYS = tuple(FLOORS)
SUM_KEYS = ("q", "qw", "p", "pv", "hh", "net", "chp_el", "chp_th", "gas", "cost", "co2")
MEAN_KEYS = ("buf_hp_top", "buf_hp_bot", "buf_chp")
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

CSS = (
    ".wp{font-size:12px;line-height:1.35}"
    ".wp .lg{color:var(--secondary-text-color);margin:0 0 6px}"
    ".wp details{border:1px solid var(--divider-color);border-radius:8px;margin:6px 0}"
    ".wp summary{cursor:pointer;padding:6px 8px;list-style-position:inside}"
    ".wp summary b{margin-right:8px}"
    ".wp .k{display:inline-block;margin:0 10px 0 0;white-space:nowrap}"
    ".wp .x{overflow-x:auto;border-top:1px solid var(--divider-color)}"
    ".wp table{border-collapse:collapse;width:100%;white-space:nowrap}"
    ".wp th{position:sticky;top:0;background:var(--card-background-color);font-weight:600;"
    "text-align:right;padding:3px 6px;border-bottom:1px solid var(--divider-color)}"
    ".wp td{text-align:right;padding:1px 6px;border-bottom:1px solid rgba(127,127,127,.12)}"
    ".wp th:first-child,.wp td:first-child{text-align:left}"
    ".wp .p{color:var(--secondary-text-color)}"
    ".wp .f td{color:var(--secondary-text-color)}"
    ".wp .n td{border-top:2px solid var(--primary-color)}"
    ".wp .s td{font-weight:600;border-top:1px solid var(--divider-color)}"
    ".wp .g{background:rgba(67,160,71,.22)}.wp .y{background:rgba(251,192,45,.32)}"
    ".wp .r{background:rgba(229,57,53,.28)}"
    ".wp .h{margin:12px 0 2px;font-weight:600}"
)

COLUMNS = [  # key, header, decimals, deviation-coloured
    ("q", "Wärme", 1, True),
    ("qw", "davon WW", 1, True),
    ("p", "WP-Strom", 2, True),
    ("cop", "COP", 2, False),
    ("pv", "PV", 2, True),
    ("hh", "Haushalt", 2, True),
    ("net", "Netz ±", 2, False),
    ("chp_el", "BHKW el", 2, False),
    ("chp_th", "BHKW th", 1, False),
    ("buf_hp", "Puffer WP o/u", 0, False),
    ("buf_chp", "Puffer BHKW", 0, False),
    ("cost", "Kosten €", 2, False),
    ("co2", "CO₂ kg", 1, False),
]


def dev_pct(fc, ist, floor):
    if fc is None or ist is None or abs(ist) < floor:
        return None
    return (fc / ist - 1) * 100


def dev_class(d) -> str:
    if d is None:
        return ""
    a = abs(d)
    return "g" if a <= 10 else ("y" if a <= 25 else "r")


def fmt(v, nd=1) -> str:
    if v is None:
        return "–"
    v = round(float(v), nd)
    if v == 0:
        v = 0.0                      # avoid "-0,0"
    return f"{v:.{nd}f}".replace(".", ",")


def _add(a, b):
    if a is None:
        return b
    return a if b is None else a + b


def economics(r: dict, feedin_ct: float, gas_ct: float | None, gas_co2: float) -> None:
    """Fill plan/actual cost (€) and CO2 (kg) of one hour row in place."""
    price, co2 = r.get("price_ct"), r.get("co2_g")
    pl = r["plan"]
    if pl.get("net") is not None and price is not None:
        imp, exp = max(pl["net"], 0.0), max(-pl["net"], 0.0)
        pl["cost"] = (imp * price - exp * feedin_ct) / 100
        pl["co2"] = imp * co2 / 1000 if co2 is not None else None
    ist = r.get("ist")
    if ist and ist.get("imp") is not None and price is not None:
        gas = ist.get("gas") or 0.0
        ist["cost"] = (ist["imp"] * price - (ist.get("exp") or 0.0) * feedin_ct
                       + gas * (gas_ct or 0.0)) / 100
        if co2 is not None:
            ist["co2"] = (ist["imp"] * co2 + gas * gas_co2) / 1000


def _cop(d: dict):
    q, p = d.get("q"), d.get("p")
    return q / p if q is not None and p else None


def _cell(key, nd, plan, ist, colour, past) -> str:
    if key == "buf_hp":
        if not past or ist is None or (ist.get("buf_hp_top") is None and ist.get("buf_hp_bot") is None):
            return "<td></td>"
        return f"<td>{fmt(ist.get('buf_hp_top'), 0)}/{fmt(ist.get('buf_hp_bot'), 0)}</td>"
    if key in ("chp_el", "chp_th", "buf_chp"):
        v = (ist or {}).get(key) if past else None
        return f"<td>{fmt(v, nd)}</td>" if past and v is not None else "<td></td>"
    pv_ = plan.get(key) if plan else None
    if not past:
        return f"<td>{fmt(pv_, nd)}</td>" if pv_ is not None else "<td>–</td>"
    iv = (ist or {}).get(key)
    cls = ""
    if colour:
        cls = dev_class(dev_pct(pv_, iv, FLOORS[key] * colour))
    cls = f' class="{cls}"' if cls else ""
    return f'<td{cls}><span class="p">{fmt(pv_, nd)}</span>/{fmt(iv, nd)}</td>'


def _row(label, plan, ist, past, extra_cls="") -> str:
    cells = "".join(_cell(k, nd, plan, ist, 1 if dev else 0, past) for k, _h, nd, dev in COLUMNS)
    cls = " ".join(c for c in (extra_cls, "" if past else "f") if c)
    attr = f' class="{cls}"' if cls else ""
    return f"<tr{attr}><td>{label}</td>{cells}</tr>"


def _day_totals(rows: list[dict]) -> dict:
    """Paired sums over completed hours (plan and actual both present) + plan of open hours."""
    pair_p, pair_i = {}, {}
    n = {k: 0 for k in SUM_KEYS}
    means = {k: [] for k in MEAN_KEYS}
    rest = {}
    for r in rows:
        pl, ist = r["plan"], r.get("ist") or {}
        if r["past"]:
            for k in SUM_KEYS:
                if k in ("chp_el", "chp_th", "gas"):
                    if ist.get(k) is not None:
                        pair_i[k] = _add(pair_i.get(k), ist[k])
                    continue
                if pl.get(k) is not None and ist.get(k) is not None:
                    pair_p[k] = _add(pair_p.get(k), pl[k])
                    pair_i[k] = _add(pair_i.get(k), ist[k])
                    n[k] += 1
            for k in MEAN_KEYS:
                if ist.get(k) is not None:
                    means[k].append(ist[k])
        else:
            for k in SUM_KEYS:
                if pl.get(k) is not None:
                    rest[k] = _add(rest.get(k), pl[k])
    ist_tot = {**pair_i, **{k: sum(v) / len(v) for k, v in means.items() if v}}
    pair_p["cop"], ist_tot["cop"] = _cop(pair_p), _cop(ist_tot)
    return {"plan": pair_p, "ist": ist_tot, "n": n, "rest": rest}


def _summary_line(label: str, tot: dict, has_past: bool) -> str:
    parts = [f"<b>{label}</b>"]
    pl, ist, rest = tot["plan"], tot["ist"], tot["rest"]
    items = (("q", "Wärme", 0, "kWh"), ("p", "WP-Strom", 1, "kWh"), ("pv", "PV", 1, "kWh"),
             ("hh", "Haushalt", 1, "kWh"), ("cost", "Kosten", 2, "€"), ("co2", "CO₂", 1, "kg"))
    for k, name, nd, unit in items:
        if has_past and (pl.get(k) is not None or ist.get(k) is not None):
            cls = ""
            if k in FLOORS:
                cls = dev_class(dev_pct(pl.get(k), ist.get(k), FLOORS[k] * max(tot["n"][k], 1)))
            txt = f'{name} <span class="p">{fmt(pl.get(k), nd)}</span>/{fmt(ist.get(k), nd)} {unit}'
            parts.append(f'<span class="k {cls}">{txt}</span>' if cls else f'<span class="k">{txt}</span>')
        elif rest.get(k) is not None:
            parts.append(f'<span class="k">{name} {fmt(rest[k], nd)} {unit}</span>')
    if has_past and rest.get("q") is not None:
        parts.append(f'<span class="k p">Rest: Wärme {fmt(rest["q"], 0)} kWh, '
                     f'WP-Strom {fmt(rest.get("p"), 1)} kWh</span>')
    if has_past and ist.get("chp_el"):
        parts.append(f'<span class="k">BHKW {fmt(ist["chp_el"], 1)} kWh el</span>')
    return " ".join(parts)


def _day_label(d: date, today: date) -> str:
    rel = {0: "Heute", 1: "Morgen", 2: "Übermorgen", -1: "Gestern"}.get((d - today).days)
    base = f"{WEEKDAYS[d.weekday()]} {d.day:02d}.{d.month:02d}."
    return f"{rel} · {base}" if rel else base


def render(rows: list[dict], tz: ZoneInfo, now: datetime, meta: dict) -> str:
    """rows: dicts with t (UTC hour start), past (bool), plan {..}, ist {..} | None."""
    today = now.astimezone(tz).date()
    days: dict[date, list[dict]] = {}
    for r in sorted(rows, key=lambda x: x["t"]):
        days.setdefault(r["t"].astimezone(tz).date(), []).append(r)
    head = "".join(f"<th>{h}</th>" for _k, h, _n, _d in COLUMNS)
    blocks_future, blocks_past = [], []
    for d, drows in days.items():
        has_past = any(r["past"] for r in drows)
        body, marked = [], False
        for r in drows:
            lt = r["t"].astimezone(tz)
            extra = ""
            if not r["past"] and not marked and has_past:
                extra, marked = "n", True
            plan = {**r["plan"], "cop": _cop(r["plan"])}
            ist = r.get("ist")
            if ist is not None:
                ist = {**ist, "cop": _cop(ist)}
            body.append(_row(f"{lt:%H}:00", plan, ist, r["past"], extra))
        tot = _day_totals(drows)
        if has_past:
            body.append(_row("Summe", tot["plan"], tot["ist"], True, "s"))
        label = _day_label(d, today)
        open_ = " open" if d in (today, today + timedelta(days=1)) else ""
        block = (f"<details{open_}><summary>{_summary_line(label, tot, has_past)}</summary>"
                 f'<div class="x"><table><tr><th>Zeit</th>{head}</tr>{"".join(body)}</table></div>'
                 f"</details>")
        (blocks_future if d >= today else blocks_past).append(block)
    lead = meta.get("lead_hours")
    legend = (
        f'<p class="lg">Je Stunde <span class="p">Prognose</span>/Ist in kWh (Temperaturen °C, '
        f"Kosten €, CO₂ kg). Prognose der vergangenen Stunden mit ≈ {fmt(lead, 0)} h Vorlauf, "
        "ab der Linie aktuelle Prognose. Abweichung Prognose/Ist: "
        '<span class="g">&nbsp;≤ 10 %&nbsp;</span> <span class="y">&nbsp;≤ 25 %&nbsp;</span> '
        '<span class="r">&nbsp;&gt; 25 %&nbsp;</span>. Kosten- und CO₂-Prognose = reiner '
        "Netzbedarf ohne Batterie und BHKW; Ist = Netzzähler + BHKW-Gas. "
        f"Stand {now.astimezone(tz):%d.%m. %H:%M}.</p>")
    notes = "".join(f'<p class="lg">{html.escape(n)}</p>' for n in meta.get("notes", []))
    past_html = ('<div class="h">Rückblick</div>' + "".join(reversed(blocks_past))) if blocks_past else ""
    return (f"<style>{CSS}</style><div class=\"wp\">{legend}{notes}"
            f"{''.join(blocks_future)}{past_html}</div>")


# ---------------------------------------------------------------- data helpers
def parse_series_attr(attrs: dict | None, factor: float = 1.0) -> dict[datetime, float]:
    """raw_today / raw_tomorrow / raw_forecast lists ([{start, value}]) -> {UTC hour: value}."""
    out: dict[datetime, float] = {}
    if not attrs:
        return out
    for key in ("raw_today", "raw_tomorrow", "raw_forecast"):
        for it in attrs.get(key) or []:
            try:
                t = datetime.fromisoformat(str(it["start"]).replace("Z", "+00:00")).astimezone(timezone.utc)
                out[t.replace(minute=0, second=0, microsecond=0)] = float(it["value"]) * factor
            except (KeyError, TypeError, ValueError):
                continue
    return out


def power_factor(unit: str | None) -> float:
    """Factor to kW for a power unit (statistics mean over one hour -> kWh)."""
    u = (unit or "").strip()
    return {"W": 0.001, "kW": 1.0, "MW": 1000.0}.get(u, 0.001)


def price_factor(unit: str | None) -> float:
    """Factor to ct/kWh."""
    u = (unit or "").lower()
    return 100.0 if ("eur" in u or "€" in u) and "ct" not in u and "cent" not in u else 1.0
