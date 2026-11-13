"""Local SQLite store.

Holds only ids (never message text):
- which Telegram message corresponds to which Rocket.Chat room/thread,
  so replies work after a restart;
- which Rocket.Chat messages were already forwarded (to avoid duplicates
  when Rocket.Chat re-broadcasts a message after a reaction or thread reply);
- small settings such as the active chat and the paused flag.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

RETENTION_SECONDS = 60 * 60 * 24 * 60  # 60 days


@dataclass(frozen=True)
class ReplyTarget:
    rid: str
    tmid: str | None
    room_label: str


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            path = Path(path)
            if not path.parent.exists():
                path.parent.mkdir(parents=True, mode=0o700)
            if not path.exists():
                fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            os.chmod(path, 0o600)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS forwarded (
                tg_message_id INTEGER PRIMARY KEY,
                rid TEXT NOT NULL,
                tmid TEXT,
                room_label TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen (
                rc_message_id TEXT PRIMARY KEY,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- dedupe -----------------------------------------------------------
    def mark_seen(self, rc_message_id: str) -> bool:
        """Record a Rocket.Chat message id. Returns False if it was already seen."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO seen (rc_message_id, created) VALUES (?, ?)",
            (rc_message_id, time.time()),
        )
        self._db.commit()
        return cur.rowcount == 1

    def unmark_seen(self, rc_message_id: str) -> None:
        self._db.execute("DELETE FROM seen WHERE rc_message_id = ?", (rc_message_id,))
        self._db.commit()

    # --- reply mapping ----------------------------------------------------
    def remember(self, tg_message_id: int, target: ReplyTarget) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO forwarded (tg_message_id, rid, tmid, room_label, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (tg_message_id, target.rid, target.tmid, target.room_label, time.time()),
        )
        self._db.commit()

    def lookup(self, tg_message_id: int) -> ReplyTarget | None:
        row = self._db.execute(
            "SELECT rid, tmid, room_label FROM forwarded WHERE tg_message_id = ?",
            (tg_message_id,),
        ).fetchone()
        return ReplyTarget(row[0], row[1], row[2]) if row else None

    # --- settings ---------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self._db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        self._db.commit()

    # --- housekeeping -----------------------------------------------------
    def prune(self, older_than: float = RETENTION_SECONDS) -> None:
        cutoff = time.time() - older_than
        self._db.execute("DELETE FROM forwarded WHERE created < ?", (cutoff,))
        self._db.execute("DELETE FROM seen WHERE created < ?", (cutoff,))
        self._db.commit()
