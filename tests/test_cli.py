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
