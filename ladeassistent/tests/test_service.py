import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx

from app.model import Sample
from app.service import Service
from app.settings import load_settings

UTC = timezone.utc


def test_compute_publish_notify(tmp_path):
    opts = {"vehicle": {"name": "Testauto", "soc": "sensor.soc", "odometer": "sensor.odo",
                        "location": "device_tracker.car", "capacity_kwh": 70},
            "charger": {"plugged": "binary_sensor.plug", "power": "sensor.wb"},
            "energy": {"surplus_entity": "sensor.fc", "price_entity": "sensor.price", "feedin_ct": 8.3},
            "notify": {"services": ["notify.mobile_app_x"]}, "policy": {"quiet_start": "23:59", "quiet_end": "00:00"}}
    (tmp_path / "options.json").write_text(json.dumps(opts))
    s = load_settings(tmp_path / "options.json")
    now = datetime.now(UTC)
    fc = [{"t": (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i)).isoformat(),
           "net": -9.0 if 2 <= i <= 5 else 1.0} for i in range(48)]
    states = {"sensor.soc": "40", "sensor.odo": "5000", "device_tracker.car": "home",
              "binary_sensor.plug": "off", "sensor.wb": "0", "sensor.price": "23.32"}
    posted, calls = {}, []

    def handler(req: httpx.Request):
        path = req.url.path
        if req.method == "GET" and path.startswith("/core/api/states/"):
            eid = path.rsplit("/", 1)[1]
            if eid == "sensor.fc":
                return httpx.Response(200, json={"state": "ok", "attributes": {"forecast": fc}})
            return httpx.Response(200, json={"state": states[eid], "attributes": {}})
        if req.method == "POST" and path.startswith("/core/api/states/"):
            posted[path.rsplit("/", 1)[1]] = json.loads(req.content)
            return httpx.Response(200, json={})
        if req.method == "POST" and path.startswith("/core/api/services/"):
            assert "return_response" not in str(req.url); calls.append((path, json.loads(req.content)))
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    svc = Service(s)
    svc.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc.ha.client = svc.client
    svc.ha.base = "http://supervisor/core"
    # four weeks of history: 30 km per day
    t0 = now - timedelta(days=28)
    svc.store.add([Sample(t0 + timedelta(days=d), soc=90 - d, odo=4160 + 30 * d, home=True, plugged=False)
                   for d in range(28)])

    async def go():
        svc.store.add([await svc.sample_now()])
        a = await svc.compute()
        await svc.publish(a)
        sent = await svc.send(a)
        return a, sent

    a, sent = asyncio.run(go())
    assert a.status == "opportunity", a.message
    assert posted["sensor.ladeassistent_status"]["state"] == "opportunity"
    assert posted["sensor.ladeassistent_status"]["attributes"]["kwh_per_km_info"]["source"] == "learned"
    assert sent and calls[0][0].endswith("/notify/mobile_app_x")
    assert calls[0][1]["data"]["actions"][1]["action"] == "LADEASSISTENT_SNOOZE"
    assert not asyncio.run(svc.send(a))              # dedup
