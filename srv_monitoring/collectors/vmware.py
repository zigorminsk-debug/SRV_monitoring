from __future__ import annotations

import ssl
from typing import Any

from .common import result


HEALTH_FIELDS = (
    ("cpuStatusInfo", "CPU sensors"),
    ("memoryStatusInfo", "Memory sensors"),
    ("storageStatusInfo", "Storage sensors"),
    ("powerStatusInfo", "Power sensors"),
    ("fanStatusInfo", "Fan sensors"),
    ("temperatureStatusInfo", "Temperature sensors"),
    ("voltageStatusInfo", "Voltage sensors"),
)


def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "key", getattr(value, "value", value))).strip().lower()


def _status_from_vmware(value: Any) -> str:
    raw = _enum_value(value).replace("_", "").replace("-", "")
    if raw in {"green", "ok", "normal", "connected", "poweredon", "healthy"}:
        return "ok"
    if raw in {"yellow", "warning", "degraded", "maintenance"}:
        return "warning"
    if raw in {"red", "error", "failed", "disconnected", "notresponding", "lost"}:
        return "critical"
    return "unknown"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def collect_vmware(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only inventory/health collection through the vSphere SOAP API."""
    try:
        from pyVim.connect import Disconnect, SmartConnect
        from pyVmomi import vim
    except ImportError as exc:
        raise RuntimeError("Модуль pyVmomi не установлен; выполните pip install -r requirements.txt") from exc

    host_name = str(config.get("host") or "").strip()
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    port = int(config.get("port", 443))
    if not host_name:
        raise ValueError("Не указан ESXi/vCenter host")
    if not username or not password:
        raise ValueError("Для VMware API требуется имя пользователя и пароль")

    verify_tls = bool(config.get("verify_tls", True))
    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    try:
        service_instance = SmartConnect(
            host=host_name,
            user=username,
            pwd=password,
            port=port,
            sslContext=context,
            connectionPoolTimeout=20,
        )
    except Exception as exc:
        # Do not propagate SDK exceptions that may echo credential or URL details.
        raise RuntimeError(f"Не удалось войти в VMware API ({type(exc).__name__})") from exc

    checks: list[dict[str, Any]] = []
    hosts_data: list[dict[str, Any]] = []
    datastores_data: list[dict[str, Any]] = []
    cpu_percentages: list[float] = []
    memory_percentages: list[float] = []
    datastore_free: list[float] = []
    hardware_critical = 0
    disk_critical = 0
    hardware_sensor_count = 0
    temperature_readings: list[float] = []
    datastore_warning = max(0.0, min(99.0, float(config.get("datastore_warning_percent", 15))))
    datastore_critical = max(0.0, min(datastore_warning, float(config.get("datastore_critical_percent", 8))))
    view = None
    datastore_view = None

    try:
        content = service_instance.RetrieveContent()
        view_manager = content.viewManager
        view = view_manager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
        hosts = list(view.view or [])
        checks.append(
            {
                "name": "Связь с VMware API",
                "status": "ok",
                "message": f"Подключение выполнено · ESXi-хостов: {len(hosts)}",
            }
        )
        if not hosts:
            checks.append({"name": "Инвентаризация ESXi", "status": "warning", "message": "В инвентаре нет ESXi-хостов"})

        for host in hosts:
            name = str(_attr(host, "name", "ESXi host"))
            runtime = _attr(host, "runtime")
            connection_state = _enum_value(_attr(runtime, "connectionState"))
            if connection_state:
                state = _status_from_vmware(connection_state)
                checks.append(
                    {
                        "name": f"ESXi · {name} · связь",
                        "status": state,
                        "message": f"Состояние подключения: {connection_state}",
                    }
                )
            else:
                state = "unknown"
                checks.append({"name": f"ESXi · {name} · связь", "status": state, "message": "Состояние подключения не опубликовано API"})

            summary = _attr(host, "summary")
            quick_stats = _attr(summary, "quickStats")
            hardware = _attr(host, "hardware")
            cpu_info = _attr(hardware, "cpuInfo")
            memory_size_bytes = _number(_attr(hardware, "memorySize"))
            cpu_usage_mhz = _number(_attr(quick_stats, "overallCpuUsage"))
            memory_usage_mb = _number(_attr(quick_stats, "overallMemoryUsage"))
            cores = _number(_attr(cpu_info, "numCpuCores")) or 0
            cpu_hz = _number(_attr(cpu_info, "hz")) or 0
            cpu_capacity_mhz = (cores * cpu_hz / 1_000_000) if cores and cpu_hz else 0
            cpu_pct = min(100.0, max(0.0, cpu_usage_mhz / cpu_capacity_mhz * 100)) if cpu_capacity_mhz and cpu_usage_mhz is not None else None
            mem_pct = min(100.0, max(0.0, memory_usage_mb / (memory_size_bytes / 1024 / 1024) * 100)) if memory_size_bytes and memory_usage_mb is not None else None
            if cpu_pct is not None:
                cpu_percentages.append(cpu_pct)
            if mem_pct is not None:
                memory_percentages.append(mem_pct)

            host_device: dict[str, Any] = {
                "name": name,
                "kind": "ESXi",
                "status": state,
                "connection_state": connection_state or "unknown",
                "cpu_percent": round(cpu_pct, 1) if cpu_pct is not None else None,
                "memory_percent": round(mem_pct, 1) if mem_pct is not None else None,
                "cpu_cores": int(cores) if cores else None,
            }
            hosts_data.append(host_device)
            if cpu_pct is not None and cpu_pct >= 90:
                checks.append({"name": f"ESXi · {name} · CPU", "status": "critical", "message": f"Загрузка CPU {cpu_pct:.1f}%"})
            elif cpu_pct is not None and cpu_pct >= 80:
                checks.append({"name": f"ESXi · {name} · CPU", "status": "warning", "message": f"Загрузка CPU {cpu_pct:.1f}%"})
            elif cpu_pct is not None:
                checks.append({"name": f"ESXi · {name} · CPU", "status": "ok", "message": f"Загрузка CPU {cpu_pct:.1f}%"})
            else:
                checks.append({"name": f"ESXi · {name} · CPU", "status": "unknown", "message": "Метрика CPU недоступна"})
            if mem_pct is not None and mem_pct >= 95:
                checks.append({"name": f"ESXi · {name} · память", "status": "critical", "message": f"Использование памяти {mem_pct:.1f}%"})
            elif mem_pct is not None and mem_pct >= 85:
                checks.append({"name": f"ESXi · {name} · память", "status": "warning", "message": f"Использование памяти {mem_pct:.1f}%"})
            elif mem_pct is not None:
                checks.append({"name": f"ESXi · {name} · память", "status": "ok", "message": f"Использование памяти {mem_pct:.1f}%"})
            else:
                checks.append({"name": f"ESXi · {name} · память", "status": "unknown", "message": "Метрика памяти недоступна"})

            storage_info = _attr(hardware, "storageInfo")
            scsi_luns = _attr(storage_info, "scsiLun", []) or []
            host_disks: list[dict[str, Any]] = []
            disk_issues: list[str] = []
            for lun in scsi_luns:
                if not isinstance(lun, vim.HostScsiDisk):
                    continue
                disk_name = str(_attr(lun, "displayName") or _attr(lun, "canonicalName") or _attr(lun, "uuid") or "SCSI disk")
                operational = _attr(lun, "operationalState", []) or []
                states = [_enum_value(value) for value in operational]
                # An empty state is unknown; an explicit state other than 'ok' is a fault.
                disk_state = "ok" if "ok" in states and len(states) == 1 else "critical" if states and any(s != "ok" for s in states) else "unknown"
                disk_device = {
                    "name": disk_name,
                    "kind": "Physical disk",
                    "vendor": _attr(lun, "vendor"),
                    "model": _attr(lun, "model"),
                    "operational_state": states,
                    "status": disk_state,
                    "local": bool(_attr(lun, "localDisk", False)),
                }
                host_disks.append(disk_device)
                if disk_state == "critical":
                    disk_issues.append(f"{disk_name}: {', '.join(states)}")
                    disk_critical += 1
            if disk_issues:
                checks.append({"name": f"ESXi · {name} · физические диски", "status": "critical", "message": "; ".join(disk_issues)[:1500]})
            elif host_disks:
                known = sum(1 for disk in host_disks if disk["status"] == "ok")
                checks.append({"name": f"ESXi · {name} · физические диски", "status": "ok" if known else "unknown", "message": f"Дисков: {len(host_disks)} · с состоянием OK: {known}"})
            else:
                checks.append({"name": f"ESXi · {name} · физические диски", "status": "unknown", "message": "API не вернул список физических SCSI-дисков"})
            host_device["disks"] = host_disks

            runtime_info = _attr(runtime, "healthSystemRuntime")
            hardware_status = _attr(runtime_info, "hardwareStatusInfo")
            reported_categories = 0
            for field_name, label in HEALTH_FIELDS:
                values = _attr(hardware_status, field_name, []) or []
                known_values = []
                for item in values:
                    status_value = _attr(item, "status")
                    state_value = _status_from_vmware(status_value)
                    if state_value == "unknown":
                        continue
                    sensor_name = str(_attr(item, "name") or _attr(item, "label") or label)
                    known_values.append((sensor_name, state_value))
                if not known_values:
                    continue
                reported_categories += 1
                bad = [(sensor_name, state_value) for sensor_name, state_value in known_values if state_value != "ok"]
                severity = "critical" if any(state_value == "critical" for _, state_value in bad) else "warning" if bad else "ok"
                if severity == "critical":
                    hardware_critical += len([1 for _, state_value in bad if state_value == "critical"])
                message = "; ".join(f"{sensor_name}: {state_value}" for sensor_name, state_value in bad)
                if not message:
                    message = f"Проверено датчиков: {len(known_values)} · проблем не обнаружено"
                checks.append({"name": f"ESXi · {name} · {label}", "status": severity, "message": message[:1500]})

            system_health = _attr(runtime_info, "systemHealthInfo")
            numeric_sensors = _attr(system_health, "numericSensorInfo", []) or []
            sensor_labels = {
                "fan": "Вентиляторы", "power": "Питание", "temperature": "Температура",
                "voltage": "Напряжение", "processor": "Процессор", "memory": "Память",
                "storage": "Хранилище", "systemboard": "Системная плата", "battery": "Батарея",
                "bios": "BIOS", "cable": "Кабели", "watchdog": "Watchdog", "other": "Прочие датчики",
            }
            sensor_groups: dict[str, list[dict[str, Any]]] = {}
            host_sensor_details: list[dict[str, Any]] = []
            for sensor in numeric_sensors:
                sensor_name = str(_attr(sensor, "name") or _attr(sensor, "id") or "Hardware sensor")
                sensor_type = _enum_value(_attr(sensor, "sensorType")) or "other"
                sensor_type = sensor_type.replace(" ", "").lower()
                sensor_state = _status_from_vmware(_attr(sensor, "healthState"))
                current_reading = _number(_attr(sensor, "currentReading"))
                modifier = _number(_attr(sensor, "unitModifier")) or 0
                reading = current_reading * (10 ** int(modifier)) if current_reading is not None else None
                units = str(_attr(sensor, "baseUnits") or "")
                sensor_detail = {
                    "name": sensor_name,
                    "kind": "Hardware sensor",
                    "sensor_type": sensor_type,
                    "status": sensor_state,
                    "reading": round(reading, 3) if reading is not None else None,
                    "units": units,
                }
                host_sensor_details.append(sensor_detail)
                if sensor_state == "unknown":
                    continue
                hardware_sensor_count += 1
                sensor_groups.setdefault(sensor_type, []).append(sensor_detail)
                if sensor_type == "temperature" and reading is not None:
                    temperature_readings.append(reading)
                if sensor_state != "ok":
                    checks.append(
                        {
                            "name": f"ESXi · {name} · датчик · {sensor_name}",
                            "status": sensor_state,
                            "message": f"{sensor_labels.get(sensor_type, sensor_type)}: {reading:g} {units} · состояние {sensor_state}" if reading is not None else f"Состояние датчика: {sensor_state}",
                        }
                    )
                    if sensor_state == "critical":
                        hardware_critical += 1
            for sensor_type, group in sensor_groups.items():
                bad = [sensor for sensor in group if sensor["status"] != "ok"]
                severity = "critical" if any(sensor["status"] == "critical" for sensor in bad) else "warning" if bad else "ok"
                group_message = "; ".join(
                    f"{sensor['name']}: {sensor['status']}" for sensor in bad
                ) or f"Проверено датчиков: {len(group)} · проблем не обнаружено"
                label = sensor_labels.get(sensor_type, sensor_type)
                checks.append({"name": f"ESXi · {name} · {label}", "status": severity, "message": group_message[:1500]})
                reported_categories += 1
            host_device["sensors"] = host_sensor_details
            if reported_categories == 0:
                checks.append({"name": f"ESXi · {name} · аппаратные датчики", "status": "unknown", "message": "Health System Runtime не предоставил состояние датчиков"})

        datastore_view = view_manager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
        datastores = list(datastore_view.view or [])
        for datastore in datastores:
            ds_name = str(_attr(datastore, "name", "Datastore"))
            summary = _attr(datastore, "summary")
            capacity = _number(_attr(summary, "capacity")) or 0
            free_space = _number(_attr(summary, "freeSpace"))
            accessible = bool(_attr(summary, "accessible", False))
            free_pct = (free_space / capacity * 100) if capacity and free_space is not None else None
            if free_pct is not None:
                datastore_free.append(free_pct)
            if not accessible:
                status = "critical"
                message = "Datastore недоступен"
            elif free_pct is None:
                status = "unknown"
                message = "API не вернул объём/свободное место"
            elif free_pct <= datastore_critical:
                status = "critical"
                message = f"Свободно {free_pct:.1f}%"
            elif free_pct <= datastore_warning:
                status = "warning"
                message = f"Свободно {free_pct:.1f}%"
            else:
                status = "ok"
                message = f"Свободно {free_pct:.1f}%"
            checks.append({"name": f"Datastore · {ds_name}", "status": status, "message": message})
            datastores_data.append(
                {
                    "name": ds_name,
                    "kind": "Datastore",
                    "status": status,
                    "accessible": accessible,
                    "capacity_bytes": int(capacity) if capacity else None,
                    "free_bytes": int(free_space) if free_space is not None else None,
                    "free_percent": round(free_pct, 1) if free_pct is not None else None,
                }
            )
    except Exception as exc:
        raise RuntimeError(f"Ошибка чтения VMware API ({type(exc).__name__})") from exc
    finally:
        try:
            if datastore_view:
                datastore_view.Destroy()
        except Exception:
            pass
        try:
            if view:
                view.Destroy()
        except Exception:
            pass
        try:
            Disconnect(service_instance)
        except Exception:
            pass

    if not verify_tls:
        checks.append(
            {
                "name": "TLS-сертификат VMware",
                "status": "warning",
                "message": "Проверка сертификата отключена; установите доверенный сертификат ESXi/vCenter",
            }
        )
    all_devices = hosts_data + datastores_data
    metrics = {
        "esxi_host_count": len(hosts_data),
        "datastore_count": len(datastores_data),
        "cpu_percent": round(sum(cpu_percentages) / len(cpu_percentages), 1) if cpu_percentages else None,
        "memory_percent": round(sum(memory_percentages) / len(memory_percentages), 1) if memory_percentages else None,
        "datastore_min_free_percent": round(min(datastore_free), 1) if datastore_free else None,
        "hardware_critical_count": hardware_critical + disk_critical,
        "hardware_sensor_count": hardware_sensor_count,
        "temperature_max_c": round(max(temperature_readings), 1) if temperature_readings else None,
        "physical_disk_critical_count": disk_critical,
    }
    summary = (
        f"ESXi-хостов: {len(hosts_data)} · хранилищ: {len(datastores_data)} · "
        f"проблемных дисков: {disk_critical}"
    )
    return result(checks, summary, metrics, all_devices, {"provider": "VMware vSphere API (pyVmomi)"})
