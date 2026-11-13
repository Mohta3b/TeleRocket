"""Logging setup with secret redaction.

Every log record passes through ``RedactingFilter``, which replaces known
secret values with ``***``. Chatty HTTP loggers (which print request URLs that
contain the Telegram bot token) are lowered to WARNING.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "***"

# Telegram bot tokens look like 123456789:AA... ; catch them even if unknown.
_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")

# user:password@ inside any URL (e.g. a proxy address).
_URL_CREDS_RE = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@")

_NOISY_LOGGERS = ("httpx", "httpcore", "telegram", "websockets", "apscheduler")


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        # Longest first so a secret containing another is fully replaced.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _URL_CREDS_RE.sub(REDACTED + "@", text)
        return _BOT_TOKEN_RE.sub(REDACTED, text)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self.redact(message)
        if record.exc_info:
            # Format the traceback now so it can be redacted as well.
            formatter = logging.Formatter()
            redacted = f"{redacted}\n{self.redact(formatter.formatException(record.exc_info))}"
            record.exc_info = None
            record.exc_text = None
        if record.stack_info:
            redacted = f"{redacted}\n{self.redact(record.stack_info)}"
            record.stack_info = None
        record.msg = redacted
        record.args = None
        return True


def setup_logging(level: str, secrets: Iterable[str]) -> RedactingFilter:
    """Configure the root logger to write redacted logs to stderr."""
    redactor = RedactingFilter(secrets)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(redactor)

    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return redactor
