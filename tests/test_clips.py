import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from app.clips.pipeline import ClipWorker, covering_segments, plan_clips
from pathlib import Path

from app.clips.render import RenderOptions, render_vertical, video_filter
from app.db import utcnow
from app.recording.media import probe
from app.recording.retention import enforce_rolling_buffer

PTS0 = 18000.0
T0 = 1_795_382_700


def setup(db, record_enabled=1, layout="blur", rect=None):
    db.execute("""INSERT INTO channels (channel_id, title, source_url, created_at, record_enabled, layout, facecam_rect)
                  VALUES ('UC1','x','u',?,?,?,?)""", (utcnow(), record_enabled, layout, rect))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('vid','UC1','live',?)",
                     (utcnow(),)).lastrowid
    return sid


def add_spike(db, sid, start_bucket, pts=True, status="closed"):
    c0, c1 = start_bucket - 27.0, start_bucket + 13.0
    return db.execute(
        """INSERT INTO spikes (session_id, start_bucket, peak_bucket, end_bucket, peak_authors, peak_messages,
               baseline_authors, z_score, ratio, chat_delay_s, event_utc, clip_start_utc, clip_end_utc,
               clip_start_pts, clip_end_pts, detected_at, updated_at, status)
           VALUES (?, ?, ?, ?, 50, 60, 20, 5, 2.5, 12, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, start_bucket, start_bucket, start_bucket, start_bucket - 12, c0, c1,
         PTS0 + (c0 - T0) if pts else None, PTS0 + (c1 - T0) if pts else None, utcnow(), utcnow(), status)).lastrowid


def add_segment(db, sid, seq, pts_start, duration=60.0, path=None, created_at=None):
    db.execute("""INSERT INTO segments (session_id, seq, path, pts_start, duration, size_bytes, created_at)
                  VALUES (?, ?, ?, ?, ?, 1, ?)""",
               (sid, seq, path or f"seg{seq}.ts", pts_start, duration, created_at or utcnow()))


# -- planning --------------------------------------------------------------------------

def test_closed_spike_becomes_clip_and_overlapping_spike_reuses_it(db):
    sid = setup(db)
    k1 = add_spike(db, sid, T0 + 100)
    k2 = add_spike(db, sid, T0 + 115)        # windows overlap by 25 s
    k3 = add_spike(db, sid, T0 + 200)        # far away → its own clip
    created = plan_clips(db, min_overlap=10)
    assert len(created) == 2
    links = {r["id"]: r["clip_id"] for r in db.query("SELECT id, clip_id FROM spikes")}
    assert links[k1] == links[k2] != links[k3]
    c = db.one("SELECT * FROM clips WHERE id = ?", (links[k1],))
    assert c["window_end_pts"] - c["window_start_pts"] == 40


def test_poll_burst_is_not_clipped_live(db):
    sid = setup(db)
    kid = add_spike(db, sid, T0 + 600)
    for i in range(30):
        db.execute("""INSERT INTO chat_messages (session_id, msg_id, published_us, kind, author_id, text, source)
                      VALUES (?, ?, ?, 'text', ?, ?, 'ytdlp')""",
                   (sid, f"m{i}", (T0 + 600 + i % 10) * 1_000_000, f"UC{i}", "1" if i % 2 else "2"))
    assert plan_clips(db) == []
    assert "giveaway or poll" in db.one("SELECT skip_reason FROM spikes WHERE id = ?", (kid,))["skip_reason"]
    assert plan_clips(db) == []          # decided once, not re-checked every run


def test_open_spikes_unrecorded_channels_and_unanchored_spikes_wait(db):
    sid = setup(db)
    add_spike(db, sid, T0 + 100, status="open")
    add_spike(db, sid, T0 + 300, pts=False)          # no pts and no anchor yet
    assert plan_clips(db) == []
    db.execute("INSERT INTO time_anchors (session_id, pts, utc, sampled_at) VALUES (?, ?, ?, ?)",
               (sid, PTS0, datetime.fromtimestamp(T0, tz=timezone.utc).isoformat(), utcnow()))
    [cid] = plan_clips(db)                             # anchor arrived → window mapped now
    assert db.one("SELECT window_start_pts s FROM clips WHERE id = ?", (cid,))["s"] == PTS0 + 273


def test_channel_not_recording_gets_no_clips(db):
    sid = setup(db, record_enabled=0)
    add_spike(db, sid, T0 + 100)
    assert plan_clips(db) == []


# -- footage coverage ------------------------------------------------------------------------

def test_coverage_states(db):
    sid = setup(db)
    add_segment(db, sid, 1, PTS0)
    add_segment(db, sid, 2, PTS0 + 60)
    assert covering_segments(db, sid, PTS0 + 50, PTS0 + 90)[1] == "ok"
    assert covering_segments(db, sid, PTS0 + 100, PTS0 + 140)[1] == "waiting"   # not written yet, still live
    assert covering_segments(db, sid, PTS0 - 30, PTS0 + 10)[1] == "missing"     # before recording started
    db.execute("UPDATE live_sessions SET status = 'ended'")
    assert covering_segments(db, sid, PTS0 + 100, PTS0 + 140)[1] == "missing"


def test_recorder_gap_inside_window_is_missing(db):
    sid = setup(db)
    add_segment(db, sid, 1, PTS0)
    add_segment(db, sid, 2, PTS0 + 75)                 # 15 s hole (restart)
    assert covering_segments(db, sid, PTS0 + 50, PTS0 + 90)[1] == "missing"


def test_pending_clip_protects_its_footage_from_rolling_buffer(db, tmp_path):
    sid = setup(db)
    old = "2026-01-01T00:00:00+00:00"
    for seq, pts in ((1, PTS0), (2, PTS0 + 60), (3, PTS0 + 120)):
        f = tmp_path / f"seg{seq}.ts"
        f.write_bytes(b"x")
        add_segment(db, sid, seq, pts, path=str(f), created_at=old)
    db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                                     created_at, updated_at) VALUES (?, ?, ?, 0, 0, ?, ?)""",
               (sid, PTS0 + 70, PTS0 + 110, utcnow(), utcnow()))
    enforce_rolling_buffer(db, buffer_minutes=30)
    assert [(tmp_path / f"seg{i}.ts").exists() for i in (1, 2, 3)] == [False, True, False]


# -- rendering (real ffmpeg) ---------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_recording(tmp_path):
    """20 s source: red for 10 s then blue, PTS offset like YouTube, split into 5 s segments with -copyts."""
    full = tmp_path / "full.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=red:s=1280x720:r=30:d=10",
                    "-f", "lavfi", "-i", "color=blue:s=1280x720:r=30:d=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=20",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
                    "-output_ts_offset", str(PTS0), "-f", "mpegts", str(full)], check=True)
    segdir = tmp_path / "rec"
    segdir.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-copyts", "-i", str(full), "-c", "copy",
                    "-f", "segment", "-segment_time", "5", "-segment_format", "mpegts",
                    str(segdir / "seg_%06d.ts")], check=True)
    return sorted(segdir.glob("seg_*.ts"))


def pixel(video, t):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(video),
                          "-vf", "crop=2:2:539:959", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return pixel_name(out[0], out[2])


def pixel_name(r, b):
    return "red" if r > 150 and b < 100 else "blue" if b > 150 and r < 100 else f"?({r},{b})"


@needs_ffmpeg
@pytest.mark.parametrize("layout,rect", [("blur", None), ("split", "0.75,0.0,0.25,0.3")])
def test_worker_cuts_exact_window_across_segments(db, tmp_path, layout, rect):
    sid = setup(db, layout=layout, rect=rect)
    segs = make_recording(tmp_path)
    for i, f in enumerate(segs, start=1):
        p = probe(f)
        add_segment(db, sid, i, p.start_time, p.duration, path=str(f))
    # The mpegts muxer delays the first frame past the requested offset, so anchor on the real first frame:
    # red→blue switch is at first_frame + 10 s.
    first_frame = probe(segs[0]).start_time
    db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                                     created_at, updated_at) VALUES (?, ?, ?, 0, 0, ?, ?)""",
               (sid, first_frame + 8, first_frame + 14, utcnow(), utcnow()))
    from app.config import Settings
    from app.clips.pipeline import render_options_factory
    worker = ClipWorker(db, tmp_path / "clips")

    [cid] = worker.run_all()

    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "cut" and clip["vertical_path"] is None, clip["error"]   # no render before review
    assert abs(clip["duration"] - 6) < 0.01
    opts = render_options_factory(db, Settings(_env_file=None))(dict(clip))
    assert opts.layout == layout
    vertical = tmp_path / "vertical.mp4"
    render_vertical(Path(clip["source_path"]), vertical, clip["window_start_pts"] - clip["source_start_pts"],
                    clip["duration"], opts)
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height", "-of", "csv=p=0", str(vertical)],
                          capture_output=True, text=True).stdout.strip()
    assert info == "1080,1920"
    assert abs(probe(vertical).duration - 6) < 0.2
    kinds = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                            str(vertical)], capture_output=True, text=True).stdout.split()
    assert kinds == ["video", "audio"]
    if layout == "blur":   # centre pixel shows the source: red until source t=10 (clip t=2), then blue
        assert pixel(vertical, 0.5) == "red"
        assert pixel(vertical, 4.0) == "blue"


def test_split_without_rect_rejected():
    with pytest.raises(ValueError):
        video_filter(RenderOptions(layout="split"))


@needs_ffmpeg
def test_raw_preview_of_live_footage_plays_in_a_browser_at_the_right_second(db, tmp_path):
    """Rejected/waiting clips have no render: the dashboard plays the raw source instead. Live sources are
    MPEG-TS with stream PTS; the MP4 copy must start at 0 so '#t=window_start - source_start' lands on the moment."""
    from app.main import playable_source
    segs = make_recording(tmp_path)                       # red for 10 s then blue, PTS offset like YouTube
    source = tmp_path / "source.ts"
    source.write_bytes(b"".join(s.read_bytes() for s in segs))
    source_start = probe(source).start_time
    preview = playable_source(source, "ffmpeg")
    assert preview.suffix == ".mp4" and playable_source(source, "ffmpeg") == preview     # made once, then reused
    assert probe(preview).start_time < 0.1
    first = probe(segs[0]).start_time
    assert pixel_at(preview, first + 8 - source_start) == "red"      # stream second first+8: before the switch
    assert pixel_at(preview, first + 12 - source_start) == "blue"    # after it


def pixel_at(video, t):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(video),
                          "-vf", "crop=2:2:639:359", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return pixel_name(out[0], out[2])
