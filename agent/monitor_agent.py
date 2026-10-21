from __future__ import annotations

import argparse
import json
import logging
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil

AGENT_VERSION = "0.1.0"
LOG = logging.getLogger("srv-monitoring-agent")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def state_for_threshold(value: float, warning: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "ok"


def _run_powershell(script: str, executable: str = "powershell.exe", timeout: int = 15) -> str:
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        raise RuntimeError((completed.stderr or "PowerShell command failed").strip()[:500])
    return completed.stdout.strip()


def collect_windows_events(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if platform.system().lower() != "windows":
        return [], []
    minutes = max(1, min(1440, int(config.get("windows_event_minutes", 15))))
    executable = str(config.get("powershell_path") or "powershell.exe")
    script = (
        f"$since=(Get-Date).AddMinutes(-{minutes}); "
        "$items=@('System','Application') | ForEach-Object { "
        "Get-WinEvent -FilterHashtable @{LogName=$_; Level=1,2; StartTime=$since} "
        "-ErrorAction SilentlyContinue } | Sort-Object TimeCreated -Descending | "
        "Select-Object -First 30 @{Name='source';Expression={$_.ProviderName}}, "
        "@{Name='event_id';Expression={$_.Id}}, "
        "@{Name='level';Expression={$_.LevelDisplayName}}, "
        "@{Name='timestamp';Expression={$_.TimeCreated.ToUniversalTime().ToString('o')}}, "
        "@{Name='message';Expression={($_.Message -replace '[\\r\\n]+',' ').Substring(0,[Math]::Min(500,$_.Message.Length))}}; "
        "ConvertTo-Json -InputObject @($items) -Depth 3 -Compress"
    )
    try:
        output = _run_powershell(script, executable, timeout=20)
        parsed = json.loads(output) if output else []
        if isinstance(parsed, dict):
            parsed = [parsed]
        events = []
        for item in parsed if isinstance(parsed, list) else []:
            if not isinstance(item, dict):
                continue
            events.append(
                {
                    "source": str(item.get("source") or "Windows")[:80],
                    "event_id": item.get("event_id"),
                    "level": str(item.get("level") or "error")[:40],
                    "timestamp": item.get("timestamp"),
                    "message": str(item.get("message") or "")[:1500],
                }
            )
        checks = []
        if events:
            critical = sum(1 for item in events if str(item.get("level", "")).lower() in {"critical", "1", "fatal", "error", "2"})
            checks.append(
                {
                    "name": "Критические события Windows",
                    "status": "critical" if critical else "warning",
                    "message": f"За последние {minutes} мин. найдено событий уровня Error/Critical: {len(events)}",
                }
            )
        else:
            checks.append(
                {
                    "name": "Критические события Windows",
                    "status": "ok",
                    "message": f"Ошибок в System/Application за последние {minutes} мин. не найдено",
                }
            )
        return events, checks
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        return [], [
            {
                "name": "Критические события Windows",
                "status": "warning",
                "message": f"Не удалось прочитать журнал событий: {str(exc)[:300]}",
            }
        ]


def _collect_service_checks(names: list[str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if platform.system().lower() != "windows" or not names:
        return checks
    for name in names[:40]:
        service_name = str(name).strip()
        if not service_name:
            continue
        try:
            service = psutil.win_service_get(service_name)
            info = service.as_dict()
            state = "ok" if str(info.get("status", "")).lower() == "running" else "critical"
            checks.append(
                {
                    "name": f"Windows service · {service_name}",
                    "status": state,
                    "message": f"Состояние: {info.get('status', 'unknown')}",
                }
            )
        except (psutil.Error, OSError) as exc:
            checks.append(
                {
                    "name": f"Windows service · {service_name}",
                    "status": "critical",
                    "message": f"Сервис недоступен: {str(exc)[:300]}",
                }
            )
    return checks


def _collect_external_checks(configured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for item in configured[:30]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "Пользовательская проверка")[:160]
        command = item.get("command")
        args = item.get("args", [])
        if not command or not isinstance(args, list):
            checks.append({"name": name, "status": "warning", "message": "Проверьте command/args в конфигурации агента"})
            continue
        timeout = max(1, min(120, int(item.get("timeout_seconds", 30))))
        try:
            completed = subprocess.run(
                [str(command), *[str(argument) for argument in args]],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = completed.stdout.strip()
            try:
                payload = json.loads(output) if output else {}
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and str(payload.get("status", "")).lower() in {"ok", "warning", "critical", "unknown"}:
                status = str(payload["status"]).lower()
                message = str(payload.get("message") or output or f"Exit code {completed.returncode}")[:1500]
                check_name = str(payload.get("name") or name)[:180]
            else:
                status = "ok" if completed.returncode == 0 else "critical"
                message = (output or completed.stderr.strip() or f"Exit code {completed.returncode}")[:1500]
                check_name = name
            checks.append({"name": check_name, "status": status, "message": message})
        except subprocess.TimeoutExpired:
            checks.append({"name": name, "status": "critical", "message": f"Превышен тайм-аут {timeout} сек."})
        except (OSError, ValueError) as exc:
            checks.append({"name": name, "status": "critical", "message": str(exc)[:500]})
    return checks


def _load_backup_report(config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = str(config.get("backup_status_file") or "").strip()
    if not path:
        return [], {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [{"name": "Статус резервного копирования", "status": "warning", "message": f"Файл отчёта не найден: {path}"}], {}
    except (OSError, json.JSONDecodeError) as exc:
        return [{"name": "Статус резервного копирования", "status": "warning", "message": f"Не удалось прочитать отчёт: {str(exc)[:300]}"}], {}
    if not isinstance(payload, dict):
        return [{"name": "Статус резервного копирования", "status": "warning", "message": "Ожидался JSON-объект"}], {}
    checks = payload.get("checks", [])
    metrics = payload.get("metrics", {})
    normalized = []
    for item in checks[:100] if isinstance(checks, list) else []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "unknown")).lower()
        if status not in {"ok", "warning", "critical", "unknown"}:
            status = "unknown"
        normalized.append(
            {
                "name": str(item.get("name") or "Backup status")[:180],
                "status": status,
                "message": str(item.get("message") or "")[:1500],
            }
        )
    return normalized, metrics if isinstance(metrics, dict) else {}


def collect_report(config: dict[str, Any]) -> dict[str, Any]:
    hostname = socket.gethostname()
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {"hostname": hostname}

    cpu = float(psutil.cpu_percent(interval=1))
    memory = float(psutil.virtual_memory().percent)
    metrics["cpu_percent"] = round(cpu, 1)
    metrics["memory_percent"] = round(memory, 1)
    checks.append(
        {
            "name": "CPU",
            "status": state_for_threshold(cpu, 80, 95),
            "message": f"Загрузка процессора: {cpu:.1f}%",
            "value": round(cpu, 1),
        }
    )
    checks.append(
        {
            "name": "Оперативная память",
            "status": state_for_threshold(memory, 85, 95),
            "message": f"Использование памяти: {memory:.1f}%",
            "value": round(memory, 1),
        }
    )

    disk_metrics: list[dict[str, Any]] = []
    warning_threshold = float(config.get("disk_warning_free_percent", 15))
    critical_threshold = float(config.get("disk_critical_free_percent", 8))
    for partition in psutil.disk_partitions(all=False):
        if "cdrom" in (partition.opts or "").lower():
            continue
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue
        free_percent = usage.free / usage.total * 100 if usage.total else 0
        drive = str(partition.mountpoint)
        disk_metrics.append(
            {
                "drive": drive,
                "mountpoint": drive,
                "total_bytes": int(usage.total),
                "used_bytes": int(usage.used),
                "free_bytes": int(usage.free),
                "free_percent": round(free_percent, 1),
            }
        )
        if free_percent <= critical_threshold:
            status = "critical"
        elif free_percent <= warning_threshold:
            status = "warning"
        else:
            status = "ok"
        checks.append(
            {
                "name": f"Диск · {drive}",
                "status": status,
                "message": f"Свободно {free_percent:.1f}% ({usage.free / (1024 ** 3):.1f} GiB)",
                "value": round(free_percent, 1),
            }
        )
    metrics["disks"] = disk_metrics
    metrics["disk_min_free_percent"] = min((disk["free_percent"] for disk in disk_metrics), default=None)
    metrics["uptime_seconds"] = int(max(0, time.time() - psutil.boot_time()))
    metrics["memory_total_bytes"] = int(psutil.virtual_memory().total)

    checks.extend(_collect_service_checks(config.get("monitored_services", [])))
    events, event_checks = collect_windows_events(config)
    checks.extend(event_checks)
    checks.extend(_collect_external_checks(config.get("external_checks", [])))
    backup_checks, backup_metrics = _load_backup_report(config)
    checks.extend(backup_checks)
    metrics.update({f"backup_{key}": value for key, value in backup_metrics.items()})

    return {
        "hostname": hostname,
        "agent_version": AGENT_VERSION,
        "collected_at": now_iso(),
        "metrics": metrics,
        "checks": checks,
        "events": events,
    }


def send_report(config: dict[str, Any], report: dict[str, Any]) -> None:
    server_url = str(config.get("server_url") or "").strip().rstrip("/")
    agent_id = str(config.get("agent_id") or "").strip()
    token = str(config.get("token") or "").strip()
    if not server_url.startswith("https://") and not (server_url.startswith("http://") and config.get("allow_http", False)):
        raise ValueError("Для передачи токена агенту требуется HTTPS. HTTP разрешён только при allow_http=true в изолированной тестовой сети.")
    if not agent_id or not token:
        raise ValueError("В конфигурации должны быть указаны agent_id и token")
    verify_tls = bool(config.get("verify_tls", True))
    timeout = max(3, min(60, int(config.get("timeout_seconds", 15))))
    endpoint = f"{server_url}/api/ingest/{agent_id}"
    with httpx.Client(verify=verify_tls, timeout=timeout) as client:
        response = client.post(endpoint, json=report, headers={"Authorization": f"Bearer {token}"})
    if response.status_code < 200 or response.status_code >= 300:
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"Сервер вернул HTTP {response.status_code}{': ' + detail if detail else ''}")


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Конфигурация агента должна быть JSON-объектом")
    return config


def run(config_path: Path, once: bool) -> int:
    config = load_config(config_path)
    interval = max(15, min(86400, int(config.get("interval_seconds", 60))))
    LOG.info("Агент запущен; сервер: %s; интервал: %s сек.", config.get("server_url"), interval)
    while True:
        try:
            report = collect_report(config)
            send_report(config, report)
            LOG.info("Heartbeat отправлен: %s · %s", report["hostname"], report.get("collected_at"))
        except Exception as exc:
            LOG.error("Не удалось отправить heartbeat: %s", str(exc)[:600])
        if once:
            return 0
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="SRV Monitoring remote Windows agent")
    parser.add_argument("--config", default="agent.json", help="Путь к JSON-конфигурации")
    parser.add_argument("--once", action="store_true", help="Собрать и отправить один отчёт")
    parser.add_argument("--debug", action="store_true")
    arguments = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if arguments.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return run(Path(arguments.config).expanduser().resolve(), arguments.once)
    except KeyboardInterrupt:
        LOG.info("Агент остановлен")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("Ошибка конфигурации: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
