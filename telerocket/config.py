"""Configuration loading and validation.

Settings come from environment variables, optionally loaded from a local
``.env`` file. Secrets are never printed: ``Config.__repr__`` masks them.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

FORWARD_SCOPES = ("dm_mentions", "all")
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    rocket_url: str
    rocket_user_id: str
    rocket_token: str
    telegram_bot_token: str
    telegram_owner_id: int
    forward_scope: str = "dm_mentions"
    ignore_rooms: frozenset[str] = field(default_factory=frozenset)
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    telegram_proxy_url: str | None = None
    confirm_before_send: bool = False

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        values = [self.rocket_token, self.telegram_bot_token, self.rocket_user_id]
        if self.telegram_proxy_url:
            parsed = urlparse(self.telegram_proxy_url)
            values += [v for v in (parsed.password, parsed.username) if v]
            values.append(self.telegram_proxy_url)
        return values

    @property
    def rocket_ws_url(self) -> str:
        parsed = urlparse(self.rocket_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}{parsed.path}/websocket"

    def __repr__(self) -> str:  # never leak secrets via repr/str
        return (
            f"Config(rocket_url={self.rocket_url!r}, rocket_user_id='***', "
            f"rocket_token='***', telegram_bot_token='***', "
            f"telegram_owner_id={self.telegram_owner_id}, "
            f"forward_scope={self.forward_scope!r}, "
            f"ignore_rooms={sorted(self.ignore_rooms)!r}, "
            f"data_dir={str(self.data_dir)!r}, log_level={self.log_level!r}, "
            f"telegram_proxy_url={describe_proxy(self.telegram_proxy_url)!r}, "
            f"confirm_before_send={self.confirm_before_send})"
        )

    __str__ = __repr__


def describe_proxy(url: str | None) -> str:
    """Proxy address without credentials, safe to print."""
    if not url:
        return "none"
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def parse_proxy_url(value: str | None) -> str | None:
    """Validate an optional proxy URL such as socks5://127.0.0.1:1080."""
    value = (value or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    try:
        parsed.port  # raises ValueError for a non-numeric / out of range port
        valid_port = True
    except ValueError:
        valid_port = False
    if parsed.scheme not in PROXY_SCHEMES or not parsed.hostname or not valid_port:
        raise ConfigError(
            "TELEGRAM_PROXY_URL must look like socks5://127.0.0.1:1080 or "
            "http://127.0.0.1:8080 (schemes: " + ", ".join(PROXY_SCHEMES) + ")"
        )
    return value


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file (comments and quotes supported)."""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def env_file_is_private(path: Path) -> bool:
    """True if the file is not readable or writable by group/others."""
    mode = path.stat().st_mode
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def _require(env: dict[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value or value.startswith("<") or value.lower().startswith("your-"):
        raise ConfigError(f"{key} is not set (see .env.example)")
    return value


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _strict_bool(env: dict[str, str], key: str, *, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true or false")


def build_config(env: dict[str, str]) -> Config:
    """Validate raw settings and build a Config."""
    url = _require(env, "ROCKETCHAT_URL").rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ConfigError("ROCKETCHAT_URL must look like https://chat.example.com")
    if parsed.scheme == "http" and not _bool(env.get("ALLOW_INSECURE_HTTP", "")):
        raise ConfigError(
            "ROCKETCHAT_URL uses plain http, which would send your token unencrypted. "
            "Use https, or set ALLOW_INSECURE_HTTP=true if you really mean it."
        )

    owner_raw = _require(env, "TELEGRAM_OWNER_ID")
    try:
        owner_id = int(owner_raw)
    except ValueError:
        raise ConfigError("TELEGRAM_OWNER_ID must be your numeric Telegram user id") from None
    if owner_id <= 0:
        raise ConfigError("TELEGRAM_OWNER_ID must be a positive user id")

    bot_token = _require(env, "TELEGRAM_BOT_TOKEN")
    if ":" not in bot_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN does not look like a BotFather token")

    scope = env.get("FORWARD_SCOPE", "dm_mentions").strip() or "dm_mentions"
    if scope not in FORWARD_SCOPES:
        raise ConfigError(f"FORWARD_SCOPE must be one of {', '.join(FORWARD_SCOPES)}")

    ignore = frozenset(
        name.strip().lstrip("#@").lower()
        for name in env.get("IGNORE_ROOMS", "").split(",")
        if name.strip()
    )

    return Config(
        rocket_url=url,
        rocket_user_id=_require(env, "ROCKETCHAT_USER_ID"),
        rocket_token=_require(env, "ROCKETCHAT_TOKEN"),
        telegram_bot_token=bot_token,
        telegram_owner_id=owner_id,
        forward_scope=scope,
        ignore_rooms=ignore,
        data_dir=Path(env.get("DATA_DIR", "data").strip() or "data").expanduser(),
        log_level=(env.get("LOG_LEVEL", "INFO").strip() or "INFO").upper(),
        telegram_proxy_url=parse_proxy_url(env.get("TELEGRAM_PROXY_URL")),
        confirm_before_send=_strict_bool(env, "CONFIRM_BEFORE_SEND", default=False),
    )


def load_config(env_file: Path | None = Path(".env")) -> tuple[Config, list[str]]:
    """Load config from the .env file (if present) and the process environment.

    Process environment variables win over the file. Returns the config and a
    list of warnings to show the user.
    """
    warnings: list[str] = []
    env: dict[str, str] = {}
    if env_file is not None and env_file.exists():
        if not env_file_is_private(env_file):
            warnings.append(
                f"{env_file} is readable by other users on this machine. "
                f"Run: chmod 600 {env_file}"
            )
        env.update(parse_env_file(env_file))
    env.update({k: v for k, v in os.environ.items() if k in _KNOWN_KEYS})
    return build_config(env), warnings


_KNOWN_KEYS = {
    "ROCKETCHAT_URL",
    "ROCKETCHAT_USER_ID",
    "ROCKETCHAT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_ID",
    "FORWARD_SCOPE",
    "IGNORE_ROOMS",
    "DATA_DIR",
    "LOG_LEVEL",
    "ALLOW_INSECURE_HTTP",
    "TELEGRAM_PROXY_URL",
    "CONFIRM_BEFORE_SEND",
}
