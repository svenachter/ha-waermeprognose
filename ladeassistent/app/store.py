"""SQLite: vehicle samples and small JSON state (sent notifications, snooze)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import Sample

KEEP_DAYS = 400


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, soc REAL, odo REAL,
                    home INTEGER, plugged INTEGER, kw REAL);
                CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """)
            self.db.commit()

    def add(self, samples: list[Sample]) -> None:
        b = lambda x: None if x is None else int(bool(x))
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?)",
                [(int(s.ts.timestamp()), s.soc, s.odo, b(s.home), b(s.plugged), s.charge_kw) for s in samples])
            cutoff = int((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).timestamp())
            self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self.db.commit()

    def samples(self, since: datetime | None = None) -> list[Sample]:
        q, args = "SELECT * FROM samples", ()
        if since:
            q, args = q + " WHERE ts >= ?", (int(since.timestamp()),)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY ts", args).fetchall()
        bo = lambda x: None if x is None else bool(x)
        return [Sample(datetime.fromtimestamp(r[0], tz=timezone.utc), r[1], r[2], bo(r[3]), bo(r[4]), r[5])
                for r in rows]

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def get(self, key: str, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key: str, value) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
            self.db.commit()
