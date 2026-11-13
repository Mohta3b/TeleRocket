"""Minimal Rocket.Chat REST client (personal access token auth)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class RocketChatError(Exception):
    """A Rocket.Chat API call failed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RocketChatAuthError(RocketChatError):
    """The user id / token were rejected."""


@dataclass(frozen=True)
class Room:
    rid: str
    name: str  # handle: username for DMs, channel name otherwise
    display_name: str
    type: str  # 'd' direct, 'c' public channel, 'p' private group, 'l' livechat
    unread: int = 0
    updated_at: str = ""

    @property
    def target(self) -> str:
        """Human handle usable with /to and /send."""
        return f"@{self.name}" if self.type == "d" else f"#{self.name}"

    @property
    def label(self) -> str:
        prefix = "👤 " if self.type == "d" else ("🔒 " if self.type == "p" else "# ")
        unread = f" ({self.unread})" if self.unread else ""
        return f"{prefix}{self.display_name}{unread}"


class RocketChatRest:
    def __init__(
        self,
        base_url: str,
        user_id: str,
        token: str,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v1/",
            headers={"X-User-Id": user_id, "X-Auth-Token": token},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # never send the token to a redirect target
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RocketChatError(f"cannot reach Rocket.Chat ({type(exc).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 401:
            raise RocketChatAuthError(
                "Rocket.Chat rejected the user id / token (expired or revoked?)", 401
            )
        if resp.status_code >= 300 or data.get("success") is False:
            detail = data.get("error") or data.get("message") or resp.reason_phrase
            raise RocketChatError(f"{path} failed: {detail}", resp.status_code)
        return data

    async def me(self) -> dict[str, Any]:
        return await self._call("GET", "me")

    async def server_info(self) -> dict[str, Any]:
        return await self._call("GET", "info")

    async def send_to_room(self, rid: str, text: str, tmid: str | None = None) -> str:
        """Send a message to a room id (optionally inside a thread). Returns message id."""
        message: dict[str, Any] = {"rid": rid, "msg": text}
        if tmid:
            message["tmid"] = tmid
        data = await self._call("POST", "chat.sendMessage", json={"message": message})
        return data.get("message", {}).get("_id", "")

    async def list_rooms(self) -> list[Room]:
        """Rooms the user is subscribed to, most recently active first."""
        data = await self._call("GET", "subscriptions.get")
        rooms = [
            Room(
                rid=s["rid"],
                name=s.get("name", ""),
                display_name=s.get("fname") or s.get("name", ""),
                type=s.get("t", "c"),
                unread=int(s.get("unread", 0) or 0),
                updated_at=_updated_at(s),
            )
            for s in data.get("update", [])
            if s.get("rid") and s.get("open", True)
        ]
        rooms.sort(key=lambda r: r.updated_at, reverse=True)
        return rooms

    async def open_direct(self, username: str) -> str:
        """Open (or get) the DM room with a user. Returns room id."""
        data = await self._call("POST", "im.create", json={"username": username})
        room = data.get("room", {})
        rid = room.get("rid") or room.get("_id")
        if not rid:
            raise RocketChatError(f"could not open a direct chat with @{username}")
        return rid

    async def resolve_room(self, target: str) -> Room:
        """Resolve '@user', '#channel' or a plain room name to a Room."""
        target = target.strip()
        if not target:
            raise RocketChatError("empty room name")
        if target.startswith("@"):
            username = target[1:]
            rid = await self.open_direct(username)
            return Room(rid=rid, name=username, display_name=username, type="d")
        wanted = target.lstrip("#").lower()
        rooms = await self.list_rooms()
        for room in rooms:
            if room.name.lower() == wanted:
                return room
        for room in rooms:
            if room.display_name.lower() == wanted:
                return room
        raise RocketChatError(f"you are not in a chat called {target!r}")


def _updated_at(sub: dict[str, Any]) -> str:
    value = sub.get("lm") or sub.get("_updatedAt") or sub.get("ts") or ""
    if isinstance(value, dict):  # {"$date": 1700000000000}
        return f"{value.get('$date', 0):020d}"
    return str(value)
