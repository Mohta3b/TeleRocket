"""The proxy applies to Telegram only; Rocket.Chat always connects directly."""

import asyncio

import pytest
from telegram.error import NetworkError

from telerocket.rocket.rest import RocketChatRest
from telerocket.telegram_bot import build_bot, make_request


class RecordingProxy:
    """A fake HTTP proxy that records the first request line and hangs up."""

    def __init__(self):
        self.lines = []
        self.server = None

    async def _handle(self, reader, writer):
        line = await reader.readline()
        self.lines.append(line.decode().strip())
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


async def test_telegram_requests_go_through_proxy():
    async with RecordingProxy() as proxy:
        bot = build_bot("123456789:AAEexampleexampleexampleexampleexam", proxy.url)
        with pytest.raises(NetworkError):
            await bot.get_me()
    assert proxy.lines and proxy.lines[0].startswith("CONNECT api.telegram.org:443")


def test_socks_proxy_supported():
    # Needs the socks extra (socksio); constructing must not raise.
    make_request("socks5://127.0.0.1:1080")
    make_request("socks5h://user:pass@127.0.0.1:1080", long_polling=True)


async def test_rocket_rest_ignores_proxy_env(monkeypatch):
    async with RecordingProxy() as proxy:
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
            monkeypatch.setenv(var, proxy.url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        # A plain local HTTP server standing in for Rocket.Chat.
        hits = []

        async def handle(reader, writer):
            hits.append((await reader.readline()).decode())
            body = b'{"_id":"uid","username":"me","success":true}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        rest = RocketChatRest(f"http://127.0.0.1:{port}", "uid", "tok")
        try:
            assert (await rest.me())["username"] == "me"
        finally:
            await rest.aclose()
            server.close()
            await server.wait_closed()
    assert hits and hits[0].startswith("GET /api/v1/me")
    assert proxy.lines == []
