"""Recorder integration tests with real ffmpeg. A synthetic live source stands in for Streamlink:
ffmpeg generates test video at real-time speed with PTS offset by 18000 s, like YouTube's
broadcast-relative timestamps."""

import shutil
import threading

import pytest

from app.db import utcnow
from app.recording.recorder import Recorder

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

PTS_OFFSET = 18000


def fake_source(seconds: float):
    def build(session, backend):
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                "-c:a", "aac", "-output_ts_offset", str(PTS_OFFSET), "-f", "mpegts", "pipe:1"]
    return build


def make_session(db):
    sid = db.execute(
        "INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('vidX', 'UCx', 'live', ?)",
        (utcnow(),)).lastrowid
    return dict(db.one("SELECT * FROM live_sessions WHERE id = ?", (sid,)))


def sequence(*values):
    it = iter(values)
    return lambda: next(it, False)


def test_records_segments_with_preserved_broadcast_pts(db, tmp_path):
    session = make_session(db)
    rec = Recorder(db, session, tmp_path, fake_source(7), segment_seconds=2,
                   is_live=sequence(True), index_interval=0.5, min_healthy_seconds=1)
    rec.start()
    rec.join(30)
    assert not rec.is_alive()

    segs = db.query("SELECT * FROM segments ORDER BY seq")
    assert len(segs) >= 3
    assert [s["seq"] for s in segs] == list(range(1, len(segs) + 1))
    # -copyts must keep the source's broadcast-relative PTS, not reset to 0
    assert segs[0]["pts_start"] >= PTS_OFFSET
    # contiguous: each segment starts where the previous ended (within one frame)
    for a, b in zip(segs, segs[1:]):
        assert abs(a["pts_start"] + a["duration"] - b["pts_start"]) < 0.1
    assert abs(sum(s["duration"] for s in segs) - 7) < 0.5
    assert db.one("SELECT status FROM recordings")["status"] == "stopped"


def test_restart_after_source_drop_continues_numbering(db, tmp_path):
    session = make_session(db)
    rec = Recorder(db, session, tmp_path, fake_source(3), segment_seconds=2,
                   is_live=sequence(True, True), index_interval=0.5, min_healthy_seconds=1)
    rec.start()
    rec.join(40)

    parts = db.query("SELECT part FROM recordings ORDER BY part")
    assert [p["part"] for p in parts] == [1, 2]
    seqs = [s["seq"] for s in db.query("SELECT seq FROM segments ORDER BY seq")]
    assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) >= 3
    # nothing overwritten: every file on disk is indexed exactly once
    assert len(list(tmp_path.glob("seg_*.ts"))) == len(seqs)


def test_stop_finalises_open_segment(db, tmp_path):
    session = make_session(db)
    rec = Recorder(db, session, tmp_path, fake_source(120), segment_seconds=60,
                   is_live=lambda: True, index_interval=0.5, min_healthy_seconds=1)
    rec.start()
    threading.Event().wait(4)
    rec.stop()
    rec.join(30)

    assert not rec.is_alive()
    segs = db.query("SELECT * FROM segments")
    assert len(segs) == 1 and segs[0]["duration"] > 2  # the in-progress segment was flushed & probed
    assert db.one("SELECT status FROM recordings")["status"] == "stopped"
