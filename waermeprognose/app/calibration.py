"""Nightly re-estimation of the heat demand parameters (ridge regression toward the prior)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

import numpy as np

from .model import ema
from .settings import ModelParams

# prior standard deviations: base kW, slope kW/K, knee slope kW/K, solar kW per W/m²
PRIOR_SCALE = np.array([2.0, 0.2, 1.0, 0.005])
BLOCK_H = 6
NOISE_KW = 1.0


@dataclass
class CalibrationResult:
    params: ModelParams
    n_hours: int
    n_blocks: int
    rmse_kw: float
    rel_error: float
    coefficients: dict
    note: str = ""

    def as_dict(self) -> dict:
        return {"n_hours": self.n_hours, "n_blocks": self.n_blocks,
                "rmse_kw": round(self.rmse_kw, 3), "rel_error": round(self.rel_error, 3),
                "coefficients": self.coefficients, "note": self.note}


def fit_flow_offset(hp_flow: np.ndarray, curve: np.ndarray, q_heat_kwh: np.ndarray,
                    q_dhw_kwh: np.ndarray, min_hours: int = 24) -> tuple[float | None, int]:
    """Median gap between actual HP flow and the highest circuit set point.

    Only pure space-heating hours (no DHW, HP running) are used. Returns (offset, n_hours).
    """
    f, c = np.asarray(hp_flow, float), np.asarray(curve, float)
    qh, qd = np.asarray(q_heat_kwh, float), np.nan_to_num(np.asarray(q_dhw_kwh, float))
    ok = np.isfinite(f) & np.isfinite(c) & np.isfinite(qh) & (qh > 1.0) & (qd < 0.2)
    n = int(ok.sum())
    if n < min_hours:
        return None, n
    return float(np.clip(np.median(f[ok] - c[ok]), -8.0, 5.0)), n


def fit_heat_model(hours: list[datetime], q_heat_kwh: np.ndarray, t_out: np.ndarray,
                   gti: np.ndarray, prior: ModelParams, prior_hours: float) -> CalibrationResult:
    """Fit q = b0 + b1*Td + b2*max(0, knee-Td) - s*GTI on 6-h blocks.

    ``hours`` must be consecutive hour starts; missing values are NaN.
    The prior acts like ``prior_hours`` hours of data at the prior parameters.
    """
    q = np.asarray(q_heat_kwh, float)
    t = np.asarray(t_out, float)
    g = np.nan_to_num(np.asarray(gti, float))
    # fill short temperature gaps for the damping filter
    if np.isnan(t).all():
        return CalibrationResult(prior, 0, 0, np.nan, prior.uncertainty, {}, "keine Temperaturdaten")
    t_f = np.interp(np.arange(len(t)), np.flatnonzero(~np.isnan(t)), t[~np.isnan(t)])
    td = ema(t_f, 1.0, prior.damping_hours)
    warm = int(min(len(td), prior.damping_hours * 2))  # filter warm-up
    knee = np.maximum(0.0, prior.knee_temp - td)

    rows_X, rows_y = [], []
    for s in range(warm, len(q) - BLOCK_H + 1, BLOCK_H):
        sl = slice(s, s + BLOCK_H)
        if np.isnan(q[sl]).any() or np.isnan(t[sl]).any():
            continue
        rows_X.append([1.0, td[sl].mean(), knee[sl].mean(), g[sl].mean()])
        rows_y.append(q[sl].mean())
    n_blocks = len(rows_y)
    theta0 = np.array([prior.q_base_kw, prior.q_slope_kw_per_k,
                       prior.knee_slope_kw_per_k, -prior.solar_gain_kw_per_wm2])
    if n_blocks < 8:
        return CalibrationResult(prior, n_blocks * BLOCK_H, n_blocks, np.nan, prior.uncertainty,
                                 {}, "zu wenig vollständige Daten (< 48 h)")
    X, y = np.array(rows_X), np.array(rows_y)
    lam = prior_hours / BLOCK_H
    # Gaussian prior: penalty = lam * sigma² / s_j² with an assumed 6-h block noise of 1 kW
    D = np.diag(lam * NOISE_KW**2 / PRIOR_SCALE**2)
    # The knee term is only identifiable with data below the knee
    if (X[:, 2] > 0).sum() < 4:
        D[2, 2] = 1e9
    if np.ptp(X[:, 3]) < 20:
        D[3, 3] = 1e9
    theta = np.linalg.solve(X.T @ X + D, X.T @ y + D @ theta0)
    theta[2] = max(theta[2], 0.0)
    theta[3] = min(theta[3], 0.0)
    resid = y - X @ theta
    rmse = float(np.sqrt(np.mean(resid**2)))
    rel = float(np.clip(rmse / max(y.mean(), 0.1), 0.08, 0.4))
    new = replace(prior, q_base_kw=float(theta[0]), q_slope_kw_per_k=float(theta[1]),
                  knee_slope_kw_per_k=float(theta[2]), solar_gain_kw_per_wm2=float(-theta[3]),
                  uncertainty=rel)
    coef = {"q_base_kw": round(theta[0], 3), "q_slope_kw_per_k": round(theta[1], 4),
            "knee_slope_kw_per_k": round(theta[2], 3), "solar_gain_kw_per_wm2": round(-theta[3], 5)}
    return CalibrationResult(new, n_blocks * BLOCK_H, n_blocks, rmse, rel, coef)
