from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.model import (PresenceModel, Sample, UsageModel, daily_km, estimate_kwh_per_km,
                       weighted_quantile)
from app.planner import Hour, advise, cheapest, immediate, surplus_window

TZ = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def test_weighted_quantile():
    assert weighted_quantile([1, 2, 3, 4], [1, 1, 1, 1], 0.5) == 2
    assert weighted_quantile([1, 2, 3, 4], [1, 1, 1, 1], 0.8) == 4
    assert weighted_quantile([], [], 0.5) is None


def test_kwh_per_km_learned_and_fallback():
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    s = [Sample(t0, soc=80, odo=1000), Sample(t0 + timedelta(hours=5), soc=60, odo=1070),
         Sample(t0 + timedelta(hours=8), soc=90, odo=1070, charge_kw=11),   # charging: ignored
         Sample(t0 + timedelta(hours=20), soc=70, odo=1140)]
    v, info = estimate_kwh_per_km(s, 70, 0.2)
    assert info["source"] == "learned"
    assert abs(v - (0.4 * 70) / 140) < 1e-6
    # SoC reported late: km in one sample, SoC drop in the next
    lag = [Sample(t0, soc=80, odo=1000), Sample(t0 + timedelta(hours=1), soc=80, odo=1050),
           Sample(t0 + timedelta(hours=2), soc=65, odo=1050)]
    v2, _ = estimate_kwh_per_km(lag, 70, 0.2, min_km=10)
    assert abs(v2 - 0.15 * 70 / 50) < 1e-6
    v, info = estimate_kwh_per_km(s[:2], 70, 0.2, min_km=100)
    assert v == 0.2 and info["source"] == "fallback"


def test_daily_km_spreads_gaps():
    t = lambda d, h: datetime(2026, 11, d, h, tzinfo=UTC)
    s = [Sample(t(1, 10), odo=100), Sample(t(2, 10), odo=130), Sample(t(5, 10), odo=190)]
    km = daily_km(s, TZ)
    assert km[date(2026, 11, 2)] == 30
    assert km[date(2026, 11, 3)] == km[date(2026, 11, 5)] == 20


def test_usage_days_left():
    today = date(2026, 11, 25)
    data = {today - timedelta(days=i): 10.0 for i in range(1, 29)}
    m = UsageModel.build(data, today)
    assert m.quantile(0, 0.8) == 10.0
    start = datetime(2026, 11, 25, 23, 0, tzinfo=TZ)        # 1/24 of today left
    dl = m.days_left(start, TZ, 20.0, 0.8)
    assert abs(dl - (1 / 24 + 20 / 10 - 10 / 24 / 10)) < 0.01
    thin = UsageModel.build({}, today, default_kwh=7)
    assert thin.quantile(3, 0.8) == 7


def test_presence_prior_and_learning():
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    samples = []
    for d in range(1, 43):
        t = now - timedelta(days=d)
        samples.append(Sample(t, home=(d % 7 != 0)))   # away on the same weekday each week
    m = PresenceModel.build(samples, TZ, now)
    assert m.prob(now, TZ) < 0.5            # same weekday/hour as the two absences
    assert m.prob(now + timedelta(days=1), TZ) > 0.8
    assert PresenceModel().prob(now, TZ) == 0.9


def _hours(start, surplus, price=23.3, present=1.0):
    return [Hour(start + timedelta(hours=i), s, price, present) for i, s in enumerate(surplus)]


def test_cheapest_prefers_surplus_and_min_power():
    start = datetime(2026, 11, 26, 6, tzinfo=UTC)
    h = _hours(start, [0, 0, 3.0, 8.0, 12.0, 6.0, 0])
    p = cheapest(h, 20, charge_kw=11, min_kw=4.1, feedin_ct=8.3)
    # 3 kW is below the minimum charging power -> not usable
    assert p.kwh_surplus == 20
    assert p.kwh == 20 and p.kwh_grid == 0
    assert abs(p.cost_eur - 20 * 0.083) < 1e-9
    base = immediate(h, 20, 11, 4.1, 8.3)
    assert base.kwh_grid == 20 and abs(base.cost_eur - 20 * 0.233) < 1e-9
    w = surplus_window(p)
    assert w[0] == start + timedelta(hours=3) and w[2] == 20


def test_cheapest_respects_presence():
    start = datetime(2026, 11, 26, 6, tzinfo=UTC)
    h = _hours(start, [11, 11, 0, 0])
    h[0].present = h[1].present = 0.2
    p = cheapest(h, 10, 11, 4.1, 8.3)
    assert p.kwh_surplus == 0 and p.kwh_grid == 10
    p = cheapest(h, 10, 11, 4.1, 8.3, presence_min=0.0)
    assert p.kwh_surplus == 10


def _adv(**kw):
    base = dict(now=datetime(2026, 11, 26, 7, tzinfo=UTC), tz=TZ, soc=40, capacity_kwh=72.6,
                target_soc=80, reserve_soc=15, days_left=4.0, plugged=False,
                hours=_hours(datetime(2026, 11, 26, 7, tzinfo=UTC), [0, 0, 0, 9, 11, 11, 7, 0]),
                charge_kw=11, min_kw=4.1, feedin_ct=8.3, urgent_days=1.5, min_savings_eur=1.0,
                min_kwh=5, vehicle="IONIQ 5")
    base.update(kw)
    return advise(**base)


def test_advise_opportunity():
    a = _adv()
    assert a.status == "opportunity"
    assert "Uhr" in a.title and a.key.startswith("window:")
    assert a.window["savings_eur"] >= 1
    assert a.as_dict(TZ)["plan"]["kwh_surplus"] == 29.0


def test_advise_urgent_and_plugged_and_ok():
    assert _adv(days_left=1.0).status == "urgent"
    assert _adv(plugged=True).status == "plugged"
    flat = _hours(datetime(2026, 11, 26, 7, tzinfo=UTC), [0] * 48)
    assert _adv(hours=flat).status == "ok"
    assert _adv(soc=None).status == "unknown"
    assert _adv(soc=85).status == "ok"          # nothing to charge


from app.notify import ACTION_SNOOZE, handle_action, in_quiet, mark_sent, should_send


def test_notify_rules():
    a = _adv()
    st = {}
    q = dict(quiet_start="21:30", quiet_end="07:30", lead_hours=16)
    now = datetime(2026, 11, 26, 7, tzinfo=UTC)               # 08:00 local
    assert should_send(a, now, TZ, st, **q) == (True, "ok")
    mark_sent(st, a.key, now)
    assert should_send(a, now, TZ, st, **q)[1] == "bereits gemeldet"
    assert in_quiet(datetime(2026, 11, 26, 22, tzinfo=UTC), TZ, "21:30", "07:30")
    assert not in_quiet(datetime(2026, 11, 26, 12, tzinfo=UTC), TZ, "21:30", "07:30")
    st2 = {}
    handle_action(st2, ACTION_SNOOZE, now)
    assert should_send(a, now + timedelta(hours=1), TZ, st2, **q)[1] == "pausiert"
    assert should_send(_adv(plugged=True), now, TZ, {}, **q)[0] is False
