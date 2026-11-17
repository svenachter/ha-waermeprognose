from datetime import datetime, timezone

import numpy as np

from app.model import CurvePoints, ForecastInputs, cop_carnot, ema, heat_demand_kw, run, slot_grid
from app.settings import ModelParams


def test_heating_curve_matches_lambda_controller():
    # HA showed: HC1 set point 35.3 °C at damped outdoor temp 8.5 °C, offset -1 K
    c = CurvePoints(48.3, 39.0, 32.0, offset=-1.0)
    assert abs(float(c.flow(np.array([8.5]))[0]) - 35.3) < 0.05
    # clamped outside -22..+22
    assert float(c.flow(np.array([30.0]))[0]) == 31.0


def test_cop_carnot_values():
    cop = cop_carnot(np.array([35.0]), np.array([6.5]), 0.56)[0]
    assert abs(cop - 0.56 * 308.15 / 28.5) < 1e-9
    assert cop_carnot(np.array([35.0]), np.array([34.0]), 0.56)[0] <= 12.0  # min lift 5 K, cap 12


def test_ema_converges():
    out = ema(np.r_[np.zeros(10), np.ones(400)], 0.25, 12.0)
    assert out[9] == 0 and 0.98 < out[-1] <= 1.0


def test_heat_demand_knee_and_floor():
    p = ModelParams()
    q = heat_demand_kw(np.array([9.0, -3.0, -10.0]), np.zeros(3), p)
    assert abs(q[0] - (7.2 - 0.45)) < 1e-9
    assert abs(q[1] - (7.2 + 0.15)) < 1e-9
    assert abs(q[2] - (7.2 + 0.5 + 1.76 * 7)) < 1e-9          # ≈ 20 kW design point
    p.solar_gain_kw_per_wm2 = 1.0
    assert heat_demand_kw(np.array([5.0]), np.array([100.0]), p)[0] == 0.0


def _inputs(t_out, q_dhw=0.0):
    n = len(t_out)
    times = slot_grid(datetime(2026, 11, 17, 12, 7, tzinfo=timezone.utc), n / 4)
    t = np.asarray(t_out, float)
    return ForecastInputs(times, t, t, np.full(n, 80.0), np.zeros(n), np.full(n, q_dhw),
                          [CurvePoints(48.3, 39.0, 32.0, -1.0)])


def test_run_frost_switches_eta_and_backup():
    p = ModelParams()
    out = run(_inputs([8.0, 0.0, -14.0, -14.0]), p)
    assert not out["frost_risk"][0] and out["frost_risk"][1]
    # at -14 °C demand (7.2+0.7+1.76*11=27.3) exceeds max output (~18.3 kW) -> heating rod
    assert out["p_backup_kw"][2] > 5
    assert np.all(out["p_el_kw"] > 0)
    # system COP at 8 °C close to the measured 6–7 range
    assert 5.5 < out["cop_system"][0] < 7.5


def test_slot_grid_aligns_to_quarter():
    g = slot_grid(datetime(2026, 11, 17, 12, 7, 33, tzinfo=timezone.utc), 1)
    assert g[0].minute == 0 and len(g) == 4 and g[1].minute == 15
