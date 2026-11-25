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
    household_power: str = "sensor.hausverbrauch_ohne_warmepumpe"   # W, house without HP
    hp_power: str = "sensor.warmepumpe_leistung"                    # W, controller value
    pv_power: str = "sensor.e3dc_farm_pv_leistung"                  # W, actual PV (comparison)


@dataclass
class Circuit:
    """Heating circuit. mode "curve": set point from heating curve vs. forecast temperature;
    mode "target": the current set point (e.g. pool, fixed flow) is held over the horizon."""
    name: str
    mode: str = "curve"
    curve_cold: str | None = None
    curve_mid: str | None = None
    curve_warm: str | None = None
    offset: str | None = None
    target_flow: str | None = None
    state: str | None = None


INACTIVE_STATES_DEFAULT = ["OFF", "RELEASE-OFF", "TIME-OFF", "CH-SUMMER", "COOLING",
                           "CC-WINTER", "STBY-COOLING", "ERROR"]


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
class PvSettings:
    enabled: bool = True
    service: str = "helios_forecast.get_forecast"
    config_entry_id: str = ""


@dataclass
class HouseholdSettings:
    enabled: bool = True
    days: int = 3                 # last N complete days of the same day type
    aggregate: str = "mean"       # mean | median
    split_weekend: bool = True
    correct_hp_share: bool = True  # subtract (power_correction-1) x HP power


@dataclass
class CompareSettings:
    hours: int = 48              # look-back window
    lead_hours: float = 24.0     # preferred forecast lead time ("as forecast the day before")
    min_lead_hours: float = 1.0  # never use forecasts issued later than this before the hour


@dataclass
class PlanViewSettings:
    """HTML plan/actual table (sensor.waermeprognose_plan_html). Empty entity = column omitted."""
    enabled: bool = True
    days: int = 7                                   # look-back in full days (+ today)
    grid_import_power: str = "sensor.e3dc_netzbezug_leistung"       # W or kW
    grid_export_power: str = "sensor.e3dc_einspeisung_leistung"     # W or kW
    chp_power: str = "sensor.bhkw_br18_leistung_elektrisch"         # W or kW, electrical
    chp_gas_energy: str = "sensor.bhkw_gasverbrauch_korrigiert"     # kWh counter
    buffer_hp_top: str = "sensor.eu20l_buff1_actual_high_temperature"
    buffer_hp_bottom: str = "sensor.eu20l_buff1_actual_low_temperature"
    buffer_chp_top: str = "sensor.bhkw_br18_puffer_oben"
    price: str = "sensor.strompreis_brutto_aktuell"                 # ct/kWh (or EUR/kWh)
    co2: str = "sensor.energy_price_forecast_eu_de_aktuelle_co2_intensitat"   # g/kWh
    co2_forecast: str = "sensor.energy_price_forecast_eu_de_co2_prognosereihe"  # raw_* attributes
    gas_price: str = "input_number.bhkw_gaspreis"                   # ct/kWh
    feedin_ct_per_kwh: float = 0.0                  # feed-in tariff for exported energy
    chp_heat_per_el: float = 3.0                    # kWh heat per kWh electricity (BHKW)
    gas_co2_g_per_kwh: float = 240.0                # natural gas incl. upstream chain


@dataclass
class CalibrationSettings:
    enabled: bool = True
    hour: int = 3
    prior_hours: int = 72


def _default_circuits() -> list[Circuit]:
    def c(n: int, mode: str) -> Circuit:
        p = f"eu20l_hc{n}"
        return Circuit(
            name=f"HC{n}", mode=mode,
            curve_cold=f"number.{p}_heating_curve_cold_outside_temp_number",
            curve_mid=f"number.{p}_heating_curve_mid_outside_temp_number",
            curve_warm=f"number.{p}_heating_curve_warm_outside_temp_number",
            offset=f"number.{p}_flow_line_offset_temperature_number",
            target_flow=f"sensor.{p}_target_temp_flow_line",
            state=f"sensor.{p}_operating_state",
        )
    # HC3 (pool) runs a fixed set point; the others follow the heating curve
    return [c(1, "curve"), c(2, "curve"), c(3, "target"), c(4, "curve")]


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
    pv: PvSettings = field(default_factory=PvSettings)
    household: HouseholdSettings = field(default_factory=HouseholdSettings)
    compare: CompareSettings = field(default_factory=CompareSettings)
    plan_view: PlanViewSettings = field(default_factory=PlanViewSettings)
    inactive_states: list[str] = field(default_factory=lambda: list(INACTIVE_STATES_DEFAULT))
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
        keys = ("name", "mode", "curve_cold", "curve_mid", "curve_warm", "offset",
                "target_flow", "state")
        s.circuits = [Circuit(**{k: c[k] for k in keys if c.get(k) not in (None, "")})
                      for c in raw["circuits"]]
    s.model = _merge(ModelParams, raw.get("model"))
    s.dhw = _merge(DhwSettings, raw.get("dhw"))
    s.calibration = _merge(CalibrationSettings, raw.get("calibration"))
    s.pv = _merge(PvSettings, raw.get("pv"))
    s.household = _merge(HouseholdSettings, raw.get("household"))
    s.compare = _merge(CompareSettings, raw.get("compare"))
    s.plan_view = _merge(PlanViewSettings, raw.get("plan_view"))
    s.plan_view.days = int(max(1, min(13, s.plan_view.days)))
    if raw.get("inactive_states"):
        s.inactive_states = [str(x).upper() for x in raw["inactive_states"]]
    s.data_dir = os.environ.get("DATA_DIR", str(path.parent))
    s.horizon_hours = int(max(12, min(72, s.horizon_hours)))
    return s
