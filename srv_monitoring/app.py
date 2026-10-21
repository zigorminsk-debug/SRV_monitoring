from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Settings, load_settings
from .db import Database
from .models import AgentCreate, AgentReport, AlertAck, LoginRequest, SettingsPayload, SourcePayload
from .scheduler import PollingService
from .security import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    SecretBox,
    create_session,
    hash_agent_token,
    new_agent_token,
    verify_admin_password,
    verify_session,
)
from .collectors.registry import SECRET_FIELDS, validate_source_config


LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_ATTEMPTS = 8


def _public_source(source: dict[str, Any], box: SecretBox) -> dict[str, Any]:
    config = box.decrypt_json(source["config_ciphertext"])
    safe_config = {key: value for key, value in config.items() if key not in SECRET_FIELDS}
    result = {key: value for key, value in source.items() if key != "config_ciphertext"}
    result["config"] = safe_config
    result["has_password"] = bool(config.get("password"))
    result["has_bearer_token"] = bool(config.get("bearer_token") or config.get("token"))
    return result


def _device_status_counts(devices: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"total": len(devices), "critical": 0, "warning": 0, "unknown": 0, "ok": 0}
    for device in devices:
        status = str(device.get("status", "unknown")).lower()
        if status not in counts or status == "total":
            status = "unknown"
        counts[status] += 1
    return counts


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or load_settings()
    database = database or Database(settings.database_path)
    secret_box = SecretBox(settings.encryption_key)
    poller = PollingService(database, secret_box)
    login_attempts: dict[str, list[float]] = {}

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database.initialize()
        task = asyncio.create_task(poller.run_forever(), name="monitor-poller")
        application.state.poller_task = task
        try:
            yield
        finally:
            poller.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    application = FastAPI(
        title="SRV Monitoring",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.database = database
    application.state.secret_box = secret_box
    application.state.poller = poller

    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    @application.middleware("http")
    async def security_headers_and_body_limit(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_request_bytes:
                    return JSONResponse({"detail": "Request body is too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        # Only trust proxy headers after Uvicorn has accepted them from a configured
        # proxy address. Never infer HTTPS from a client-supplied header here.
        is_secure = request.url.scheme == "https"
        if settings.require_https and not is_secure and request.url.path != "/api/health":
            return JSONResponse(
                {"detail": "HTTPS is required. Configure a TLS reverse proxy or SSL_CERTFILE/SSL_KEYFILE."},
                status_code=426,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"},
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store") if request.url.path.startswith("/api/") else response.headers.get("Cache-Control", "no-cache")
        return response

    application.mount("/static", StaticFiles(directory=str(settings.root_dir / "static")), name="static")

    async def require_user(request: Request) -> str:
        username = verify_session(settings, request.cookies.get(COOKIE_NAME))
        if not username:
            raise HTTPException(status_code=401, detail="Требуется авторизация")
        return username

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(settings.root_dir / "static" / "index.html")

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get("/api/auth/me")
    async def auth_me(request: Request) -> dict[str, Any]:
        user = verify_session(settings, request.cookies.get(COOKIE_NAME))
        return {"authenticated": bool(user), "username": user}

    @application.post("/api/auth/login")
    async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = [attempt for attempt in login_attempts.get(ip, []) if now - attempt < LOGIN_WINDOW_SECONDS]
        login_attempts[ip] = attempts
        if len(attempts) >= LOGIN_MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="Слишком много попыток. Подождите 5 минут.")
        if not verify_admin_password(settings, body.username, body.password):
            attempts.append(now)
            login_attempts[ip] = attempts
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")
        login_attempts.pop(ip, None)
        token = create_session(settings, settings.admin_user)
        secure = settings.secure_cookies or request.url.scheme == "https"
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/",
        )
        return {"authenticated": True, "username": settings.admin_user}

    @application.post("/api/auth/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        return {"ok": True}

    @application.get("/api/settings")
    async def get_settings(_: str = Depends(require_user)) -> dict[str, Any]:
        return {"poll_interval_seconds": database.get_poll_interval(), "minimum_poll_interval_seconds": 15}

    @application.put("/api/settings")
    async def update_settings(body: SettingsPayload, _: str = Depends(require_user)) -> dict[str, Any]:
        database.set_setting("poll_interval_seconds", body.poll_interval_seconds)
        return {"poll_interval_seconds": database.get_poll_interval()}

    @application.get("/api/sources")
    async def get_sources(_: str = Depends(require_user)) -> list[dict[str, Any]]:
        return [_public_source(source, secret_box) for source in database.list_sources()]

    @application.post("/api/sources")
    async def create_source(body: SourcePayload, _: str = Depends(require_user)) -> dict[str, Any]:
        try:
            config = validate_source_config(body.type, body.config)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source = database.insert_source(body.name, body.type, secret_box.encrypt_json(config), body.enabled)
        return _public_source(source, secret_box)

    @application.put("/api/sources/{source_id}")
    async def update_source(source_id: str, body: SourcePayload, _: str = Depends(require_user)) -> dict[str, Any]:
        existing = database.get_source(source_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Источник не найден")
        try:
            previous = secret_box.decrypt_json(existing["config_ciphertext"]) if existing["type"] == body.type else None
            config = validate_source_config(body.type, body.config, previous)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source = database.update_source(
            source_id, body.name, body.type, secret_box.encrypt_json(config), body.enabled
        )
        return _public_source(source, secret_box)

    @application.delete("/api/sources/{source_id}")
    async def delete_source(source_id: str, _: str = Depends(require_user)) -> dict[str, bool]:
        if not database.delete_source(source_id):
            raise HTTPException(status_code=404, detail="Источник не найден")
        return {"ok": True}

    @application.post("/api/sources/{source_id}/poll")
    async def poll_now(source_id: str, _: str = Depends(require_user)) -> dict[str, Any]:
        if database.get_source(source_id) is None:
            raise HTTPException(status_code=404, detail="Источник не найден")
        result = await poller.poll_source(source_id)
        updated_source = database.get_source(source_id)
        if result is None or updated_source is None:
            raise HTTPException(status_code=404, detail="Источник не найден")
        return {"result": result, "source": _public_source(updated_source, secret_box)}

    @application.get("/api/sources/{source_id}/runs")
    async def source_runs(source_id: str, limit: int = 20, _: str = Depends(require_user)) -> list[dict[str, Any]]:
        if database.get_source(source_id) is None:
            raise HTTPException(status_code=404, detail="Источник не найден")
        return database.list_runs(source_id, limit)

    @application.get("/api/agents")
    async def get_agents(_: str = Depends(require_user)) -> list[dict[str, Any]]:
        return database.list_agents()

    @application.post("/api/agents")
    async def create_agent(body: AgentCreate, request: Request, _: str = Depends(require_user)) -> dict[str, Any]:
        token = new_agent_token()
        agent = database.insert_agent(body.name, hash_agent_token(token))
        server_url = settings.public_base_url or str(request.base_url).rstrip("/")
        return {**agent, "token": token, "server_url": server_url}

    @application.delete("/api/agents/{agent_id}")
    async def revoke_agent(agent_id: str, _: str = Depends(require_user)) -> dict[str, bool]:
        if not database.revoke_agent(agent_id):
            raise HTTPException(status_code=404, detail="Агент не найден")
        return {"ok": True}

    @application.post("/api/ingest/{agent_id}")
    async def ingest_agent(
        agent_id: str,
        report: AgentReport,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        expected_hash = database.get_agent_token_hash(agent_id)
        if not expected_hash:
            raise HTTPException(status_code=401, detail="Агент отключён или не найден")
        scheme, _, supplied_token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not supplied_token:
            raise HTTPException(status_code=401, detail="Требуется Bearer token")
        if not hmac.compare_digest(hash_agent_token(supplied_token), expected_hash):
            raise HTTPException(status_code=401, detail="Неверный токен агента")
        normalized = report.model_dump(mode="json")
        stored = database.record_agent_report(agent_id, normalized)
        if stored is None:
            raise HTTPException(status_code=401, detail="Агент отключён")
        return {"accepted": True, "received_at": stored["received_at"], "status": stored["status"]}

    @application.get("/api/alerts")
    async def get_alerts(limit: int = 100, active_only: bool = False, _: str = Depends(require_user)) -> list[dict[str, Any]]:
        return database.list_alerts(limit, active_only)

    @application.patch("/api/alerts/{alert_id}")
    async def update_alert(alert_id: str, body: AlertAck, _: str = Depends(require_user)) -> dict[str, bool]:
        if not database.acknowledge_alert(alert_id, body.acknowledged):
            raise HTTPException(status_code=404, detail="Событие не найдено")
        return {"ok": True}

    @application.get("/api/history")
    async def get_history(hours: int = 24, limit: int = 600, _: str = Depends(require_user)) -> list[dict[str, Any]]:
        return database.get_history(hours, limit)

    @application.get("/api/overview")
    async def overview(_: str = Depends(require_user)) -> dict[str, Any]:
        interval = database.get_poll_interval()
        sources = database.list_sources()
        agents = database.list_agents()
        devices: list[dict[str, Any]] = []
        for source in sources:
            latest = source.get("latest") or {}
            devices.append(
                {
                    "id": source["id"],
                    "entity_type": "source",
                    "name": source["name"],
                    "type": source["type"],
                    "enabled": source["enabled"],
                    "status": source["last_status"],
                    "last_seen": source["last_poll_at"],
                    "summary": source["last_summary"],
                    "metrics": latest.get("metrics", {}),
                    "checks": latest.get("checks", []),
                    "components": latest.get("devices", []),
                    "error": source["last_error"],
                }
            )
        for agent in agents:
            latest = agent.get("latest") or {}
            metrics = latest.get("metrics", {})
            cpu = metrics.get("cpu_percent")
            memory = metrics.get("memory_percent")
            parts = []
            if isinstance(cpu, (int, float)):
                parts.append(f"CPU {cpu:.0f}%")
            if isinstance(memory, (int, float)):
                parts.append(f"RAM {memory:.0f}%")
            if not parts:
                parts.append("Ожидает heartbeat" if not agent["last_seen"] else "Удалённый Windows-агент")
            devices.append(
                {
                    "id": agent["id"],
                    "entity_type": "agent",
                    "name": str(metrics.get("hostname") or latest.get("hostname") or agent["name"]),
                    "label": agent["name"],
                    "type": "agent",
                    "enabled": agent["enabled"],
                    "status": agent["last_status"],
                    "last_seen": agent["last_seen"],
                    "summary": " · ".join(parts),
                    "metrics": metrics,
                    "checks": latest.get("checks", []),
                    "components": metrics.get("disks", []),
                    "error": None,
                }
            )
        alerts = database.list_alerts(100, active_only=True)
        counts = _device_status_counts(devices)
        return {
            "version": __version__,
            "poll_interval_seconds": interval,
            "counts": counts,
            "devices": devices,
            "alerts": alerts,
            "sources": [_public_source(source, secret_box) for source in sources],
            "agents": agents,
            "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    return application


app = create_app()
