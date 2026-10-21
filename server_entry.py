from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _program_data() -> Path:
    root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return root / "SRV Monitoring"


def main() -> None:
    if getattr(sys, "frozen", False):
        app_data = _program_data()
        app_data.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MONITORING_ENV_FILE", str(app_data / ".env"))
        os.environ.setdefault("MONITORING_DATA_DIR", str(app_data / "data"))

    # Import the app only after the frozen runtime has selected ProgramData config.
    from srv_monitoring.app import app

    settings = app.state.settings
    log_dir = settings.data_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[RotatingFileHandler(log_dir / "server.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")],
        force=True,
    )

    import uvicorn

    forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.ssl_certfile,
        ssl_keyfile=settings.ssl_keyfile,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        log_config=None,
        access_log=True,
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing

        multiprocessing.freeze_support()
    main()
