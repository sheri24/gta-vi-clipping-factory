import logging
from types import SimpleNamespace

import pytest

from app.dashboard import clip_action, overview
from app.db import utcnow
from app.logsafe import SecretFilter, redact, scrub_file, tail

FAKE_KEY = "AIza" + "x" * 35


def test_redacts_api_keys_and_tokens():
    url = f"HTTP Request: GET https://www.googleapis.com/youtube/v3/videos?part=snippet&key={FAKE_KEY}&id=a"
    assert FAKE_KEY not in redact(url) and "key=REDACTED&id=a" in redact(url)
    assert "ya29" not in redact("Authorization: Bearer ya29.a0AfB_secret")
    assert "s3cret" not in redact('{"refresh_token": "s3cret", "x": 1}')


def test_filter_scrubs_formatted_log_records(tmp_path):
    path = tmp_path / "app.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.addFilter(SecretFilter())
    log = logging.getLogger("test.secret")
    log.addHandler(handler)
    log.warning("GET %s", f"https://x/videos?key={FAKE_KEY}")
    handler.close()
    log.removeHandler(handler)
    assert FAKE_KEY not in path.read_text(encoding="utf-8")


def test_tail_and_scrub_old_log(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("".join(f"line {i} key={FAKE_KEY}\n" for i in range(5000)), encoding="utf-8")
    last = tail(path, 3, chunk=100)
    assert [l.split(" key")[0] for l in last] == ["line 4997", "line 4998", "line 4999"]
    assert all(FAKE_KEY not in l for l in last)
    assert scrub_file(path) == 5000 and FAKE_KEY not in path.read_text(encoding="utf-8")


def add_clip(db, status, final=True, vertical=True, source=True, transcript=True):
    if not db.one("SELECT 1 FROM channels"):
        db.execute("INSERT INTO channels (channel_id, title, source_url, created_at) VALUES ('UC1','Lacy','u',?)",
                   (utcnow(),))
        db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','live',?)",
                   (utcnow(),))
    cid = db.execute(
        """INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc, status,
                              final_path, vertical_path, source_path, created_at, updated_at, error)
           VALUES (1, 0, 40, 0, 40, ?, ?, ?, ?, ?, ?, 'held')""",
        (status, "f.mp4" if final else None, "v.mp4" if vertical else None, "s.ts" if source else None,
         utcnow(), utcnow())).lastrowid
    if transcript:
        db.execute("INSERT INTO transcripts (clip_id, text, words, model, created_at) VALUES (?, '', '[]', 'x', ?)",
                   (cid, utcnow()))
    return cid


def test_resume_picks_the_first_unfinished_stage(db):
    assert clip_action(db, add_clip(db, "on_hold", final=False, transcript=False), "requeue")["status"] == "cut"
    assert clip_action(db, add_clip(db, "failed", final=False, source=False), "requeue")["status"] == "pending"
    row = clip_action(db, add_clip(db, "rejected"), "requeue")
    assert row["status"] == "transcribed" and row["error"] is None and row["attempts"] == 0


def test_approving_a_rejected_clip_renders_it_first(db):
    assert clip_action(db, add_clip(db, "rejected", final=False), "approve")["status"] == "passed"
    assert clip_action(db, add_clip(db, "rejected"), "approve")["status"] == "approved"   # already rendered
    gone = add_clip(db, "rejected", final=False, source=False)
    db.execute("UPDATE clips SET files_deleted_at = ? WHERE id = ?", (utcnow(), gone))
    with pytest.raises(ValueError, match="deleted"):
        clip_action(db, gone, "approve")


def test_manual_approve_needs_a_transcript_and_reject_cancels_queue(db):
    with pytest.raises(ValueError, match="transcribed"):
        clip_action(db, add_clip(db, "on_hold", final=False, transcript=False), "approve")
    cid = add_clip(db, "approved")
    db.execute("INSERT INTO publish_queue (clip_id, queued_at, updated_at) VALUES (?, ?, ?)", (cid, utcnow(), utcnow()))
    assert clip_action(db, cid, "reject")["status"] == "rejected"
    assert db.one("SELECT status FROM publish_queue")["status"] == "cancelled"


def test_published_and_busy_clips_are_locked(db):
    for status in ("published", "captioning"):
        with pytest.raises(ValueError):
            clip_action(db, add_clip(db, status), "reject")
    with pytest.raises(LookupError):
        clip_action(db, 999, "approve")


class _Alive:
    def __init__(self, session_id, name="ytdlp"):
        self.session, self.source = {"id": session_id}, SimpleNamespace(name=name)

    def is_alive(self):
        return True


def test_overview_shows_live_channel_with_trace_and_spike(db, tmp_path):
    add_clip(db, "approved")
    db.execute("INSERT INTO chat_buckets VALUES (1, 1000, 30, 20, 0, 0, 0), (1, 1010, 90, 70, 0, 0, 0)")
    db.execute("""INSERT INTO spikes (session_id, start_bucket, peak_bucket, end_bucket, peak_authors, peak_messages,
                  baseline_authors, z_score, ratio, chat_delay_s, event_utc, clip_start_utc, clip_end_utc,
                  detected_at, updated_at, clip_id) VALUES (1, 1010, 1010, 1010, 70, 90, 20, 6, 3.5, 40, 970, 955,
                  995, ?, ?, 1)""", (utcnow(), utcnow()))
    settings = SimpleNamespace(recordings_dir=tmp_path, clips_dir=tmp_path / "clips", clip_purge_after_publish=True,
                               clip_purge_grace_minutes=60, min_free_gb=10, buffer_minutes=30, poll_interval_seconds=60,
                               youtube_token_path=tmp_path / "token.json", chat_bucket_seconds=10,
                               vod_enabled=True, vod_scan_minutes=30, vod_max_age_hours=72, vod_max_clips=6)
    queue = SimpleNamespace(enabled=False, privacy="private", max_per_day=6, why_not_now=lambda: "disabled",
                            published_today=lambda now: 0)
    services = {"settings": settings, "db": db, "api": SimpleNamespace(api_key="k"),
                "ledger": SimpleNamespace(used=lambda: 12, daily_budget=9000), "publish_queue": queue,
                "recorders": SimpleNamespace(recorders={1: _Alive(1)}),
                "chat": SimpleNamespace(collectors={1: _Alive(1)}), "vod_paused": lambda: None}
    o = overview(services)
    [card] = o["channels"]
    assert card["session"]["recording"] and card["session"]["chat_source"] == "ytdlp"
    assert card["trace"]["points"] == [[1000, 20, 30], [1010, 70, 90]]
    assert card["spikes"][0]["clip_id"] == 1 and card["spikes"][0]["event_utc"] == 970
    assert o["clip_counts"] == {"approved": 1} and o["publishing"]["youtube"]["signed_in"] is False


def test_review_again_with_a_note_saves_it_and_requeues(db):
    cid = add_clip(db, "rejected")
    row = clip_action(db, cid, "requeue", note="  GTA 6 pause menu, then the post-credits scene ")
    assert row["status"] == "transcribed" and row["review_note"] == "GTA 6 pause menu, then the post-credits scene"
    assert clip_action(db, cid, "requeue")["review_note"] == row["review_note"]   # plain resume keeps it
