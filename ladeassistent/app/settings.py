"""Options (/data/options.json) → typed settings with generic defaults."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Vehicle:
    name: str = "E-Auto"
    soc: str = ""                       # sensor, %
    odometer: str = ""                  # sensor, km
    location: str = ""                  # device_tracker / person (home = "home")
    plugged: str = ""                   # optional: car-side plug sensor
    capacity_kwh: float = 60.0
    kwh_per_km: float = 0.20            # start value until learned


@dataclass
class Charger:
    plugged: str = ""                   # binary_sensor at the wallbox
    power: str = ""                     # charging power sensor
    power_unit: str = "W"               # W | kW
    max_kw: float = 11.0
    min_kw: float = 4.1                 # lowest power the car accepts (3-phase 6 A ≈ 4.1 kW)


@dataclass
class Energy:
    surplus_entity: str = ""            # entity with an hourly forecast attribute
    surplus_attribute: str = "forecast"
    surplus_time_key: str = "t"
    surplus_key: str = "net"
    surplus_invert: bool = True         # net > 0 = grid import → surplus = max(0, -net)
    surplus_unit: str = "kW"            # kW | W
    price_entity: str = ""              # current grid price
    price_unit: str = "ct"              # ct | EUR (per kWh)
    price_ct: float = 30.0              # fallback when no entity
    feedin_ct: float = 8.0


@dataclass
class Policy:
    target_soc: float = 80
    reserve_soc: float = 15
    urgent_days: float = 1.5
    min_savings_eur: float = 1.0
    min_kwh: float = 5.0
    lead_hours: float = 16
    quiet_start: str = "21:30"
    quiet_end: str = "07:30"
    horizon_hours: int = 48
    quantile: float = 0.8
    halflife_days: float = 28
    max_per_day: int = 2
    snooze_hours: float = 24


@dataclass
class Notify:
    services: list[str] = field(default_factory=list)   # e.g. notify.mobile_app_phone
    enabled: bool = True


@dataclass
class Settings:
    vehicle: Vehicle = field(default_factory=Vehicle)
    charger: Charger = field(default_factory=Charger)
    energy: Energy = field(default_factory=Energy)
    policy: Policy = field(default_factory=Policy)
    notify: Notify = field(default_factory=Notify)
    poll_minutes: int = 5
    plan_minutes: int = 15
    bootstrap_days: int = 60
    log_level: str = "info"
    data_dir: str = "/data"


def _merge(cls, data):
    obj = cls()
    names = {f.name for f in fields(cls)}
    for k, v in (data or {}).items():
        if k in names and v is not None and v != "":
            setattr(obj, k, v)
    return obj


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("OPTIONS_PATH", "/data/options.json"))
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    s = Settings(vehicle=_merge(Vehicle, raw.get("vehicle")), charger=_merge(Charger, raw.get("charger")),
                 energy=_merge(Energy, raw.get("energy")), policy=_merge(Policy, raw.get("policy")),
                 notify=_merge(Notify, raw.get("notify")))
    for k in ("poll_minutes", "plan_minutes", "bootstrap_days", "log_level"):
        if raw.get(k) not in (None, ""):
            setattr(s, k, raw[k])
    s.data_dir = os.environ.get("DATA_DIR", str(path.parent))
    s.policy.horizon_hours = int(max(12, min(72, s.policy.horizon_hours)))
    return s
