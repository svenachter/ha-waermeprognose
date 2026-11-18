"""REST API (FastAPI)."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .aggregate import aggregate_hourly  # noqa: F401  (re-exported)
from .service import VERSION, ForecastService, Scheduler
from .settings import Settings, load_settings

log = logging.getLogger(__name__)

def create_app(settings: Settings | None = None, service: ForecastService | None = None,
               run_scheduler: bool = True) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient()
        svc = service or ForecastService(settings, client)
        app.state.svc = svc
        sched = None
        if run_scheduler:
            await svc.startup()
            sched = Scheduler(svc)
            sched.start()
        yield
        if sched:
            await sched.stop()
        await client.aclose()

    app = FastAPI(title="Wärmeprognose", version=VERSION, lifespan=lifespan,
                  description="Wärme- und Strombedarfsprognose der Wärmepumpe im 15-min-Raster.")

    def auth(authorization: str | None = Header(default=None),
             x_api_key: str | None = Header(default=None)):
        token = settings.api_token
        if not token:
            return
        given = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ")
                              else None)
        if not given or not hmac.compare_digest(given, token):
            raise HTTPException(status_code=401, detail="Ungültiges oder fehlendes Token")

    def svc() -> ForecastService:
        return app.state.svc

    def need_result(s: ForecastService) -> dict:
        if s.result is None:
            raise HTTPException(status_code=503, detail=s.last_error or "Noch keine Prognose verfügbar")
        return s.result

    @app.get("/api/v1/health")
    async def health():
        s = svc()
        age = (datetime.now(timezone.utc) - s.last_run).total_seconds() if s.last_run else None
        if s.result and not s.last_error:
            status = "ok"
        elif s.result:
            status = "degraded"      # last run failed, older forecast still served
        elif s.last_error:
            status = "error"
        else:
            status = "starting"
        return {"status": status,
                "version": VERSION, "last_run": s.last_run.isoformat() if s.last_run else None,
                "age_seconds": age, "last_error": s.last_error}

    @app.get("/api/v1/forecast", dependencies=[Depends(auth)])
    async def forecast(horizon: int = Query(default=72, ge=1, le=72, description="Stunden"),
                       resolution: Literal["15min", "60min"] = "15min"):
        res = need_result(svc())
        rows = res["series"][: horizon * 4]
        if resolution == "60min":
            rows = aggregate_hourly(rows)
        return {k: v for k, v in res.items() if k != "series"} | {"resolution": resolution, "series": rows}

    @app.get("/api/v1/forecast/summary", dependencies=[Depends(auth)])
    async def summary():
        res = need_result(svc())
        return {"created_at": res["created_at"], **res["summary"]}

    @app.get("/api/v1/params", dependencies=[Depends(auth)])
    async def params():
        s = svc()
        return {"settings": s.s.public_dict(), "active_params": s.params.__dict__,
                "calibration": s.calibration_info, "dhw_profile": s.profile.summary(),
                "household_profile": s.household.summary(),
                "household_profile_kw": {
                    k: None if v is None else [None if x != x else round(float(x), 3) for x in v]
                    for k, v in (("workday", s.household.workday), ("weekend", s.household.weekend))},
                "dhw_profile_kw": {"workday": [round(float(x), 3) for x in s.profile.workday],
                                   "weekend": None if s.profile.weekend is None else
                                   [round(float(x), 3) for x in s.profile.weekend]}}

    @app.get("/api/v1/backtest", dependencies=[Depends(auth)])
    async def backtest(days: int = Query(default=7, ge=1, le=14), lead_hours: int = Query(default=24, ge=0, le=48)):
        return await svc().backtest(days, lead_hours)

    @app.get("/api/v1/compare", dependencies=[Depends(auth)])
    async def compare(hours: int = Query(default=48, ge=1, le=336),
                      lead_hours: float = Query(default=24.0, ge=0, le=72)):
        """Stored forecast (lead closest to lead_hours) vs. measured values, hour by hour."""
        return await svc().compare_hourly(hours, lead_hours)

    @app.post("/api/v1/refresh", dependencies=[Depends(auth)])
    async def refresh():
        try:
            res = await svc().run_once()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"created_at": res["created_at"]}

    @app.post("/api/v1/calibrate", dependencies=[Depends(auth)])
    async def calibrate():
        s = svc()
        info = await s.calibrate()
        await s.refresh_dhw_profile()
        return info

    return app
