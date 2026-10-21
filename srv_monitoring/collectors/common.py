from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


STATUS_RANK = {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}


def overall_status(checks: Iterable[dict[str, Any]], fallback: str = "ok") -> str:
    states = [str(check.get("status", "unknown")).lower() for check in checks]
    if not states:
        return fallback
    return max(states, key=lambda state: STATUS_RANK.get(state, 1))


def result(
    checks: list[dict[str, Any]],
    summary: str,
    metrics: dict[str, Any] | None = None,
    devices: list[dict[str, Any]] | None = None,
    details: dict[str, Any] | None = None,
    fallback: str = "unknown",
) -> dict[str, Any]:
    value = {
        "status": overall_status(checks, fallback),
        "summary": summary,
        "checks": checks,
        "metrics": metrics or {},
        "devices": devices or [],
    }
    if details:
        value["details"] = details
    return value


def classify_health(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("Health") or value.get("health") or value.get("State") or value.get("state")
    if value is None:
        return "unknown"
    normalized = str(value).strip().lower().replace("_", "").replace("-", "")
    if normalized in {"ok", "healthy", "green", "enabled", "normal", "operational", "on"}:
        return "ok"
    if normalized in {"warning", "warn", "yellow", "degraded", "predictivefailure", "predictivefail"}:
        return "warning"
    if normalized in {
        "critical", "red", "failed", "failure", "unhealthy", "nonrecoverableerror",
        "fatal", "lostcommunication", "error", "fault", "offline",
    }:
        return "critical"
    if normalized in {"unknown", "na", "n/a", "notavailable", "unavailable", "notreported"}:
        return "unknown"
    return "unknown"


def get_path(data: Any, path: str | None, default: Any = None) -> Any:
    if path is None or not path.strip():
        return data
    current = data
    for part in path.strip().strip(".").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
