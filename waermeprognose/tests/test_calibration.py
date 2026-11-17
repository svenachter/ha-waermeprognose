from datetime import datetime, timedelta, timezone

import numpy as np

from app.calibration import fit_heat_model
from app.model import ema
from app.settings import ModelParams


def _synthetic(true: ModelParams, hours=24 * 21, seed=1):
    rng = np.random.default_rng(seed)
    h = np.arange(hours)
    t = 2 - 8 * np.sin(h / (24 * 5)) + 3 * np.sin(2 * np.pi * h / 24)      # -6..13 °C
    gti = np.clip(400 * np.sin(2 * np.pi * (h % 24 - 6) / 24), 0, None) * rng.uniform(0.2, 1, hours)
    td = ema(t, 1.0, true.damping_hours)
    q = (true.q_base_kw + true.q_slope_kw_per_k * td
         + true.knee_slope_kw_per_k * np.maximum(0, true.knee_temp - td)
         - true.solar_gain_kw_per_wm2 * gti + rng.normal(0, 0.8, hours))
    times = [datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(hours=int(i)) for i in h]
    return times, q, t, gti


def test_recovers_parameters_with_weak_prior():
    true = ModelParams(q_base_kw=6.0, q_slope_kw_per_k=-0.3, knee_slope_kw_per_k=1.0,
                       solar_gain_kw_per_wm2=0.004)
    times, q, t, g = _synthetic(true)
    res = fit_heat_model(times, q, t, g, ModelParams(), prior_hours=6)
    p = res.params
    assert abs(p.q_base_kw - 6.0) < 0.6
    assert abs(p.q_slope_kw_per_k + 0.3) < 0.08
    assert abs(p.knee_slope_kw_per_k - 1.0) < 0.4
    assert abs(p.solar_gain_kw_per_wm2 - 0.004) < 0.0015
    assert res.n_blocks > 70


def test_too_little_data_keeps_prior():
    prior = ModelParams()
    times, q, t, g = _synthetic(prior, hours=30)
    res = fit_heat_model(times, q, t, g, prior, prior_hours=72)
    assert res.params == prior and "zu wenig" in res.note


def test_flat_november_data_keeps_knee_prior():
    # data only above the knee (like 11.–15.11.): knee slope must stay at the prior
    prior = ModelParams()
    times = [datetime(2026, 11, 11, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(120)]
    t = np.linspace(9, -1, 120)
    q = np.full(120, 7.1) + np.random.default_rng(2).normal(0, 0.5, 120)
    res = fit_heat_model(times, q, t, np.zeros(120), prior, prior_hours=72)
    assert abs(res.params.knee_slope_kw_per_k - prior.knee_slope_kw_per_k) < 1e-6
    assert abs(res.params.q_base_kw - 7.1) < 0.6


def test_flow_offset_uses_only_space_heating_hours():
    from app.calibration import fit_flow_offset
    curve = np.full(48, 37.0)
    flow = np.full(48, 34.0)
    flow[:10] = 50.0                      # DHW hours -> excluded
    dhw = np.zeros(48); dhw[:10] = 5.0
    heat = np.full(48, 8.0)
    off, n = fit_flow_offset(flow, curve, heat, dhw)
    assert n == 38 and abs(off + 3.0) < 1e-9
    assert fit_flow_offset(flow[:20], curve[:20], heat[:20], dhw[:20])[0] is None
