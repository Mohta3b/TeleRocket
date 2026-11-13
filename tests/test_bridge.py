import asyncio

import pytest

from telerocket.bridge import Bridge
from telerocket.formatting import Identity
from telerocket.rocket.rest import Room, RocketChatError
from telerocket.store import Store

ME = Identity("me123", "amir")


class FakeOut:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self._next = 1000

    async def send(self, html):
        if self.fail:
            raise RuntimeError("telegram down")
        self._next += 1
        self.sent.append((self._next, html))
        return self._next


class FakeRest:
    def __init__(self):
        self.sent = []
        self.rooms = [Room("r-dm", "sara", "Sara K", "d"), Room("r-gen", "general", "General", "c")]
        self.fail_send = False

    async def send_to_room(self, rid, text, tmid=None):
        if self.fail_send:
            raise RocketChatError("room not found")
        self.sent.append((rid, text, tmid))
        return "newid"

    async def list_rooms(self):
        return self.rooms

    async def resolve_room(self, target):
        name = target.lstrip("@#").lower()
        for r in self.rooms:
            if r.name == name:
                return r
        raise RocketChatError(f"you are not in a chat called {target!r}")


@pytest.fixture
def parts(config):
    out, rest, store = FakeOut(), FakeRest(), Store(":memory:")
    bridge = Bridge(config, rest, store, out, ME, outage_notice_delay=0.05)
    return bridge, out, rest, store


def dm(mid="m1", **kw):
    m = {"_id": mid, "rid": "r-dm", "msg": "hello", "u": {"_id": "u2", "username": "sara", "name": "Sara K"}}
    m.update(kw)
    return m, {"roomType": "d", "roomName": "sara"}


async def test_forward_and_reply_roundtrip(parts):
    bridge, out, rest, _ = parts
    await bridge.on_rocket_message(*dm())
    assert len(out.sent) == 1
    tg_id, html = out.sent[0]
    assert "Sara K" in html and "hello" in html
    result = await bridge.reply(tg_id, "hi back")
    assert result == "✅ Sent to @sara"
    assert rest.sent == [("r-dm", "hi back", None)]
    # the replied-to chat becomes the active chat
    assert await bridge.send_text("and more") == "✅ Sent to @sara"
    assert rest.sent[-1] == ("r-dm", "and more", None)


async def test_reply_goes_to_thread(parts):
    bridge, out, rest, _ = parts
    await bridge.on_rocket_message(*dm(tmid="thread1"))
    result = await bridge.reply(out.sent[0][0], "in thread")
    assert rest.sent == [("r-dm", "in thread", "thread1")]
    assert "(thread)" in result


async def test_duplicates_not_forwarded(parts):
    bridge, out, _, _ = parts
    await bridge.on_rocket_message(*dm())
    await bridge.on_rocket_message(*dm())  # re-broadcast after a reaction
    assert len(out.sent) == 1


async def test_channel_without_mention_not_forwarded(parts):
    bridge, out, _, _ = parts
    await bridge.on_rocket_message(
        {"_id": "m2", "rid": "r-gen", "msg": "x", "u": {"_id": "u2"}}, {"roomType": "c", "roomName": "general"}
    )
    assert out.sent == []


async def test_telegram_failure_allows_retry(parts, config):
    bridge, _, rest, store = parts
    bridge.out = FakeOut(fail=True)
    await bridge.on_rocket_message(*dm())
    bridge.out = FakeOut()
    await bridge.on_rocket_message(*dm())
    assert len(bridge.out.sent) == 1


async def test_reply_to_unknown_message(parts):
    bridge, _, rest, _ = parts
    assert "don't know" in await bridge.reply(5, "x")
    assert rest.sent == []


async def test_active_chat_flow(parts):
    bridge, _, rest, store = parts
    assert "No active chat" in await bridge.send_text("hi")
    assert "#general" in await bridge.set_active("#general")
    assert await bridge.send_text("hello all") == "✅ Sent to #general"
    assert rest.sent[-1] == ("r-gen", "hello all", None)
    assert "@sara" in await bridge.set_active_rid("r-dm")
    await bridge.send_text("yo")
    assert rest.sent[-1] == ("r-dm", "yo", None)
    assert "no longer" in await bridge.set_active_rid("gone")


async def test_send_to_and_errors(parts):
    bridge, _, rest, _ = parts
    assert await bridge.send_to("@sara", "ping") == "✅ Sent to @sara"
    assert "not in a chat" in await bridge.send_to("#nope", "ping")
    rest.fail_send = True
    assert "Not sent" in await bridge.send_to("@sara", "ping")
    assert await bridge.send_to("@sara", "   ") == "Nothing to send."


async def test_pause_resume(parts):
    bridge, out, _, _ = parts
    bridge.pause()
    await bridge.on_rocket_message(*dm())
    assert out.sent == []
    assert "paused" in bridge.status()
    bridge.resume()
    await bridge.on_rocket_message(*dm())
    assert len(out.sent) == 1


async def test_status_has_no_secrets(parts, config):
    text = parts[0].status()
    for secret in config.secrets:
        assert secret not in text


async def test_outage_notice_after_delay_and_reconnect(parts):
    bridge, out, _, _ = parts
    await bridge.on_rocket_state(True, None)
    await bridge.on_rocket_state(False, "connection closed")
    await asyncio.sleep(0.1)
    assert "Lost connection" in out.sent[-1][1]
    await bridge.on_rocket_state(True, None)
    assert "Reconnected" in out.sent[-1][1]


async def test_short_blip_is_silent(parts):
    bridge, out, _, _ = parts
    await bridge.on_rocket_state(False, "connection closed")
    await bridge.on_rocket_state(True, None)
    await asyncio.sleep(0.1)
    assert out.sent == []


async def test_auth_failure_notified_immediately(parts):
    bridge, out, _, _ = parts
    await bridge.on_rocket_state(False, "Rocket.Chat login failed: logged out")
    assert "rejected the token" in out.sent[-1][1]


async def test_hold_confirm_sends_once_and_sets_active(parts):
    bridge, out, rest, store = parts
    await bridge.on_rocket_message(*dm(tmid="t9"))
    target = bridge.resolve_reply(out.sent[0][0])
    pending = bridge.hold(target, "ok then", "Sara: hello")
    assert rest.sent == []  # nothing sent before confirmation
    result, held = await bridge.confirm(pending.id)
    assert result == "✅ Sent to @sara (thread)" and held.text == "ok then"
    assert rest.sent == [("r-dm", "ok then", "t9")]
    assert store.get("active_rid") == "r-dm"
    result, held = await bridge.confirm(pending.id)  # double tap
    assert held is None and "Nothing was sent" in result
    assert len(rest.sent) == 1


async def test_cancel_sends_nothing(parts):
    bridge, _, rest, _ = parts
    target = await bridge.resolve_named("#general")
    pending = bridge.hold(target, "never mind")
    assert bridge.cancel(pending.id).text == "never mind"
    assert bridge.cancel(pending.id) is None
    result, _ = await bridge.confirm(pending.id)
    assert "Nothing was sent" in result
    assert rest.sent == []


async def test_pending_expires(parts, monkeypatch):
    import telerocket.bridge as bridge_mod

    bridge, _, rest, _ = parts
    pending = bridge.hold(bridge_mod.SendTarget("r-gen", None, "#general"), "late")
    monkeypatch.setattr(bridge_mod, "PENDING_TTL", -1)
    result, held = await bridge.confirm(pending.id)
    assert held is None and rest.sent == []


async def test_resolve_errors_are_messages(parts):
    bridge, _, _, _ = parts
    assert isinstance(bridge.resolve_reply(12345), str)
    assert isinstance(bridge.resolve_active(), str)
    assert isinstance(await bridge.resolve_named("#nope"), str)


async def test_status_shows_confirmation_setting(parts):
    assert "Confirm before sending: off" in parts[0].status()


# --- settings changed from Telegram -----------------------------------------

def chan(mid="c1", room="general", rid="r-gen", **kw):
    m = {"_id": mid, "rid": rid, "msg": "hey", "u": {"_id": "u2", "username": "sara"}}
    m.update(kw)
    return m, {"roomType": "c", "roomName": room}


async def test_scope_change_applies_immediately_and_persists(parts, config):
    bridge, out, rest, store = parts
    await bridge.on_rocket_message(*chan("c1"))
    assert out.sent == []  # default dm_mentions
    assert "All messages" in bridge.set_scope("all")
    await bridge.on_rocket_message(*chan("c2"))
    assert len(out.sent) == 1
    # a new Bridge over the same store (i.e. after restart) keeps it
    assert Bridge(config, rest, store, out, ME).forward_scope == "all"
    assert "⚠️" in bridge.set_scope("everything")
    assert bridge.set_scope("dm") == "Forwarding: DMs + mentions"


async def test_confirm_toggle_overrides_env(parts):
    bridge, _, _, _ = parts
    assert bridge.confirm_before_send is False  # from .env default
    bridge.set_confirm(True)
    assert bridge.confirm_before_send is True
    bridge.set_confirm(False)
    assert bridge.confirm_before_send is False


async def test_ignore_by_name_dm_and_unignore(parts):
    bridge, out, _, _ = parts
    bridge.set_scope("all")
    assert "Ignoring #general" in await bridge.ignore("#general")
    assert "already" in await bridge.ignore("#general")
    await bridge.on_rocket_message(*chan("c1"))
    assert out.sent == []
    assert "Ignoring @sara" in await bridge.ignore("@sara")
    await bridge.on_rocket_message(*dm("d1"))
    assert out.sent == []
    assert "forwarded again" in bridge.unignore("#general")
    assert "forwarded again" in bridge.unignore(0)  # @sara is now first
    assert bridge.ignored_rooms() == []
    await bridge.on_rocket_message(*dm("d2"))
    assert len(out.sent) == 1
    assert "not in the ignored list" in bridge.unignore("#nope")
    assert "not in the ignored list" in bridge.unignore(5)


async def test_ignore_matches_by_room_id_even_if_renamed(parts):
    bridge, out, _, _ = parts
    bridge.set_scope("all")
    await bridge.ignore_rid("r-gen")
    await bridge.on_rocket_message(*chan("c1", room="general-renamed"))
    assert out.sent == []
    assert "no longer" in await bridge.ignore_rid("gone")


async def test_env_ignore_list_is_default_and_reset_restores_it(env, tmp_path):
    from telerocket.config import build_config

    cfg = build_config(dict(env, IGNORE_ROOMS="random", DATA_DIR=str(tmp_path)))
    out, rest, store = FakeOut(), FakeRest(), Store(":memory:")
    bridge = Bridge(cfg, rest, store, out, ME)
    assert [r.label for r in bridge.ignored_rooms()] == ["#random"]
    assert bridge.is_ignored("any-rid", "Random")
    bridge.unignore("#random")  # .env rooms can be un-ignored from Telegram too
    assert not bridge.is_ignored("any-rid", "random")
    bridge.set_scope("all")
    bridge.set_confirm(True)
    bridge.reset_settings()
    assert bridge.is_ignored("any-rid", "random")
    assert bridge.forward_scope == "dm_mentions" and bridge.confirm_before_send is False


async def test_settings_text(parts):
    bridge, _, _, _ = parts
    await bridge.ignore("#general")
    text = bridge.settings_text()
    assert "DMs + mentions" in text and "Confirm before sending: off" in text and "#general" in text
