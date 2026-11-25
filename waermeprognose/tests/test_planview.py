import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import planview
from app.settings import load_settings

TZ = ZoneInfo("Europe/Berlin")


def test_dev_class_thresholds():
    assert planview.dev_class(None) == ""
    assert planview.dev_class(10.0) == "g" and planview.dev_class(-10.0) == "g"
    assert planview.dev_class(10.1) == "y" and planview.dev_class(-25.0) == "y"
    assert planview.dev_class(25.1) == "r"


def test_fmt_german_and_no_negative_zero():
    assert planview.fmt(1.234, 2) == "1,23"
    assert planview.fmt(-0.01, 1) == "0,0"
    assert planview.fmt(None) == "–"


def test_economics_plan_and_actual():
    r = {"price_ct": 30.0, "co2_g": 400.0,
         "plan": {"net": 2.0},
         "ist": {"imp": 1.5, "exp": 1.0, "gas": 10.0}}
    planview.economics(r, feedin_ct=8.0, gas_ct=8.0, gas_co2=240.0)
    assert abs(r["plan"]["cost"] - 0.60) < 1e-9 and abs(r["plan"]["co2"] - 0.8) < 1e-9
    # 1.5*30 - 1*8 + 10*8 = 117 ct
    assert abs(r["ist"]["cost"] - 1.17) < 1e-9
    assert abs(r["ist"]["co2"] - (1.5 * 400 + 10 * 240) / 1000) < 1e-9
    surplus = {"price_ct": 30.0, "co2_g": None, "plan": {"net": -3.0}, "ist": None}
    planview.economics(surplus, 8.0, 8.0, 240.0)
    assert abs(surplus["plan"]["cost"] + 0.24) < 1e-9 and surplus["plan"]["co2"] is None


def test_parse_series_attr_and_factors():
    attrs = {"raw_today": [{"start": "2026-11-25T09:00:00Z", "value": 297.1}],
             "raw_forecast": [{"start": "2026-11-25T10:00:00Z", "value": 280.7}, {"bad": 1}]}
    m = planview.parse_series_attr(attrs)
    assert m[datetime(2026, 11, 25, 10, tzinfo=timezone.utc)] == 280.7 and len(m) == 2
    assert planview.power_factor("W") == 0.001 and planview.power_factor("kW") == 1.0
    assert planview.price_factor("EUR/kWh") == 100.0 and planview.price_factor("ct/kWh") == 1.0


def _rows(now):
    end = now.replace(minute=0, second=0, microsecond=0)
    rows = []
    for i in range(-30, 20):
        t = end + timedelta(hours=i)
        past = t < end
        plan = {"q": 10.0, "qw": 2.0, "p": 3.0, "pv": 0.5, "hh": 2.0, "net": 4.5, "cost": 1.0, "co2": 1.5}
        ist = ({"q": 12.0 if i % 2 else 10.5, "qw": 2.0, "p": 4.0, "pv": 0.5, "hh": 2.0, "net": 4.0,
                "chp_el": 7.5, "chp_th": 22.5, "buf_hp_top": 45.0, "buf_hp_bot": 30.0, "buf_chp": 80.0,
                "cost": 0.9, "co2": 1.4} if past else None)
        rows.append({"t": t, "past": past, "plan": plan, "ist": ist})
    return rows


def test_render_structure():
    now = datetime(2026, 11, 25, 10, 30, tzinfo=timezone.utc)
    out = planview.render(_rows(now), TZ, now, {"lead_hours": 24, "notes": ["Hinweis <x>"]})
    assert out.startswith("<style>") and '<div class="wp">' in out
    assert "Heute · Mi 25.11." in out and "Gestern · Di 24.11." in out and "Rückblick" in out
    assert out.count("<details open>") == 2                    # today + tomorrow
    assert 'class="n f"' in out                                 # now marker in today's table
    assert 'class="r"' in out and 'class="g"' in out           # +20 %/-4.8 % heat, -25 % power
    assert "45/30" in out and "Hinweis &lt;x&gt;" in out
    # future rows: plan only, no "/" pair
    tomorrow = out.split("Morgen · Do 26.11.")[1].split("</details>")[0]
    assert '<span class="p">' not in tomorrow.split("</summary>")[1]


def test_settings_plan_view_defaults_and_override(tmp_path):
    p = tmp_path / "options.json"
    p.write_text('{"plan_view": {"days": 30, "feedin_ct_per_kwh": 7.9, "chp_power": ""}}')
    s = load_settings(p)
    assert s.plan_view.days == 13 and s.plan_view.feedin_ct_per_kwh == 7.9
    assert s.plan_view.chp_power == "" and s.plan_view.price == "sensor.strompreis_brutto_aktuell"
