from __future__ import annotations

import asyncio

import pytest

import srv_monitoring.collectors.backup_json as backup_json
from srv_monitoring.collectors.backup_json import _status, _values, DEFAULT_FAILURE, DEFAULT_SUCCESS
from srv_monitoring.collectors.common import classify_health, get_path, parse_datetime
from srv_monitoring.collectors.registry import validate_source_config
from srv_monitoring.collectors.ipmi import _sel_faults


def test_get_path_supports_nested_objects_and_arrays() -> None:
    payload = {"data": {"jobs": [{"status": "success"}]}}
    assert get_path(payload, "data.jobs.0.status") == "success"
    assert get_path(payload, "data.missing", "fallback") == "fallback"


def test_classify_redfish_health_values() -> None:
    assert classify_health("OK") == "ok"
    assert classify_health({"Health": "Warning"}) == "warning"
    assert classify_health("Critical") == "critical"
    assert classify_health("not-reported") == "unknown"


def test_parse_datetime_handles_iso_and_epoch() -> None:
    assert parse_datetime("2026-10-21T10:00:00Z").isoformat() == "2026-10-21T10:00:00+00:00"
    assert parse_datetime("not a date") is None
    assert parse_datetime(0).year == 1970


def test_backup_status_mapping_and_custom_values() -> None:
    assert _status("Succeeded", DEFAULT_SUCCESS, DEFAULT_FAILURE) == "ok"
    assert _status("Running", DEFAULT_SUCCESS, DEFAULT_FAILURE) == "ok"
    assert _status("Failed", DEFAULT_SUCCESS, DEFAULT_FAILURE) == "critical"
    assert _status("Queued", DEFAULT_SUCCESS, DEFAULT_FAILURE) == "warning"
    assert _values("done,good", set()) == {"done", "good"}


def test_backup_collector_maps_jobs_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "data": {
            "jobs": [
                {"name": "nightly", "state": "Success", "last_run": "2026-10-21T02:15:00Z"},
                {"name": "weekly", "state": "Failed", "last_run": "2026-10-20T02:15:00Z"},
            ]
        }
    }

    class FakeResponse:
        status_code = 200

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, **kwargs):
            self.options = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            assert url == "https://backup.example/api/jobs"
            return FakeResponse()

    monkeypatch.setattr(backup_json.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(backup_json.collect_backup_json(
        {
            "url": "https://backup.example/api/jobs",
            "auth_type": "none",
            "jobs_path": "data.jobs",
            "status_path": "state",
            "job_name_path": "name",
            "last_run_path": "last_run",
            "expected_interval_minutes": 0,
        }
    ))
    assert result["status"] == "critical"
    assert result["metrics"]["backup_job_count"] == 2
    assert result["metrics"]["backup_failed_count"] == 1
    assert any(check["name"] == "Бэкап · weekly" for check in result["checks"])


def test_ipmi_sel_asserted_fault_is_active_until_cleared() -> None:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%m/%d/%Y | %H:%M:%S")
    asserted = f"1 | {stamp} | Power Supply 1 | Failure detected | Asserted"
    active, _ = _sel_faults([asserted], lookback_hours=24)
    assert len(active) == 1
    assert active[0]["severity"] == "critical"
    cleared = f"2 | {stamp} | Power Supply 1 | Failure detected | Deasserted"
    active, _ = _sel_faults([asserted, cleared], lookback_hours=24)
    assert active == []


def test_source_config_validation_and_secret_preservation() -> None:
    initial = validate_source_config(
        "backup_json",
        {
            "url": "https://backup.example/api/jobs",
            "auth_type": "basic",
            "username": "monitor",
            "password": "supersecret",
            "jobs_path": "data.jobs",
        },
    )
    changed = validate_source_config(
        "backup_json",
        {
            "url": "https://backup.example/api/jobs",
            "auth_type": "basic",
            "username": "monitor",
            "password": "",
            "jobs_path": "data.jobs",
        },
        initial,
    )
    assert changed["password"] == "supersecret"
    assert changed["jobs_path"] == "data.jobs"


def test_source_config_rejects_credentials_embedded_in_url() -> None:
    with pytest.raises(ValueError, match="не помещайте логин/пароль"):
        validate_source_config(
            "backup_json",
            {"url": "https://user:pass@backup.example/api", "auth_type": "none"},
        )
    with pytest.raises(ValueError, match="не передавайте секреты"):
        validate_source_config(
            "backup_json",
            {"url": "https://backup.example/api?api_key=secret", "auth_type": "none"},
        )
