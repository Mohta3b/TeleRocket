"""Telegram side: python-telegram-bot handlers, locked to the owner."""

from __future__ import annotations

import logging

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telegram.request import HTTPXRequest

from html import escape

from .bridge import Bridge, PendingSend

log = logging.getLogger(__name__)

HELP = """TeleRocket bridges your Rocket.Chat account to this chat.

• Incoming Rocket.Chat messages show up here.
• Reply (swipe / Reply) to one of them to answer in the same chat or thread. That chat also becomes your active chat.
• Plain messages (not replies) go to the active chat.

Commands
/chats – pick the active chat from your recent chats
/to @user | #channel – set the active chat
/send @user | #channel text – send one message
/status – connection and settings
/pause, /resume – stop / restart forwarding
/settings – change settings with buttons
/ignore #room | @user – stop forwarding a chat (no argument: pick from a list)
/unignore #room | @user – forward it again (no argument: pick from a list)
/scope all | dm_mentions – what gets forwarded
/confirm on | off – ask before sending
/help – this text

Settings changed here are saved and override .env until you reset them in /settings."""

COMMANDS = [
    BotCommand("chats", "Pick the active Rocket.Chat chat"),
    BotCommand("to", "Set the active chat: /to @user or /to #channel"),
    BotCommand("send", "Send once: /send @user text"),
    BotCommand("status", "Connection and settings"),
    BotCommand("pause", "Pause forwarding"),
    BotCommand("resume", "Resume forwarding"),
    BotCommand("settings", "Change settings"),
    BotCommand("ignore", "Stop forwarding a chat"),
    BotCommand("unignore", "Forward an ignored chat again"),
    BotCommand("scope", "What gets forwarded: all or dm_mentions"),
    BotCommand("confirm", "Ask before sending: on or off"),
    BotCommand("help", "How to use the bridge"),
]


def make_request(proxy_url: str | None, *, long_polling: bool = False) -> HTTPXRequest:
    """HTTP client for the Telegram API, optionally through a proxy.

    Timeouts are a bit longer than the defaults because proxies add latency.
    """
    return HTTPXRequest(
        proxy=proxy_url,
        connect_timeout=20.0,
        read_timeout=30.0 if long_polling else 20.0,
        write_timeout=20.0,
        pool_timeout=5.0,
        connection_pool_size=8 if not long_polling else 1,
    )


def build_application(token: str, proxy_url: str | None) -> Application:
    return (
        Application.builder()
        .token(token)
        .request(make_request(proxy_url))
        .get_updates_request(make_request(proxy_url, long_polling=True))
        .build()
    )


def build_bot(token: str, proxy_url: str | None):
    from telegram import Bot

    return Bot(
        token,
        request=make_request(proxy_url),
        get_updates_request=make_request(proxy_url, long_polling=True),
    )


class TelegramSender:
    """Implements bridge.TelegramOut on top of a PTB bot."""

    def __init__(self, app: Application, owner_id: int):
        self._app = app
        self._owner_id = owner_id

    async def send(self, html: str) -> int:
        message = await self._app.bot.send_message(
            chat_id=self._owner_id,
            text=html,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return message.message_id


PREVIEW_TEXT_LIMIT = 3000
QUOTE_LIMIT = 200
RULE = "──────────"


def _quote(text: str | None) -> str | None:
    if not text:
        return None
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= QUOTE_LIMIT else one_line[: QUOTE_LIMIT - 1] + "…"


def describe_pending(pending: PendingSend) -> str:
    """Recipient, quoted message and text, as Telegram HTML."""
    target = pending.target
    lines = [f"<b>To:</b> {escape(target.label)}" + (" · in thread 🧵" if target.tmid else "")]
    if pending.quoted:
        lines.append(f"<b>Replying to:</b> <i>{escape(pending.quoted)}</i>")
    text = pending.text.strip()
    if len(text) > PREVIEW_TEXT_LIMIT:
        hidden = len(text) - PREVIEW_TEXT_LIMIT
        text = f"{text[:PREVIEW_TEXT_LIMIT]}… (+{hidden} more characters, sent in full)"
    lines += [RULE, escape(text)]
    return "\n".join(lines)


def preview_markup(pending: PendingSend) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Send", callback_data=f"send:{pending.id}"),
            InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{pending.id}"),
        ]]
    )


def settings_markup(bridge: Bridge) -> InlineKeyboardMarkup:
    scope = bridge.forward_scope
    confirm = bridge.confirm_before_send
    ignored = len(bridge.ignored_rooms())

    def mark(label: str, on: bool) -> str:
        return f"✓ {label}" if on else label

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(mark("DMs + mentions", scope == "dm_mentions"), callback_data="set:scope:dm_mentions"),
                InlineKeyboardButton(mark("All messages", scope == "all"), callback_data="set:scope:all"),
            ],
            [
                InlineKeyboardButton(mark("Confirm: on", confirm), callback_data="set:confirm:on"),
                InlineKeyboardButton(mark("Confirm: off", not confirm), callback_data="set:confirm:off"),
            ],
            [
                InlineKeyboardButton("🔕 Ignore a chat", callback_data="set:ignore"),
                InlineKeyboardButton(f"🔔 Un-ignore ({ignored})", callback_data="set:unignore"),
            ],
            [InlineKeyboardButton("↩️ Reset to .env", callback_data="set:reset")],
        ]
    )


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("« Back", callback_data="set:menu")]


def owner_filter(owner_id: int) -> filters.BaseFilter:
    return filters.User(user_id=owner_id) & filters.ChatType.PRIVATE


def build_handlers(bridge: Bridge, owner_id: int) -> list:
    owner = owner_filter(owner_id)

    async def answer(update: Update, text: str) -> None:
        await update.effective_message.reply_text(text, disable_notification=True)

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await answer(update, HELP)

    async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await answer(update, bridge.status())

    async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await answer(update, bridge.pause())

    async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await answer(update, bridge.resume())

    async def chats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            rooms = await bridge.recent_rooms()
        except Exception as exc:  # RocketChatError or network
            await answer(update, f"⚠️ {exc}")
            return
        if not rooms:
            await answer(update, "No chats found.")
            return
        keyboard = [
            [InlineKeyboardButton(room.label[:60], callback_data=f"room:{room.rid}")]
            for room in rooms
        ]
        await update.effective_message.reply_text(
            "Pick the active chat:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def to_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await answer(update, "Usage: /to @username or /to #channel")
            return
        await answer(update, await bridge.set_active(" ".join(context.args)))

    async def send_or_confirm(update: Update, target, text: str, quoted: str | None = None) -> None:
        """Send right away, or show a preview with Send / Cancel buttons."""
        message = update.effective_message
        if isinstance(target, str):  # could not resolve the destination
            await message.reply_text(target, disable_notification=True)
            return
        if not text.strip():
            await message.reply_text("Nothing to send.", disable_notification=True)
            return
        if not bridge.confirm_before_send:
            result = await bridge.deliver(target, text)
            await message.reply_text(result, disable_notification=True)
            return
        pending = bridge.hold(target, text, _quote(quoted))
        await message.reply_text(
            "📤 <b>Send this message?</b>\n" + describe_pending(pending),
            parse_mode=ParseMode.HTML,
            reply_markup=preview_markup(pending),
            disable_web_page_preview=True,
            disable_notification=True,
        )

    async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.effective_message.text or ""
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await answer(update, "Usage: /send @username your message")
            return
        await send_or_confirm(update, await bridge.resolve_named(parts[1]), parts[2])

    # --- settings ---------------------------------------------------
    async def show_settings(update: Update, note: str | None = None) -> None:
        text = (f"{note}\n\n" if note else "") + bridge.settings_text()
        await update.effective_message.reply_text(
            text, reply_markup=settings_markup(bridge), disable_notification=True
        )

    async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await show_settings(update)

    async def scope_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await show_settings(update)
            return
        await answer(update, bridge.set_scope(context.args[0]))

    async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        arg = (context.args[0].lower() if context.args else "")
        if arg in {"on", "true", "yes", "1"}:
            await answer(update, bridge.set_confirm(True))
        elif arg in {"off", "false", "no", "0"}:
            await answer(update, bridge.set_confirm(False))
        else:
            await answer(update, "Usage: /confirm on or /confirm off")

    async def ignore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.args:
            await answer(update, await bridge.ignore(" ".join(context.args)))
            return
        markup = await ignore_picker()
        await update.effective_message.reply_text(
            "Which chat should I stop forwarding?" if markup else "No chats found.",
            reply_markup=markup,
        )

    async def unignore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.args:
            await answer(update, bridge.unignore(" ".join(context.args)))
            return
        markup = unignore_picker()
        await update.effective_message.reply_text(
            "Which chat should be forwarded again?" if markup else "No chats are ignored.",
            reply_markup=markup,
        )

    async def ignore_picker(with_back: bool = False) -> InlineKeyboardMarkup | None:
        try:
            rooms = await bridge.recent_rooms(limit=20)
        except Exception as exc:
            log.warning("could not list rooms: %s", exc)
            return None
        rows = [
            [InlineKeyboardButton(room.label[:60], callback_data=f"ign:{room.rid}")]
            for room in rooms
            if not bridge.is_ignored(room.rid, room.name)
        ][:12]
        if with_back:
            rows.append(_back_row())
        return InlineKeyboardMarkup(rows) if rows else None

    def unignore_picker(with_back: bool = False) -> InlineKeyboardMarkup | None:
        rows = [
            [InlineKeyboardButton(f"🔔 {room.label}"[:60], callback_data=f"unign:{i}")]
            for i, room in enumerate(bridge.ignored_rooms())
        ]
        if with_back:
            rows.append(_back_row())
        return InlineKeyboardMarkup(rows) if rows else None

    async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query.from_user is None or query.from_user.id != owner_id:
            log.warning("ignored button press from non-owner user id %s", getattr(query.from_user, "id", "?"))
            await query.answer()
            return
        data = query.data or ""
        note = None
        if data.startswith("set:scope:"):
            note = bridge.set_scope(data.split(":", 2)[2])
        elif data.startswith("set:confirm:"):
            note = bridge.set_confirm(data.endswith(":on"))
        elif data == "set:reset":
            note = bridge.reset_settings()
        elif data == "set:ignore":
            await query.answer()
            markup = await ignore_picker(with_back=True)
            await query.edit_message_text(
                "Which chat should I stop forwarding?",
                reply_markup=markup or InlineKeyboardMarkup([_back_row()]),
            )
            return
        elif data == "set:unignore":
            await query.answer()
            markup = unignore_picker(with_back=True)
            await query.edit_message_text(
                "Which chat should be forwarded again?" if markup and len(markup.inline_keyboard) > 1
                else "No chats are ignored.",
                reply_markup=markup or InlineKeyboardMarkup([_back_row()]),
            )
            return
        elif data.startswith("ign:"):
            note = await bridge.ignore_rid(data[4:])
        elif data.startswith("unign:"):
            try:
                note = bridge.unignore(int(data[6:]))
            except ValueError:
                note = "⚠️ Unknown room."
        await query.answer(note[:190] if note else None)
        text = (f"{note}\n\n" if note else "") + bridge.settings_text()
        await query.edit_message_text(text, reply_markup=settings_markup(bridge))

    async def on_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query.from_user is None or query.from_user.id != owner_id:
            log.warning("ignored button press from non-owner user id %s", getattr(query.from_user, "id", "?"))
            await query.answer()
            return
        action, _, pending_id = (query.data or "").partition(":")
        if action == "send":
            await query.answer("Sending…")
            result, pending = await bridge.confirm(pending_id)
        else:
            pending = bridge.cancel(pending_id)
            await query.answer("Cancelled")
            result = "✖️ Cancelled, nothing was sent." if pending else "Already handled."
        body = f"{escape(result)}\n{describe_pending(pending)}" if pending else escape(result)
        await query.edit_message_text(body, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def on_room_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query.from_user is None or query.from_user.id != owner_id:
            log.warning("ignored button press from non-owner user id %s", getattr(query.from_user, "id", "?"))
            await query.answer()
            return
        await query.answer()
        rid = (query.data or "").removeprefix("room:")
        result = await bridge.set_active_rid(rid)
        await query.edit_message_text(result)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        original = message.reply_to_message
        if original is not None:
            target = bridge.resolve_reply(original.message_id)
            quoted = original.text or original.caption
        else:
            target = bridge.resolve_active()
            quoted = None
        await send_or_confirm(update, target, message.text, quoted)

    async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await answer(update, "Only text messages can be sent to Rocket.Chat for now.")

    async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await answer(update, "Unknown command. See /help")

    async def on_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        # Log the id only (never the content) and do not reply.
        log.warning("ignored update from non-owner user id %s", user.id if user else "?")

    return [
        CommandHandler(["start", "help"], help_cmd, filters=owner),
        CommandHandler("status", status_cmd, filters=owner),
        CommandHandler("pause", pause_cmd, filters=owner),
        CommandHandler("resume", resume_cmd, filters=owner),
        CommandHandler("chats", chats_cmd, filters=owner),
        CommandHandler("to", to_cmd, filters=owner),
        CommandHandler("send", send_cmd, filters=owner),
        CommandHandler("settings", settings_cmd, filters=owner),
        CommandHandler("scope", scope_cmd, filters=owner),
        CommandHandler("confirm", confirm_cmd, filters=owner),
        CommandHandler("ignore", ignore_cmd, filters=owner),
        CommandHandler("unignore", unignore_cmd, filters=owner),
        CallbackQueryHandler(on_settings_button, pattern=r"^(set:|ign:|unign:)"),
        CallbackQueryHandler(on_room_button, pattern=r"^room:"),
        CallbackQueryHandler(on_confirm_button, pattern=r"^(send|cancel):"),
        MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text),
        MessageHandler(owner & filters.COMMAND, on_unknown_command),
        MessageHandler(owner & ~filters.TEXT, on_other),
        MessageHandler(~owner, on_stranger),
    ]


async def install_commands(app: Application, owner_id: int) -> None:
    """Show the command menu only in the owner's chat."""
    await app.bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
