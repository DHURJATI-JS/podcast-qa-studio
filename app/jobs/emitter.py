"""Socket.IO event emitters.

The web process emits through its own ``AsyncServer``; the worker process emits through a
write-only Redis manager. Events are always sent to the owner's private room ``user:<id>``.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import socketio


def user_room(user_id: uuid.UUID | str) -> str:
    return f"user:{user_id}"


class Emitter(Protocol):
    async def emit(self, event: str, data: Any, *, room: str | None = None) -> None: ...


class RedisEmitter:
    def __init__(self, redis_url: str, channel: str = "pq-socketio") -> None:
        self.manager = socketio.AsyncRedisManager(redis_url, channel=channel, write_only=True)

    async def emit(self, event: str, data: Any, *, room: str | None = None) -> None:
        await self.manager.emit(event, data, namespace="/", room=room)


class ServerEmitter:
    def __init__(self, sio: socketio.AsyncServer) -> None:
        self.sio = sio

    async def emit(self, event: str, data: Any, *, room: str | None = None) -> None:
        await self.sio.emit(event, data, room=room, namespace="/")


class RecordingEmitter:
    """Test helper: remembers every event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any, str | None]] = []

    async def emit(self, event: str, data: Any, *, room: str | None = None) -> None:
        self.events.append((event, data, room))
