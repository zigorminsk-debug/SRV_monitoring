from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qsl, urlparse

from .backup_json import collect_backup_json
from .ipmi import collect_ipmi
from .redfish import collect_redfish
from .vmware import collect_vmware


SECRET_FIELDS = {"password", "bearer_token", "token"}


def _required_text(config: dict[str, Any], key: str, label: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise ValueError(f"Поле «{label}» обязательно")
    if len(value) > 2000:
        raise ValueError(f"Поле «{label}» слишком длинное")
    return value


def _validate_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Поле «{label}»: укажите полный URL с http:// или https://")
    if parsed.username or parsed.password:
        raise ValueError(f"Поле «{label}»: не помещайте логин/пароль в URL")
    sensitive_query_names = {"token", "access_token", "api_key", "apikey", "key", "secret", "password", "passwd", "authorization", "auth"}
    if any(key.lower() in sensitive_query_names for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError(f"Поле «{label}»: не передавайте секреты в query string; используйте Basic/Bearer авторизацию")
    return value.rstrip("/")


def validate_source_config(source_type: str, raw: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize source options and preserve stored secrets on edit when left blank."""
    previous = previous or {}
    config = dict(raw)
    for key in SECRET_FIELDS:
        if key not in config or config.get(key) in (None, ""):
            if key in previous:
                config[key] = previous[key]

    if source_type == "vmware":
        allowed = {"host", "port", "username", "password", "verify_tls", "datastore_warning_percent", "datastore_critical_percent", "timeout_seconds"}
        config = {key: value for key, value in config.items() if key in allowed}
        config["host"] = _required_text(config, "host", "ESXi/vCenter host")
        config["username"] = _required_text(config, "username", "Пользователь")
        config["password"] = _required_text(config, "password", "Пароль")
        config["port"] = int(config.get("port", 443))
        if not 1 <= config["port"] <= 65535:
            raise ValueError("Порт должен быть от 1 до 65535")
        config["verify_tls"] = bool(config.get("verify_tls", True))
        config["datastore_warning_percent"] = _bounded_float(config.get("datastore_warning_percent", 15), 1, 99, "Порог предупреждения datastore")
        config["datastore_critical_percent"] = _bounded_float(config.get("datastore_critical_percent", 8), 0, config["datastore_warning_percent"], "Критический порог datastore")
        config["timeout_seconds"] = _bounded_int(config.get("timeout_seconds", 20), 3, 120, "Тайм-аут")
    elif source_type == "redfish":
        allowed = {"url", "username", "password", "verify_tls", "timeout_seconds"}
        config = {key: value for key, value in config.items() if key in allowed}
        config["url"] = _validate_url(_required_text(config, "url", "URL Redfish"), "URL Redfish")
        config["username"] = str(config.get("username") or "")
        config["password"] = str(config.get("password") or "")
        if not config["username"] and not config["password"]:
            # Some BMC endpoints are deployed read-only without authentication.
            config["username"] = ""
            config["password"] = ""
        elif not config["username"] or not config["password"]:
            raise ValueError("Для Redfish заполните и имя пользователя, и пароль")
        config["verify_tls"] = bool(config.get("verify_tls", True))
        config["timeout_seconds"] = _bounded_int(config.get("timeout_seconds", 15), 3, 60, "Тайм-аут")
    elif source_type == "ipmi":
        allowed = {"host", "username", "password", "ipmitool_path", "timeout_seconds"}
        config = {key: value for key, value in config.items() if key in allowed}
        config["host"] = _required_text(config, "host", "Адрес BMC/IPMI")
        config["username"] = _required_text(config, "username", "Пользователь")
        config["password"] = _required_text(config, "password", "Пароль")
        config["ipmitool_path"] = str(config.get("ipmitool_path") or "ipmitool").strip()
        config["timeout_seconds"] = _bounded_int(config.get("timeout_seconds", 30), 3, 120, "Тайм-аут")
    elif source_type == "backup_json":
        allowed = {
            "url", "auth_type", "username", "password", "bearer_token", "verify_tls", "timeout_seconds",
            "jobs_path", "status_path", "job_name_path", "last_run_path", "success_values", "failure_values",
            "expected_interval_minutes", "provider",
        }
        config = {key: value for key, value in config.items() if key in allowed}
        config["url"] = _validate_url(_required_text(config, "url", "URL API резервного копирования"), "URL API")
        config["auth_type"] = str(config.get("auth_type", "basic")).lower()
        if config["auth_type"] not in {"basic", "bearer", "none"}:
            raise ValueError("Тип авторизации должен быть basic, bearer или none")
        config["username"] = str(config.get("username") or "")
        config["password"] = str(config.get("password") or "")
        config["bearer_token"] = str(config.get("bearer_token") or "")
        if config["auth_type"] == "basic":
            config["bearer_token"] = ""
            if not config["username"] or not config["password"]:
                raise ValueError("Для Basic auth укажите логин и пароль")
        elif config["auth_type"] == "bearer":
            config["username"] = ""
            config["password"] = ""
            if not config["bearer_token"]:
                raise ValueError("Для Bearer auth укажите токен")
        else:
            config["username"] = ""
            config["password"] = ""
            config["bearer_token"] = ""
        config["verify_tls"] = bool(config.get("verify_tls", True))
        config["timeout_seconds"] = _bounded_int(config.get("timeout_seconds", 15), 3, 60, "Тайм-аут")
        config["jobs_path"] = _path_value(config.get("jobs_path", "jobs"), "JSON-путь к заданиям")
        config["status_path"] = _path_value(config.get("status_path", "status"), "JSON-путь к статусу")
        config["job_name_path"] = _path_value(config.get("job_name_path", "name"), "JSON-путь к имени задания")
        config["last_run_path"] = _path_value(config.get("last_run_path", "last_run"), "JSON-путь к времени запуска")
        config["success_values"] = _list_values(config.get("success_values", "success,succeeded,completed,ok,finished"))
        config["failure_values"] = _list_values(config.get("failure_values", "failed,error,aborted,cancelled,canceled,timeout"))
        config["expected_interval_minutes"] = _bounded_int(config.get("expected_interval_minutes", 0), 0, 525600, "Ожидаемый интервал бэкапа")
        config["provider"] = str(config.get("provider") or "Vinchin / Backup API")[:80]
    else:
        raise ValueError(f"Неизвестный тип источника: {source_type}")

    return config


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Поле «{label}» должно быть числом") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"Поле «{label}» должно быть от {minimum} до {maximum}")
    return result


def _bounded_float(value: Any, minimum: float, maximum: float, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Поле «{label}» должно быть числом") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"Поле «{label}» должно быть от {minimum} до {maximum}")
    return result


def _path_value(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > 200 or ".." in text:
        raise ValueError(f"Поле «{label}» имеет недопустимый формат")
    return text


def _list_values(value: Any) -> list[str]:
    raw = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError("Список статусов должен быть строкой через запятую или массивом")
    values = [str(item).strip() for item in raw if str(item).strip()]
    if len(values) > 40:
        raise ValueError("Слишком много значений статусов")
    return values


async def collect(source_type: str, config: dict[str, Any]) -> dict[str, Any]:
    if source_type == "vmware":
        return await asyncio.to_thread(collect_vmware, config)
    if source_type == "redfish":
        return await collect_redfish(config)
    if source_type == "ipmi":
        return await asyncio.to_thread(collect_ipmi, config)
    if source_type == "backup_json":
        return await collect_backup_json(config)
    raise ValueError(f"Нет коллектора для типа «{source_type}»")
