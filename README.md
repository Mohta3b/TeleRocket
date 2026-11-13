# TeleRocket

A personal bridge between your Rocket.Chat account (on your company's own
server) and your Telegram, through a Telegram bot that runs on your laptop.

- Rocket.Chat messages you receive show up in Telegram.
- Reply to one of them in Telegram and the answer goes to the same Rocket.Chat
  chat (and the same thread, if it came from a thread).
- Send new messages to any Rocket.Chat chat from Telegram.
- The bot only listens to you; everyone else is ignored.

See [PLAN.md](PLAN.md) for the design and the security choices.

> **Before you start:** forwarded messages are stored on Telegram's servers.
> Make sure your company allows forwarding work chat to Telegram.

## Requirements

- Linux/macOS with Python 3.10+ and [`uv`](https://docs.astral.sh/uv/) (already installed here).
- A Rocket.Chat account on your company server, with permission to create
  *Personal Access Tokens* (most servers allow it; if not, ask the admin).
- A Telegram account.

Install the dependencies once:

```bash
cd ~/Applications/TeleRocket
uv sync
```

## 1. Create your local config

```bash
cp .env.example .env
chmod 600 .env
```

Open `.env` in an editor. You will fill it in during the next steps.
`.env` is git-ignored, so your secrets never end up in a commit. Never paste
these values into a chat, an issue or a screenshot.

## 2. Connect Rocket.Chat (personal access token)

A personal access token works with any login method (password, SSO, LDAP, 2FA)
and you can revoke it at any time without changing your password.

1. Open your company Rocket.Chat in the browser and sign in as usual.
2. Click your avatar, then **My Account > Personal Access Tokens**
   (or **Profile > Personal Access Tokens** on newer versions).
   If you don't see that menu, the admin hasn't given you the
   *create-personal-access-tokens* permission; ask them.
3. Type a name such as `telerocket` and click **Add**.
   - If your account uses two-factor authentication, tick
     **Ignore Two Factor Authentication**. Otherwise sending messages through
     the API may be refused with a "TOTP required" error. The token then gives
     full access to your account without 2FA, so keep it as safe as a password.
4. Rocket.Chat shows the **Token** and your **User ID** once. Copy them into `.env`:

   ```ini
   ROCKETCHAT_URL=https://chat.yourcompany.com   # the address you use in the browser
   ROCKETCHAT_USER_ID=<User ID>
   ROCKETCHAT_TOKEN=<Token>
   ```

The URL must be `https://`. Rocket.Chat is always reached directly, never
through a proxy (even if `HTTP_PROXY`/`HTTPS_PROXY` are set in your shell).

## 3. Connect Telegram (your own bot)

### If Telegram is blocked on your network: set a proxy first

The bridge can reach Telegram through a proxy while Rocket.Chat stays direct.
Point `TELEGRAM_PROXY_URL` in `.env` at the local proxy your VPN or proxy
client provides (v2rayN/v2rayA, Xray, Clash, Shadowsocks, Psiphon, Tor, and so on).
Look in that app's settings for the local SOCKS or HTTP port:

```ini
TELEGRAM_PROXY_URL=socks5h://127.0.0.1:1080   # SOCKS5, DNS through the proxy (preferred)
# TELEGRAM_PROXY_URL=http://127.0.0.1:8080    # or an HTTP proxy
# TELEGRAM_PROXY_URL=socks5h://user:pass@host:1080   # remote proxy with a login
```

- Use `socks5h://` rather than `socks5://` when you can: it also resolves
  `api.telegram.org` through the proxy, which avoids DNS blocking.
- A username and password in the URL are treated as secrets (kept out of logs).
- MTProto proxies (the `tg://proxy?...` links for the Telegram app) don't work
  here: the Bot API needs a SOCKS5 or HTTP proxy.
- If you use a full-system VPN instead, leave `TELEGRAM_PROXY_URL` empty. But
  check that the VPN doesn't break your access to the Rocket.Chat server; a
  split-tunnel or the proxy setting above avoids that.

`whoami`, `check` and the bridge itself all use this setting.

### Create the bot

1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a
   username ending in `bot`. BotFather replies with a token like
   `123456789:AA...`. Put it in `.env`:

   ```ini
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   ```

2. Optional but recommended, in BotFather:
   `/setjoingroups` > your bot > **Disable** (nobody can add it to groups).
3. Find your numeric Telegram user id. Run this, then send any message to your
   new bot in Telegram (open it via the link BotFather gave you and press **Start**):

   ```bash
   uv run telerocket whoami
   ```

   It prints your id on the terminal (nothing is sent back in Telegram). Put it in `.env`:

   ```ini
   TELEGRAM_OWNER_ID=<your id>
   ```

The bot only reacts to messages from this id in your private chat with it.

## 4. Choose what gets forwarded (optional)

In `.env`:

- `FORWARD_SCOPE=dm_mentions` (default): direct messages, plus channel messages
  that mention you, `@all` or `@here`.
- `FORWARD_SCOPE=all`: every message from every room you are in (can be a lot).
- `IGNORE_ROOMS=general,random`: rooms that are never forwarded.

## 5. Check the setup

```bash
uv run telerocket check
```

You should see something like:

```
✔ Rocket.Chat server reachable at https://chat.yourcompany.com (version 6.x)
✔ Rocket.Chat token valid, logged in as @you
✔ You are in 42 chats
✔ Telegram reachable via proxy socks5h://127.0.0.1:1080, bot token valid: @your_bot
✔ Sent a test message to your Telegram
All good.
```

and a "TeleRocket check" message arrives in Telegram. Each failing line says
what to fix.

## 6. Run the bridge

```bash
uv run telerocket
```

You get "🚀 TeleRocket started" in Telegram. Stop it with `Ctrl+C`.

## How to test it end to end

Ask a colleague to help (or use a second Rocket.Chat account), with the bridge running:

1. **Incoming DM:** the colleague sends you a direct message in Rocket.Chat.
   It appears in Telegram as `👤 Their Name` followed by the text.
2. **Reply:** in Telegram, reply to that message (swipe left, or long press >
   Reply) and type an answer. The bot confirms `✅ Sent to @colleague` and the
   colleague sees the answer from you in Rocket.Chat.
3. **Keep chatting:** after a reply, that chat is your *active chat*; plain
   messages (not replies) go there too.
4. **Mention:** the colleague mentions `@you` in a channel. It appears as
   `💬 #channel · Their Name`. Replying answers in that channel. Messages in
   threads show 🧵 and replies stay in the thread.
5. **New message:** `/chats` lists your recent chats as buttons; tap one, then
   type. Or use `/to @colleague` / `/to #channel`, or a one-off
   `/send @colleague hello`.
6. **Owner lock:** from another Telegram account, message your bot. It does not
   answer, and the terminal logs only `ignored update from non-owner user id …`.
7. **Status:** `/status` shows the connection, forwarding mode and active chat.
   `/pause` and `/resume` turn forwarding off and on.
8. **Reconnect:** turn Wi-Fi off for a couple of minutes. You get a "Lost
   connection" notice after about 90 seconds offline, and "Reconnected" when it
   is back. Messages sent while the bridge was offline are not forwarded later.

Your own messages (from Rocket.Chat or sent through the bot) are never echoed back.

### Automated tests

```bash
uv run pytest
```

The tests use fakes for both services (including a fake Rocket.Chat websocket
server), so they need no credentials or network.

## Run it in the background (start with the laptop)

A systemd user service is included:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/telerocket.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telerocket
```

- Logs: `journalctl --user -u telerocket -f`
- Restart after editing `.env`: `systemctl --user restart telerocket`
- To keep it running even when you are logged out: `sudo loginctl enable-linger $USER`

The service file assumes the project is in `~/Applications/TeleRocket` and
`uv` is at `/snap/bin/uv`; edit it if that changes.

## Security notes

- Secrets live only in `.env` (mode 600, git-ignored). `.env.example` has placeholders only.
- All logs pass through a redaction filter that removes your tokens and user id;
  message text is never logged. HTTP request logs (which would include the bot
  token in the URL) are turned off.
- The bot ignores everyone except `TELEGRAM_OWNER_ID`, and ignores even you in groups.
- The Rocket.Chat token is sent only to `ROCKETCHAT_URL` over HTTPS, directly
  (never through the Telegram proxy); redirects are not followed.
- Telegram traffic through the proxy is still HTTPS end to end, so the proxy
  can't read your bot token or messages; it only sees that you talk to api.telegram.org. The bridge also checks that the token belongs to `ROCKETCHAT_USER_ID`.
- `data/telerocket.sqlite3` (mode 600) stores only message ids, never message text.
- If a token leaks: delete it in Rocket.Chat (*Personal Access Tokens*) or with
  BotFather (`/revoke`), create a new one and update `.env`.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `Rocket.Chat rejected the user id / token` | Token deleted or expired, or User ID copied wrong. Create a new token. |
| `TOTP required` / 2FA errors when sending | Recreate the token with **Ignore Two Factor Authentication** ticked. |
| `cannot reach Rocket.Chat` | Check the URL and whether you need the company VPN. |
| `Cannot reach Telegram` | Telegram is blocked: set `TELEGRAM_PROXY_URL`, or check that your proxy app is running and the port is right. |
| `the bot cannot message you yet` | Open your bot in Telegram and press **Start**; check `TELEGRAM_OWNER_ID`. |
| Channel messages don't arrive | Default mode only forwards mentions; set `FORWARD_SCOPE=all`. |
| `.env is readable by other users` warning | `chmod 600 .env` |
