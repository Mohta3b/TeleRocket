"""Decide what to forward and format Rocket.Chat messages for Telegram (HTML)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from html import escape
from typing import Any

TELEGRAM_LIMIT = 4096


@dataclass(frozen=True)
class Identity:
    user_id: str
    username: str


def room_type(msg: dict[str, Any], meta: dict[str, Any], me: Identity) -> str:
    rtype = meta.get("roomType")
    if rtype:
        return rtype
    # Direct room ids are the two user ids concatenated.
    rid = msg.get("rid", "")
    return "d" if me.user_id and me.user_id in rid and len(rid) > len(me.user_id) else "c"


def mentions_me(msg: dict[str, Any], me: Identity) -> bool:
    for mention in msg.get("mentions") or []:
        if mention.get("_id") == me.user_id:
            return True
        if mention.get("username") in {me.username, "all", "here"}:
            return True
    return False


def should_forward(
    msg: dict[str, Any],
    meta: dict[str, Any],
    me: Identity,
    *,
    scope: str,
    ignore_rooms: Iterable[str] = (),
) -> bool:
    if (msg.get("u") or {}).get("_id") == me.user_id:
        return False  # my own message (sent from Rocket.Chat or from this bridge)
    if msg.get("t"):
        return False  # system message: joins, leaves, topic changes...
    if msg.get("editedAt"):
        return False
    room_name = (meta.get("roomName") or "").lower()
    if room_name and room_name in set(ignore_rooms):
        return False
    if scope == "all":
        return True
    return room_type(msg, meta, me) == "d" or mentions_me(msg, me)


def _sender(msg: dict[str, Any]) -> str:
    user = msg.get("u") or {}
    return user.get("name") or user.get("username") or "someone"


def header(msg: dict[str, Any], meta: dict[str, Any], me: Identity) -> str:
    rtype = room_type(msg, meta, me)
    sender = escape(_sender(msg))
    thread = " 🧵" if msg.get("tmid") else ""
    if rtype == "d":
        return f"👤 <b>{sender}</b>{thread}"
    room = escape(meta.get("roomName") or "room")
    icon = "🔒" if rtype == "p" else "💬"
    return f"{icon} <b>#{room}</b> · {sender}{thread}"


def body(msg: dict[str, Any]) -> str:
    parts = []
    text = (msg.get("msg") or "").strip()
    if text:
        parts.append(escape(text))
    seen = set()
    for att in msg.get("attachments") or []:
        name = att.get("title") or (att.get("image_url") and "image") or None
        if att.get("text") and not att.get("title"):
            # quoted message / link preview text
            parts.append(f"<i>{escape(att['text'][:500])}</i>")
            continue
        if name and name not in seen:
            seen.add(name)
            parts.append(f"📎 {escape(name)}")
    file_name = (msg.get("file") or {}).get("name")
    if file_name and file_name not in seen:
        parts.append(f"📎 {escape(file_name)}")
    return "\n".join(parts) if parts else "<i>(empty message)</i>"


def split_html(text: str, limit: int) -> list[str]:
    """Split escaped text into chunks <= limit without cutting an HTML entity
    or a tag."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        amp = text.rfind("&", 0, cut)
        if amp != -1 and ";" not in text[amp:cut]:
            cut = amp
        lt = text.rfind("<", 0, cut)
        if lt != -1 and ">" not in text[lt:cut]:
            cut = lt
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def format_message(
    msg: dict[str, Any], meta: dict[str, Any], me: Identity, limit: int = TELEGRAM_LIMIT
) -> list[str]:
    head = header(msg, meta, me)
    text = body(msg)
    room_for_body = limit - len(head) - 16
    pieces = split_html(text, room_for_body)
    out = []
    for i, piece in enumerate(pieces):
        suffix = f" ({i + 1}/{len(pieces)})" if len(pieces) > 1 else ""
        out.append(f"{head}{suffix}\n{piece}")
    return out
