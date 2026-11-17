"""Pure calculation chain: weather -> heat demand -> flow temperature -> COP -> electric power.

All functions are side-effect free and work on numpy arrays so they can be tested
and re-run offline against logged data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .settings import ModelParams

K0 = 273.15
SLOT_H = 0.25  # 15 min


@dataclass
class CurvePoints:
    """Lambda heating curve: flow set point at -22 / 0 / +22 °C plus offset."""
    cold: float
    mid: float
    warm: float
    offset: float = 0.0

    def flow(self, t_out: np.ndarray) -> np.ndarray:
        t = np.clip(np.asarray(t_out, dtype=float), -22.0, 22.0)
        return np.interp(t, [-22.0, 0.0, 22.0], [self.cold, self.mid, self.warm]) + self.offset


def ema(values: np.ndarray, dt_h: float, tau_h: float, init: float | None = None) -> np.ndarray:
    """Exponential smoothing (building/controller damping of outdoor temperature)."""
    v = np.asarray(values, dtype=float)
    out = np.empty_like(v)
    a = 1.0 - np.exp(-dt_h / max(tau_h, 1e-6))
    acc = v[0] if init is None else init
    for i, x in enumerate(v):
        if np.isfinite(x):
            acc = acc + a * (x - acc)
        out[i] = acc
    return out


def heat_demand_kw(t_damp: np.ndarray, gti_wm2: np.ndarray, p: ModelParams) -> np.ndarray:
    """Space heating (incl. pool) demand in kW.

    q = base + slope*T + knee_slope*max(0, knee - T) - solar*GTI, clipped at 0.
    The knee term adds the steeper load increase expected in cold weather
    that the November data could not show yet.
    """
    t = np.asarray(t_damp, dtype=float)
    g = np.nan_to_num(np.asarray(gti_wm2, dtype=float))
    q = (p.q_base_kw + p.q_slope_kw_per_k * t
         + p.knee_slope_kw_per_k * np.maximum(0.0, p.knee_temp - t)
         - p.solar_gain_kw_per_wm2 * g)
    return np.maximum(q, 0.0)


def cop_carnot(t_flow: np.ndarray, t_source: np.ndarray, eta: np.ndarray | float) -> np.ndarray:
    lift = np.maximum(np.asarray(t_flow, float) - np.asarray(t_source, float), 5.0)
    cop = np.asarray(eta, float) * (np.asarray(t_flow, float) + K0) / lift
    return np.clip(cop, 1.0, 12.0)


def q_max_kw(t_out: np.ndarray, table: list[list[float]]) -> np.ndarray:
    xs, ys = zip(*sorted(table))
    return np.interp(np.asarray(t_out, float), xs, ys)


@dataclass
class ForecastInputs:
    times: list[datetime]          # slot start, UTC, 15-min spacing (forecast part only)
    t_out: np.ndarray              # forecast temperature (bias corrected), °C
    t_damped: np.ndarray           # damped temperature for curve + demand
    rh: np.ndarray                 # relative humidity, %
    gti: np.ndarray                # summed GTI over facades, W/m²
    q_dhw: np.ndarray              # DHW heat, kW (slot mean)
    curves: list[CurvePoints]


def run(inputs: ForecastInputs, p: ModelParams) -> dict[str, np.ndarray]:
    """Return per-slot arrays (kW unless noted)."""
    n = len(inputs.times)
    q_heat = heat_demand_kw(inputs.t_damped, inputs.gti, p)
    if inputs.curves:
        flow_heat = np.max(np.vstack([c.flow(inputs.t_damped) for c in inputs.curves]), axis=0)
    else:
        flow_heat = np.full(n, 35.0)
    flow_heat = flow_heat + p.flow_offset_hp
    t_src = inputs.t_out + p.source_temp_offset
    frost = t_src <= p.frost_threshold
    eta = np.where(frost, p.eta_frost, p.eta)
    cop_heat = cop_carnot(flow_heat, t_src, eta)
    cop_dhw = cop_carnot(np.full(n, p.flow_dhw), t_src, eta)

    q_dhw = np.maximum(np.nan_to_num(inputs.q_dhw), 0.0)
    q_total = q_heat + q_dhw
    qmax = q_max_kw(inputs.t_out, p.q_max_table)
    backup = np.maximum(q_total - qmax, 0.0)          # heating rod, COP 1
    share_hp = np.where(q_total > 0, (q_total - backup) / np.where(q_total > 0, q_total, 1), 1.0)
    p_hp = (q_heat * share_hp / cop_heat + q_dhw * share_hp / cop_dhw) * p.power_correction
    p_el = p_hp + backup
    cop_sys = np.where(p_el > 0, q_total / np.where(p_el > 0, p_el, 1), np.nan)

    u = p.uncertainty
    return {
        "t_out": inputs.t_out,
        "t_damped": inputs.t_damped,
        "gti": inputs.gti,
        "q_heat_kw": q_heat,
        "q_dhw_kw": q_dhw,
        "q_total_kw": q_total,
        "flow_temp": flow_heat,
        "cop_heat": cop_heat,
        "cop_dhw": cop_dhw,
        "cop_system": cop_sys,
        "p_el_kw": p_el,
        "p_backup_kw": backup,
        "frost_risk": frost,
        "below_min_modulation": q_total < p.q_min_kw,
        "q_heat_p10": q_heat * (1 - u),
        "q_heat_p90": q_heat * (1 + u),
        "p_el_p10": p_el * (1 - u),
        "p_el_p90": p_el * (1 + u),
    }


def slot_grid(start: datetime, hours: int) -> list[datetime]:
    """15-min slot starts beginning at the quarter hour containing ``start``."""
    s = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
    return [s + timedelta(minutes=15 * i) for i in range(int(hours * 4))]
