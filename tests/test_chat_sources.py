import sys
import textwrap
import threading
from types import SimpleNamespace

import grpc

from app.chat.collector import ChatCollector
from app.chat.official_source import OfficialChatSource
from app.chat.store import ChatStore
from app.chat.ytdlp_source import YtDlpChatSource
from app.db import utcnow
from app.youtube.api import QuotaLedger
from app.youtube.proto import stream_list_pb2 as pb
from tests.chat_fixtures import gift_line, text_line


def make_session(db, chat_id="chat-1"):
    sid = db.execute(
        "INSERT INTO live_sessions (video_id, channel_id, status, detected_at, live_chat_id) VALUES ('vidX', 'UC', 'live', ?, ?)",
        (utcnow(), chat_id)).lastrowid
    return dict(db.one("SELECT * FROM live_sessions WHERE id = ?", (sid,)))


# -- yt-dlp source with a fake yt-dlp process ------------------------------------------

def test_ytdlp_source_tails_growing_file_and_cleans_up(db, tmp_path):
    lines = [text_line(f"m{i}", 1000 + i * 300) for i in range(5)] + [gift_line("g1", 3000)]
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(textwrap.dedent(f"""
        import sys, time
        out = sys.argv[1] + ".live_chat.json.part"
        lines = {lines!r}
        for i, ln in enumerate(lines):
            with open(out, "a", encoding="utf-8") as f:
                f.write(ln + "\\n")
            time.sleep(0.2)
    """))
    work = tmp_path / "chat"
    src = YtDlpChatSource("unused", work, poll_seconds=0.1,
                          cmd_builder=lambda s, out: [sys.executable, str(script), str(out)])
    session = make_session(db)
    result, added, error = src.run(session, ChatStore(db), threading.Event())

    assert (result, added, error) == ("exited", 6, None)
    kinds = [r["kind"] for r in db.query("SELECT kind FROM chat_messages ORDER BY published_us")]
    assert kinds.count("text") == 5 and kinds.count("gift") == 1
    assert list((work / "vidX").iterdir()) == []      # temp chat files removed


def test_ytdlp_source_reports_failure(db, tmp_path):
    src = YtDlpChatSource("unused", tmp_path, poll_seconds=0.05, cmd_builder=lambda s, out: [
        sys.executable, "-c", "import sys; sys.stderr.write('ERROR: This live event has ended'); sys.exit(1)"])
    result, added, error = src.run(make_session(db), ChatStore(db), threading.Event())
    assert result == "exited" and added == 0 and "live event has ended" in error


# -- official source with a fake gRPC stub -------------------------------------------------

def item(i, sec):
    return pb.LiveChatMessage(id=f"o{i}", snippet=pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.Type.Value("TEXT_MESSAGE_EVENT"),
        published_at=f"2026-11-22T21:00:{sec:02d}.000000+00:00", author_channel_id=f"UC{i}",
        display_message="yo"))


class FakeStub:
    """Each StreamList call = one ~10 s connection; scripted responses per connection."""

    def __init__(self, connections):
        self.connections = list(connections)
        self.tokens_seen = []

    def StreamList(self, req, metadata, timeout):
        self.tokens_seen.append(req.page_token or None)
        assert dict(metadata)["x-goog-api-key"] == "key"
        conn = self.connections.pop(0)
        if isinstance(conn, Exception):
            raise conn
        yield from conn


class FakeRpcError(grpc.RpcError):
    def __init__(self, code, details):
        self._code, self._details = code, details

    def code(self):
        return self._code

    def details(self):
        return self._details


def test_official_source_resumes_with_token_and_charges_per_connection(db):
    stub = FakeStub([
        [pb.LiveChatMessageListResponse(items=[item(1, 1), item(2, 2)], next_page_token="t1")],
        [pb.LiveChatMessageListResponse(items=[item(2, 2), item(3, 3)], next_page_token="t2")],  # overlap
        [pb.LiveChatMessageListResponse(items=[], next_page_token="t3", offline_at="2026-11-22T22:00:00Z")],
    ])
    ledger = QuotaLedger(db, daily_budget=1000)
    result, added, error = OfficialChatSource("key", ledger, stub).run(make_session(db), ChatStore(db), threading.Event())
    assert (result, added, error) == ("ended", 3, None)
    assert stub.tokens_seen == [None, "t1", "t2"]
    assert ledger.used() == 15


def test_official_source_stops_on_local_budget(db):
    ledger = QuotaLedger(db, daily_budget=7)
    stub = FakeStub([[pb.LiveChatMessageListResponse(items=[item(1, 1)], next_page_token="t")]] * 5)
    result, added, _ = OfficialChatSource("key", ledger, stub).run(make_session(db), ChatStore(db), threading.Event())
    assert result == "quota" and added == 1 and ledger.used() == 5


def test_official_source_surfaces_permission_errors(db):
    stub = FakeStub([FakeRpcError(grpc.StatusCode.PERMISSION_DENIED, "chat disabled")])
    result, _, error = OfficialChatSource("key", QuotaLedger(db, 100), stub).run(
        make_session(db), ChatStore(db), threading.Event())
    assert result == "error" and "PERMISSION_DENIED" in error


# -- collector fallback ------------------------------------------------------------------------

class ScriptedSource:
    def __init__(self, name, results, is_available=True):
        self.name, self.results, self.calls, self.is_available = name, list(results), 0, is_available

    def available(self):
        return self.is_available

    def run(self, session, store, stop):
        self.calls += 1
        return self.results.pop(0) if self.results else ("exited", 0, "boom")


def run_collector(db, sources, live_checks):
    it = iter([True] * live_checks)
    c = ChatCollector(db, ChatStore(db), make_session(db), sources, is_live=lambda: next(it, False),
                      healthy_seconds=999)
    c.stop_event.wait = lambda t=None: False   # no real backoff sleeps in tests
    c.run()
    return c


def test_switches_source_after_three_quick_failures(db):
    a = ScriptedSource("ytdlp", [("exited", 0, "e")] * 3)
    b = ScriptedSource("official", [("stopped", 10, None)])
    run_collector(db, [a, b], live_checks=4)
    assert (a.calls, b.calls) == (3, 1)
    assert [r["source"] for r in db.query("SELECT source FROM chat_runs ORDER BY id")] == ["ytdlp"] * 3 + ["official"]


def test_quota_result_switches_immediately(db):
    a = ScriptedSource("official", [("quota", 5, "budget")])
    b = ScriptedSource("ytdlp", [("stopped", 1, None)])
    run_collector(db, [a, b], live_checks=2)
    assert (a.calls, b.calls) == (1, 1)


def test_unavailable_source_is_skipped(db):
    a = ScriptedSource("official", [], is_available=False)
    b = ScriptedSource("ytdlp", [("stopped", 1, None)])
    run_collector(db, [a, b], live_checks=2)
    assert (a.calls, b.calls) == (0, 1)


def test_fallback_source_hands_back_to_primary_after_retry_window(db):
    """After an outage we may land on the quota-costing official source, which loops internally;
    the collector must interrupt it and try the free primary again."""
    class LoopsUntilStopped:
        name, calls = "official", 0

        def run(self, session, store, stop):
            self.calls += 1
            assert stop.wait(5), "collector never interrupted the fallback"
            return "stopped", 3, None

    primary = ScriptedSource("ytdlp", [("exited", 0, "net down")] * 3 + [("stopped", 50, None)])
    fallback = LoopsUntilStopped()
    it = iter([True] * 5)
    c = ChatCollector(db, ChatStore(db), make_session(db), [primary, fallback], is_live=lambda: next(it, False),
                      healthy_seconds=999, primary_retry_seconds=0.05)
    c.stop_event.wait = lambda t=None: False
    c.run()
    assert fallback.calls == 1 and primary.calls == 4
    assert [r["source"] for r in db.query("SELECT source FROM chat_runs ORDER BY id")] == \
        ["ytdlp"] * 3 + ["official", "ytdlp"]


def test_monitor_leaves_sessions_alone_when_internet_is_down(db, api, web, fake_yt):
    import httpx as _httpx
    from app.db import utcnow
    from app.monitor import Monitor
    db.execute("INSERT INTO channels (channel_id, title, source_url, created_at) VALUES ('UC1','x','u',?)", (utcnow(),))
    db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v1','UC1','live',?)",
               (utcnow(),))

    def offline(*a, **k):
        raise _httpx.ConnectError("getaddrinfo failed")
    api.get_videos = offline
    web.fail_rss = True
    m = Monitor(db, api, web)
    for _ in range(5):                                   # 5 minutes offline
        m.poll()
    row = db.one("SELECT status, missed_checks FROM live_sessions")
    assert row["status"] == "live" and row["missed_checks"] == 0
