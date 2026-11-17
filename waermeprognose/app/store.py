"""SQLite persistence: issued forecasts (hourly aggregates) and calibrated parameters."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

KEEP_DAYS = 14


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS fc_hourly (
                    issued INTEGER, target INTEGER, q_heat REAL, q_dhw REAL, p_el REAL,
                    PRIMARY KEY (issued, target));
                CREATE INDEX IF NOT EXISTS fc_target ON fc_hourly(target);
                CREATE TABLE IF NOT EXISTS params (created INTEGER PRIMARY KEY, data TEXT, info TEXT);
            """)
            self.db.commit()

    def save_forecast(self, issued: datetime, hourly: list[tuple[datetime, float, float, float]]):
        ts = int(issued.timestamp())
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO fc_hourly VALUES (?,?,?,?,?)",
                [(ts, int(t.timestamp()), qh, qd, pe) for t, qh, qd, pe in hourly])
            self.db.execute("DELETE FROM fc_hourly WHERE issued < ?", (ts - KEEP_DAYS * 86400,))
            self.db.commit()

    def forecasts_for(self, t_from: datetime, t_to: datetime) -> list[tuple]:
        with self.lock:
            cur = self.db.execute(
                "SELECT issued, target, q_heat, q_dhw, p_el FROM fc_hourly "
                "WHERE target >= ? AND target < ? ORDER BY target, issued",
                (int(t_from.timestamp()), int(t_to.timestamp())))
            return cur.fetchall()

    def save_params(self, params: dict, info: dict):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO params VALUES (?,?,?)",
                            (int(datetime.now(timezone.utc).timestamp()), json.dumps(params), json.dumps(info)))
            self.db.commit()

    def latest_params(self) -> tuple[dict, dict, datetime] | None:
        with self.lock:
            row = self.db.execute("SELECT created, data, info FROM params ORDER BY created DESC LIMIT 1").fetchone()
        if not row:
            return None
        return json.loads(row[1]), json.loads(row[2]), datetime.fromtimestamp(row[0], tz=timezone.utc)
