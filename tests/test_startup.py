"""Startup waits for the network/proxy instead of exiting."""

import asyncio

import httpx
import pytest
import respx
from telegram.error import InvalidToken, NetworkError

from telerocket.__main__ import EXIT_CONFIG, EXIT_OK, retry_until_ready, run
from telerocket.rocket.rest import RocketChatAuthError, RocketChatError


async def test_retries_network_errors_until_success():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RocketChatError("cannot reach Rocket.Chat (ConnectError)")
        if len(calls) == 2:
            raise NetworkError("proxy down")
        return "ok"

    assert await retry_until_ready("x", flaky, asyncio.Event(), first_delay=0.01) == "ok"
    assert len(calls) == 3


@pytest.mark.parametrize("exc", [RocketChatAuthError("bad token"), InvalidToken()])
async def test_credential_errors_are_not_retried(exc):
    async def bad():
        raise exc

    with pytest.raises(type(exc)):
        await retry_until_ready("x", bad, asyncio.Event(), first_delay=0.01)


async def test_stop_while_waiting_returns_none():
    stop = asyncio.Event()

    async def down():
        raise NetworkError("down")

    asyncio.get_running_loop().call_later(0.05, stop.set)
    assert await retry_until_ready("x", down, stop, first_delay=10) is None


async def test_run_exits_with_config_code_on_rejected_token(config):
    with respx.mock:
        respx.get("https://chat.example.com/api/v1/me").respond(401, json={})
        assert await run(config) == EXIT_CONFIG


async def test_run_keeps_waiting_while_rocket_unreachable(config):
    stop = asyncio.Event()
    with respx.mock:
        route = respx.get("https://chat.example.com/api/v1/me").mock(side_effect=httpx.ConnectError("x"))
        asyncio.get_running_loop().call_later(0.2, stop.set)
        assert await run(config, stop=stop) == EXIT_OK
        assert route.called
