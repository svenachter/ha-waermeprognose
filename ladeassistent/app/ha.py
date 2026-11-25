"""Home Assistant access via the Supervisor proxy (REST + WebSocket)."""
from __future__ import annotations

import itertools
import json
import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


def _parse_start(v) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)


class HomeAssistant:
    def __init__(self, client: httpx.AsyncClient, token: str | None = None,
                 base_url: str | None = None, ws_url: str | None = None):
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = (base_url or os.environ.get("HA_URL", "http://supervisor/core")).rstrip("/")
        self.ws_url = ws_url or os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
        self.client = client
        self._ids = itertools.count(1)

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def config(self) -> dict:
        r = await self.client.get(f"{self.base}/api/config", headers=self._headers, timeout=15)
        r.raise_for_status()
        return r.json()

    async def state(self, entity_id: str) -> dict | None:
        r = await self.client.get(f"{self.base}/api/states/{entity_id}", headers=self._headers, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def float_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        s = await self.state(entity_id)
        try:
            return float(s["state"]) if s else None
        except (TypeError, ValueError):
            return None

    async def set_state(self, entity_id: str, state, attributes: dict) -> None:
        r = await self.client.post(f"{self.base}/api/states/{entity_id}", headers=self._headers,
                                   content=json.dumps({"state": state, "attributes": attributes}),
                                   timeout=15)
        r.raise_for_status()

    async def call_service(self, service: str, data: dict | None = None) -> dict:
        """Call ``domain.service`` and return its response data (``service_response``)."""
        domain, name = service.split(".", 1)
        r = await self.client.post(f"{self.base}/api/services/{domain}/{name}?return_response",
                                   headers=self._headers, content=json.dumps(data or {}), timeout=30)
        r.raise_for_status()
        body = r.json()
        return body.get("service_response", body) if isinstance(body, dict) else {}

    async def statistics(self, statistic_ids: list[str], start: datetime, end: datetime,
                         period: str = "hour", types: tuple[str, ...] = ("change", "mean")
                         ) -> dict[str, list[dict]]:
        """recorder/statistics_during_period via WebSocket. Returns rows with UTC ``start``."""
        from websockets.asyncio.client import connect

        async with connect(self.ws_url, max_size=32 * 2**20, open_timeout=20) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"Unerwartete WS-Antwort: {hello}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            ok = json.loads(await ws.recv())
            if ok.get("type") != "auth_ok":
                raise RuntimeError("WebSocket-Authentifizierung fehlgeschlagen")
            msg_id = next(self._ids)
            await ws.send(json.dumps({
                "id": msg_id, "type": "recorder/statistics_during_period",
                "start_time": start.astimezone(timezone.utc).isoformat(),
                "end_time": end.astimezone(timezone.utc).isoformat(),
                "statistic_ids": statistic_ids, "period": period, "types": list(types),
            }))
            while True:
                res = json.loads(await ws.recv())
                if res.get("id") == msg_id:
                    break
            if not res.get("success"):
                raise RuntimeError(f"Statistik-Abfrage fehlgeschlagen: {res.get('error')}")
        out = {}
        for sid, rows in (res.get("result") or {}).items():
            out[sid] = [{**r, "start": _parse_start(r["start"])} for r in rows]
        return out

    async def history(self, entity_ids: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[datetime, str]]]:
        """REST /api/history (short-term, ≈10 days by default recorder settings)."""
        ids = [e for e in entity_ids if e]
        if not ids:
            return {}
        r = await self.client.get(
            f"{self.base}/api/history/period/{start.astimezone(timezone.utc).isoformat()}",
            params={"filter_entity_id": ",".join(ids), "end_time": end.astimezone(timezone.utc).isoformat(),
                    "minimal_response": "", "no_attributes": ""},
            headers=self._headers, timeout=60)
        r.raise_for_status()
        out: dict[str, list[tuple[datetime, str]]] = {}
        for series in r.json():
            if not series:
                continue
            eid = series[0].get("entity_id")
            out[eid] = [(_parse_start(x.get("last_changed") or x.get("last_updated")), x.get("state"))
                        for x in series]
        return out

    async def events(self, event_types: list[str], handler) -> None:
        """Subscribe to ``event_types``; ``await handler(event_type, data)`` until the socket closes."""
        from websockets.asyncio.client import connect

        async with connect(self.ws_url, max_size=8 * 2**20, open_timeout=20, ping_interval=30) as ws:
            json.loads(await ws.recv())
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                raise RuntimeError("WebSocket-Authentifizierung fehlgeschlagen")
            for i, et in enumerate(event_types, 1):
                await ws.send(json.dumps({"id": i, "type": "subscribe_events", "event_type": et}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "event":
                    ev = msg["event"]
                    await handler(ev.get("event_type"), ev.get("data") or {})
