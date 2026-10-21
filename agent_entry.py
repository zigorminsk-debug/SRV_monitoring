from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _agent_data_dir() -> Path:
    root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return root / "SRV Monitoring Agent"


def main() -> int:
    frozen = getattr(sys, "frozen", False)
    if frozen:
        app_data = _agent_data_dir()
        app_data.mkdir(parents=True, exist_ok=True)
        log_file = app_data / "agent.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")],
            force=True,
        )
        if "--config" not in sys.argv:
            sys.argv.extend(["--config", str(app_data / "agent.json")])

    from agent.monitor_agent import main as agent_main

    return agent_main()


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing

        multiprocessing.freeze_support()
    raise SystemExit(main())
