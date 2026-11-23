from app.chat.store import ChatMessage, ChatStore
from app.db import utcnow

BASE = 1_795_382_700 * 1_000_000   # a bucket boundary (multiple of 10 s)


def session(db):
    return db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v', 'UC', 'live', ?)",
                      (utcnow(),)).lastrowid


def msg(i, sec, kind="text", author=None):
    return ChatMessage(f"id{i}", BASE + int(sec * 1_000_000), kind, author or f"UC{i}")


def test_duplicates_ignored_across_sources(db):
    sid, store = session(db), ChatStore(db)
    assert store.add(sid, [msg(1, 1), msg(2, 2)], "ytdlp") == 2
    assert store.add(sid, [msg(2, 2), msg(3, 3)], "official") == 1
    assert db.one("SELECT COUNT(*) n FROM chat_messages")["n"] == 3


def test_buckets_count_by_youtube_time(db):
    sid, store = session(db), ChatStore(db, bucket_seconds=10)
    store.add(sid, [
        msg(1, 0.5, author="UCa"), msg(2, 3, author="UCa"), msg(3, 9.9, author="UCb"),   # bucket 0
        msg(4, 10.0, "paid", "UCc"), msg(5, 12, "gift", None), msg(6, 15, "membership", "UCd"),  # bucket 1
    ], "ytdlp")
    store.aggregate(sid)
    b0, b1 = store.buckets(sid)
    assert b0["bucket_start"] == BASE // 1_000_000 and b1["bucket_start"] == b0["bucket_start"] + 10
    assert (b0["messages"], b0["unique_authors"]) == (3, 2)
    # gifts and memberships are tracked but don't inflate the message count
    assert (b1["messages"], b1["paid"], b1["gifts"], b1["memberships"]) == (1, 1, 1, 1)


def test_late_message_updates_existing_bucket(db):
    sid, store = session(db), ChatStore(db)
    store.add(sid, [msg(1, 1), msg(2, 25)], "ytdlp")
    store.aggregate(sid)
    store.add(sid, [msg(3, 2)], "official")          # arrived late, belongs to bucket 0
    store.aggregate(sid)
    assert [b["messages"] for b in store.buckets(sid)] == [2, 1]


def test_delete_session_messages_keeps_buckets(db):
    sid, store = session(db), ChatStore(db)
    store.add(sid, [msg(1, 1)], "ytdlp")
    store.aggregate(sid)
    store.delete_session_messages(sid)
    assert db.one("SELECT COUNT(*) n FROM chat_messages")["n"] == 0
    assert len(store.buckets(sid)) == 1
