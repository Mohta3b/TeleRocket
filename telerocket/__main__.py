"""Command line entry point.

    telerocket run      start the bridge (default)
    telerocket check    verify both accounts and send a test message
    telerocket whoami   print the Telegram user id of whoever messages the bot
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from telegram.error import Forbidden, InvalidToken, NetworkError, TelegramError

from .bridge import Bridge
from .config import Config, ConfigError, describe_proxy, load_config, parse_env_file, parse_proxy_url
from .formatting import Identity
from .logging_setup import setup_logging
from .rocket.realtime import RocketRealtime
from .rocket.rest import RocketChatError, RocketChatRest
from .store import Store
from .telegram_bot import (
    TelegramSender,
    build_application,
    build_bot,
    build_handlers,
    install_commands,
)

log = logging.getLogger("telerocket")


async def verify_rocket(config: Config, rest: RocketChatRest) -> Identity:
    me = await rest.me()
    if me.get("_id") != config.rocket_user_id:
        raise RocketChatError("ROCKETCHAT_USER_ID does not match the token's user")
    return Identity(user_id=me["_id"], username=me.get("username", ""))


async def run(config: Config) -> int:
    rest = RocketChatRest(config.rocket_url, config.rocket_user_id, config.rocket_token)
    store = Store(config.data_dir / "telerocket.sqlite3")
    store.prune()
    try:
        me = await verify_rocket(config, rest)
    except RocketChatError as exc:
        log.error("Rocket.Chat check failed: %s", exc)
        await rest.aclose()
        return 2
    log.info("Rocket.Chat login OK as @%s", me.username)

    app = build_application(config.telegram_bot_token, config.telegram_proxy_url)
    if config.telegram_proxy_url:
        log.info("Telegram via proxy %s", describe_proxy(config.telegram_proxy_url))
    sender = TelegramSender(app, config.telegram_owner_id)
    bridge = Bridge(config, rest, store, sender, me)
    app.add_handlers(build_handlers(bridge, config.telegram_owner_id))
    realtime = RocketRealtime(
        config.rocket_ws_url,
        config.rocket_user_id,
        config.rocket_token,
        bridge.on_rocket_message,
        on_state=bridge.on_rocket_state,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        async with app:
            await install_commands(app, config.telegram_owner_id)
            try:
                await sender.send(f"🚀 TeleRocket started. Bridging @{me.username} on {config.rocket_url}")
            except Forbidden:
                log.error("Telegram refused to message you: open your bot in Telegram and press Start, "
                          "and check TELEGRAM_OWNER_ID")
                return 2
            await app.start()
            await app.updater.start_polling(allowed_updates=["message", "callback_query"])
            rt_task = asyncio.create_task(realtime.run())
            log.info("TeleRocket is running. Press Ctrl+C to stop.")
            await stop.wait()
            log.info("Stopping...")
            await realtime.stop()
            await asyncio.wait_for(rt_task, 10)
            await app.updater.stop()
            await app.stop()
    except InvalidToken:
        log.error("Telegram rejected TELEGRAM_BOT_TOKEN")
        return 2
    except NetworkError as exc:
        log.error("Cannot reach Telegram (%s). %s", exc, _network_hint(config.telegram_proxy_url))
        return 3
    finally:
        await rest.aclose()
        store.close()
    return 0


async def check(config: Config) -> int:
    ok = True
    rest = RocketChatRest(config.rocket_url, config.rocket_user_id, config.rocket_token)
    try:
        info = await rest.server_info()
        print(f"✔ Rocket.Chat server reachable at {config.rocket_url} "
              f"(version {info.get('info', {}).get('version') or info.get('version', '?')})")
        me = await verify_rocket(config, rest)
        print(f"✔ Rocket.Chat token valid, logged in as @{me.username}")
        rooms = await rest.list_rooms()
        print(f"✔ You are in {len(rooms)} chats")
    except RocketChatError as exc:
        ok = False
        print(f"✘ Rocket.Chat: {exc}")
    finally:
        await rest.aclose()

    bot = build_bot(config.telegram_bot_token, config.telegram_proxy_url)
    via = f" via proxy {describe_proxy(config.telegram_proxy_url)}" if config.telegram_proxy_url else ""
    try:
        async with bot:
            print(f"✔ Telegram reachable{via}, bot token valid: @{bot.username}")
            await bot.send_message(config.telegram_owner_id, "✅ TeleRocket check: the bot can reach you.")
            print("✔ Sent a test message to your Telegram")
    except InvalidToken:
        ok = False
        print("✘ Telegram rejected TELEGRAM_BOT_TOKEN")
    except Forbidden:
        ok = False
        print("✘ Telegram: the bot cannot message you yet. Open the bot in Telegram, press Start, "
              "and check TELEGRAM_OWNER_ID (run: telerocket whoami)")
    except NetworkError as exc:
        ok = False
        print(f"✘ Cannot reach Telegram{via} ({exc}). {_network_hint(config.telegram_proxy_url)}")
    except TelegramError as exc:
        ok = False
        print(f"✘ Telegram: {exc}")
    print("All good." if ok else "Some checks failed.")
    return 0 if ok else 1


async def whoami(token: str, proxy_url: str | None, seconds: int = 120) -> int:
    print(f"Send any message to your bot in Telegram within {seconds}s...")
    if proxy_url:
        print(f"(using proxy {describe_proxy(proxy_url)})")
    bot = build_bot(token, proxy_url)
    offset = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    try:
        async with bot:
            print(f"(listening as @{bot.username})")
            while loop.time() < deadline:
                updates = await bot.get_updates(offset=offset, timeout=20, allowed_updates=["message"])
                for upd in updates:
                    offset = upd.update_id + 1
                    user = upd.effective_user
                    if user:
                        print(f"\nTelegram user id: {user.id}  (name: {user.full_name}, "
                              f"username: @{user.username or '-'})")
                        print(f"Put this in .env:  TELEGRAM_OWNER_ID={user.id}")
                        # Acknowledge so these updates are not delivered again.
                        await bot.get_updates(offset=offset, timeout=0)
                        return 0
    except InvalidToken:
        print("Telegram rejected TELEGRAM_BOT_TOKEN")
        return 2
    except NetworkError as exc:
        print(f"Cannot reach Telegram ({exc}). {_network_hint(proxy_url)}")
        return 3
    print("No message received.")
    return 1


def _network_hint(proxy_url: str | None) -> str:
    if proxy_url:
        return "Check that your proxy is running and TELEGRAM_PROXY_URL is right."
    return "If Telegram is blocked on your network, set TELEGRAM_PROXY_URL in .env."


def _telegram_only(env_file: Path) -> tuple[str | None, str | None]:
    """Bot token and proxy URL, for commands that don't need the full config."""
    values = parse_env_file(env_file) if env_file.exists() else {}

    def get(key: str) -> str:
        return os.environ.get(key) or values.get(key) or ""

    token = get("TELEGRAM_BOT_TOKEN")
    token = token if token and not token.startswith("<") else None
    return token, parse_proxy_url(get("TELEGRAM_PROXY_URL"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telerocket", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "whoami"])
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="default: ./.env")
    args = parser.parse_args(argv)

    if args.command == "whoami":
        try:
            token, proxy_url = _telegram_only(args.env_file)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        if not token:
            print("Set TELEGRAM_BOT_TOKEN in .env first.")
            return 2
        setup_logging("WARNING", [token, proxy_url or ""])
        return asyncio.run(whoami(token, proxy_url))

    try:
        config, warnings = load_config(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level if args.command == "run" else "WARNING", config.secrets)
    for warning in warnings:
        log.warning(warning)

    if args.command == "check":
        return asyncio.run(check(config))
    return asyncio.run(run(config))


if __name__ == "__main__":
    sys.exit(main())
