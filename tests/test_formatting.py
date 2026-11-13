import pytest

from telerocket.formatting import Identity, format_message, should_forward, split_html

ME = Identity("me123", "amir")


def msg(**kw):
    base = {"_id": "m1", "rid": "room1", "msg": "hi", "u": {"_id": "u2", "username": "sara", "name": "Sara K"}}
    base.update(kw)
    return base


DM = {"roomType": "d", "roomName": "sara"}
CHAN = {"roomType": "c", "roomName": "general"}


@pytest.mark.parametrize(
    "m, meta, scope, expected",
    [
        (msg(), DM, "dm_mentions", True),
        (msg(), CHAN, "dm_mentions", False),
        (msg(), CHAN, "all", True),
        (msg(mentions=[{"_id": "me123", "username": "amir"}]), CHAN, "dm_mentions", True),
        (msg(mentions=[{"_id": "x", "username": "here"}]), CHAN, "dm_mentions", True),
        (msg(mentions=[{"_id": "x", "username": "bob"}]), CHAN, "dm_mentions", False),
        (msg(u={"_id": "me123"}), DM, "all", False),  # own message
        (msg(t="uj"), CHAN, "all", False),  # system message
        (msg(editedAt={"$date": 1}), DM, "all", False),
    ],
)
def test_should_forward(m, meta, scope, expected):
    assert should_forward(m, meta, ME, scope=scope) is expected


def test_ignore_rooms():
    assert not should_forward(msg(), {"roomType": "c", "roomName": "General"}, ME,
                              scope="all", ignore_rooms={"general"})


def test_dm_detected_from_rid_without_meta():
    assert should_forward(msg(rid="me123u2"), {}, ME, scope="dm_mentions")


def test_format_dm_escapes_html():
    out = format_message(msg(msg="<b>x</b> & y", u={"_id": "u2", "name": "A<B"}), DM, ME)
    assert out == ["👤 <b>A&lt;B</b>\n&lt;b&gt;x&lt;/b&gt; &amp; y"]


def test_format_channel_thread_and_private():
    out = format_message(msg(tmid="t1"), {"roomType": "p", "roomName": "team"}, ME)
    assert out[0].startswith("🔒 <b>#team</b> · Sara K 🧵\n")


def test_format_attachments():
    out = format_message(msg(msg="", file={"name": "report.pdf"},
                             attachments=[{"title": "report.pdf"}, {"text": "quoted"}]), DM, ME)
    assert "📎 report.pdf" in out[0]
    assert out[0].count("report.pdf") == 1
    assert "<i>quoted</i>" in out[0]


def test_format_empty():
    assert "(empty message)" in format_message(msg(msg=""), DM, ME)[0]


def test_long_message_is_split_under_limit():
    out = format_message(msg(msg="a & b " * 2000), DM, ME, limit=4096)
    assert len(out) > 1
    assert all(len(chunk) <= 4096 for chunk in out)
    assert out[0].startswith("👤 <b>Sara K</b> (1/")
    for chunk in out:  # no broken entities
        body = chunk.split("\n", 1)[1]
        assert body.count("&") == body.count("&amp;")


def test_split_html_does_not_cut_entities():
    text = "x" * 9 + "&amp;" + "y" * 10
    chunks = split_html(text, 12)
    assert "".join(chunks) == text
    assert all("&" not in c or "&amp;" in c for c in chunks)
