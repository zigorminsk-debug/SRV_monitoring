from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Any

from .common import result


CRITICAL_STATES = {"cr", "critical", "nr", "ucr", "lcr", "non-recoverable", "nonrecoverable"}
WARNING_STATES = {"nc", "warning", "unc", "lnc", "non-critical", "noncritical", "asserted"}
IGNORED_STATES = {"na", "ns", "n/a", "no reading", "not available", "not readable"}
CRITICAL_TERMS = ("failure", "failed", "fault", "critical", "non-recoverable", "over temperature", "overtemperature", "lost communication", "unrecoverable")
WARNING_TERMS = ("threshold", "predictive", "degraded", "non-critical", "noncritical", "warning")


def _run(command: list[str], password: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["IPMI_PASSWORD"] = password
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _sel_faults(lines: list[str], lookback_hours: int = 24) -> tuple[list[dict[str, Any]], list[str]]:
    now = datetime.now(timezone.utc)
    entries: list[dict[str, Any]] = []
    raw_lines = [line.strip() for line in lines if line.strip()][-500:]
    for line in raw_lines:
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 5:
            continue
        try:
            timestamp = datetime.strptime(f"{parts[1]} {parts[2]}", "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            timestamp = None
        sensor = parts[3] if len(parts) > 3 else "IPMI sensor"
        direction = parts[-1].lower()
        description = " ".join(parts[4:-1]).strip() or (parts[4] if len(parts) > 4 else "")
        text = f"{sensor} {description}".lower()
        severity = "critical" if any(term in text for term in CRITICAL_TERMS) else "warning" if any(term in text for term in WARNING_TERMS) else None
        if severity is None:
            continue
        active = "deassert" not in direction and ("assert" in direction or direction in {"1", "on", "active"})
        entries.append(
            {
                "record_id": parts[0],
                "timestamp": timestamp,
                "sensor": sensor,
                "description": description,
                "direction": direction,
                "severity": severity,
                "active": active,
                "raw": line,
            }
        )

    # SEL is chronological on most BMCs, but sort by timestamp when available.
    entries.sort(key=lambda item: item["timestamp"] or datetime.min.replace(tzinfo=timezone.utc))
    active_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in entries:
        key = (item["sensor"].lower(), item["description"].lower())
        if item["active"]:
            active_by_key[key] = item
        else:
            active_by_key.pop(key, None)
    threshold = max(1, min(720, int(lookback_hours)))
    active = [
        item for item in active_by_key.values()
        if item["timestamp"] is not None and (now - item["timestamp"]).total_seconds() <= threshold * 3600
    ]
    recent_raw = raw_lines[-20:]
    return active, recent_raw


def collect_ipmi(config: dict[str, Any]) -> dict[str, Any]:
    host = str(config.get("host") or "").strip()
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    tool = str(config.get("ipmitool_path") or "ipmitool").strip()
    if not host:
        raise ValueError("Не указан адрес BMC/IPMI")
    if not username or not password:
        raise ValueError("Для IPMI требуется имя пользователя и пароль")

    command = [tool, "-I", "lanplus", "-H", host, "-U", username, "-E", "sensor", "list"]
    try:
        completed = _run(command, password, int(config.get("timeout_seconds", 30)))
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Не найден ipmitool. Установите ipmitool и укажите полный путь к ipmitool.exe в настройках"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Тайм-аут ответа IPMI BMC") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "Команда ipmitool завершилась с ошибкой").strip()
        raise RuntimeError(f"IPMI: {message[-600:]}")

    checks: list[dict[str, Any]] = [
        {"name": "Связь с IPMI BMC", "status": "ok", "message": "Получен список аппаратных датчиков"}
    ]
    sensors: list[dict[str, Any]] = []
    critical = warning = 0
    for line in completed.stdout.splitlines():
        if "|" not in line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 4:
            continue
        name, reading, units, raw_state = parts[:4]
        if not name:
            continue
        state_token = raw_state.strip().lower()
        if state_token in IGNORED_STATES or not state_token:
            continue
        if state_token in CRITICAL_STATES:
            state = "critical"
            critical += 1
        elif state_token in WARNING_STATES:
            state = "warning"
            warning += 1
        elif state_token in {"ok", "normal", "deasserted"}:
            state = "ok"
        else:
            # Vendor-specific sensor states are reported, not silently treated as healthy.
            state = "unknown"
        message = f"Показание: {reading} {units} · состояние: {raw_state}"
        checks.append({"name": f"IPMI · {name}"[:180], "status": state, "message": message[:1500]})
        sensors.append({"name": name, "reading": reading, "units": units, "state": raw_state, "health": state})

    if not sensors:
        checks.append(
            {
                "name": "Датчики IPMI",
                "status": "warning",
                "message": "Ответ получен, но ipmitool не вернул распознаваемые показания датчиков",
            }
        )

    sel_lines: list[str] = []
    try:
        sel = _run([tool, "-I", "lanplus", "-H", host, "-U", username, "-E", "sel", "elist"], password, 20)
        if sel.returncode == 0:
            sel_lines = [line.strip() for line in sel.stdout.splitlines() if line.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    active_sel, recent_sel = _sel_faults(sel_lines, int(config.get("sel_lookback_hours", 24)))
    for event in active_sel:
        timestamp = event["timestamp"].isoformat(timespec="seconds") if event["timestamp"] else ""
        name = f"IPMI SEL · {event['sensor']} · {event['description']}"[:180]
        message = f"{timestamp} · {event['description']} · {event['direction']}".strip(" ·")
        checks.append({"name": name, "status": event["severity"], "message": message[:1500]})
        if event["severity"] == "critical":
            critical += 1
        else:
            warning += 1
        sensors.append(
            {
                "name": event["sensor"],
                "kind": "System Event Log",
                "status": event["severity"],
                "message": event["description"],
                "last_event_at": timestamp,
            }
        )

    return result(
        checks,
        f"Датчиков: {len(sensors) - len(active_sel)} · критических: {critical} · предупреждений: {warning} · активных SEL: {len(active_sel)}",
        metrics={
            "ipmi_sensor_count": len(sensors) - len(active_sel),
            "ipmi_critical_sensor_count": critical,
            "ipmi_warning_sensor_count": warning,
            "ipmi_sel_fault_count": len(active_sel),
        },
        devices=sensors,
        details={"provider": "IPMI / ipmitool", "recent_sel_entries": recent_sel},
    )
