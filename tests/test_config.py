import os

import pytest

from telerocket.config import ConfigError, build_config, load_config, parse_env_file


def test_valid_config(env):
    cfg = build_config(env)
    assert cfg.rocket_url == "https://chat.example.com"
    assert cfg.rocket_ws_url == "wss://chat.example.com/websocket"
    assert cfg.telegram_owner_id == 42
    assert cfg.forward_scope == "dm_mentions"


def test_ws_url_keeps_subpath(env):
    env["ROCKETCHAT_URL"] = "https://corp.example.com/chat"
    assert build_config(env).rocket_ws_url == "wss://corp.example.com/chat/websocket"


def test_repr_hides_secrets(env):
    text = repr(build_config(env)) + str(build_config(env))
    for secret in (env["ROCKETCHAT_TOKEN"], env["TELEGRAM_BOT_TOKEN"], env["ROCKETCHAT_USER_ID"]):
        assert secret not in text


@pytest.mark.parametrize("key", ["ROCKETCHAT_URL", "ROCKETCHAT_USER_ID", "ROCKETCHAT_TOKEN",
                                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"])
def test_missing_required(env, key):
    del env[key]
    with pytest.raises(ConfigError, match=key):
        build_config(env)


def test_placeholder_rejected(env):
    env["ROCKETCHAT_TOKEN"] = "<your-personal-access-token>"
    with pytest.raises(ConfigError, match="ROCKETCHAT_TOKEN"):
        build_config(env)


def test_http_rejected_unless_allowed(env):
    env["ROCKETCHAT_URL"] = "http://chat.local"
    with pytest.raises(ConfigError, match="http"):
        build_config(env)
    env["ALLOW_INSECURE_HTTP"] = "true"
    assert build_config(env).rocket_ws_url == "ws://chat.local/websocket"


@pytest.mark.parametrize("value", ["abc", "-5", "0"])
def test_bad_owner_id(env, value):
    env["TELEGRAM_OWNER_ID"] = value
    with pytest.raises(ConfigError):
        build_config(env)


def test_bad_scope(env):
    env["FORWARD_SCOPE"] = "everything"
    with pytest.raises(ConfigError):
        build_config(env)


def test_ignore_rooms_normalised(env):
    env["IGNORE_ROOMS"] = " General, #Random ,, @bot "
    assert build_config(env).ignore_rooms == {"general", "random", "bot"}


def test_parse_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n\nA=1\nexport B = two \nC='quoted # not comment'\nD=val # comment\nE=\"x\"\n"
    )
    assert parse_env_file(f) == {"A": "1", "B": "two", "C": "quoted # not comment",
                                 "D": "val", "E": "x"}


def test_parse_env_file_bad_line(tmp_path):
    f = tmp_path / ".env"
    f.write_text("NOEQUALS\n")
    with pytest.raises(ConfigError, match=":1:"):
        parse_env_file(f)


def test_load_config_warns_on_open_permissions(env, tmp_path, monkeypatch):
    for k in env:
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text("\n".join(f"{k}={v}" for k, v in env.items()))
    os.chmod(f, 0o644)
    _, warnings = load_config(f)
    assert warnings and "chmod 600" in warnings[0]
    os.chmod(f, 0o600)
    _, warnings = load_config(f)
    assert warnings == []


def test_process_env_overrides_file(env, tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("\n".join(f"{k}={v}" for k, v in env.items()))
    os.chmod(f, 0o600)
    monkeypatch.setenv("FORWARD_SCOPE", "all")
    cfg, _ = load_config(f)
    assert cfg.forward_scope == "all"


@pytest.mark.parametrize("url", ["socks5://127.0.0.1:1080", "socks5h://u:p@proxy.local:1080",
                                 "http://127.0.0.1:8080", "https://proxy.example.com:443"])
def test_proxy_url_accepted(env, url):
    env["TELEGRAM_PROXY_URL"] = url
    assert build_config(env).telegram_proxy_url == url


@pytest.mark.parametrize("url", ["127.0.0.1:1080", "ftp://x:1", "socks5://:1080", "http://h:notaport"])
def test_proxy_url_rejected(env, url):
    env["TELEGRAM_PROXY_URL"] = url
    with pytest.raises(ConfigError, match="TELEGRAM_PROXY_URL"):
        build_config(env)


def test_proxy_optional(env):
    env["TELEGRAM_PROXY_URL"] = "  "
    assert build_config(env).telegram_proxy_url is None


def test_proxy_credentials_are_secret(env):
    env["TELEGRAM_PROXY_URL"] = "socks5://alice:pr0xyPass@proxy.local:1080"
    cfg = build_config(env)
    assert "pr0xyPass" in cfg.secrets and "alice" in cfg.secrets
    assert "pr0xyPass" not in repr(cfg) and "alice" not in repr(cfg)
    assert "socks5://proxy.local:1080" in repr(cfg)
