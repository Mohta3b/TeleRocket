import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from telerocket.rocket.realtime import RocketRealtime


class FakeDDPServer:
    """Speaks just enough DDP to exercise the client."""

    def __init__(self, *, user_id="uid", token="tok", refuse_sub=False, drop_after_first=False):
        self.user_id = user_id
        self.token = token
        self.refuse_sub = refuse_sub
        self.drop_after_first = drop_after_first
        self.connections = 0
        self.received = []
        self.pong = asyncio.Event()

    async def handler(self, ws):
        self.connections += 1
        await ws.send(json.dumps({"server_id": "0"}))
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            kind = msg.get("msg")
            if kind == "connect":
                await ws.send(json.dumps({"msg": "connected", "session": "s"}))
                await ws.send(json.dumps({"msg": "ping"}))
            elif kind == "pong":
                self.pong.set()
            elif kind == "method" and msg["method"] == "login":
                if msg["params"][0].get("resume") == self.token:
                    await ws.send(json.dumps({"msg": "result", "id": msg["id"],
                                              "result": {"id": self.user_id, "token": self.token}}))
                else:
                    await ws.send(json.dumps({"msg": "result", "id": msg["id"],
                                              "error": {"error": 403, "reason": "You've been logged out by the server."}}))
            elif kind == "sub":
                if self.refuse_sub:
                    await ws.send(json.dumps({"msg": "nosub", "id": msg["id"], "error": {}}))
                    continue
                await ws.send(json.dumps({"msg": "ready", "subs": [msg["id"]]}))
                await ws.send(json.dumps({
                    "msg": "changed", "collection": "stream-room-messages", "id": "id",
                    "fields": {"eventName": "__my_messages__", "args": [
                        {"_id": f"m{self.connections}", "rid": "r1", "msg": "hello",
                         "u": {"_id": "other", "username": "sara"}},
                        {"roomType": "d", "roomName": "sara"},
                    ]},
                }))
                if self.drop_after_first:
                    await ws.close()


@pytest.fixture
async def server_factory():
    servers = []

    async def make(fake):
        srv = await serve(fake.handler, "127.0.0.1", 0)
        servers.append(srv)
        port = srv.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/websocket"

    yield make
    for srv in servers:
        srv.close()
        await srv.wait_closed()


async def _run_until(client, condition, timeout=5):
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(condition(), timeout)
    finally:
        await client.stop()
        await asyncio.wait_for(task, 5)


async def test_receives_messages_and_answers_ping(server_factory):
    fake = FakeDDPServer()
    url = await server_factory(fake)
    got = []
    done = asyncio.Event()

    async def on_message(msg, meta):
        got.append((msg, meta))
        done.set()

    client = RocketRealtime(url, "uid", "tok", on_message)
    await _run_until(client, done.wait)
    assert got[0][0]["msg"] == "hello"
    assert got[0][1]["roomType"] == "d"
    assert fake.pong.is_set()
    login = next(m for m in fake.received if m.get("method") == "login")
    assert login["params"] == [{"resume": "tok"}]
    sub = next(m for m in fake.received if m.get("msg") == "sub")
    assert sub["name"] == "stream-room-messages" and sub["params"][0] == "__my_messages__"


async def test_bad_token_reports_auth_error(server_factory):
    fake = FakeDDPServer(token="right")
    url = await server_factory(fake)
    states = []
    failed = asyncio.Event()

    async def on_state(connected, error):
        states.append((connected, error))
        if error:
            failed.set()

    async def on_message(msg, meta):
        pass

    client = RocketRealtime(url, "uid", "wrong", on_message, on_state=on_state)
    await _run_until(client, failed.wait)
    assert not client.connected
    assert "logged out" in client.last_error
    assert "wrong" not in client.last_error


async def test_token_for_other_user_rejected(server_factory):
    fake = FakeDDPServer(user_id="someone-else")
    url = await server_factory(fake)
    failed = asyncio.Event()

    async def on_state(connected, error):
        if error:
            failed.set()

    async def noop(msg, meta):
        pass

    client = RocketRealtime(url, "uid", "tok", noop, on_state=on_state)
    await _run_until(client, failed.wait)
    assert "different user" in client.last_error


async def test_reconnects_after_drop(server_factory):
    fake = FakeDDPServer(drop_after_first=True)
    url = await server_factory(fake)
    got = []
    twice = asyncio.Event()

    async def on_message(msg, meta):
        got.append(msg["_id"])
        if len(got) >= 2:
            twice.set()

    client = RocketRealtime(url, "uid", "tok", on_message, min_backoff=0.05, max_backoff=0.1)
    await _run_until(client, twice.wait)
    assert got[:2] == ["m1", "m2"]
    assert fake.connections >= 2


async def test_handler_exception_does_not_kill_connection(server_factory):
    fake = FakeDDPServer(drop_after_first=True)
    url = await server_factory(fake)
    calls = []
    twice = asyncio.Event()

    async def on_message(msg, meta):
        calls.append(1)
        if len(calls) >= 2:
            twice.set()
        raise RuntimeError("handler bug")

    client = RocketRealtime(url, "uid", "tok", on_message, min_backoff=0.05, max_backoff=0.1)
    await _run_until(client, twice.wait)


async def test_refused_subscription(server_factory):
    fake = FakeDDPServer(refuse_sub=True)
    url = await server_factory(fake)
    failed = asyncio.Event()

    async def on_state(connected, error):
        if error:
            failed.set()

    async def noop(msg, meta):
        pass

    client = RocketRealtime(url, "uid", "tok", noop, on_state=on_state, min_backoff=0.05)
    await _run_until(client, failed.wait)
    assert "refused" in client.last_error


async def test_realtime_ignores_proxy_env(server_factory, monkeypatch):
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:1")  # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    fake = FakeDDPServer()
    url = await server_factory(fake)
    done = asyncio.Event()

    async def on_message(msg, meta):
        done.set()

    client = RocketRealtime(url, "uid", "tok", on_message)
    await _run_until(client, done.wait)
