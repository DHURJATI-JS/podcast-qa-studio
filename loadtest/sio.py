"""Minimal Socket.IO (Engine.IO v4) WebSocket client used by the smoke/stress tests.

Speaks the wire protocol directly over ``websockets`` so the tests need no extra packages:
open packet ``0{...}``, namespace connect ``40{auth}``, events ``42[name, data]``, ping ``2``/pong ``3``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import websockets


class SocketClient:
    def __init__(self, base_url: str, cookies: dict[str, str], csrf: str, *, host: str | None = None) -> None:
        self.url = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/socket.io/?EIO=4&transport=websocket"
        origin = base_url.rstrip("/")
        self.headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()), "Origin": origin}
        if host:
            self.headers["Host"] = host
        self.csrf = csrf
        self.ws = None
        self.events: list[tuple[str, object]] = []
        self.waiters: list[tuple[Callable[[str, object], bool], asyncio.Future]] = []
        self.task: asyncio.Task | None = None
        self.refused: str | None = None
        self.disconnected = False

    async def connect(self, timeout: float = 10.0) -> bool:
        self.ws = await websockets.connect(self.url, additional_headers=self.headers, open_timeout=timeout)
        opened = await asyncio.wait_for(self.ws.recv(), timeout)
        assert opened.startswith("0"), opened
        await self.ws.send("40" + json.dumps({"csrf": self.csrf}))
        while True:
            message = await asyncio.wait_for(self.ws.recv(), timeout)
            if message.startswith("40"):
                break
            if message.startswith("44"):
                self.refused = message[2:]
                await self.ws.close()
                return False
            if message == "2":
                await self.ws.send("3")
        self.task = asyncio.create_task(self._reader())
        return True

    async def _reader(self) -> None:
        try:
            async for message in self.ws:
                if message == "2":
                    await self.ws.send("3")
                elif message.startswith("41"):  # server closed our namespace connection
                    self.events.append(("__disconnect__", None))
                    self.disconnected = True
                elif message.startswith("42"):
                    name, data = json.loads(message[2:])
                    self.events.append((name, data))
                    for predicate, future in list(self.waiters):
                        if not future.done() and predicate(name, data):
                            future.set_result((name, data))
        except websockets.ConnectionClosed:
            pass

    async def wait_for(self, predicate: Callable[[str, object], bool], timeout: float) -> tuple[str, object]:
        for name, data in self.events:
            if predicate(name, data):
                return name, data
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.waiters.append((predicate, future))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.waiters.remove((predicate, future))

    async def emit(self, event: str, data: object) -> None:
        await self.ws.send("42" + json.dumps([event, data]))

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
        if self.ws:
            await self.ws.close()
