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
