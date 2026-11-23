"""Phase 11: one synthetic stream through every stage, using the real code for each.

Only the three slow or external parts are faked: Whisper, the Ollama judge and the YouTube upload.
Everything else runs for real: ffmpeg segments, chat buckets, spike detection, timeline mapping,
cutting, caption burn-in, objective QC, the publish queue and the end-of-stream storage cleanup."""

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.captions.ass import CaptionStyle, Word
from app.captions.transcribe import Transcript
from app.captions.worker import CaptionWorker
from app.chat.store import ChatMessage, ChatStore
from app.clips.pipeline import ClipWorker, plan_clips
from app.clips.render import RenderOptions
from app.db import utcnow
from app.publish.metadata import assemble
from app.publish.queue import PublishQueue, enqueue_approved
from app.publish.youtube import UploadResult
from app.qc.llm import LlmVerdict
from app.qc.worker import QcWorker
from app.recording.media import probe
from app.recording.retention import cleanup_ended_sessions, purge_published_clips
from app.spikes.detector import Params, SpikeDetector

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

T0 = 1_795_382_700          # UTC epoch of the first recorded frame (multiple of the 10 s bucket)
PTS0 = 18_000.0             # the stream's own clock at that frame
SPIKE_AT = T0 + 70          # chat explodes here...
EDGE_LAG, TARGET = 30.0, 2.0   # ...measured latency → chat delay = 30 + 2 + 3 = 35 s → moment at T0+35


def record_fake_stream(rec_dir: Path) -> list[Path]:
    """90 s of moving test pattern + tone, cut into ~30 s MPEG-TS segments that keep the
    stream clock, the way the real recorder writes them (-copyts + segment muxer)."""
    rec_dir.mkdir(parents=True)
    full = rec_dir / "full.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=90",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=90",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-c:a", "aac",
                    "-output_ts_offset", str(PTS0), "-f", "mpegts", str(full)], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-copyts", "-i", str(full), "-map", "0",
                    "-c", "copy", "-f", "segment", "-segment_time", "30", "-segment_format", "mpegts",
                    str(rec_dir / "seg_%06d.ts")], check=True)
    full.unlink()
    return sorted(rec_dir.glob("seg_*.ts"))


def chat_history() -> list[ChatMessage]:
    """5–7 chatters per 10 s for 5 min either side, and a 3-bucket burst of 60 at SPIKE_AT."""
    msgs = []
    for b in range(T0 - 300, T0 + 300, 10):
        burst = SPIKE_AT <= b < SPIKE_AT + 30
        n = 60 if burst else 5 + (b // 10) % 3
        for i in range(n):
            msgs.append(ChatMessage(f"{b}-{i}", (b + 1) * 1_000_000 + i * 1000, "text",
                                    f"{'s' if burst else 'a'}{i}", f"@viewer{i}",
                                    "NOOOO HE GOT SHOT 💀" if burst else "lol"))
    return msgs


class FakeTranscriber:
    model_name = "fake-whisper"

    def transcribe_clip(self, source, offset, duration, language=None, ffmpeg_bin="ffmpeg"):
        words = [Word(0.5, 1.0, " no"), Word(1.0, 1.6, " way"), Word(2.0, 3.0, " NOOOO")]
        return Transcript("en", 0.98, "no way NOOOO", words, 0.1)


class FakeJudge:
    model = "fake-vision"

    def available(self):
        return True

    def judge(self, frames, **ctx):
        assert frames and "NOOOO" in json.dumps(ctx["chat"])      # the real chat reached the judge
        return LlmVerdict({k: 8 for k in ("excitement", "reaction", "gameplay", "standalone",
                                          "visual_quality", "clean_start_end")},
                          "Jason gets shot in the finale.", False, "huge reaction")


class FakeYouTube:
    platform = "youtube"

    def __init__(self):
        self.uploads = []

    def ready(self):
        return True, "ok"

    def upload(self, video, title, description, tags, privacy):
        assert Path(video).exists()
        self.uploads.append({"video": str(video), "title": title, "description": description, "privacy": privacy})
        return UploadResult("abc123", "https://youtube.com/shorts/abc123")


class FakeWriter:
    def write(self, ctx):
        return assemble({"title": f"{ctx['channel']} can't believe it 😭", "description": "The finale.",
                         "tags": ["gta 6 finale"]}, ctx, ["gta 6", "shorts"], "llm")


def test_stream_to_published_short(db, tmp_path):
    # -- a permitted, recorded channel goes live ------------------------------------------------
    db.execute("""INSERT INTO channels (channel_id, title, handle, source_url, created_at, record_enabled,
                                        permission_confirmed, layout, chat_delay_auto, language)
                  VALUES ('UC1', 'Lacy', '@lacyhimself', 'u', ?, 1, 1, 'blur', 1, 'en')""", (utcnow(),))
    sid = db.execute("""INSERT INTO live_sessions (video_id, channel_id, title, status, detected_at, actual_start_at)
                        VALUES ('vid', 'UC1', 'GTA VI FINALE', 'live', ?, ?)""",
                     (utcnow(), datetime.fromtimestamp(T0 - 3600, tz=timezone.utc).isoformat())).lastrowid

    # -- recording: segments on the stream clock + a PDT anchor with measured latency --------------
    rec_dir = tmp_path / "recordings" / "vid"
    for seq, path in enumerate(record_fake_stream(rec_dir)):
        info = probe(path)
        db.execute("""INSERT INTO segments (session_id, seq, path, pts_start, duration, size_bytes, created_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?)""",
                   (sid, seq, str(path), info.start_time, info.duration, path.stat().st_size, utcnow()))
    db.execute("""INSERT INTO time_anchors (session_id, pts, utc, sampled_at, edge_lag_s, target_duration)
                  VALUES (?, ?, ?, ?, ?, ?)""",
               (sid, PTS0, datetime.fromtimestamp(T0, tz=timezone.utc).isoformat(), utcnow(), EDGE_LAG, TARGET))

    # -- chat → buckets → spike -------------------------------------------------------------------
    store = ChatStore(db, 10)
    store.add(sid, chat_history(), "ytdlp")
    store.aggregate(sid, lookback_seconds=3600)
    db.execute("UPDATE live_sessions SET status = 'ended', ended_at = ? WHERE id = ?",
               ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"), sid))
    SpikeDetector(db, Params(), pre_seconds=3, post_seconds=5).run(sid, final=True, lookback_minutes=60)
    [spike] = db.query("SELECT * FROM spikes")
    assert spike["start_bucket"] == SPIKE_AT and spike["status"] == "closed"
    assert spike["chat_delay_s"] == pytest.approx(EDGE_LAG + TARGET + 3)
    assert spike["event_utc"] == pytest.approx(T0 + 35)
    assert spike["clip_start_pts"] == pytest.approx(PTS0 + 32)       # moment − 3 s, on the stream clock

    # -- cut → captions → QC ---------------------------------------------------------------------
    [cid] = plan_clips(db, min_overlap=2)
    opts = lambda clip: RenderOptions(layout="blur", encoder="libx264", crf=28)
    assert ClipWorker(db, tmp_path / "clips").run_all() == [cid]
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "cut", clip["error"]
    assert abs(clip["duration"] - 8) < 0.3
    assert "NOOOO" in clip["chat_sample"]

    captions = CaptionWorker(db, FakeTranscriber(), opts, CaptionStyle())
    assert captions.run_all() == [cid]
    assert db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"] == "transcribed"

    assert QcWorker(db, FakeJudge(), frames=3).run_all() == [cid]          # judged before any editing
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "passed" and clip["final_path"] is None and clip["score"] >= 6, clip["error"]

    assert captions.render_all() == [cid]                                   # only now the 9:16 edit
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "approved", clip["error"]
    final = Path(clip["final_path"])
    info = probe(final)
    assert abs(info.duration - 8) < 0.3

    # -- publish --------------------------------------------------------------------------------
    assert len(enqueue_approved(db)) == 1
    yt = FakeYouTube()
    item = PublishQueue(db, yt, FakeWriter(), enabled=True, privacy="private", interval_minutes=30,
                        max_per_day=6).run_once()
    assert item["status"] == "published" and item["remote_url"] == "https://youtube.com/shorts/abc123"
    [up] = yt.uploads
    assert up["video"] == str(final) and up["privacy"] == "private"
    assert up["title"] == "Lacy can't believe it 😭"
    assert "Clip from Lacy's livestream (@lacyhimself)" in up["description"]
    assert f"watch?v=vid&t={T0 + 32 - (T0 - 3600)}s" in up["description"]   # credit links to the moment
    assert db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"] == "published"

    # -- the stream's raw footage is wiped; the Short and its source survive ------------------------
    freed = cleanup_ended_sessions(db, 10, lambda _: False, tmp_path / "recordings",
                                   on_cleaned=store.delete_session_messages)
    assert freed > 0 and not rec_dir.exists()
    assert final.exists() and Path(clip["source_path"]).exists()
    assert db.one("SELECT COUNT(*) n FROM chat_messages")["n"] == 0
    assert db.one("SELECT storage_cleaned_at FROM live_sessions")["storage_cleaned_at"]

    # -- once posted, the clip's own files go too (after the grace period) --------------------------
    clips_dir = tmp_path / "clips"
    assert purge_published_clips(db, clips_dir, grace_minutes=60) == 0          # just uploaded: kept
    later = datetime.now(timezone.utc) + timedelta(minutes=61)
    assert purge_published_clips(db, clips_dir, grace_minutes=60, now=later) > 0
    assert not final.exists() and not (clips_dir / "vid").exists() and clips_dir.exists()
    gone = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert gone["files_deleted_at"] and gone["final_path"] is None and gone["status"] == "published"
    assert db.one("SELECT text FROM transcripts WHERE clip_id = ?", (cid,))["text"] == "no way NOOOO"
    assert purge_published_clips(db, clips_dir, grace_minutes=60, now=later) == 0   # idempotent
