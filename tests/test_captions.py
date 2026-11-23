import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.captions.ass import CaptionStyle, Word, ass_color, ass_time, build_ass, chunk_words, subtitles_filter
from app.captions.transcribe import Transcript
from app.captions.worker import CaptionWorker
from app.clips.render import RenderOptions
from app.db import utcnow
from app.recording.media import probe


def words(*spec):
    return [Word(s, e, t) for s, e, t in spec]


def test_ass_time_and_color():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(65.432) == "0:01:05.43"
    assert ass_time(3725.0) == "1:02:05.00"
    assert ass_color("FFE11A") == "&H001AE1FF"   # RGB → ASS BGR


def test_chunks_respect_size_and_pauses():
    w = words((0, .3, " he"), (.3, .6, " did"), (.6, .9, " not"), (.9, 1.2, " see"),
              (3.0, 3.3, " that"), (3.3, 3.5, " "))
    chunks = chunk_words(w, per_line=3)
    assert [[x.text.strip() for x in c] for c in chunks] == [["he", "did", "not"], ["see"], ["that"]]


def test_build_ass_highlights_one_word_per_event_and_escapes():
    style = CaptionStyle(words_per_line=3)
    ass = build_ass(words((0, .4, " no"), (.4, .9, " {way}")), style, credit="@TGG_", duration=5)
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0")]
    assert len(events) == 2
    assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.40,Caption")
    hi = r"{\c&H001AE1FF}"
    assert hi + "NO" in events[0] and hi + "(WAY)" in events[1]      # uppercase, braces neutralised
    assert "Dialogue: 1,0:00:00.00,0:00:05.00,Credit,,0,0,0,,@TGG_" in ass
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass


def test_subtitles_filter_escapes_windows_drive():
    f = subtitles_filter(Path("P:/dev/clips/captions.ass"))
    assert f.startswith(",subtitles='") and "P\\:/dev/clips/captions.ass" in f


# -- worker with real ffmpeg/libass, fake transcriber ------------------------------------------

class FakeTranscriber:
    model_name = "fake"

    def transcribe_clip(self, source, offset, duration, language=None, ffmpeg_bin="ffmpeg"):
        assert language == "en" and abs(duration - 4) < 1e-6
        return Transcript("en", 0.99, "no way bro", words((0.2, 0.6, " no"), (0.6, 1.0, " way"), (1.0, 1.5, " bro")), 0.1)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_worker_transcribes_and_burns_captions(db, tmp_path):
    src = tmp_path / "source.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=gray:s=1280x720:r=30:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-output_ts_offset", "18000",
                    "-f", "mpegts", str(src)], check=True)
    start = probe(src).start_time
    db.execute("INSERT INTO channels (channel_id, title, handle, source_url, created_at, language) "
               "VALUES ('UC1','TGG','@tgg_','u',?, 'en')", (utcnow(),))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','ended',?)",
                     (utcnow(),)).lastrowid
    cid = db.execute(
        """INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc, status,
                              source_path, source_start_pts, created_at, updated_at)
           VALUES (?, ?, ?, 0, 0, 'cut', ?, ?, ?, ?)""",
        (sid, start + 1, start + 5, str(src), start, utcnow(), utcnow())).lastrowid
    worker = CaptionWorker(db, FakeTranscriber(), lambda clip: RenderOptions(), CaptionStyle())

    assert worker.run_all() == [cid]
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "transcribed" and clip["final_path"] is None, clip["error"]   # no render before review
    assert worker.render_all() == []                       # nothing passed the AI review yet

    db.execute("UPDATE clips SET status = 'passed' WHERE id = ?", (cid,))
    assert worker.render_all() == [cid]
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "approved", clip["error"]
    assert Path(clip["final_path"]).exists() and abs(probe(Path(clip["final_path"])).duration - 4) < 0.2
    kinds = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                            clip["final_path"]], capture_output=True, text=True).stdout.split()
    assert kinds == ["video", "audio"]     # regression: a bad seek once produced audio-only output
    ass = Path(clip["captions_path"]).read_text(encoding="utf-8")
    assert "@tgg_" in ass and "WAY" in ass
    t = db.one("SELECT * FROM transcripts WHERE clip_id = ?", (cid,))
    assert t["text"] == "no way bro" and len(json.loads(t["words"])) == 3

    # captions really are burned in: white glyphs appear in the caption band of an otherwise flat gray video
    band = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.8", "-i", clip["final_path"],
                           "-vf", "crop=1080:260:0:1250", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, check=True).stdout
    assert max(band) > 200


def test_backend_setting_selects_engine(tmp_path):
    from app.captions.transcribe import OpenAIWhisperTranscriber, Transcriber, make_transcriber
    from app.config import Settings
    assert type(make_transcriber(Settings(_env_file=None, data_dir=tmp_path))) is OpenAIWhisperTranscriber
    assert type(make_transcriber(Settings(_env_file=None, data_dir=tmp_path, whisper_backend="faster"))) is Transcriber


def test_unavailable_model_leaves_clips_waiting(db):
    class NoModel:
        def ensure_loaded(self):
            raise RuntimeError("429 Too Many Requests")

        def transcribe_clip(self, *a, **k):
            raise AssertionError("must not be called")

    db.execute("INSERT INTO channels (channel_id, title, source_url, created_at) VALUES ('UC1','x','u',?)", (utcnow(),))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','ended',?)",
                     (utcnow(),)).lastrowid
    cid = db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                        status, source_path, source_start_pts, created_at, updated_at)
                        VALUES (?, 0, 4, 0, 0, 'cut', 'x.ts', 0, ?, ?)""", (sid, utcnow(), utcnow())).lastrowid
    w = CaptionWorker(db, NoModel(), lambda c: RenderOptions(), CaptionStyle())
    for _ in range(5):
        assert w.run_all() == []
    row = db.one("SELECT status, attempts FROM clips WHERE id = ?", (cid,))
    assert (row["status"], row["attempts"]) == ("cut", 0)
    # backed off: one load attempt (and one logged error), not one per scheduler tick
    assert db.one("SELECT COUNT(*) n FROM errors WHERE component = 'captions.model'")["n"] == 1
    w.model_backoff_seconds, w._model_retry_at = 0, 0
    w.run_all()
    assert db.one("SELECT COUNT(*) n FROM errors WHERE component = 'captions.model'")["n"] == 2


def test_failed_transcription_retries_then_fails(db, tmp_path):
    class Broken:
        def transcribe_clip(self, *a, **k):
            raise RuntimeError("model exploded")

    db.execute("INSERT INTO channels (channel_id, title, source_url, created_at) VALUES ('UC1','x','u',?)", (utcnow(),))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','ended',?)",
                     (utcnow(),)).lastrowid
    cid = db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                        status, source_path, source_start_pts, created_at, updated_at)
                        VALUES (?, 0, 4, 0, 0, 'cut', 'x.ts', 0, ?, ?)""", (sid, utcnow(), utcnow())).lastrowid
    w = CaptionWorker(db, Broken(), lambda c: RenderOptions(), CaptionStyle())
    for _ in range(3):
        w.run_all()
    row = db.one("SELECT status, error FROM clips WHERE id = ?", (cid,))
    assert row["status"] == "failed" and "model exploded" in row["error"]
