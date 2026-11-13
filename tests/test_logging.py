import logging

from telerocket.logging_setup import RedactingFilter, setup_logging

from .conftest import BOT_TOKEN


def _record(msg, *args, exc_info=None):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, exc_info)


def test_redacts_known_secrets():
    f = RedactingFilter(["supersecret", "", "ab"])
    rec = _record("token=%s and %s", "supersecret", "ok")
    f.filter(rec)
    assert rec.getMessage() == "token=*** and ok"


def test_redacts_unknown_bot_token_shape():
    f = RedactingFilter([])
    rec = _record("POST https://api.telegram.org/bot%s/getUpdates", BOT_TOKEN)
    f.filter(rec)
    assert BOT_TOKEN not in rec.getMessage()
    assert "bot***/getUpdates" in rec.getMessage()


def test_redacts_tracebacks():
    f = RedactingFilter(["supersecret"])
    try:
        raise RuntimeError("failed with supersecret")
    except RuntimeError:
        import sys

        rec = _record("boom", exc_info=sys.exc_info())
    f.filter(rec)
    assert "supersecret" not in rec.getMessage()
    assert "RuntimeError" in rec.getMessage()
    assert rec.exc_info is None


def test_setup_logging_end_to_end(capsys):
    setup_logging("INFO", ["supersecret"])
    logging.getLogger("x").info("value supersecret here")
    logging.getLogger("httpx").info("HTTP Request: https://api.telegram.org/bot%s", BOT_TOKEN)
    err = capsys.readouterr().err
    assert "value *** here" in err
    assert "supersecret" not in err
    assert BOT_TOKEN not in err
