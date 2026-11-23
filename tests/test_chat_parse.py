from app.chat.ytdlp_source import FileTailer, LineParser
from tests.chat_fixtures import T0_US, gift_line, paid_line, remove_line, text_line


def test_text_message_with_emoji():
    [m] = LineParser().parse(text_line("m1", 1500, text="LETS GO ", emoji="🇺🇸"))
    assert (m.msg_id, m.kind, m.author_id) == ("m1", "text", "UCa")
    assert m.published_us == T0_US + 1_500_000
    assert m.text == "LETS GO 🇺🇸"


def test_custom_emote_uses_shortcut():
    [m] = LineParser().parse(text_line("m1", 10, text="", emoji="speed-laugh"))
    assert m.text == ":speed-laugh:"


def test_paid_message():
    [m] = LineParser().parse(paid_line("p1", 2000))
    assert m.kind == "paid" and m.text == "GG"


def test_gift_dated_from_run_start_once_known():
    p = LineParser()
    assert p.parse(gift_line("g0", 0)) == []          # replayed history: undatable, skipped
    p.parse(text_line("m1", 1000))                     # establishes run t0
    [g] = p.parse(gift_line("g1", 4000))
    assert g.kind == "gift" and g.published_us == T0_US + 4_000_000 and g.author_name == "@gifter"


def test_history_text_does_not_set_t0():
    p = LineParser()
    p.parse(text_line("old", 0, ts_us=T0_US - 3_600_000_000))   # offset 0 = history
    assert p.run_t0_us is None


def test_non_message_actions_ignored():
    assert LineParser().parse(remove_line(500)) == []


def test_tailer_returns_only_complete_lines(tmp_path):
    f = tmp_path / "chat.part"
    t = FileTailer(f)
    assert t.read_lines() == []                      # file not created yet
    f.write_bytes(b'{"a":1}\n{"b":')
    assert t.read_lines() == ['{"a":1}']
    with open(f, "ab") as fh:
        fh.write(b'2}\n')
    assert t.read_lines() == ['{"b":2}']
    assert t.read_lines() == []
