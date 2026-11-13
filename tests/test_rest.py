import httpx
import pytest
import respx

from telerocket.rocket.rest import RocketChatAuthError, RocketChatError, RocketChatRest

BASE = "https://chat.example.com"
API = BASE + "/api/v1/"


@pytest.fixture
async def rest():
    client = RocketChatRest(BASE, "uid", "SECRETTOK123")
    yield client
    await client.aclose()


@respx.mock
async def test_auth_headers_sent(rest):
    route = respx.get(API + "me").respond(json={"_id": "uid", "username": "amir", "success": True})
    me = await rest.me()
    assert me["username"] == "amir"
    req = route.calls.last.request
    assert req.headers["X-User-Id"] == "uid"
    assert req.headers["X-Auth-Token"] == "SECRETTOK123"


@respx.mock
async def test_401_raises_auth_error_without_token(rest):
    respx.get(API + "me").respond(401, json={"status": "error", "message": "You must be logged in"})
    with pytest.raises(RocketChatAuthError) as exc:
        await rest.me()
    assert "SECRETTOK123" not in str(exc.value)


@respx.mock
async def test_api_error_message(rest):
    respx.post(API + "chat.sendMessage").respond(400, json={"success": False, "error": "room not found"})
    with pytest.raises(RocketChatError, match="room not found"):
        await rest.send_to_room("bad", "hi")


@respx.mock
async def test_network_error_is_wrapped(rest):
    respx.get(API + "me").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(RocketChatError, match="cannot reach"):
        await rest.me()


@respx.mock
async def test_redirect_not_followed(rest):
    respx.get(API + "me").respond(302, headers={"Location": "https://evil.example/"})
    evil = respx.get("https://evil.example/").respond(200, json={})
    with pytest.raises(RocketChatError):
        await rest.me()
    assert not evil.called


@respx.mock
async def test_send_to_room_with_thread(rest):
    route = respx.post(API + "chat.sendMessage").respond(
        json={"success": True, "message": {"_id": "m1"}}
    )
    assert await rest.send_to_room("r1", "hello", tmid="t1") == "m1"
    import json

    body = json.loads(route.calls.last.request.content)
    assert body == {"message": {"rid": "r1", "msg": "hello", "tmid": "t1"}}


SUBS = {
    "success": True,
    "update": [
        {"rid": "r-old", "name": "general", "fname": "General", "t": "c", "_updatedAt": "2026-01-01T00:00:00Z"},
        {"rid": "r-dm", "name": "sara", "fname": "Sara K", "t": "d", "unread": 2, "_updatedAt": "2026-03-01T00:00:00Z"},
        {"rid": "r-priv", "name": "team-x", "fname": "Team X", "t": "p", "_updatedAt": "2026-02-01T00:00:00Z"},
        {"rid": "r-hidden", "name": "closed", "t": "c", "open": False},
    ],
}


@respx.mock
async def test_list_rooms_sorted_and_hidden_removed(rest):
    respx.get(API + "subscriptions.get").respond(json=SUBS)
    rooms = await rest.list_rooms()
    assert [r.rid for r in rooms] == ["r-dm", "r-priv", "r-old"]
    assert rooms[0].target == "@sara"
    assert rooms[0].label == "👤 Sara K (2)"
    assert rooms[1].target == "#team-x"
    assert rooms[1].label.startswith("🔒")


@respx.mock
async def test_resolve_channel_by_name_and_display_name(rest):
    respx.get(API + "subscriptions.get").respond(json=SUBS)
    assert (await rest.resolve_room("#General")).rid == "r-old"
    assert (await rest.resolve_room("Team X")).rid == "r-priv"
    with pytest.raises(RocketChatError, match="not in a chat"):
        await rest.resolve_room("#nope")


@respx.mock
async def test_resolve_direct_opens_dm(rest):
    route = respx.post(API + "im.create").respond(json={"success": True, "room": {"rid": "dm1", "_id": "dm1"}})
    room = await rest.resolve_room("@bob")
    assert room.rid == "dm1" and room.type == "d" and room.target == "@bob"
    import json

    assert json.loads(route.calls.last.request.content) == {"username": "bob"}


@respx.mock
async def test_server_info_uses_api_info_not_v1(rest):
    route = respx.get(BASE + "/api/info").respond(json={"version": "8.7", "success": True})
    assert (await rest.server_info())["version"] == "8.7"
    assert route.called
