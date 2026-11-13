from telerocket.__main__ import main


def test_check_without_config_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for key in ("ROCKETCHAT_URL", "ROCKETCHAT_USER_ID", "ROCKETCHAT_TOKEN",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"):
        monkeypatch.delenv(key, raising=False)
    assert main(["check"]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_whoami_needs_bot_token(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=<your-bot-token>\n")
    assert main(["whoami"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().out


def test_check_reports_server_version_and_continues(tmp_path, monkeypatch, capsys):
    import respx
    from unittest.mock import AsyncMock, patch

    from telerocket.__main__ import check
    from telerocket.config import build_config
    import asyncio

    cfg = build_config({
        "ROCKETCHAT_URL": "https://chat.example.com", "ROCKETCHAT_USER_ID": "uid",
        "ROCKETCHAT_TOKEN": "tok-123456", "TELEGRAM_BOT_TOKEN": "1:abc", "TELEGRAM_OWNER_ID": "42",
    })
    with respx.mock:
        respx.get("https://chat.example.com/api/info").respond(404, json={})
        respx.get("https://chat.example.com/api/v1/me").respond(json={"_id": "uid", "username": "amir", "success": True})
        respx.get("https://chat.example.com/api/v1/subscriptions.get").respond(json={"update": [], "success": True})
        bot = AsyncMock()
        bot.__aenter__.return_value = bot
        bot.username = "testbot"
        with patch("telerocket.__main__.build_bot", return_value=bot):
            assert asyncio.run(check(cfg)) == 0
    out = capsys.readouterr().out
    assert "server info not available" in out
    assert "logged in as @amir" in out
    assert "All good." in out
