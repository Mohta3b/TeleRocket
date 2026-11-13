"""Rocket.Chat realtime client over the DDP websocket API.

Logs in with the personal access token (DDP ``login`` with ``resume``),
subscribes to ``stream-room-messages`` / ``__my_messages__`` which delivers new
messages from every room the user is in, and reconnects with exponential
backoff when the connection drops.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

log = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]
StateHandler = Callable[[bool, "str | None"], Awaitable[None]]

STREAM = "stream-room-messages"
MY_MESSAGES = "__my_messages__"


class RealtimeError(Exception):
    pass


class RealtimeAuthError(RealtimeError):
    pass


class RocketRealtime:
    def __init__(
        self,
        ws_url: str,
        user_id: str,
        token: str,
        on_message: MessageHandler,
        *,
        on_state: StateHandler | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
        timeout: float = 20.0,
    ):
        self._ws_url = ws_url
        self._user_id = user_id
        self._token = token
        self._on_message = on_message
        self._on_state = on_state
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._stopping = asyncio.Event()
        self._ws: ClientConnection | None = None
        self.connected = False
        self.last_error: str | None = None

    async def stop(self) -> None:
        self._stopping.set()
        if self._ws is not None:
            await self._ws.close()

    async def run(self) -> None:
        """Connect and keep the connection alive until ``stop()`` is called."""
        backoff = self._min_backoff
        while not self._stopping.is_set():
            try:
                async with connect(
                    self._ws_url,
                    open_timeout=self._timeout,
                    max_size=8 * 1024 * 1024,
                    proxy=None,  # always direct, ignore proxy env vars
                ) as ws:
                    self._ws = ws
                    await self._handshake(ws)
                    await self._set_state(True, None)
                    backoff = self._min_backoff
                    await self._listen(ws)
                if not self._stopping.is_set():
                    raise RealtimeError("connection closed by server")
            except asyncio.CancelledError:
                raise
            except RealtimeAuthError as exc:
                await self._set_state(False, str(exc))
                backoff = self._max_backoff  # retrying quickly won't help
            except (RealtimeError, WebSocketException, OSError, asyncio.TimeoutError) as exc:
                reason = str(exc) or type(exc).__name__
                await self._set_state(False, reason)
            finally:
                self._ws = None
            if self._stopping.is_set():
                break
            log.info("Rocket.Chat realtime reconnecting in %.0fs", backoff)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self._max_backoff)
        await self._set_state(False, None)

    async def _set_state(self, connected: bool, error: str | None) -> None:
        changed = connected != self.connected or (error and error != self.last_error)
        self.connected = connected
        if error:
            self.last_error = error
            log.warning("Rocket.Chat realtime: %s", error)
        elif connected:
            self.last_error = None
            log.info("Rocket.Chat realtime connected")
        if changed and self._on_state is not None:
            try:
                await self._on_state(connected, error)
            except Exception:
                log.exception("state handler failed")

    async def _send(self, ws: ClientConnection, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload))

    async def _recv(self, ws: ClientConnection) -> dict[str, Any]:
        raw = await asyncio.wait_for(ws.recv(), timeout=self._timeout)
        data = json.loads(raw)
        if data.get("msg") == "ping":
            await self._send(ws, {"msg": "pong", **({"id": data["id"]} if "id" in data else {})})
        return data

    async def _wait_for(self, ws: ClientConnection, predicate: Callable[[dict], bool]) -> dict:
        while True:
            data = await self._recv(ws)
            if predicate(data):
                return data

    async def _handshake(self, ws: ClientConnection) -> None:
        await self._send(ws, {"msg": "connect", "version": "1", "support": ["1"]})
        reply = await self._wait_for(ws, lambda d: d.get("msg") in ("connected", "failed"))
        if reply["msg"] == "failed":
            raise RealtimeError("server refused DDP connection")

        login_id = str(next(self._ids))
        await self._send(
            ws,
            {"msg": "method", "method": "login", "id": login_id, "params": [{"resume": self._token}]},
        )
        result = await self._wait_for(ws, lambda d: d.get("msg") == "result" and d.get("id") == login_id)
        if "error" in result:
            reason = result["error"].get("reason") or result["error"].get("message") or "login failed"
            raise RealtimeAuthError(f"Rocket.Chat login failed: {reason}")
        logged_in_as = (result.get("result") or {}).get("id")
        if logged_in_as != self._user_id:
            raise RealtimeAuthError("Rocket.Chat token belongs to a different user id")

        sub_id = str(next(self._ids))
        await self._send(
            ws, {"msg": "sub", "id": sub_id, "name": STREAM, "params": [MY_MESSAGES, False]}
        )
        reply = await self._wait_for(
            ws,
            lambda d: (d.get("msg") == "ready" and sub_id in d.get("subs", []))
            or (d.get("msg") == "nosub" and d.get("id") == sub_id),
        )
        if reply["msg"] == "nosub":
            raise RealtimeError("subscription to messages was refused")

    async def _listen(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            msg = data.get("msg")
            if msg == "ping":
                await self._send(ws, {"msg": "pong", **({"id": data["id"]} if "id" in data else {})})
            elif msg == "changed" and data.get("collection") == STREAM:
                args = (data.get("fields") or {}).get("args") or []
                if not args or not isinstance(args[0], dict):
                    continue
                meta = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
                try:
                    await self._on_message(args[0], meta)
                except Exception:
                    log.exception("failed to handle a Rocket.Chat message")
