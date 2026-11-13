# TeleRocket plan

A personal bridge between one Rocket.Chat account (on a self-hosted company
server) and one Telegram account, through a Telegram bot that runs locally on
the owner's own computer (no public server needed).

## Goals

1. Forward incoming Rocket.Chat messages to the owner's Telegram chat with the bot.
2. Reply from Telegram: replying (Telegram "Reply") to a forwarded message posts
   the answer to the same Rocket.Chat room (and the same thread, if the original
   was in a thread).
3. Send new messages from Telegram to a chosen Rocket.Chat chat.
4. Keep credentials safe and make the bot answer only its owner.

## Non-goals (for now)

- Multiple users, hosting as a public service, webhooks.
- Sending files/images from Telegram to Rocket.Chat (text only; attachments
  coming from Rocket.Chat are announced with their name).
- Catching up on messages missed while the bridge was offline.

## Technology choices

| Concern | Choice | Why |
| --- | --- | --- |
| Language | Python 3.10+ | Widely available, good async libraries. |
| Env / deps | `uv` + `pyproject.toml` | Reproducible, lock file, `uv` is installed. |
| Telegram | `python-telegram-bot` v21 (long polling) | Mature, async; polling needs no public URL, which fits a personal computer. |
| Rocket.Chat realtime | Rocket.Chat DDP websocket (`websockets`) | Push delivery of new messages, no polling delay. |
| Rocket.Chat REST | `httpx` | Sending messages, listing chats, checking the login. |
| Local state | SQLite (stdlib) | Maps Telegram message ids to Rocket.Chat room/thread ids so replies work after restarts. Stores ids only, never message text. |
| Tests | `pytest`, `pytest-asyncio`, `respx` | Unit tests plus a fake DDP server for the realtime client. |

## Authentication

- **Rocket.Chat: Personal Access Token (PAT)** (`User ID` + `Token`), created in
  *My Account > Personal Access Tokens*. Works with SSO / LDAP / 2FA logins
  because no password is involved, can be revoked any time, and is used both
  for REST (`X-User-Id` / `X-Auth-Token` headers) and for the realtime
  websocket (DDP `login` with `resume`). The server URL is configurable.
  If the company server has PATs disabled, a fallback is documented (ask the
  admin, or use a token copied from a browser session, which expires).
- **Telegram:** a bot token from @BotFather plus the owner's numeric Telegram
  user id. A helper command prints your id locally so you don't need a third
  party bot.

## Security design

- All secrets live in a local `.env` file that is git-ignored; only
  `.env.example` with placeholders is committed. The app warns if `.env` is
  readable by other users (should be `chmod 600`).
- Owner lock: every Telegram handler is filtered on the owner's user id **and**
  private chat. Messages from anyone else are dropped without a reply; only the
  sender id is logged (never content).
- Log redaction: a logging filter replaces every configured secret (bot token,
  RC token, user id) with `***` in all log records. `httpx`/`telegram` request
  logs (which include the bot token in the URL) are turned down to WARNING.
  Message bodies are never logged.
- HTTPS is required for the Rocket.Chat URL (plain `http://` must be enabled
  explicitly, e.g. for a lab server).
- The SQLite file holds ids only and is created with `0600` permissions.
- Caveat to document: forwarded company messages are stored on Telegram's
  servers. The owner should check that this is allowed by company policy.

## Behaviour

Forwarding (Rocket.Chat → Telegram):
- Subscribe to `stream-room-messages` / `__my_messages__` (all rooms the user is in).
- Skip own messages, system messages (join/leave etc.) and edits.
- `FORWARD_SCOPE=dm_mentions` (default): direct messages, and channel messages
  that mention you (or @all/@here). `FORWARD_SCOPE=all`: every message.
- `IGNORE_ROOMS`: comma separated room names never forwarded.
- Format: `💬 #room · Sender Name` (or `👤 Sender Name` for DMs, `🧵` marker
  for threads) followed by the text, HTML-escaped, split at Telegram's 4096 limit.

Telegram → Rocket.Chat (owner only):
- Reply to a forwarded message → posts to that room/thread.
- `/chats` → recent Rocket.Chat chats as buttons; tapping one makes it the
  *active chat*. Plain (non-reply) messages go to the active chat.
- `/to <@user | #channel | room>` → set the active chat by name.
- `/send <@user | #channel> <text>` → one-off message.
- `/status`, `/pause`, `/resume`, `/help`.
- `/settings` (buttons), `/scope`, `/confirm`, `/ignore`, `/unignore`: change
  forwarding scope, send confirmation and ignored chats at runtime. Saved in the
  local store, override `.env` until reset.
- Every sent message is confirmed with a ✅ reaction-like reply or an error.

Reliability: websocket keep-alive (answer DDP pings), reconnect with
exponential backoff, re-login and re-subscribe after reconnect.

## Project layout

```
telerocket/
  config.py          # load + validate .env
  logging_setup.py   # secret redaction
  formatting.py      # Telegram message formatting
  store.py           # SQLite id mapping + settings
  rocket/rest.py     # REST client
  rocket/realtime.py # DDP websocket client
  bridge.py          # core logic (framework independent, unit tested)
  telegram_bot.py    # python-telegram-bot handlers (owner-locked)
  __main__.py        # CLI: run, check, whoami
tests/
```

## Steps (one commit each)

1. Plan + git repo + `.gitignore` (secrets ignored from the first commit).
2. Project skeleton (`pyproject.toml`, uv lock, package, test setup).
3. Config loading/validation + log redaction, with tests.
4. Rocket.Chat REST client, with tests.
5. Rocket.Chat realtime client, with tests against a fake DDP server.
6. Formatting + store, with tests.
7. Bridge core logic, with tests.
8. Telegram bot layer + CLI (`run`, `check`, `whoami`).
9. README: setup, connecting both accounts, testing, running as a user service.
