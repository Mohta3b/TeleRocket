import os
import stat

from telerocket.store import ReplyTarget, Store


def test_file_permissions(tmp_path):
    path = tmp_path / "data" / "state.sqlite3"
    Store(path).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_mark_seen_dedupes():
    s = Store(":memory:")
    assert s.mark_seen("m1") is True
    assert s.mark_seen("m1") is False
    s.unmark_seen("m1")
    assert s.mark_seen("m1") is True


def test_reply_mapping_survives_restart(tmp_path):
    path = tmp_path / "s.sqlite3"
    s = Store(path)
    s.remember(100, ReplyTarget("r1", "t1", "#general"))
    s.close()
    s = Store(path)
    assert s.lookup(100) == ReplyTarget("r1", "t1", "#general")
    assert s.lookup(101) is None


def test_settings():
    s = Store(":memory:")
    assert s.get("x", "d") == "d"
    s.set("x", "1")
    assert s.get("x") == "1"
    s.set("x", None)
    assert s.get("x") is None


def test_prune():
    s = Store(":memory:")
    s.remember(1, ReplyTarget("r", None, ""))
    s.mark_seen("m")
    s.prune(older_than=-1)
    assert s.lookup(1) is None
    assert s.mark_seen("m") is True
