import json
import shutil
import subprocess

import pytest

from app.clips.pipeline import chat_sample
from app.db import utcnow
from app.qc.llm import LlmVerdict
from app.qc.signals import MediaSignals, Signals, hard_rejects, measure, objective_score, parse_ffmpeg_detectors
from app.qc.worker import QcWorker, decide

STDERR = """
[blackdetect @ 0x1] black_start:0 black_end:2.5 black_duration:2.5
[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 10
[freezedetect @ 0x2] lavfi.freezedetect.freeze_duration: 4
[freezedetect @ 0x2] lavfi.freezedetect.freeze_end: 14
[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 36
[silencedetect @ 0x3] silence_start: 1.0
[silencedetect @ 0x3] silence_end: 4.0 | silence_duration: 3.0
[Parsed_volumedetect_1 @ 0x4] mean_volume: -21.3 dB
"""


def test_parse_detectors_including_open_freeze_at_eof():
    m = parse_ffmpeg_detectors(STDERR, duration=40)
    assert (m.black_seconds, m.frozen_seconds, m.silent_seconds, m.mean_volume_db) == (2.5, 8.0, 3.0, -21.3)


def sig(ratio=3.0, z=8.0, wps=2.0, black=0.0, frozen=0.0, silent=0.0, vol=-20.0, conf=0.9):
    return Signals(MediaSignals(40, black * 40, frozen * 40, silent * 40, vol), ratio, z, wps, conf)


def test_objective_score_orders_sensibly():
    strong, weak = objective_score(sig(ratio=4.5, z=12)), objective_score(sig(ratio=1.7, z=3.6))
    assert strong > 8 > weak
    assert objective_score(sig(silent=0.85)) < objective_score(sig()) - 3   # dead air hurts
    assert 0 <= objective_score(sig(ratio=1, z=0, wps=0, silent=1, frozen=1)) <= 10


def test_hard_rejects():
    assert hard_rejects(sig()) == []
    assert any("black" in r for r in hard_rejects(sig(black=0.5)))
    assert any("frozen" in r for r in hard_rejects(sig(frozen=0.6)))
    assert any("audio" in r for r in hard_rejects(sig(vol=-70)))


def test_decide():
    assert decide(7, 8, False, [], 6, 0.6) == (7.6, "approved", [])
    assert decide(7, 8, True, [], 6, 0.6)[1] == "rejected"           # model veto
    assert decide(3, 5, False, [], 6, 0.6)[1] == "rejected"          # 4.2 < 6
    assert decide(9, None, False, ["black screen"], 6, 0.6)[1] == "rejected"


# -- real ffmpeg measurements + worker ------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_video(path, video_src, audio=True, d=8):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{video_src}:d={d}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=500:sample_rate=48000:duration={d}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-g", "30"] + (["-c:a", "aac"] if audio else []) + \
           ["-f", "mpegts" if str(path).endswith(".ts") else "mp4", str(path)]
    subprocess.run(cmd, check=True)


@needs_ffmpeg
def test_measure_black_frozen_and_moving(tmp_path):
    black, frozen, moving = tmp_path / "b.mp4", tmp_path / "f.mp4", tmp_path / "m.mp4"
    make_video(black, "color=black:s=320x240:r=30")
    make_video(frozen, "color=gray:s=320x240:r=30")
    make_video(moving, "testsrc2=s=320x240:r=30")
    assert measure(black, 8).black_frac > 0.9
    assert measure(frozen, 8).frozen_frac > 0.7
    m = measure(moving, 8)
    assert m.black_frac == 0 and m.frozen_frac == 0 and m.silent_frac == 0


class FakeJudge:
    model = "fake-vision"

    def __init__(self, scores=8, reject=False, up=True, gta=True, funny=3):
        self.scores, self.reject, self.up, self.calls = scores, reject, up, []
        self.gta, self.funny = gta, funny

    def available(self):
        return self.up

    def judge(self, frames, **ctx):
        self.calls.append((len(frames), ctx))
        return LlmVerdict({k: self.scores for k in ("excitement", "reaction", "gameplay", "standalone",
                                                    "visual_quality", "clean_start_end")},
                          "He crashes a jet into the beach.", self.reject, "big reaction", self.gta, self.funny)


def lavfi(video_src, opts):
    return f"{video_src}{':' if '=' in video_src else '='}{opts}"   # "testsrc2=s=…" vs "color=black:s=…"


def make_clip(db, tmp_path, video_src, ratio=3.5, z=9.0):
    src = tmp_path / f"src_{video_src[:5]}.ts"
    make_video(src, lavfi(video_src, "s=640x360:r=30"), d=8)
    final = tmp_path / f"final_{video_src[:5]}.mp4"
    make_video(final, lavfi(video_src, "s=320x568:r=30"), d=8)
    if not db.one("SELECT 1 FROM channels"):
        db.execute("INSERT INTO channels (channel_id, title, source_url, created_at) VALUES ('UC1','TGG','u',?)", (utcnow(),))
        db.execute("INSERT INTO live_sessions (video_id, channel_id, title, status, detected_at) "
                   "VALUES ('v','UC1','GTA 6 chapter 4','ended',?)", (utcnow(),))
    sid = db.one("SELECT id FROM live_sessions")["id"]
    kid = db.execute("""INSERT INTO spikes (session_id, start_bucket, peak_bucket, end_bucket, peak_authors, peak_messages,
                        baseline_authors, z_score, ratio, chat_delay_s, event_utc, clip_start_utc, clip_end_utc,
                        detected_at, updated_at, status)
                        VALUES (?, ?, 0, 0, 90, 100, 25, ?, ?, 12, 0, 0, 0, ?, ?, 'closed')""",
                     (sid, len(video_src) + int(ratio * 100), z, ratio, utcnow(), utcnow())).lastrowid
    from app.recording.media import probe
    start = probe(src).start_time
    cid = db.execute(
        """INSERT INTO clips (session_id, spike_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                              status, source_path, source_start_pts, final_path, chat_sample, created_at, updated_at)
           VALUES (?, ?, ?, ?, 0, 0, 'captioned', ?, ?, ?, ?, ?, ?)""",
        (sid, kid, start, start + 8, str(src), start, str(final),
         json.dumps([{"t": 1, "a": "@x", "m": "HE CRASHED 💀"}]), utcnow(), utcnow())).lastrowid
    db.execute("INSERT INTO transcripts (clip_id, text, words, model, created_at) VALUES (?, ?, ?, 'fake', ?)",
               (cid, "no way bro", json.dumps([{"start": 0, "end": .5, "text": "no", "prob": .9}] * 16), utcnow()))
    return cid


@needs_ffmpeg
def test_worker_approves_good_clip_with_context_passed_to_judge(db, tmp_path):
    cid = make_clip(db, tmp_path, "testsrc2")
    judge = FakeJudge(scores=8)
    assert QcWorker(db, judge, frames=4).run_all() == [cid]
    clip = db.one("SELECT status, score FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "approved" and clip["score"] >= 6
    n_frames, ctx = judge.calls[0]
    assert n_frames == 4
    assert ctx["channel"] == "TGG" and ctx["stream_title"] == "GTA 6 chapter 4"
    assert ctx["chat"][0]["m"] == "HE CRASHED 💀" and ctx["transcript"] == "no way bro"
    ev = db.one("SELECT * FROM evaluations WHERE clip_id = ?", (cid,))
    assert ev["decision"] == "approved" and json.loads(ev["llm_result"])["summary"].startswith("He crashes")


@needs_ffmpeg
def test_black_clip_rejected_without_calling_the_model(db, tmp_path):
    cid = make_clip(db, tmp_path, "color=black")
    judge = FakeJudge()
    QcWorker(db, judge).run_all()
    assert db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"] == "rejected"
    assert judge.calls == []
    assert "black" in db.one("SELECT reasons FROM evaluations WHERE clip_id = ?", (cid,))["reasons"]


@needs_ffmpeg
def test_low_scores_rejected(db, tmp_path):
    cid = make_clip(db, tmp_path, "testsrc2", ratio=1.7, z=3.6)
    QcWorker(db, FakeJudge(scores=3)).run_all()
    assert db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"] == "rejected"


@needs_ffmpeg
def test_off_game_answer_alone_never_rejects(db, tmp_path):
    """The 'GTA on screen / funny' answers are information only: the user found the rule rejected good clips."""
    cid = make_clip(db, tmp_path, "testsrc2")
    QcWorker(db, FakeJudge(scores=8, gta=False, funny=1)).run_all()
    assert db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"] == "approved"


def test_unavailable_judge_never_auto_approves(db):
    db.execute("INSERT INTO channels (channel_id, title, source_url, created_at) VALUES ('UC1','x','u',?)", (utcnow(),))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','ended',?)",
                     (utcnow(),)).lastrowid
    cid = db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                        status, created_at, updated_at) VALUES (?, 0, 40, 0, 0, 'captioned', ?, ?)""",
                     (sid, utcnow(), utcnow())).lastrowid
    w = QcWorker(db, FakeJudge(up=False))
    assert w.run_all() == [] and w.run_all() == []          # second call is inside the backoff window
    assert db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"] == "captioned"
    assert db.one("SELECT COUNT(*) n FROM errors WHERE component = 'qc.ollama'")["n"] == 1


def test_chat_sample_snapshot_window(db):
    db.execute("INSERT INTO channels (channel_id, title, source_url, created_at) VALUES ('UC1','x','u',?)", (utcnow(),))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','live',?)",
                     (utcnow(),)).lastrowid
    kid = db.execute("""INSERT INTO spikes (session_id, start_bucket, peak_bucket, end_bucket, peak_authors, peak_messages,
                        baseline_authors, z_score, ratio, chat_delay_s, event_utc, clip_start_utc, clip_end_utc,
                        detected_at, updated_at) VALUES (?, 1000, 1000, 1010, 1, 1, 1, 1, 1, 12, 0, 0, 0, ?, ?)""",
                     (sid, utcnow(), utcnow())).lastrowid
    for i, (t, msg) in enumerate([(990, "too early"), (996, "wait what"), (1002, "LMAOOO"), (1029, "W"), (1040, "late")]):
        db.execute("INSERT INTO chat_messages (session_id, msg_id, published_us, kind, author_name, text, source) "
                   "VALUES (?, ?, ?, 'text', '@a', ?, 'ytdlp')", (sid, f"m{i}", t * 1_000_000, msg))
    sample = chat_sample(db, {"spike_id": kid, "session_id": sid})
    assert [m["m"] for m in sample] == ["wait what", "LMAOOO", "W"]


@needs_ffmpeg
def test_operator_note_reaches_the_judge(db, tmp_path):
    cid = make_clip(db, tmp_path, "testsrc2")
    db.execute("UPDATE clips SET review_note = 'post-credits scene of GTA 6' WHERE id = ?", (cid,))
    judge = FakeJudge()
    QcWorker(db, judge).run_all()
    assert judge.calls[0][1]["note"] == "post-credits scene of GTA 6"
