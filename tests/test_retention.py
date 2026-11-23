from datetime import datetime, timedelta, timezone

from app.db import utcnow
from app.recording.retention import cleanup_ended_sessions, enforce_rolling_buffer

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def make_session(db, video_id, status="live", ended_minutes_ago=None):
    ended = iso(NOW - timedelta(minutes=ended_minutes_ago)) if ended_minutes_ago is not None else None
    return db.execute(
        """INSERT INTO live_sessions (video_id, channel_id, status, detected_at, ended_at)
           VALUES (?, 'UCx', ?, ?, ?)""", (video_id, status, iso(NOW), ended)).lastrowid


def make_segment(db, tmp_path, session_id, seq, age_minutes, pinned=0):
    d = tmp_path / "recordings" / f"vid{session_id}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"seg_{seq:06d}.ts"
    f.write_bytes(b"x" * 1000)
    db.execute(
        """INSERT INTO segments (session_id, seq, path, pts_start, duration, size_bytes, created_at, pinned)
           VALUES (?, ?, ?, ?, 60, 1000, ?, ?)""",
        (session_id, seq, str(f), seq * 60.0, iso(NOW - timedelta(minutes=age_minutes)), pinned))
    return f


def test_rolling_buffer_deletes_only_old_unpinned_segments(db, tmp_path):
    sid = make_session(db, "vid1")
    old = make_segment(db, tmp_path, sid, 1, age_minutes=45)
    old_pinned = make_segment(db, tmp_path, sid, 2, age_minutes=40, pinned=1)
    fresh = make_segment(db, tmp_path, sid, 3, age_minutes=5)

    freed = enforce_rolling_buffer(db, buffer_minutes=30, now=NOW)

    assert freed == 1000
    assert not old.exists() and old_pinned.exists() and fresh.exists()
    assert db.one("SELECT deleted_at FROM segments WHERE seq = 1")["deleted_at"] is not None


def test_rolling_buffer_ignores_ended_sessions(db, tmp_path):
    sid = make_session(db, "vid1", status="ended", ended_minutes_ago=1)
    f = make_segment(db, tmp_path, sid, 1, age_minutes=90)
    assert enforce_rolling_buffer(db, 30, now=NOW) == 0 and f.exists()


def test_ended_session_wiped_after_grace(db, tmp_path):
    sid = make_session(db, "vid1", status="ended", ended_minutes_ago=15)
    f = make_segment(db, tmp_path, sid, 1, age_minutes=20)
    (f.parent / "part001.streamlink.log").write_text("log")

    cleanup_ended_sessions(db, 10, lambda _: False, tmp_path / "recordings", now=NOW)

    assert not f.parent.exists()
    assert db.one("SELECT storage_cleaned_at FROM live_sessions")["storage_cleaned_at"] is not None


def test_ended_session_kept_during_grace_or_while_recorder_flushes(db, tmp_path):
    sid_recent = make_session(db, "vid1", status="ended", ended_minutes_ago=3)
    f1 = make_segment(db, tmp_path, sid_recent, 1, age_minutes=5)
    sid_flushing = make_session(db, "vid2", status="ended", ended_minutes_ago=30)
    f2 = make_segment(db, tmp_path, sid_flushing, 1, age_minutes=35)

    cleanup_ended_sessions(db, 10, lambda s: s == sid_flushing, tmp_path / "recordings", now=NOW)

    assert f1.exists() and f2.exists()


def test_pinned_clip_segments_survive_end_cleanup_until_unpinned(db, tmp_path):
    sid = make_session(db, "vid1", status="ended", ended_minutes_ago=30)
    plain = make_segment(db, tmp_path, sid, 1, age_minutes=40)
    clip_src = make_segment(db, tmp_path, sid, 2, age_minutes=40, pinned=1)

    cleanup_ended_sessions(db, 10, lambda _: False, tmp_path / "recordings", now=NOW)
    assert not plain.exists() and clip_src.exists()
    assert db.one("SELECT storage_cleaned_at FROM live_sessions")["storage_cleaned_at"] is None

    db.execute("UPDATE segments SET pinned = 0")  # Phase 6: clip finished
    cleanup_ended_sessions(db, 10, lambda _: False, tmp_path / "recordings", now=NOW)
    assert not clip_src.parent.exists()


def test_purge_only_touches_published_clips_inside_the_clips_folder(db, tmp_path):
    from app.db import utcnow
    from app.recording.retention import purge_published_clips
    clips = tmp_path / "clips"
    db.execute("INSERT INTO live_sessions (id, video_id, channel_id, status, detected_at) VALUES (1,'v','UC1','ended',?)",
               (utcnow(),))
    outside = tmp_path / "elsewhere" / "final.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    for cid, status, folder in ((1, "published", clips / "v" / "clip00001"), (2, "approved", clips / "v" / "clip00002"),
                                (3, "published", outside.parent)):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "final.mp4").write_bytes(b"x" * 100)
        db.execute("""INSERT INTO clips (id, session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                          status, final_path, created_at, updated_at) VALUES (?, 1, 0, 1, 0, 1, ?, ?, ?, ?)""",
                   (cid, status, str(folder / "final.mp4"), utcnow(), utcnow()))
        db.execute("INSERT INTO publish_queue (clip_id, status, queued_at, published_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                   (cid, "published" if status == "published" else "queued", utcnow(), "2026-01-01T00:00:00+00:00", utcnow()))
    assert purge_published_clips(db, clips, grace_minutes=0) == 100
    assert not (clips / "v" / "clip00001").exists()
    assert (clips / "v" / "clip00002" / "final.mp4").exists()          # approved, not posted yet
    assert outside.exists()                                              # never deletes outside clips/


def test_rejected_clips_files_are_deleted_after_the_grace_period(db, tmp_path):
    from app.recording.retention import purge_rejected_clips
    clips_dir = tmp_path / "clips"
    db.execute("INSERT INTO channels (channel_id, title, source_url, created_at) VALUES ('UC1','x','u',?)", (utcnow(),))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','ended',?)",
                     (utcnow(),)).lastrowid
    made = {}
    for status in ("rejected", "approved"):
        folder = clips_dir / "v" / status
        folder.mkdir(parents=True)
        (folder / "source.mp4").write_bytes(b"x" * 1000)
        made[status] = db.execute(
            """INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                                  status, source_path, created_at, updated_at) VALUES (?, 0, 40, 0, 40, ?, ?, ?, ?)""",
            (sid, status, str(folder / "source.mp4"), utcnow(), utcnow())).lastrowid
    assert purge_rejected_clips(db, clips_dir, grace_minutes=360) == 0              # just rejected: kept
    later = datetime.now(timezone.utc) + timedelta(minutes=361)
    assert purge_rejected_clips(db, clips_dir, grace_minutes=360, now=later) == 1000
    gone = db.one("SELECT * FROM clips WHERE id = ?", (made["rejected"],))
    assert gone["files_deleted_at"] and gone["source_path"] is None and gone["status"] == "rejected"
    assert not (clips_dir / "v" / "rejected").exists() and (clips_dir / "v" / "approved").exists()
    assert purge_rejected_clips(db, clips_dir, grace_minutes=360, now=later) == 0   # idempotent
