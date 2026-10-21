from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from srv_monitoring.config import Settings
from srv_monitoring.db import Database, utc_now
from srv_monitoring.app import create_app


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=Path(__file__).resolve().parents[1],
        data_dir=tmp_path,
        database_path=tmp_path / "test.sqlite3",
        host="127.0.0.1",
        port=8765,
        public_base_url=None,
        admin_user="admin",
        admin_password="test-password",
        session_secret=b"a-session-signing-secret-that-is-long-enough",
        encryption_key=Fernet.generate_key(),
        trusted_hosts=("*",),
        ssl_certfile=None,
        ssl_keyfile=None,
        secure_cookies=False,
        require_https=False,
        max_request_bytes=262144,
    )


def login(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200


def test_auth_source_secret_and_agent_ingest(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), Database(tmp_path / "test.sqlite3"))
    with TestClient(app) as client:
        assert client.get("/api/sources").status_code == 401
        login(client)
        created = client.post(
            "/api/sources",
            json={
                "name": "Vinchin test",
                "type": "backup_json",
                "config": {
                    "url": "https://backup.example/api/jobs",
                    "auth_type": "basic",
                    "username": "readonly",
                    "password": "do-not-return-this",
                    "jobs_path": "data.jobs",
                },
            },
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["has_password"] is True
        assert "do-not-return-this" not in created.text
        listed = client.get("/api/sources").json()
        assert "password" not in listed[0]["config"]

        agent = client.post("/api/agents", json={"name": "Windows backup node"})
        assert agent.status_code == 200
        enrollment = agent.json()
        assert enrollment["token"]
        report = {
            "hostname": "win-backup-01",
            "agent_version": "0.1.0",
            "collected_at": utc_now(),
            "metrics": {"cpu_percent": 12.5, "memory_percent": 40.0, "disk_min_free_percent": 21.0},
            "checks": [{"name": "CPU", "status": "ok", "message": "12.5%"}],
            "events": [],
        }
        wrong = client.post(f"/api/ingest/{enrollment['id']}", json=report, headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401
        accepted = client.post(
            f"/api/ingest/{enrollment['id']}",
            json=report,
            headers={"Authorization": f"Bearer {enrollment['token']}"},
        )
        assert accepted.status_code == 200
        overview = client.get("/api/overview").json()
        assert overview["counts"]["total"] == 2
        assert any(item["name"] == "win-backup-01" for item in overview["devices"])


def test_settings_and_source_validation(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), Database(tmp_path / "test.sqlite3"))
    with TestClient(app) as client:
        login(client)
        invalid_interval = client.put("/api/settings", json={"poll_interval_seconds": 3})
        assert invalid_interval.status_code == 422
        saved = client.put("/api/settings", json={"poll_interval_seconds": 300})
        assert saved.status_code == 200
        assert saved.json()["poll_interval_seconds"] == 300
        invalid_source = client.post(
            "/api/sources",
            json={"name": "no host", "type": "vmware", "config": {"username": "x", "password": "y"}},
        )
        assert invalid_source.status_code == 400
