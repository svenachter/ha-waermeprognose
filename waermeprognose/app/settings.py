"""Load app options (/data/options.json) into typed settings with defaults."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any


@dataclass
class Facade:
    name: str = "sued"
    tilt: float = 90.0
    azimuth: float = 0.0  # open-meteo: 0 = south, -90 = east, 90 = west


@dataclass
class Entities:
    outdoor_temp: str = "sensor.eu20l_ambient_temperature_1h"
    heat_thermal: str = "sensor.eu20l_hp1_heating_thermal_energy_total"
    heat_electric: str = "sensor.eu20l_hp1_heating_energy_total"
    dhw_thermal: str = "sensor.eu20l_hp1_hot_water_thermal_energy_total"
    dhw_electric: str = "sensor.eu20l_hp1_hot_water_energy_total"
    hp_flow: str = "sensor.eu20l_hp1_flow_line_temperature"


@dataclass
class Circuit:
    name: str
    curve_cold: str
    curve_mid: str
    curve_warm: str
    offset: str | None = None


@dataclass
class ModelParams:
    eta: float = 0.56              # Carnot efficiency, no defrost
    eta_frost: float = 0.51        # Carnot efficiency incl. defrost losses
    frost_threshold: float = 4.0   # °C air inlet temperature
    source_temp_offset: float = 1.5  # air inlet vs. forecast temperature (K)
    power_correction: float = 1.07   # house meter / controller power value
    flow_offset_hp: float = -3.0     # HP flow vs. highest circuit set point (K), calibrated
    flow_dhw: float = 48.0
    damping_hours: float = 12.0
    q_base_kw: float = 7.2
    q_slope_kw_per_k: float = -0.05
    knee_temp: float = -3.0
    knee_slope_kw_per_k: float = 1.76
    solar_gain_kw_per_wm2: float = 0.0
    q_min_kw: float = 8.7
    uncertainty: float = 0.15
    # Max. heat output of the HP vs. outdoor temperature (datasheet EU20L)
    q_max_table: list[list[float]] = field(
        default_factory=lambda: [[-15.0, 17.9], [-10.0, 20.0], [20.0, 20.0]]
    )


@dataclass
class DhwSettings:
    source: str = "both"  # logfile | ha_statistics | both
    logfile_glob: str = "/share/waermeprognose/*.xlsx"
    history_days: int = 28


@dataclass
class CalibrationSettings:
    enabled: bool = True
    hour: int = 3
    prior_hours: int = 72


def _default_circuits() -> list[Circuit]:
    out = []
    for n, off in (("hc1", True), ("hc2", True)):
        out.append(
            Circuit(
                name=n.upper(),
                curve_cold=f"number.eu20l_{n}_heating_curve_cold_outside_temp_number",
                curve_mid=f"number.eu20l_{n}_heating_curve_mid_outside_temp_number",
                curve_warm=f"number.eu20l_{n}_heating_curve_warm_outside_temp_number",
                offset=f"number.eu20l_{n}_flow_line_offset_temperature_number" if off else None,
            )
        )
    return out


@dataclass
class Settings:
    api_token: str = ""
    latitude: float | None = None
    longitude: float | None = None
    horizon_hours: int = 48
    weather_model: str = "best_match"
    facades: list[Facade] = field(default_factory=lambda: [Facade()])
    entities: Entities = field(default_factory=Entities)
    circuits: list[Circuit] = field(default_factory=_default_circuits)
    model: ModelParams = field(default_factory=ModelParams)
    dhw: DhwSettings = field(default_factory=DhwSettings)
    calibration: CalibrationSettings = field(default_factory=CalibrationSettings)
    publish_sensors: bool = True
    log_level: str = "info"
    data_dir: str = "/data"

    def public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["api_token"] = "***" if self.api_token else ""
        return d


def _merge(cls, data: dict | None):
    """Build dataclass ``cls`` from ``data``, ignoring unknown keys, keeping defaults."""
    obj = cls()
    if not data:
        return obj
    names = {f.name for f in fields(cls)}
    for k, v in data.items():
        if k in names and v is not None:
            setattr(obj, k, v)
    return obj


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("OPTIONS_PATH", "/data/options.json"))
    raw: dict[str, Any] = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    s = Settings()
    for key in ("api_token", "latitude", "longitude", "horizon_hours", "weather_model",
                "publish_sensors", "log_level"):
        if raw.get(key) is not None:
            setattr(s, key, raw[key])
    if raw.get("facades"):
        s.facades = [_merge(Facade, f) for f in raw["facades"]]
    s.entities = _merge(Entities, raw.get("entities"))
    if raw.get("circuits"):
        s.circuits = [Circuit(**{k: c.get(k) for k in
                                 ("name", "curve_cold", "curve_mid", "curve_warm", "offset")})
                      for c in raw["circuits"]]
    s.model = _merge(ModelParams, raw.get("model"))
    s.dhw = _merge(DhwSettings, raw.get("dhw"))
    s.calibration = _merge(CalibrationSettings, raw.get("calibration"))
    s.data_dir = os.environ.get("DATA_DIR", str(path.parent))
    s.horizon_hours = int(max(12, min(72, s.horizon_hours)))
    return s
