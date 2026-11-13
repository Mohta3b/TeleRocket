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

from .bridge import Bridge

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
/help – this text"""

COMMANDS = [
    BotCommand("chats", "Pick the active Rocket.Chat chat"),
    BotCommand("to", "Set the active chat: /to @user or /to #channel"),
    BotCommand("send", "Send once: /send @user text"),
    BotCommand("status", "Connection and settings"),
    BotCommand("pause", "Pause forwarding"),
    BotCommand("resume", "Resume forwarding"),
    BotCommand("help", "How to use the bridge"),
]


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

    async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.effective_message.text or ""
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await answer(update, "Usage: /send @username your message")
            return
        await answer(update, await bridge.send_to(parts[1], parts[2]))

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
        if message.reply_to_message is not None:
            result = await bridge.reply(message.reply_to_message.message_id, message.text)
        else:
            result = await bridge.send_text(message.text)
        await message.reply_text(result, disable_notification=True)

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
        CallbackQueryHandler(on_room_button, pattern=r"^room:"),
        MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text),
        MessageHandler(owner & filters.COMMAND, on_unknown_command),
        MessageHandler(owner & ~filters.TEXT, on_other),
        MessageHandler(~owner, on_stranger),
    ]


async def install_commands(app: Application, owner_id: int) -> None:
    """Show the command menu only in the owner's chat."""
    await app.bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
