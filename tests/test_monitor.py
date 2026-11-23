from app.db import utcnow
from app.monitor import Monitor, classify, next_session_status
from tests.conftest import video_item

CH = "UCWsDFcIhY2DBi3GB5uykGXA"


def add_channel(db, channel_id=CH):
    db.execute(
        "INSERT INTO channels (channel_id, title, source_url, created_at) VALUES (?, 'x', 'u', ?)",
        (channel_id, utcnow()),
    )


def make_monitor(db, api, web):
    return Monitor(db, api, web, live_page_probe=True)


# -- classify ------------------------------------------------------------------

def test_classify():
    assert classify(None) == "missing"
    assert classify(video_item("a", CH, "live")) == "live"
    assert classify(video_item("a", CH, "upcoming")) == "upcoming"
    assert classify(video_item("a", CH, "none")) == "none"
    assert classify(video_item("a", CH, "live", ended=True)) == "none"


# -- next_session_status: only the unambiguous cases are pinned here -------------

def test_still_live_resets_missed_checks():
    assert next_session_status(2, video_item("a", CH, "live")) == ("live", 0)


def test_actual_end_time_ends_immediately():
    status, _ = next_session_status(0, video_item("a", CH, "none", ended=True))
    assert status == "ended"


def test_repeated_missing_eventually_ends():
    missed, status = 0, "live"
    for _ in range(20):
        status, missed = next_session_status(missed, None)
        if status == "ended":
            break
    assert status == "ended"


# -- monitor end-to-end with fakes -------------------------------------------------

def test_detects_live_stream_from_rss_and_opens_session(db, api, web, fake_yt):
    add_channel(db)
    web.rss[CH] = ["old_vod_001", "live_vid_01"]
    fake_yt.videos["old_vod_001"] = video_item("old_vod_001", CH, "none")
    fake_yt.videos["live_vid_01"] = video_item("live_vid_01", CH, "live")
    m = make_monitor(db, api, web)
    started = []
    m.on_started.append(started.append)

    r = m.poll()

    assert r.started == ["live_vid_01"]
    assert [s["video_id"] for s in started] == ["live_vid_01"]
    s = db.one("SELECT * FROM live_sessions WHERE video_id = 'live_vid_01'")
    assert s["status"] == "live" and s["live_chat_id"] == "chat-1"


def test_live_page_probe_catches_stream_missing_from_rss(db, api, web, fake_yt):
    add_channel(db)
    web.rss[CH] = []
    web.live_page[CH] = "probe_vid_1"
    fake_yt.videos["probe_vid_1"] = video_item("probe_vid_1", CH, "live")
    assert make_monitor(db, api, web).poll().started == ["probe_vid_1"]


def test_no_duplicate_session_or_notification_across_polls(db, api, web, fake_yt):
    add_channel(db)
    web.rss[CH] = ["live_vid_01"]
    fake_yt.videos["live_vid_01"] = video_item("live_vid_01", CH, "live")
    m = make_monitor(db, api, web)
    started = []
    m.on_started.append(started.append)
    m.poll(); m.poll(); m.poll()
    assert len(started) == 1
    assert db.one("SELECT COUNT(*) n FROM live_sessions")["n"] == 1


def test_finished_vods_are_not_requeried(db, api, web, fake_yt):
    add_channel(db)
    web.rss[CH] = [f"vod_{i:07d}" for i in range(15)]
    for v in web.rss[CH]:
        fake_yt.videos[v] = video_item(v, CH, "none")
    m = make_monitor(db, api, web)
    m.poll()
    assert m.poll().candidates == 0
    assert api.ledger.used() == 1  # one batched videos.list in total


def test_stream_end_closes_session_and_notifies(db, api, web, fake_yt):
    add_channel(db)
    web.rss[CH] = ["live_vid_01"]
    fake_yt.videos["live_vid_01"] = video_item("live_vid_01", CH, "live")
    m = make_monitor(db, api, web)
    ended = []
    m.on_ended.append(ended.append)
    m.poll()
    fake_yt.videos["live_vid_01"] = video_item("live_vid_01", CH, "none", ended=True)
    r = m.poll()
    assert r.ended == ["live_vid_01"] and len(ended) == 1
    s = db.one("SELECT * FROM live_sessions")
    assert s["status"] == "ended" and s["actual_end_at"] == "2026-11-22T19:00:00Z"


def test_quota_exhaustion_does_not_end_live_sessions(db, api, web, fake_yt):
    add_channel(db)
    web.rss[CH] = ["live_vid_01"]
    fake_yt.videos["live_vid_01"] = video_item("live_vid_01", CH, "live")
    m = make_monitor(db, api, web)
    m.poll()
    api.ledger.mark_exhausted()
    r = m.poll()
    assert r.errors and r.ended == []
    assert db.one("SELECT status FROM live_sessions")["status"] == "live"


def test_rss_failure_is_logged_and_probe_still_used(db, api, web, fake_yt):
    add_channel(db)
    web.fail_rss = True
    web.live_page[CH] = "probe_vid_1"
    fake_yt.videos["probe_vid_1"] = video_item("probe_vid_1", CH, "live")
    r = make_monitor(db, api, web).poll()
    assert r.started == ["probe_vid_1"]
    assert db.one("SELECT COUNT(*) n FROM errors WHERE component = 'monitor.rss'")["n"] == 1


def test_failing_listener_does_not_break_monitor(db, api, web, fake_yt):
    add_channel(db)
    web.rss[CH] = ["live_vid_01"]
    fake_yt.videos["live_vid_01"] = video_item("live_vid_01", CH, "live")
    m = make_monitor(db, api, web)
    m.on_started.append(lambda s: 1 / 0)
    assert m.poll().started == ["live_vid_01"]
    assert db.one("SELECT COUNT(*) n FROM errors WHERE component = 'monitor.listener'")["n"] == 1


def test_disabled_channels_are_skipped(db, api, web, fake_yt):
    add_channel(db)
    db.execute("UPDATE channels SET enabled = 0")
    web.rss[CH] = ["live_vid_01"]
    fake_yt.videos["live_vid_01"] = video_item("live_vid_01", CH, "live")
    assert make_monitor(db, api, web).poll().started == []


def test_ended_session_is_reopened_when_youtube_says_still_live(db, api, web, fake_yt):
    """Removing and re-adding a channel mid-stream (or a long outage) marks a session ended while the
    stream goes on; it must come back to life, notify listeners once, and be cleanable again later."""
    add_channel(db)
    web.rss[CH] = ["live_vid_01"]
    fake_yt.videos["live_vid_01"] = video_item("live_vid_01", CH, "live")
    db.execute("""INSERT INTO live_sessions (video_id, channel_id, status, detected_at, ended_at, storage_cleaned_at)
                  VALUES ('live_vid_01', ?, 'ended', '2026-11-22T22:00:00+00:00', '2026-11-23T00:59:52+00:00',
                          '2026-11-23T01:14:53+00:00')""", (CH,))
    m = make_monitor(db, api, web)
    started = []
    m.on_started.append(started.append)

    assert m.poll().started == ["live_vid_01"]
    s = db.one("SELECT * FROM live_sessions WHERE video_id = 'live_vid_01'")
    assert s["status"] == "live" and s["ended_at"] is None and s["storage_cleaned_at"] is None
    assert s["detected_at"] == "2026-11-22T22:00:00+00:00"            # same session, not a new one
    assert m.poll().started == [] and len(started) == 1                # no repeat notification
    assert db.one("SELECT COUNT(*) n FROM live_sessions")["n"] == 1


def test_quiet_poll_still_counts_as_a_check(db, api, web, fake_yt):
    """No new videos means nothing to ask the API about, but the channel was still checked; the
    dashboard's "checked 2 min ago" must not freeze at the last time a video showed up."""
    add_channel(db)
    web.rss[CH] = ["old_vod_001"]
    fake_yt.videos["old_vod_001"] = video_item("old_vod_001", CH, "none")
    m = make_monitor(db, api, web)
    m.poll()
    db.execute("UPDATE channels SET last_checked_at = '2026-01-01T00:00:00+00:00'")
    assert m.poll().candidates == 0
    assert db.one("SELECT last_checked_at FROM channels")["last_checked_at"] > "2026-01-01T00:00:00+00:00"


def test_offline_poll_does_not_count_as_a_check(db, api, web, fake_yt):
    add_channel(db)
    db.execute("UPDATE channels SET last_checked_at = '2026-01-01T00:00:00+00:00'")
    web.fail_rss = web.fail_live_page = True
    make_monitor(db, api, web).poll()
    assert db.one("SELECT last_checked_at FROM channels")["last_checked_at"] == "2026-01-01T00:00:00+00:00"
