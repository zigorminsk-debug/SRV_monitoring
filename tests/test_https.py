from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from srv_monitoring.app import create_app
from srv_monitoring.config import Settings
from srv_monitoring.db import Database


def test_public_app_requires_real_https_not_a_forwarded_header(tmp_path: Path) -> None:
    settings = Settings(
        root_dir=Path(__file__).resolve().parents[1],
        data_dir=tmp_path,
        database_path=tmp_path / "test.sqlite3",
        host="0.0.0.0",
        port=8765,
        public_base_url=None,
        admin_user="admin",
        admin_password="test-password",
        session_secret=b"secure-test-session-secret",
        encryption_key=Fernet.generate_key(),
        trusted_hosts=("*",),
        ssl_certfile=None,
        ssl_keyfile=None,
        secure_cookies=True,
        require_https=True,
        max_request_bytes=262144,
    )
    app = create_app(settings, Database(settings.database_path))
    with TestClient(app) as client:
        assert client.get("/").status_code == 426
        # A raw HTTP client must not bypass TLS enforcement by spoofing a proxy header.
        assert client.get("/", headers={"X-Forwarded-Proto": "https"}).status_code == 426
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/").status_code == 200
