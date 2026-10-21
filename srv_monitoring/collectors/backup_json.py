from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .common import get_path, parse_datetime, result

DEFAULT_SUCCESS = {"success", "succeeded", "completed", "complete", "ok", "finished"}
DEFAULT_FAILURE = {"failed", "failure", "error", "aborted", "cancelled", "canceled", "timeout"}
DEFAULT_RUNNING = {"running", "in progress", "in_progress", "started", "processing"}


def _values(value: Any, defaults: set[str]) -> set[str]:
    if value is None or value == "":
        return defaults
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = defaults
    return {str(item).strip().lower() for item in values if str(item).strip()}


def _status(raw: Any, success: set[str], failure: set[str]) -> str:
    value = str(raw or "").strip().lower()
    if value in success:
        return "ok"
    if value in failure:
        return "critical"
    if value in DEFAULT_RUNNING:
        return "ok"
    if value in {"pending", "queued", "waiting", "scheduled"}:
        return "warning"
    return "warning"


def _auth(config: dict[str, Any]) -> tuple[httpx.Auth | None, dict[str, str]]:
    auth_type = str(config.get("auth_type", "basic")).lower()
    headers: dict[str, str] = {}
    if auth_type == "bearer":
        token = str(config.get("bearer_token") or config.get("token") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return None, headers
    if auth_type == "none":
        return None, headers
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    if username or password:
        return httpx.BasicAuth(username, password), headers
    return None, headers


async def collect_backup_json(config: dict[str, Any]) -> dict[str, Any]:
    url = str(config.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Укажите полный URL API резервного копирования (http:// или https://)")

    auth, headers = _auth(config)
    timeout = max(3, min(60, float(config.get("timeout_seconds", 15))))
    verify_tls = bool(config.get("verify_tls", True))
    async with httpx.AsyncClient(
        auth=auth,
        headers=headers,
        verify=verify_tls,
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        try:
            response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise RuntimeError("Тайм-аут API резервного копирования") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ошибка подключения к API резервного копирования: {type(exc).__name__}") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"API резервного копирования вернул HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("API резервного копирования вернул не JSON") from exc

    jobs_path = str(config.get("jobs_path", "jobs"))
    jobs_data = get_path(payload, jobs_path, default=payload if isinstance(payload, list) else None)
    if isinstance(jobs_data, dict):
        # A few APIs wrap collections under items/results; allow an explicit path
        # as the stable option, and use these common wrappers as a safe fallback.
        jobs_data = jobs_data.get("items", jobs_data.get("results", jobs_data.get("jobs", jobs_data)))
    if isinstance(jobs_data, dict):
        jobs = [jobs_data]
    elif isinstance(jobs_data, list):
        jobs = jobs_data
    else:
        raise RuntimeError(
            f"В JSON не найден список заданий по пути «{jobs_path or '(корень)'}». "
            "Проверьте JSON-пути в настройках источника."
        )

    status_path = str(config.get("status_path", "status"))
    name_path = str(config.get("job_name_path", "name"))
    time_path = str(config.get("last_run_path", "last_run"))
    success_values = _values(config.get("success_values"), DEFAULT_SUCCESS)
    failure_values = _values(config.get("failure_values"), DEFAULT_FAILURE)
    expected_interval = max(0, int(config.get("expected_interval_minutes", 0) or 0))
    now = datetime.now(timezone.utc)
    checks: list[dict[str, Any]] = [
        {
            "name": "API резервного копирования",
            "status": "ok",
            "message": f"API доступен · заданий: {len(jobs)}",
        }
    ]
    normalized_jobs: list[dict[str, Any]] = []
    failed = succeeded = running = 0
    latest_success: datetime | None = None

    for index, job in enumerate(jobs[:500]):
        if not isinstance(job, dict):
            continue
        name = str(get_path(job, name_path, f"Задание {index + 1}"))[:120]
        raw_status = get_path(job, status_path)
        state = _status(raw_status, success_values, failure_values)
        timestamp_raw = get_path(job, time_path)
        timestamp = parse_datetime(timestamp_raw)
        message = f"Статус: {raw_status if raw_status is not None else 'не указан'}"
        if timestamp_raw is not None:
            message += f" · последний запуск: {timestamp_raw}"
        if state == "critical":
            failed += 1
            message += " · резервное копирование завершилось ошибкой"
        elif state == "ok" and str(raw_status or "").strip().lower() in success_values:
            succeeded += 1
            if timestamp and (latest_success is None or timestamp > latest_success):
                latest_success = timestamp
        elif state == "ok":
            running += 1
            message += " · выполняется"
        checks.append({"name": f"Бэкап · {name}", "status": state, "message": message})
        normalized_jobs.append(
            {
                "name": name,
                "status": str(raw_status or "unknown"),
                "last_run": timestamp_raw,
                "health": state,
            }
        )
        if expected_interval and str(raw_status or "").strip().lower() in success_values:
            if timestamp is None:
                checks.append(
                    {
                        "name": f"Свежесть бэкапа · {name}",
                        "status": "warning",
                        "message": "API не вернул распознаваемое время последнего успешного запуска",
                    }
                )
            else:
                age_minutes = (now - timestamp).total_seconds() / 60
                if age_minutes > expected_interval:
                    checks.append(
                        {
                            "name": f"Свежесть бэкапа · {name}",
                            "status": "warning",
                            "message": f"Последний запуск был {int(age_minutes)} мин. назад; ожидалось не более {expected_interval} мин.",
                        }
                    )

    if not normalized_jobs:
        checks.append(
            {
                "name": "Задания резервного копирования",
                "status": "warning",
                "message": "API доступен, но список заданий пуст или имеет неизвестный формат",
            }
        )

    total = len(normalized_jobs)
    summary = f"Заданий: {total} · успешно: {succeeded} · с ошибкой: {failed} · выполняется: {running}"
    return result(
        checks,
        summary,
        metrics={
            "backup_job_count": total,
            "backup_failed_count": failed,
            "backup_succeeded_count": succeeded,
            "backup_running_count": running,
            "backup_last_success_at": latest_success.isoformat() if latest_success else None,
        },
        devices=normalized_jobs,
        details={"provider": str(config.get("provider", "JSON backup API")), "jobs": normalized_jobs},
    )
