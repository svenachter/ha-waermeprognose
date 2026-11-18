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
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(fc_hourly)")}
            for col in ("pv", "hh"):          # added in 0.3.0
                if col not in cols:
                    self.db.execute(f"ALTER TABLE fc_hourly ADD COLUMN {col} REAL")
            self.db.commit()

    def save_forecast(self, issued: datetime, hourly: list[tuple]):
        """hourly: (hour start, q_heat kWh, q_dhw kWh, p_el kWh, pv kWh | None, household kWh | None)"""
        ts = int(issued.timestamp())
        rows = []
        for h in hourly:
            t, qh, qd, pe, *rest = h
            pv, hh = (list(rest) + [None, None])[:2]
            rows.append((ts, int(t.timestamp()), qh, qd, pe, pv, hh))
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO fc_hourly (issued, target, q_heat, q_dhw, p_el, pv, hh) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            self.db.execute("DELETE FROM fc_hourly WHERE issued < ?", (ts - KEEP_DAYS * 86400,))
            self.db.commit()

    def forecasts_for(self, t_from: datetime, t_to: datetime) -> list[tuple]:
        with self.lock:
            cur = self.db.execute(
                "SELECT issued, target, q_heat, q_dhw, p_el, pv, hh FROM fc_hourly "
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
