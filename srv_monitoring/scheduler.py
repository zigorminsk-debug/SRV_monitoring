from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from .collectors.registry import collect
from .db import Database, utc_now
from .security import SecretBox


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PollingService:
    def __init__(self, database: Database, secret_box: SecretBox, max_concurrent: int = 5) -> None:
        self.database = database
        self.secret_box = secret_box
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.locks: dict[str, asyncio.Lock] = {}
        self.stop_event = asyncio.Event()
        self.last_prune = 0.0

    async def poll_source(self, source_id: str) -> dict[str, Any] | None:
        lock = self.locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            source = self.database.get_source(source_id)
            if source is None:
                return None
            started = utc_now()
            try:
                config = self.secret_box.decrypt_json(source["config_ciphertext"])
                async with self.semaphore:
                    collected = await collect(source["type"], config)
                saved = self.database.record_source_poll(source_id, started, collected)
                return saved
            except Exception as exc:
                # Collector adapters avoid embedding credentials in their messages.
                message = str(exc).strip() or type(exc).__name__
                saved = self.database.record_source_poll(source_id, started, {}, error=message)
                return saved

    async def run_forever(self) -> None:
        while not self.stop_event.is_set():
            interval = self.database.get_poll_interval()
            self.database.check_stale_agents(max(300, interval * 3))
            if time.monotonic() - self.last_prune >= 86400:
                try:
                    self.database.prune_history()
                except Exception:
                    pass
                self.last_prune = time.monotonic()
            now = datetime.now(timezone.utc)
            due: list[str] = []
            for source in self.database.list_sources():
                if not source["enabled"]:
                    continue
                last = _timestamp(source.get("last_poll_at"))
                if last is None or (now - last).total_seconds() >= interval:
                    due.append(source["id"])
            if due:
                await asyncio.gather(*(self.poll_source(source_id) for source_id in due), return_exceptions=True)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=min(5, max(1, interval / 3)))
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self.stop_event.set()
