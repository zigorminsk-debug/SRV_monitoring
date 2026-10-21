from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .common import classify_health, result


async def collect_redfish(config: dict[str, Any]) -> dict[str, Any]:
    base_url = str(config.get("url") or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Для Redfish укажите базовый URL, например https://10.0.0.20")

    auth = None
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    if username or password:
        auth = httpx.BasicAuth(username, password)
    timeout = max(3, min(60, float(config.get("timeout_seconds", 15))))
    verify_tls = bool(config.get("verify_tls", True))
    origin = (parsed.scheme.lower(), parsed.netloc.lower())
    checks: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    component_counts = {"critical": 0, "warning": 0, "ok": 0, "unknown": 0}
    metric_values: dict[str, list[float]] = {"temperatures_c": [], "power_watts": []}

    async with httpx.AsyncClient(
        auth=auth,
        verify=verify_tls,
        timeout=timeout,
        follow_redirects=False,
        headers={"Accept": "application/json"},
    ) as client:
        async def get_json(reference: str) -> dict[str, Any]:
            target = urljoin(base_url + "/", reference)
            target_parsed = urlparse(target)
            if (target_parsed.scheme.lower(), target_parsed.netloc.lower()) != origin:
                raise RuntimeError("Redfish API вернул ссылку на другой узел; переход заблокирован")
            try:
                response = await client.get(target)
            except httpx.TimeoutException as exc:
                raise RuntimeError("Тайм-аут соединения с Redfish BMC") from exc
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Ошибка подключения Redfish: {type(exc).__name__}") from exc
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(f"Redfish вернул HTTP {response.status_code} для ресурса {target_parsed.path}")
            try:
                value = response.json()
            except ValueError as exc:
                raise RuntimeError(f"Redfish вернул не JSON для ресурса {target_parsed.path}") from exc
            if not isinstance(value, dict):
                raise RuntimeError("Redfish вернул JSON неожиданного формата")
            return value

        async def items_from(reference: str) -> list[dict[str, Any]]:
            collection = await get_json(reference)
            members = collection.get("Members")
            if not isinstance(members, list):
                return [collection]
            output: list[dict[str, Any]] = []
            for member in members[:100]:
                if isinstance(member, dict) and member.get("@odata.id"):
                    try:
                        output.append(await get_json(str(member["@odata.id"])))
                    except RuntimeError as exc:
                        checks.append(
                            {"name": "Redfish: компонент", "status": "warning", "message": str(exc)}
                        )
                elif isinstance(member, dict):
                    output.append(member)
            return output

        def add_health(name: str, obj: dict[str, Any]) -> None:
            status = obj.get("Status") if isinstance(obj.get("Status"), dict) else {}
            health = status.get("Health") or obj.get("Health")
            state = classify_health(health)
            if health is None:
                state = "unknown"
                message = "BMC не сообщает Health для этого компонента"
            else:
                message = f"Health: {health}"
            state_value = status.get("State") or obj.get("State")
            if state_value:
                message += f" · State: {state_value}"
            component_counts[state] = component_counts.get(state, 0) + 1
            checks.append({"name": name[:180], "status": state, "message": message[:1500]})
            devices.append(
                {
                    "name": str(obj.get("Name") or obj.get("Id") or name),
                    "status": state,
                    "health": health,
                    "state": state_value,
                    "model": obj.get("Model"),
                }
            )

        root = await get_json("/redfish/v1/")
        checks.append({"name": "Связь с BMC Redfish", "status": "ok", "message": "Redfish API доступен"})
        system_ref = (root.get("Systems") or {}).get("@odata.id") if isinstance(root.get("Systems"), dict) else None
        chassis_ref = (root.get("Chassis") or {}).get("@odata.id") if isinstance(root.get("Chassis"), dict) else None
        if not system_ref:
            system_ref = "/redfish/v1/Systems"
        if not chassis_ref:
            chassis_ref = "/redfish/v1/Chassis"

        systems = await items_from(str(system_ref))
        if not systems:
            checks.append({"name": "Системы Redfish", "status": "warning", "message": "BMC не вернул системные ресурсы"})
        for system in systems:
            system_name = str(system.get("Name") or system.get("HostName") or system.get("Id") or "Система")
            add_health(f"Система · {system_name}", system)

            links = system.get("Links") if isinstance(system.get("Links"), dict) else {}
            storage_ref = None
            for candidate in (system.get("Storage"), links.get("Storage")):
                if isinstance(candidate, dict) and candidate.get("@odata.id"):
                    storage_ref = candidate["@odata.id"]
                    break
            if storage_ref:
                try:
                    storage_items = await items_from(str(storage_ref))
                    for storage in storage_items:
                        add_health(f"Хранилище · {storage.get('Name') or storage.get('Id') or system_name}", storage)
                        drives = storage.get("Drives") or []
                        for index, drive in enumerate(drives[:200]):
                            if isinstance(drive, dict) and drive.get("@odata.id"):
                                try:
                                    drive = await get_json(str(drive["@odata.id"]))
                                except RuntimeError as exc:
                                    checks.append({"name": f"Диск · {index + 1}", "status": "warning", "message": str(exc)})
                                    continue
                            if isinstance(drive, dict):
                                drive_name = drive.get("Name") or drive.get("Model") or drive.get("Id") or f"{index + 1}"
                                add_health(f"Диск · {drive_name}", drive)
                except RuntimeError as exc:
                    checks.append({"name": "Redfish storage", "status": "warning", "message": str(exc)})

        if systems and not any(check["name"].startswith("Система ·") for check in checks):
            checks.append({"name": "Аппаратное состояние", "status": "unknown", "message": "BMC не вернул данные о здоровье системы"})

        try:
            chassis_items = await items_from(str(chassis_ref))
        except RuntimeError as exc:
            chassis_items = []
            checks.append({"name": "Redfish chassis", "status": "warning", "message": str(exc)})

        for chassis in chassis_items:
            chassis_name = str(chassis.get("Name") or chassis.get("Id") or "Chassis")
            add_health(f"Шасси · {chassis_name}", chassis)
            links = chassis.get("Links") if isinstance(chassis.get("Links"), dict) else {}
            thermal_link = chassis.get("Thermal") or links.get("Thermal")
            power_link = chassis.get("Power") or links.get("Power")
            thermal_ref = thermal_link.get("@odata.id") if isinstance(thermal_link, dict) else None
            power_ref = power_link.get("@odata.id") if isinstance(power_link, dict) else None
            if thermal_ref:
                try:
                    thermal = await get_json(str(thermal_ref))
                    for key in ("Temperatures", "Fans", "FanSpeeds", "Voltages"):
                        for index, item in enumerate(thermal.get(key) or []):
                            if not isinstance(item, dict):
                                continue
                            name = item.get("Name") or item.get("MemberId") or f"{key} {index + 1}"
                            add_health(f"{key} · {name}", item)
                            reading = item.get("ReadingCelsius")
                            if isinstance(reading, (int, float)):
                                metric_values["temperatures_c"].append(float(reading))
                except RuntimeError as exc:
                    checks.append({"name": "Redfish thermal", "status": "warning", "message": str(exc)})
            if power_ref:
                try:
                    power = await get_json(str(power_ref))
                    for key in ("PowerSupplies", "PowerControl", "Voltages"):
                        for index, item in enumerate(power.get(key) or []):
                            if not isinstance(item, dict):
                                continue
                            name = item.get("Name") or item.get("MemberId") or f"{key} {index + 1}"
                            add_health(f"{key} · {name}", item)
                            watts = item.get("PowerConsumedWatts")
                            if isinstance(watts, (int, float)):
                                metric_values["power_watts"].append(float(watts))
                except RuntimeError as exc:
                    checks.append({"name": "Redfish power", "status": "warning", "message": str(exc)})

    if not verify_tls:
        checks.append(
            {
                "name": "TLS-сертификат BMC",
                "status": "warning",
                "message": "Проверка TLS-сертификата отключена; используйте только в доверенной сети",
            }
        )
    metrics = {
        "hardware_critical_count": component_counts["critical"],
        "hardware_warning_count": component_counts["warning"],
        "temperature_max_c": max(metric_values["temperatures_c"], default=None),
        "power_consumed_watts": sum(metric_values["power_watts"]) if metric_values["power_watts"] else None,
        "hardware_component_count": len(devices),
    }
    summary = (
        f"Компонентов: {len(devices)} · критических: {component_counts['critical']} · "
        f"предупреждений: {component_counts['warning']}"
    )
    return result(checks, summary, metrics, devices, {"provider": "DMTF Redfish"})
