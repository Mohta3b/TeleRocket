"""Core bridge logic, independent of the Telegram library so it can be tested."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Protocol

from .config import Config
from .formatting import Identity, format_message, room_type, should_forward
from .rocket.rest import Room, RocketChatError, RocketChatRest
from .store import ReplyTarget, Store

log = logging.getLogger(__name__)

OUTAGE_NOTICE_DELAY = 90.0  # seconds offline before telling the owner
PENDING_TTL = 30 * 60.0  # unconfirmed messages are dropped after 30 minutes


@dataclass(frozen=True)
class SendTarget:
    rid: str
    tmid: str | None
    label: str
    make_active: bool = False


@dataclass(frozen=True)
class PendingSend:
    id: str
    target: SendTarget
    text: str
    quoted: str | None
    created: float


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
        self._pending: dict[str, PendingSend] = {}

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
    # Each way of sending is split in two: resolve the destination (cheap,
    # can fail with a message for the user) and deliver. With confirmation
    # turned on, the Telegram layer shows a preview between the two steps.

    def resolve_reply(self, reply_to_tg_id: int) -> SendTarget | str:
        target = self.store.lookup(reply_to_tg_id)
        if target is None:
            return (
                "I don't know which Rocket.Chat chat that message belongs to. "
                "Reply to a forwarded message, or use /to or /send."
            )
        # Replying makes that chat active, so you can keep talking without replying each time.
        return SendTarget(target.rid, target.tmid, target.room_label, make_active=True)

    def resolve_active(self) -> SendTarget | str:
        rid = self.store.get("active_rid")
        if not rid:
            return (
                "No active chat yet. Reply to a forwarded message, pick a chat with /chats, "
                "or use /to @user or /to #channel."
            )
        return SendTarget(rid, None, self.store.get("active_label", "") or "")

    async def resolve_named(self, name: str) -> SendTarget | str:
        try:
            room = await self.rest.resolve_room(name)
        except RocketChatError as exc:
            return f"⚠️ {exc}"
        return SendTarget(room.rid, None, room.target)

    async def deliver(self, target: SendTarget, text: str) -> str:
        result = await self._send(target.rid, text, target.tmid, target.label)
        if target.make_active and result.startswith("✅"):
            self.store.set("active_rid", target.rid)
            self.store.set("active_label", target.label)
        return result

    async def reply(self, reply_to_tg_id: int, text: str) -> str:
        target = self.resolve_reply(reply_to_tg_id)
        return target if isinstance(target, str) else await self.deliver(target, text)

    async def send_text(self, text: str) -> str:
        target = self.resolve_active()
        return target if isinstance(target, str) else await self.deliver(target, text)

    async def send_to(self, name: str, text: str) -> str:
        target = await self.resolve_named(name)
        return target if isinstance(target, str) else await self.deliver(target, text)

    # --- confirmation -------------------------------------------------
    def hold(self, target: SendTarget, text: str, quoted: str | None = None) -> PendingSend:
        """Keep a message until the owner confirms or cancels it (memory only)."""
        self._expire_pending()
        pending = PendingSend(
            id=secrets.token_hex(6), target=target, text=text, quoted=quoted, created=time.monotonic()
        )
        self._pending[pending.id] = pending
        return pending

    async def confirm(self, pending_id: str) -> tuple[str, PendingSend | None]:
        self._expire_pending()
        pending = self._pending.pop(pending_id, None)  # pop first: a double tap sends once
        if pending is None:
            return "This message expired or was already handled. Nothing was sent.", None
        return await self.deliver(pending.target, pending.text), pending

    def cancel(self, pending_id: str) -> PendingSend | None:
        return self._pending.pop(pending_id, None)

    def _expire_pending(self) -> None:
        cutoff = time.monotonic() - PENDING_TTL
        for pid in [p.id for p in self._pending.values() if p.created < cutoff]:
            del self._pending[pid]

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
            f"Confirm before sending: {'on' if self.config.confirm_before_send else 'off'}\n"
            f"Forwarded since start: {self.forwarded_count}"
        )
