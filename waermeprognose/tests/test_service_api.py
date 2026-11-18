"""End-to-end: mocked open-meteo + fake Home Assistant -> forecast -> REST API."""
import asyncio
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from zoneinfo import ZoneInfo

from app.api import aggregate_hourly, create_app
from app.service import ForecastService, Scheduler
from app.settings import Settings
from app.store import Store


def _om_payload(params: dict) -> dict:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    past = int(params.get("past_minutely_15", 0))
    fut = int(params.get("forecast_minutely_15", 0))
    if "minutely_15" in params:
        ts = [int((now + timedelta(minutes=15 * (i + 1))).timestamp()) for i in range(-past, fut)]
        vars_ = params["minutely_15"].split(",")
        block = {"time": ts}
        for v in vars_:
            if v == "temperature_2m":
                block[v] = [round(3 + 4 * math.sin(i / 20), 2) for i in range(len(ts))]
            elif v == "relative_humidity_2m":
                block[v] = [85.0] * len(ts)
            elif v == "global_tilted_irradiance":
                block[v] = [max(0.0, 300 * math.sin(i / 15)) for i in range(len(ts))]
            else:
                block[v] = [1.0] * len(ts)
        return {"minutely_15": block, "generationtime_ms": 1.2}
    days = int(params["past_days"])
    ts = [int((now - timedelta(hours=h)).timestamp()) for h in range(days * 24, -24, -1)]
    key = params["hourly"]
    return {"hourly": {"time": ts, key: [2.0] * len(ts)}}


def _transport(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "api.open-meteo.com"
    return httpx.Response(200, json=_om_payload(dict(request.url.params)))


class FakeHA:
    def __init__(self):
        self.states = {
            "sensor.eu20l_ambient_temperature_1h": "5.0",
            **{f"number.eu20l_hc{n}_heating_curve_{k}_outside_temp_number": v
               for n in (1, 2, 3, 4) for k, v in (("cold", "48.3"), ("mid", "39.0"), ("warm", "32.0"))},
            "number.eu20l_hc1_flow_line_offset_temperature_number": "-1.0",
            "number.eu20l_hc2_flow_line_offset_temperature_number": "-0.5",
            "number.eu20l_hc3_flow_line_offset_temperature_number": "3.0",
            "number.eu20l_hc4_flow_line_offset_temperature_number": "0.0",
            "sensor.eu20l_hc1_target_temp_flow_line": "35.3",
            "sensor.eu20l_hc2_target_temp_flow_line": "32.9",
            "sensor.eu20l_hc3_target_temp_flow_line": "30.0",
            "sensor.eu20l_hc4_target_temp_flow_line": "0.0",
            "sensor.eu20l_hc1_operating_state": "HEATING",
            "sensor.eu20l_hc2_operating_state": "HEATING",
            "sensor.eu20l_hc3_operating_state": "HEATING",
            "sensor.eu20l_hc4_operating_state": "RELEASE-OFF",
        }
        self.published = {}
        self.service_calls = []

    async def config(self):
        return {"latitude": 50.0, "longitude": 8.0, "time_zone": "Europe/Berlin"}

    async def state(self, eid):
        return {"state": self.states[eid]} if eid in self.states else None

    async def float_state(self, eid):
        try:
            return float(self.states[eid]) if eid else None
        except (KeyError, ValueError):
            return None

    async def set_state(self, eid, state, attrs):
        self.published[eid] = (state, attrs)

    async def call_service(self, service, data=None):
        self.service_calls.append(service)
        start = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Berlin")).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return {"forecast": [{"datetime": (start + timedelta(minutes=15 * i)).isoformat(),
                              "watts": 2000.0 if 40 <= i % 96 < 60 else 0.0,
                              "p10": None, "p90": None} for i in range(96 * 7)]}

    async def statistics(self, ids, start, end, period="hour", types=("change",)):
        step = timedelta(minutes=5) if period == "5minute" else timedelta(hours=1)
        out = {}
        for i in ids:
            rows, h = [], start
            while h < end:
                mean = 1800.0 if i == "sensor.hausverbrauch_ohne_warmepumpe" else (
                    1000.0 if i == "sensor.warmepumpe_leistung" else 4.0)
                rows.append({"start": h, "change": 7.0, "mean": mean})
                h += step
            out[i] = rows
        return out


@pytest.fixture
def svc(tmp_path):
    s = Settings(api_token="geheim", data_dir=str(tmp_path))
    s.dhw.logfile_glob = str(tmp_path / "none.xlsx")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_transport))
    return ForecastService(s, client, ha=FakeHA(), store=Store(tmp_path / "db.sqlite"))


def test_run_once_end_to_end(svc):
    async def go():
        await svc.startup()
        return await svc.run_once()
    res = asyncio.run(go())
    assert len(res["series"]) == 48 * 4
    first = res["series"][0]
    assert {"q_heat_kw", "q_dhw_kw", "p_el_kw", "cop_heat", "flow_temp", "frost_risk"} <= first.keys()
    assert res["weather"]["local_bias_k"] != 0
    assert 30 < first["flow_temp"] < 40
    assert "sensor.waermeprognose_strom_morgen" in svc.ha.published
    assert svc.ha.published["sensor.waermeprognose_status"][0] == "ok"
    # DHW profile from HA statistics: 7 kWh every hour -> 7 kW
    assert abs(first["q_dhw_kw"] - 7.0) < 1e-6
    # circuits: HC3 fixed 30 °C, HC4 off -> flow follows HC1 curve (+ HP offset -3 K)
    names = {c["name"]: c for c in res["circuits"]}
    assert names["HC3"]["active"] and names["HC3"]["setpoint"] == {"value": 30.0}
    assert not names["HC4"]["active"] and names["HC4"]["state"] == "RELEASE-OFF"
    # PV + household + net balance
    assert res["pv"]["ok"] and res["pv"]["covered_slots"] == len(res["series"])
    assert abs(first["household_kw"] - (1.8 - 0.07 * 1.0)) < 1e-6
    for r in res["series"][:8]:
        assert abs(r["net_kw"] - (r["p_el_kw"] + r["household_kw"] - r["pv_kw"])) < 2e-3
    day = next(iter(res["summary"]["days"].values()))
    assert "pv_kwh" in day and "household_kwh" in day and "net_import_kwh" in day
    assert "sensor.waermeprognose_haushalt_morgen" in svc.ha.published
    assert "sensor.waermeprognose_netzbedarf_morgen" in svc.ha.published
    ts = svc.ha.published["sensor.waermeprognose_zeitreihe"][1]["forecast"]
    assert len(ts) == 48 and {"t", "ta", "vl", "q", "cop", "wp", "hh", "pv", "net", "frost"} <= ts[0].keys()
    import json
    assert len(json.dumps(ts)) < 12000
    # hourly aggregates persisted for backtesting
    assert svc.store.forecasts_for(datetime.now(timezone.utc) - timedelta(hours=1),
                                   datetime.now(timezone.utc) + timedelta(days=3))


def test_calibration_and_backtest(svc):
    async def go():
        await svc.startup()
        await svc.run_once()
        info = await svc.calibrate()
        bt = await svc.backtest(days=2, lead_h=0)
        return info, bt
    info, bt = asyncio.run(go())
    assert info["applied"] and info["n_blocks"] >= 8
    # fake stats report DHW in every hour -> no pure heating hours -> offset unchanged
    assert info["flow_offset_hours"] == 0 and svc.params.flow_offset_hp == -3.0
    assert "days" in bt


def test_api_auth_and_resolution(svc):
    asyncio.run(svc.startup())
    asyncio.run(svc.run_once())
    app = create_app(svc.s, service=svc, run_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/api/v1/health").json()["status"] == "ok"
        assert c.get("/api/v1/forecast").status_code == 401
        r = c.get("/api/v1/forecast", params={"horizon": 24, "resolution": "60min"},
                  headers={"Authorization": "Bearer geheim"})
        assert r.status_code == 200
        body = r.json()
        assert body["resolution"] == "60min" and 24 <= len(body["series"]) <= 25
        assert {"pv_kw", "household_kw", "net_kw"} <= body["series"][0].keys()
        r2 = c.get("/api/v1/forecast/summary", headers={"X-API-Key": "geheim"})
        assert r2.status_code == 200 and r2.json()["days"]
        assert c.get("/api/v1/params", headers={"X-API-Key": "geheim"}).json()["settings"]["api_token"] == "***"


def test_api_503_before_first_run(tmp_path):
    s = Settings(data_dir=str(tmp_path))
    svc = ForecastService(s, httpx.AsyncClient(), ha=FakeHA(), store=Store(tmp_path / "x.sqlite"))
    app = create_app(s, service=svc, run_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/api/v1/forecast").status_code == 503


def test_aggregate_hourly_energy_conserved():
    rows = [{"t": f"2026-11-17T14:{m:02d}:00+01:00", "q_total_kw": 8.0 + i, "p_el_kw": 1.0,
             "frost_risk": i == 2} for i, m in enumerate((0, 15, 30, 45))]
    h = aggregate_hourly(rows)
    assert len(h) == 1 and h[0]["q_total_kw"] == 9.5 and h[0]["frost_risk"] and h[0]["t"].endswith("14:00:00+01:00")


def test_next_quarter():
    now = datetime(2026, 11, 17, 14, 15, 30, tzinfo=timezone.utc)
    assert Scheduler.next_quarter(now, 60) == datetime(2026, 11, 17, 14, 16, tzinfo=timezone.utc)
    now = datetime(2026, 11, 17, 14, 16, 30, tzinfo=timezone.utc)
    assert Scheduler.next_quarter(now, 60) == datetime(2026, 11, 17, 14, 31, tzinfo=timezone.utc)


def test_compare_hourly_picks_lead_and_computes_deviation(svc):
    async def go():
        await svc.startup()
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for i in range(48):
            h = end - timedelta(hours=48 - i)
            # day-ahead forecast: heat +10 %, power -10 %, household exact
            svc.store.save_forecast(h - timedelta(hours=24),
                                    [(h, 11.0, 4.4, 14.98 * 0.9, 0.5, 1.73)])
            # short-term forecast (2 h ahead) with different values
            svc.store.save_forecast(h - timedelta(hours=2), [(h, 7.0, 7.0, 14.98, 0.5, 1.73)])
        return await svc.compare_hourly(48, 24), await svc.compare_hourly(48, 0)
    day_ahead, short = asyncio.run(go())
    r = day_ahead["rows"][0]
    assert r["lead_h"] == 24.0 and r["q_ist"] == 14.0 and r["q_dev"] == 10.0
    assert r["p_dev"] == -10.0 and abs(r["hh_dev"]) < 0.1
    assert r["pv_dev"] is None                      # actual PV 0.004 kWh -> below floor
    s = day_ahead["summary"]
    assert s["q"]["hours"] == 48 and s["q"]["dev_pct"] == 10.0 and s["q"]["mean_abs_dev_pct"] == 10.0
    assert short["rows"][0]["lead_h"] == 2.0 and short["rows"][0]["q_dev"] == 0.0


def test_compare_published_as_sensor(svc):
    async def go():
        await svc.startup()
        await svc.run_once()
    asyncio.run(go())
    state, attrs = svc.ha.published["sensor.waermeprognose_abgleich"]
    assert len(attrs["rows"]) == 48 and {"t", "lead", "qf", "qi", "qd", "pvd", "hhd"} <= attrs["rows"][0].keys()
    import json
    assert len(json.dumps(attrs)) < 16000
