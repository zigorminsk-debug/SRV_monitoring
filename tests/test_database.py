from __future__ import annotations

from cryptography.fernet import Fernet

from srv_monitoring.db import Database, utc_now
from srv_monitoring.security import SecretBox


def test_source_alerts_open_update_and_resolve(tmp_path) -> None:
    db = Database(tmp_path / "monitoring.sqlite3")
    db.initialize()
    box = SecretBox(Fernet.generate_key())
    source = db.insert_source("ESXi lab", "vmware", box.encrypt_json({"host": "esxi.local"}), True)

    first = db.record_source_poll(
        source["id"], utc_now(),
        {"status": "warning", "summary": "low free disk", "checks": [{"name": "Datastore", "status": "warning", "message": "4% free"}], "metrics": {"cpu_percent": 10}},
    )
    assert first["status"] == "warning"
    alerts = db.list_alerts(active_only=True)
    assert len(alerts) == 1 and alerts[0]["severity"] == "warning"

    db.record_source_poll(
        source["id"], utc_now(),
        {"status": "ok", "summary": "healthy", "checks": [{"name": "Datastore", "status": "ok", "message": "40% free"}], "metrics": {"cpu_percent": 12}},
    )
    assert db.list_alerts(active_only=True) == []
    assert db.list_alerts()[0]["resolved_at"] is not None
    assert len(db.list_runs(source["id"])) == 2
    assert len(db.get_history(hours=24)) == 2


def test_consecutive_source_failures_escalate(tmp_path) -> None:
    db = Database(tmp_path / "monitoring.sqlite3")
    db.initialize()
    source = db.insert_source("BMC", "redfish", "sealed", True)
    first = db.record_source_poll(source["id"], utc_now(), {}, error="timeout")
    second = db.record_source_poll(source["id"], utc_now(), {}, error="timeout")
    assert first["status"] == "warning"
    assert second["status"] == "critical"
    assert db.list_alerts(active_only=True)[0]["severity"] == "critical"


def test_agent_report_creates_and_clears_alert(tmp_path) -> None:
    db = Database(tmp_path / "monitoring.sqlite3")
    db.initialize()
    agent = db.insert_agent("backup-win", "token-hash")
    report = {
        "hostname": "backup-win",
        "agent_version": "test",
        "collected_at": utc_now(),
        "metrics": {"cpu_percent": 5, "memory_percent": 40},
        "checks": [{"name": "Vinchin job", "status": "critical", "message": "job failed"}],
        "events": [],
    }
    db.record_agent_report(agent["id"], report)
    assert db.list_alerts(active_only=True)[0]["severity"] == "critical"
    report["checks"] = [{"name": "Vinchin job", "status": "ok", "message": "job passed"}]
    db.record_agent_report(agent["id"], report)
    assert db.list_alerts(active_only=True) == []
    assert db.list_agents()[0]["last_status"] == "ok"
