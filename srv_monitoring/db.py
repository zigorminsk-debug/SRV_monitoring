from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(value: str | None, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20, isolation_level="DEFERRED")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_ciphertext TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_poll_at TEXT,
                    last_status TEXT NOT NULL DEFAULT 'unknown',
                    last_summary TEXT NOT NULL DEFAULT 'Ожидает первого опроса',
                    last_error TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    latest_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled, last_poll_at);

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_seen TEXT,
                    last_status TEXT NOT NULL DEFAULT 'unknown',
                    latest_json TEXT
                );

                CREATE TABLE IF NOT EXISTS poll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT,
                    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_poll_runs_source_time ON poll_runs(source_id, finished_at DESC);

                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_entity_time ON samples(entity_type, entity_id, collected_at DESC);

                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    resolved_at TEXT,
                    acknowledged_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_active_fingerprint
                    ON alerts(fingerprint) WHERE resolved_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_alerts_last_seen ON alerts(last_seen DESC);
                """
            )
            row = conn.execute("SELECT value FROM settings WHERE key = 'poll_interval_seconds'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                    ("poll_interval_seconds", "60", utc_now()),
                )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, str(value), now),
            )

    def get_poll_interval(self) -> int:
        try:
            return max(15, min(86400, int(self.get_setting("poll_interval_seconds", "60"))))
        except (TypeError, ValueError):
            return 60

    def list_sources(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM sources ORDER BY name COLLATE NOCASE").fetchall()
        return [self._source_dict(row) for row in rows]

    @staticmethod
    def _source_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_poll_at": row["last_poll_at"],
            "last_status": row["last_status"],
            "last_summary": row["last_summary"],
            "last_error": row["last_error"],
            "failure_count": row["failure_count"],
            "config_ciphertext": row["config_ciphertext"],
            "latest": _load(row["latest_json"], {}),
        }

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return self._source_dict(row) if row else None

    def insert_source(self, name: str, source_type: str, config_ciphertext: str, enabled: bool) -> dict[str, Any]:
        source_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sources(id, name, type, enabled, config_ciphertext, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source_id, name, source_type, int(enabled), config_ciphertext, now, now),
            )
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return self._source_dict(row)

    def update_source(
        self, source_id: str, name: str, source_type: str, config_ciphertext: str, enabled: bool
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE sources SET name = ?, type = ?, config_ciphertext = ?, enabled = ?, updated_at = ? "
                "WHERE id = ?",
                (name, source_type, config_ciphertext, int(enabled), now, source_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return self._source_dict(row)

    def delete_source(self, source_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            conn.execute("DELETE FROM alerts WHERE entity_type = 'source' AND entity_id = ?", (source_id,))
            conn.execute("DELETE FROM samples WHERE entity_type = 'source' AND entity_id = ?", (source_id,))
            return cursor.rowcount > 0

    def list_agents(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY name COLLATE NOCASE").fetchall()
        return [self._agent_dict(row) for row in rows]

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "last_seen": row["last_seen"],
            "last_status": row["last_status"],
            "latest": _load(row["latest_json"], {}),
        }

    def insert_agent(self, name: str, token_hash: str) -> dict[str, Any]:
        agent_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO agents(id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                (agent_id, name, token_hash, now),
            )
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return self._agent_dict(row)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return self._agent_dict(row) if row else None

    def get_agent_token_hash(self, agent_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT token_hash FROM agents WHERE id = ? AND enabled = 1", (agent_id,)
            ).fetchone()
        return row["token_hash"] if row else None

    def revoke_agent(self, agent_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("UPDATE agents SET enabled = 0 WHERE id = ?", (agent_id,))
            conn.execute("UPDATE alerts SET resolved_at = ? WHERE entity_type = 'agent' AND entity_id = ? AND resolved_at IS NULL", (utc_now(), agent_id))
            return cursor.rowcount > 0

    @staticmethod
    def _severity_rank(status: str) -> int:
        return {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}.get(
            str(status).lower(), 1
        )

    @classmethod
    def _overall_status(cls, checks: Iterable[dict[str, Any]], fallback: str = "ok") -> str:
        statuses = [str(check.get("status", "unknown")).lower() for check in checks]
        if not statuses:
            return fallback
        return max(statuses, key=cls._severity_rank)

    @staticmethod
    def _sync_alerts(
        conn: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        name: str,
        checks: list[dict[str, Any]],
        now: str,
        resolve_missing: bool = True,
    ) -> None:
        active_rows = conn.execute(
            "SELECT fingerprint FROM alerts WHERE entity_type = ? AND entity_id = ? AND resolved_at IS NULL",
            (entity_type, entity_id),
        ).fetchall()
        present: set[str] = set()
        for check in checks:
            subject = str(check.get("name") or "Health")[:180]
            state = str(check.get("status", "unknown")).lower()
            fingerprint = f"{entity_type}:{entity_id}:{subject}"
            present.add(fingerprint)
            if state == "ok":
                conn.execute(
                    "UPDATE alerts SET resolved_at = ? WHERE fingerprint = ? AND resolved_at IS NULL",
                    (now, fingerprint),
                )
                continue
            severity = "critical" if state == "critical" else "warning"
            message = str(check.get("message") or f"{subject}: {state}")[:2000]
            row = conn.execute(
                "SELECT id FROM alerts WHERE fingerprint = ? AND resolved_at IS NULL", (fingerprint,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE alerts SET severity = ?, message = ?, last_seen = ? WHERE id = ?",
                    (severity, message, now, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO alerts(id, fingerprint, entity_type, entity_id, subject, severity, message, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()), fingerprint, entity_type, entity_id,
                        f"{name} · {subject}", severity, message, now, now,
                    ),
                )
        if resolve_missing:
            for row in active_rows:
                if row["fingerprint"] not in present:
                    conn.execute(
                        "UPDATE alerts SET resolved_at = ? WHERE fingerprint = ? AND resolved_at IS NULL",
                        (now, row["fingerprint"]),
                    )

    def record_source_poll(
        self,
        source_id: str,
        started_at: str,
        result: dict[str, Any],
        error: str | None = None,
    ) -> dict[str, Any] | None:
        finished_at = utc_now()
        with self.connect() as conn:
            source = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if source is None:
                return None
            checks = list(result.get("checks") or [])
            if error:
                failure_count = int(source["failure_count"] or 0) + 1
                severity = "critical" if failure_count >= 2 else "warning"
                result = {
                    "status": severity,
                    "summary": f"Не удалось подключиться: {error[:240]}",
                    "checks": [
                        {
                            "name": "Связь с источником",
                            "status": severity,
                            "message": error[:1500],
                        }
                    ],
                    "metrics": {},
                    "devices": [],
                }
                checks = result["checks"]
                last_error = error[:2000]
            else:
                failure_count = 0
                last_error = None
                result.setdefault("status", self._overall_status(checks, "ok"))
                result.setdefault("summary", "Опрос завершён")
                result.setdefault("checks", checks)
                result.setdefault("metrics", {})
                result.setdefault("devices", [])

            status = str(result.get("status", self._overall_status(checks, "unknown"))).lower()
            result["status"] = status
            result["collected_at"] = finished_at
            summary = str(result.get("summary", "Опрос завершён"))[:500]
            conn.execute(
                "UPDATE sources SET last_poll_at = ?, last_status = ?, last_summary = ?, last_error = ?, "
                "failure_count = ?, latest_json = ? WHERE id = ?",
                (finished_at, status, summary, last_error, failure_count, _json(result), source_id),
            )
            conn.execute(
                "INSERT INTO poll_runs(source_id, started_at, finished_at, status, summary, result_json, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source_id, started_at, finished_at, status, summary, _json(result), last_error),
            )
            metrics = result.get("metrics")
            if isinstance(metrics, dict) and metrics:
                conn.execute(
                    "INSERT INTO samples(entity_type, entity_id, collected_at, metrics_json) VALUES (?, ?, ?, ?)",
                    ("source", source_id, finished_at, _json(metrics)),
                )
            self._sync_alerts(
                conn, "source", source_id, source["name"], checks, finished_at,
                resolve_missing=not bool(error),
            )
        return result

    def record_agent_report(self, agent_id: str, report: dict[str, Any]) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as conn:
            agent = conn.execute(
                "SELECT * FROM agents WHERE id = ? AND enabled = 1", (agent_id,)
            ).fetchone()
            if agent is None:
                return None
            checks = [dict(check) for check in report.get("checks", [])]
            for event in report.get("events", []):
                event_id = event.get("event_id", "?")
                source = str(event.get("source", "Windows"))[:80]
                level = str(event.get("level", "error")).lower()
                status = "critical" if level in {"critical", "1", "fatal", "error", "2"} else "warning"
                timestamp = event.get("timestamp") or ""
                checks.append(
                    {
                        "name": f"Журнал {source} · {event_id}",
                        "status": status,
                        "message": f"{timestamp} {event.get('message', '')}".strip()[:1500],
                    }
                )
            status = self._overall_status(checks, "ok")
            normalized = dict(report)
            normalized["checks"] = checks
            normalized["status"] = status
            normalized["received_at"] = now
            conn.execute(
                "UPDATE agents SET last_seen = ?, last_status = ?, latest_json = ? WHERE id = ?",
                (now, status, _json(normalized), agent_id),
            )
            metrics = report.get("metrics")
            if isinstance(metrics, dict) and metrics:
                conn.execute(
                    "INSERT INTO samples(entity_type, entity_id, collected_at, metrics_json) VALUES (?, ?, ?, ?)",
                    ("agent", agent_id, now, _json(metrics)),
                )
            self._sync_alerts(conn, "agent", agent_id, agent["name"], checks, now)
            return normalized

    def list_alerts(self, limit: int = 100, active_only: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        query = "SELECT * FROM alerts"
        if active_only:
            query += " WHERE resolved_at IS NULL"
        query += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, last_seen DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_alert(self, alert_id: str, acknowledged: bool) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE alerts SET acknowledged_at = ? WHERE id = ?",
                (utc_now() if acknowledged else None, alert_id),
            )
            return cursor.rowcount > 0

    def prune_history(
        self,
        sample_days: int = 90,
        run_days: int = 30,
        resolved_alert_days: int = 180,
        max_runs_per_source: int = 1000,
    ) -> None:
        sample_days = max(7, min(3650, int(sample_days)))
        run_days = max(7, min(3650, int(run_days)))
        resolved_alert_days = max(7, min(3650, int(resolved_alert_days)))
        max_runs_per_source = max(100, min(100000, int(max_runs_per_source)))
        with self.connect() as conn:
            conn.execute("DELETE FROM samples WHERE datetime(collected_at) < datetime('now', ?)", (f"-{sample_days} days",))
            conn.execute("DELETE FROM poll_runs WHERE datetime(finished_at) < datetime('now', ?)", (f"-{run_days} days",))
            conn.execute("DELETE FROM alerts WHERE resolved_at IS NOT NULL AND datetime(resolved_at) < datetime('now', ?)", (f"-{resolved_alert_days} days",))
            conn.execute(
                "DELETE FROM poll_runs WHERE id IN ("
                "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY finished_at DESC, id DESC) AS row_num FROM poll_runs) "
                "WHERE row_num > ?)",
                (max_runs_per_source,),
            )

    def get_history(self, hours: int = 24, limit: int = 600) -> list[dict[str, Any]]:
        hours = max(1, min(720, int(hours)))
        limit = max(1, min(2000, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT samples.*, COALESCE(sources.name, agents.name, samples.entity_id) AS name "
                "FROM samples LEFT JOIN sources ON samples.entity_type = 'source' AND sources.id = samples.entity_id "
                "LEFT JOIN agents ON samples.entity_type = 'agent' AND agents.id = samples.entity_id "
                "WHERE datetime(samples.collected_at) >= datetime('now', ?) "
                "ORDER BY samples.collected_at ASC LIMIT ?",
                (f"-{hours} hours", limit),
            ).fetchall()
        return [
            {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "name": row["name"],
                "collected_at": row["collected_at"],
                "metrics": _load(row["metrics_json"], {}),
            }
            for row in rows
        ]

    def check_stale_agents(self, stale_after_seconds: int) -> None:
        now_dt = datetime.now(timezone.utc)
        now = utc_now()
        threshold = max(60, int(stale_after_seconds))
        with self.connect() as conn:
            agents = conn.execute("SELECT * FROM agents WHERE enabled = 1").fetchall()
            for agent in agents:
                baseline = agent["last_seen"] or agent["created_at"]
                try:
                    seen = datetime.fromisoformat(baseline.replace("Z", "+00:00"))
                    if seen.tzinfo is None:
                        seen = seen.replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    continue
                age = max(0, int((now_dt - seen.astimezone(timezone.utc)).total_seconds()))
                if age < threshold:
                    continue
                state = "critical" if age >= threshold * 3 else "warning"
                check = {
                    "name": "Связь удалённого агента",
                    "status": state,
                    "message": f"Нет heartbeat {age // 60} мин.; порог {threshold // 60} мин.",
                }
                conn.execute("UPDATE agents SET last_status = ? WHERE id = ?", (state, agent["id"]))
                self._sync_alerts(conn, "agent", agent["id"], agent["name"], [check], now, resolve_missing=False)

    def list_runs(self, source_id: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM poll_runs WHERE source_id = ? ORDER BY finished_at DESC LIMIT ?",
                (source_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "status": row["status"],
                "summary": row["summary"],
                "error": row["error"],
                "result": _load(row["result_json"], {}),
            }
            for row in rows
        ]
