"""Disk cache for synthesized speech.

Audio lives on the app container's disk, not in Redis, so a burst of voice requests can never
push sessions, locks or rate-limit keys out of Redis memory. Keys include the user id, so one
account cannot learn (via cache hits or timing) what another account had read aloud.
Size-capped with oldest-first eviction; safe across the web processes of one container.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path


class AudioDiskCache:
    def __init__(self, directory: str, *, max_bytes: int = 200_000_000, ttl: int = 86_400, max_entry: int = 2_000_000) -> None:
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.ttl = ttl
        self.max_entry = max_entry
        self._writes = 0

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.mp3"

    def _get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            if time.time() - path.stat().st_mtime > self.ttl:
                path.unlink(missing_ok=True)
                return None
            data = path.read_bytes()
            os.utime(path)  # LRU: mark as recently used
            return data
        except (FileNotFoundError, OSError):
            return None

    def _set(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)  # atomic: readers never see half a file

    def _cleanup(self) -> None:
        files = []
        total = 0
        now = time.time()
        for path in self.directory.glob("*/*.mp3"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if now - stat.st_mtime > self.ttl:
                path.unlink(missing_ok=True)
                continue
            files.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        for _, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size

    async def get(self, key: str) -> bytes | None:
        return await asyncio.to_thread(self._get, key)

    async def set(self, key: str, data: bytes) -> None:
        if len(data) > self.max_entry:
            return
        try:
            await asyncio.to_thread(self._set, key, data)
        except OSError:
            return
        self._writes += 1
        if self._writes % 50 == 0:
            await asyncio.to_thread(self._cleanup)
