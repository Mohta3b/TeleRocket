"""Core bridge logic, independent of the Telegram library so it can be tested."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Any, Protocol

from .config import Config
from .formatting import Identity, format_message, room_type, should_forward
from .rocket.rest import Room, RocketChatError, RocketChatRest
from .store import ReplyTarget, Store

log = logging.getLogger(__name__)

OUTAGE_NOTICE_DELAY = 90.0  # seconds offline before telling the owner


class TelegramOut(Protocol):
    async def send(self, html: str) -> int:
        """Send an HTML message to the owner; return the Telegram message id."""


class Bridge:
    def __init__(
        self,
        config: Config,
        rest: RocketChatRest,
        store: Store,
        out: TelegramOut,
        me: Identity,
        *,
        outage_notice_delay: float = OUTAGE_NOTICE_DELAY,
    ):
        self.config = config
        self.rest = rest
        self.store = store
        self.out = out
        self.me = me
        self._outage_delay = outage_notice_delay
        self._outage_task: asyncio.Task | None = None
        self._outage_notified = False
        self.rocket_connected = False
        self.rocket_error: str | None = None
        self.forwarded_count = 0

    # ------------------------------------------------------------------
    # Rocket.Chat -> Telegram
    # ------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self.store.get("paused") == "1"

    async def on_rocket_message(self, msg: dict[str, Any], meta: dict[str, Any]) -> None:
        if self.paused:
            return
        if not should_forward(
            msg, meta, self.me, scope=self.config.forward_scope, ignore_rooms=self.config.ignore_rooms
        ):
            return
        msg_id = msg.get("_id")
        if not msg_id or not self.store.mark_seen(msg_id):
            return  # already forwarded (re-broadcast after reaction / thread reply)

        rtype = room_type(msg, meta, self.me)
        if rtype == "d":
            label = "@" + ((msg.get("u") or {}).get("username") or meta.get("roomName") or "")
        else:
            label = "#" + (meta.get("roomName") or "")
        target = ReplyTarget(rid=msg["rid"], tmid=msg.get("tmid"), room_label=label)
        try:
            for chunk in format_message(msg, meta, self.me):
                tg_id = await self.out.send(chunk)
                self.store.remember(tg_id, target)
        except Exception:
            self.store.unmark_seen(msg_id)
            log.exception("could not forward a message to Telegram")
            return
        self.forwarded_count += 1

    async def on_rocket_state(self, connected: bool, error: str | None) -> None:
        self.rocket_connected = connected
        self.rocket_error = error
        if connected:
            if self._outage_task:
                self._outage_task.cancel()
                self._outage_task = None
            if self._outage_notified:
                self._outage_notified = False
                await self._notify("✅ Reconnected to Rocket.Chat.")
            return
        if error is None:
            return  # clean shutdown
        if "login failed" in error or "different user" in error:
            await self._notify(
                f"⚠️ Rocket.Chat rejected the token: {error}\n"
                "Create a new personal access token and update .env, then restart."
            )
            self._outage_notified = True
            return
        if self._outage_task is None and not self._outage_notified:
            self._outage_task = asyncio.create_task(self._notify_outage_later())

    async def _notify_outage_later(self) -> None:
        try:
            await asyncio.sleep(self._outage_delay)
        except asyncio.CancelledError:
            return
        self._outage_task = None
        if not self.rocket_connected:
            self._outage_notified = True
            await self._notify(
                f"⚠️ Lost connection to Rocket.Chat ({self.rocket_error}). Retrying in the background."
            )

    async def _notify(self, text: str) -> None:
        try:
            await self.out.send(escape(text))
        except Exception:
            log.exception("could not send a notice to Telegram")

    # ------------------------------------------------------------------
    # Telegram -> Rocket.Chat
    # ------------------------------------------------------------------
    async def reply(self, reply_to_tg_id: int, text: str) -> str:
        target = self.store.lookup(reply_to_tg_id)
        if target is None:
            return (
                "I don't know which Rocket.Chat chat that message belongs to. "
                "Reply to a forwarded message, or use /to or /send."
            )
        return await self._send(target.rid, text, target.tmid, target.room_label)

    async def send_text(self, text: str) -> str:
        rid = self.store.get("active_rid")
        if not rid:
            return (
                "No active chat yet. Reply to a forwarded message, pick a chat with /chats, "
                "or use /to @user or /to #channel."
            )
        return await self._send(rid, text, None, self.store.get("active_label", "") or "")

    async def send_to(self, target: str, text: str) -> str:
        try:
            room = await self.rest.resolve_room(target)
        except RocketChatError as exc:
            return f"⚠️ {exc}"
        return await self._send(room.rid, text, None, room.target)

    async def set_active(self, target: str) -> str:
        try:
            room = await self.rest.resolve_room(target)
        except RocketChatError as exc:
            return f"⚠️ {exc}"
        return self._activate(room.rid, room.target)

    async def set_active_rid(self, rid: str) -> str:
        try:
            rooms = await self.rest.list_rooms()
        except RocketChatError as exc:
            return f"⚠️ {exc}"
        for room in rooms:
            if room.rid == rid:
                return self._activate(room.rid, room.target)
        return "⚠️ That chat is no longer in your list."

    def _activate(self, rid: str, label: str) -> str:
        self.store.set("active_rid", rid)
        self.store.set("active_label", label)
        return f"Active chat is now {label}. Plain messages you send here go there."

    async def recent_rooms(self, limit: int = 12) -> list[Room]:
        return (await self.rest.list_rooms())[:limit]

    async def _send(self, rid: str, text: str, tmid: str | None, label: str) -> str:
        text = text.strip()
        if not text:
            return "Nothing to send."
        try:
            await self.rest.send_to_room(rid, text, tmid)
        except RocketChatError as exc:
            return f"⚠️ Not sent to {label}: {exc}"
        thread = " (thread)" if tmid else ""
        return f"✅ Sent to {label}{thread}"

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def pause(self) -> str:
        self.store.set("paused", "1")
        return "⏸ Forwarding paused. Use /resume to turn it back on."

    def resume(self) -> str:
        self.store.set("paused", None)
        return "▶️ Forwarding resumed."

    def status(self) -> str:
        conn = "connected" if self.rocket_connected else f"disconnected ({self.rocket_error or 'starting'})"
        active = self.store.get("active_label") or "none"
        return (
            f"Rocket.Chat: {conn}\n"
            f"Server: {self.config.rocket_url}\n"
            f"Logged in as: @{self.me.username}\n"
            f"Forwarding: {'paused' if self.paused else self.config.forward_scope}\n"
            f"Active chat: {active}\n"
            f"Forwarded since start: {self.forwarded_count}"
        )
