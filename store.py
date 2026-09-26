"""Data-state layer: SQLite schema, migrations and transaction handling."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core import iso

SCHEDULE_STATUSES = ("scheduled", "receiving", "received", "canceled", "preempted", "review", "superseded")
# Occupies an antenna / satellite time slot and consumes tenant quota.
ACTIVE_SCHEDULE_STATUSES = ("scheduled", "receiving")
# Finished data acquisition: records and received data are protected.
PROTECTED_SCHEDULE_STATUSES = ("received",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS satellites(id TEXT PRIMARY KEY, name TEXT NOT NULL, data_rate_mbps REAL NOT NULL, priority INTEGER NOT NULL, storage_capacity_mb REAL NOT NULL, tenant TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS stations(id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', weather TEXT NOT NULL DEFAULT 'clear');
CREATE TABLE IF NOT EXISTS antennas(id TEXT PRIMARY KEY, station_id TEXT NOT NULL REFERENCES stations(id), max_rate_mbps REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS maintenance(id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL REFERENCES stations(id), antenna_id TEXT REFERENCES antennas(id), starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visibility_windows(id INTEGER PRIMARY KEY AUTOINCREMENT, satellite_id TEXT NOT NULL REFERENCES satellites(id), station_id TEXT NOT NULL REFERENCES stations(id), starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_rate_mbps REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, satellite_id TEXT NOT NULL REFERENCES satellites(id), tenant TEXT NOT NULL, priority INTEGER NOT NULL, data_mb REAL NOT NULL, deadline TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quotas(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, station_id TEXT NOT NULL REFERENCES stations(id), daily_seconds INTEGER NOT NULL, UNIQUE(tenant,station_id));
CREATE TABLE IF NOT EXISTS schedules(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES requests(id), window_id INTEGER NOT NULL REFERENCES visibility_windows(id), station_id TEXT NOT NULL REFERENCES stations(id), antenna_id TEXT NOT NULL REFERENCES antennas(id), satellite_id TEXT NOT NULL REFERENCES satellites(id), starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, rate_mbps REAL NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', revision INTEGER NOT NULL DEFAULT 1, disposition_reason TEXT, maintenance_conflict_id INTEGER REFERENCES maintenance(id), superseded_by_schedule_id INTEGER REFERENCES schedules(id), created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, schedule_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
"""

SCHEDULE_COLUMNS = [
    "request_id", "window_id", "station_id", "antenna_id", "satellite_id", "starts_at",
    "ends_at", "rate_mbps", "status", "revision", "disposition_reason",
    "maintenance_conflict_id", "superseded_by_schedule_id", "created_by", "created_at", "updated_at",
]


class Repository:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Upgrade prototype databases: add maintenance/redispatch link columns, drop the one-row-per-request unique index."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(schedules)")}
        if "maintenance_conflict_id" not in cols:
            self.conn.execute("ALTER TABLE schedules ADD COLUMN maintenance_conflict_id INTEGER")
        if "superseded_by_schedule_id" not in cols:
            self.conn.execute("ALTER TABLE schedules ADD COLUMN superseded_by_schedule_id INTEGER")
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='schedules'").fetchone()[0] or ""
        if "request_id INTEGER NOT NULL UNIQUE" in sql.replace("\n", " "):
            with self.tx() as conn:
                conn.execute("ALTER TABLE schedules RENAME TO schedules_legacy")
                conn.execute(
                    """CREATE TABLE schedules(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES requests(id),
                       window_id INTEGER NOT NULL REFERENCES visibility_windows(id), station_id TEXT NOT NULL REFERENCES stations(id),
                       antenna_id TEXT NOT NULL REFERENCES antennas(id), satellite_id TEXT NOT NULL REFERENCES satellites(id),
                       starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, rate_mbps REAL NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled',
                       revision INTEGER NOT NULL DEFAULT 1, disposition_reason TEXT,
                       maintenance_conflict_id INTEGER REFERENCES maintenance(id),
                       superseded_by_schedule_id INTEGER REFERENCES schedules(id),
                       created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                conn.execute(
                    f"INSERT INTO schedules({', '.join(['id'] + SCHEDULE_COLUMNS)}) "
                    f"SELECT {', '.join(['id'] + SCHEDULE_COLUMNS)} FROM schedules_legacy")
                conn.execute("DROP TABLE schedules_legacy")

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    @staticmethod
    def audit(conn: sqlite3.Connection, request_id: int | None, schedule_id: int | None,
              actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(request_id,schedule_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, schedule_id, actor, role, action,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))
