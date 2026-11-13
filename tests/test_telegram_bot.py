from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Bot, Message, Update, User
from telegram.ext import CallbackQueryHandler, CommandHandler

from telerocket.telegram_bot import build_handlers, owner_filter

OWNER = 42
STRANGER = 666

BOT = Bot("123456:TESTTOKEN")
BOT._bot_user = User(id=123456, is_bot=True, first_name="bot", username="testbot")


def make_update(user_id, text, chat_type="private", reply_to=None, update_id=1):
    msg = {
        "message_id": 10,
        "date": int(datetime.now(timezone.utc).timestamp()),
        "chat": {"id": user_id if chat_type == "private" else -100, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": "x"},
        "text": text,
    }
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if reply_to:
        msg["reply_to_message"] = {
            "message_id": reply_to, "date": msg["date"], "chat": msg["chat"],
            "from": {"id": 1, "is_bot": True, "first_name": "bot"}, "text": "fwd",
        }
    return Update.de_json({"update_id": update_id, "message": msg}, bot=BOT)


def make_photo_update(user_id):
    data = make_update(user_id, "x").to_dict()
    data["message"].pop("text")
    data["message"]["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    return Update.de_json(data, bot=BOT)


def first_matching(handlers, update):
    for h in handlers:
        check = h.check_update(update)
        if check is not None and check is not False:
            return h
    return None


@pytest.fixture
def bridge():
    from telerocket.bridge import PendingSend, SendTarget

    b = MagicMock()
    b.config.confirm_before_send = False
    target = SendTarget("r-dm", None, "@sara", make_active=True)
    b.target = target
    b.resolve_reply.return_value = target
    b.resolve_active.return_value = SendTarget("r-gen", None, "#general")
    b.resolve_named = AsyncMock(return_value=SendTarget("r-dm", None, "@sara"))
    b.deliver = AsyncMock(return_value="✅ Sent to @sara")
    b.hold.side_effect = lambda t, text, quoted=None: PendingSend("abc123", t, text, quoted, 0.0)
    b.confirm = AsyncMock(side_effect=lambda pid: ("✅ Sent to @sara", PendingSend(pid, target, "hi", None, 0.0)))
    b.cancel.side_effect = lambda pid: PendingSend(pid, target, "hi", None, 0.0)
    for name in ("reply", "send_text", "send_to", "set_active", "set_active_rid", "recent_rooms"):
        setattr(b, name, AsyncMock(return_value="ok"))
    b.status.return_value = "status"
    return b


@pytest.fixture
def handlers(bridge):
    return build_handlers(bridge, OWNER)


def test_owner_filter():
    f = owner_filter(OWNER)
    assert f.check_update(make_update(OWNER, "hi"))
    assert not f.check_update(make_update(STRANGER, "hi"))
    # even the owner is ignored inside a group chat
    assert not f.check_update(make_update(OWNER, "hi", chat_type="group"))


@pytest.mark.parametrize("text", ["hi", "/status", "/send @a hi", "/help", "/to #x", "/chats"])
def test_strangers_only_reach_silent_handler(handlers, text):
    h = first_matching(handlers, make_update(STRANGER, text))
    assert h is not None and h.callback.__name__ == "on_stranger"


def test_owner_routing(handlers):
    assert first_matching(handlers, make_update(OWNER, "hello")).callback.__name__ == "on_text"
    assert isinstance(first_matching(handlers, make_update(OWNER, "/status")), CommandHandler)
    assert first_matching(handlers, make_update(OWNER, "/nope")).callback.__name__ == "on_unknown_command"
    assert first_matching(handlers, make_photo_update(OWNER)).callback.__name__ == "on_other"


def _ctx(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


_REPLY = None


@pytest.fixture(autouse=True)
def patched_reply(monkeypatch):
    global _REPLY
    _REPLY = AsyncMock()
    monkeypatch.setattr(Message, "reply_text", _REPLY)


def _patch_reply(update):
    _REPLY.reset_mock()
    return _REPLY


async def test_stranger_gets_no_reply(handlers, bridge):
    upd = make_update(STRANGER, "hi")
    reply = _patch_reply(upd)
    await first_matching(handlers, upd).callback(upd, _ctx())
    reply.assert_not_called()
    bridge.send_text.assert_not_called()


async def test_text_reply_vs_plain(handlers, bridge):
    upd = make_update(OWNER, "answer", reply_to=77)
    reply = _patch_reply(upd)
    await first_matching(handlers, upd).callback(upd, _ctx())
    bridge.resolve_reply.assert_called_once_with(77)
    bridge.deliver.assert_awaited_once_with(bridge.target, "answer")
    assert reply.await_args.args[0] == "✅ Sent to @sara"

    upd = make_update(OWNER, "plain")
    _patch_reply(upd)
    bridge.deliver.reset_mock()
    await first_matching(handlers, upd).callback(upd, _ctx())
    bridge.resolve_active.assert_called_once()
    assert bridge.deliver.await_args.args[1] == "plain"


async def test_unresolvable_target_message_is_shown(handlers, bridge):
    bridge.resolve_active.return_value = "No active chat yet."
    upd = make_update(OWNER, "plain")
    reply = _patch_reply(upd)
    await first_matching(handlers, upd).callback(upd, _ctx())
    bridge.deliver.assert_not_called()
    bridge.hold.assert_not_called()
    assert reply.await_args.args[0] == "No active chat yet."


async def test_send_command_parsing(handlers, bridge):
    upd = make_update(OWNER, "/send @sara hello there  friend")
    _patch_reply(upd)
    await first_matching(handlers, upd).callback(upd, _ctx(["@sara", "hello", "there", "friend"]))
    bridge.resolve_named.assert_awaited_once_with("@sara")
    assert bridge.deliver.await_args.args[1] == "hello there  friend"

    upd = make_update(OWNER, "/send @sara")
    reply = _patch_reply(upd)
    await first_matching(handlers, upd).callback(upd, _ctx(["@sara"]))
    assert "Usage" in reply.await_args.args[0]


async def test_confirmation_preview_instead_of_sending(handlers, bridge):
    bridge.config.confirm_before_send = True
    upd = make_update(OWNER, "see you <tomorrow>", reply_to=77)
    reply = _patch_reply(upd)
    await first_matching(handlers, upd).callback(upd, _ctx())
    bridge.deliver.assert_not_called()
    bridge.hold.assert_called_once_with(bridge.target, "see you <tomorrow>", "fwd")
    kwargs = reply.await_args.kwargs
    text = reply.await_args.args[0]
    assert "Send this message?" in text
    assert "<b>To:</b> @sara" in text
    assert "<b>Replying to:</b> <i>fwd</i>" in text
    assert "see you &lt;tomorrow&gt;" in text  # escaped
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["send:abc123", "cancel:abc123"]


async def test_confirm_and_cancel_buttons(handlers, bridge):
    cb = next(h for h in handlers if isinstance(h, CallbackQueryHandler)
              and h.check_update(MagicMock(spec=Update, callback_query=MagicMock(data="send:x"))))
    query = MagicMock()
    query.from_user.id = OWNER
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock(callback_query=query)

    query.data = "send:abc123"
    await cb.callback(update, _ctx())
    bridge.confirm.assert_awaited_once_with("abc123")
    assert query.edit_message_text.await_args.args[0].startswith("✅ Sent to @sara")

    query.data = "cancel:abc123"
    await cb.callback(update, _ctx())
    bridge.cancel.assert_called_once_with("abc123")
    assert "Cancelled" in query.edit_message_text.await_args.args[0]

    query.from_user.id = STRANGER
    bridge.confirm.reset_mock()
    query.data = "send:abc123"
    await cb.callback(update, _ctx())
    bridge.confirm.assert_not_called()


async def test_button_from_stranger_ignored(handlers, bridge):
    cb = next(h for h in handlers if isinstance(h, CallbackQueryHandler))
    query = MagicMock()
    query.from_user.id = STRANGER
    query.data = "room:r1"
    query.answer = AsyncMock()
    update = MagicMock(callback_query=query)
    await cb.callback(update, _ctx())
    bridge.set_active_rid.assert_not_called()

    query.from_user.id = OWNER
    query.edit_message_text = AsyncMock()
    await cb.callback(update, _ctx())
    bridge.set_active_rid.assert_awaited_once_with("r1")
