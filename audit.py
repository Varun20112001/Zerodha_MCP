"""Asynchronous, batched SQLite audit logging for MCP tool calls."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_STOP = object()

_SENSITIVE_KEYS = {
    "api_key", "api_secret", "access_token", "refresh_token", "request_token",
    "password", "authorization", "checksum", "token",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _json(value: Any, limit: int = 16_000) -> str:
    encoded = json.dumps(_redact(value), ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) > limit:
        return encoded[:limit] + "…[truncated]"
    return encoded


class SQLiteBatchWriter:
    """Queue audit events in memory and persist them in SQLite batches."""

    def __init__(self, path: str, batch_size: int = 25, flush_interval: float = 2.0):
        self.path = Path(path).expanduser()
        self.batch_size = max(1, batch_size)
        self.flush_interval = max(0.1, flush_interval)
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize)
        self._task = asyncio.create_task(self._worker(), name="sqlite-audit-writer")

    def _initialize(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    called_at TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    duration_ms REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_called_at ON tool_calls(called_at)")

    async def record(self, tool_name: str, arguments: Any, result: Any,
                     error: str | None, duration_ms: float) -> None:
        # Enqueueing is in-memory and returns without waiting for disk I/O.
        await self.queue.put((
            datetime.now(timezone.utc).isoformat(), tool_name, _json(arguments),
            _json(result) if result is not None else None,
            error[:4000] if error else None, duration_ms,
        ))

    async def _worker(self) -> None:
        batch: list[tuple[Any, ...]] = []
        stopping = False
        while not stopping:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=self.flush_interval)
            except asyncio.TimeoutError:
                item = None
            if item is _STOP:
                stopping = True
            elif item is None:
                # Timeout: flush a partial batch after the configured interval.
                if self.queue.empty():
                    if batch:
                        await self._write(batch)
                        batch = []
                    continue
            else:
                batch.append(item)
            while len(batch) < self.batch_size and not self.queue.empty():
                next_item = self.queue.get_nowait()
                if next_item is _STOP:
                    stopping = True
                    break
                batch.append(next_item)
            if len(batch) >= self.batch_size or stopping:
                if batch:
                    await self._write(batch)
                    batch = []
        if batch:
            await self._write(batch)

    async def _write(self, batch: list[tuple[Any, ...]]) -> None:
        rows = list(batch)
        await asyncio.to_thread(self._insert_many, rows)

    def _insert_many(self, rows: list[tuple[Any, ...]]) -> None:
        with sqlite3.connect(self.path, timeout=10) as conn:
            conn.executemany(
                "INSERT INTO tool_calls (called_at, tool_name, arguments_json, result_json, error, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows,
            )

    async def close(self) -> None:
        if not self._task:
            return
        await self.queue.put(_STOP)
        await self._task
        self._task = None

