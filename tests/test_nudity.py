"""Nudity check: rejects before the AI review, safety net for already-approved clips, upload gate."""

import json
import shutil
import subprocess

import pytest

from app.db import utcnow
from app.publish.queue import enqueue_approved
from app.qc.nudity import NudityScanner, summarize
from app.qc.worker import QcWorker
from tests.test_qc import FakeJudge, make_clip

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class Detector:
    """Stand-in for NudeNet: 'sees' buttocks from a given second on, in the bottom-right quarter only
    (a small figure the full frame would miss)."""

    def __init__(self, from_frame=None, only_frame=None, score=0.52):
        self.from_frame, self.only_frame, self.score, self.calls = from_frame, only_frame, score, 0

    def detect(self, view):
        self.calls += 1
        frame = (self.calls - 1) // 5               # 5 views per frame: full, tl, tr, bl, br
        view_name = ("full", "tl", "tr", "bl", "br")[(self.calls - 1) % 5]
        hit = (self.from_frame is not None and frame >= self.from_frame) or frame == self.only_frame
        if hit and view_name == "br":
            return [{"class": "BUTTOCKS_EXPOSED", "score": self.score, "box": [0, 0, 10, 10]},
                    {"class": "FEET_EXPOSED", "score": 0.9, "box": [0, 0, 5, 5]}]          # not sexual: ignored
        return []


@needs_ffmpeg
def test_nudity_rejects_before_the_ai_review(db, tmp_path):
    cid = make_clip(db, tmp_path, "testsrc2")
    judge = FakeJudge(scores=9)
    QcWorker(db, judge, nudity=NudityScanner(detector=Detector(from_frame=10))).run_all()   # from 5 s into an 8 s clip
    clip = db.one("SELECT status, nudity_checked_at FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "rejected" and clip["nudity_checked_at"]
    assert judge.calls == []                                        # no 2-minute AI review for a nude clip
    reasons = json.loads(db.one("SELECT reasons FROM evaluations WHERE clip_id = ?", (cid,))["reasons"])
    assert reasons[0].startswith("nudity: exposed buttocks at 0:05 (0.52), on ") and reasons[0].endswith(" frames")
    signals = json.loads(db.one("SELECT signals FROM evaluations WHERE clip_id = ?", (cid,))["signals"])
    assert signals["nudity"][0]["view"] == "br"


@needs_ffmpeg
def test_clean_clip_is_marked_scanned_and_passes(db, tmp_path):
    cid = make_clip(db, tmp_path, "testsrc2")
    QcWorker(db, FakeJudge(scores=8), nudity=NudityScanner(detector=Detector())).run_all()
    clip = db.one("SELECT status, nudity_checked_at FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "approved" and clip["nudity_checked_at"]   # make_clip clips are already rendered


@needs_ffmpeg
def test_ai_saying_nudity_also_rejects(db, tmp_path):
    cid = make_clip(db, tmp_path, "testsrc2")
    judge = FakeJudge(scores=9)
    judge.nudity = True
    QcWorker(db, judge, nudity=NudityScanner(detector=Detector())).run_all()
    assert db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"] == "rejected"
    assert "saw nudity" in db.one("SELECT reasons FROM evaluations WHERE clip_id = ?", (cid,))["reasons"]


@needs_ffmpeg
def test_already_approved_clips_are_scanned_before_upload(db, tmp_path):
    """The TGG case: approved before the scan existed, already in the upload queue."""
    cid = make_clip(db, tmp_path, "testsrc2")
    db.execute("UPDATE clips SET status = 'approved' WHERE id = ?", (cid,))
    db.execute("UPDATE channels SET permission_confirmed = 1")
    db.execute("INSERT INTO publish_queue (clip_id, queued_at, updated_at) VALUES (?, ?, ?)", (cid, utcnow(), utcnow()))
    w = QcWorker(db, FakeJudge(), nudity=NudityScanner(detector=Detector(from_frame=0)))
    assert w.check_unscanned() == [cid]
    clip = db.one("SELECT status, error FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "rejected" and clip["error"].startswith("nudity: exposed buttocks")
    assert db.one("SELECT status FROM publish_queue WHERE clip_id = ?", (cid,))["status"] == "cancelled"
    assert w.check_unscanned() == []                                # scanned once, not again


def test_unscanned_clips_are_never_queued(db):
    db.execute("""INSERT INTO channels (channel_id, title, source_url, created_at, permission_confirmed)
                  VALUES ('UC1','x','u',?,1)""", (utcnow(),))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','ended',?)",
                     (utcnow(),)).lastrowid
    cid = db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                        status, score, final_path, created_at, updated_at) VALUES (?, 0, 40, 0, 40, 'approved', 8, 'f.mp4', ?, ?)""",
                     (sid, utcnow(), utcnow())).lastrowid
    assert enqueue_approved(db) == []
    db.execute("UPDATE clips SET nudity_checked_at = ? WHERE id = ?", (utcnow(), cid))
    assert enqueue_approved(db) == [cid]


def test_summary_names_the_worst_hit():
    from app.qc.nudity import NudityHit
    hits = [NudityHit(25.0, "BUTTOCKS_EXPOSED", 0.43, "full"), NudityHit(37.5, "MALE_GENITALIA_EXPOSED", 0.61, "br")]
    assert summarize(hits) == "nudity: exposed genitals at 0:37 (0.61), on 2 frames"


def test_one_frame_blips_are_not_nudity_but_sustained_or_certain_hits_are():
    """Measured: real nudity hit 11 frames; false alarms (shoulder, arm, thumbnail) hit exactly one."""
    from app.qc.nudity import NudityHit
    s = NudityScanner(detector=Detector())
    blip = [NudityHit(6.0, "BUTTOCKS_EXPOSED", 0.52, "bl")]
    assert not s.is_nude(blip)
    assert s.is_nude([NudityHit(t, "BUTTOCKS_EXPOSED", 0.45, "br") for t in (25.0, 25.5, 26.0)])
    assert s.is_nude([NudityHit(37.0, "MALE_GENITALIA_EXPOSED", 0.8, "br")])


@needs_ffmpeg
def test_single_frame_false_alarm_doesnt_reject(db, tmp_path):
    cid = make_clip(db, tmp_path, "testsrc2")
    QcWorker(db, FakeJudge(scores=8), nudity=NudityScanner(detector=Detector(only_frame=3))).run_all()
    assert db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"] == "approved"
    signals = json.loads(db.one("SELECT signals FROM evaluations WHERE clip_id = ?", (cid,))["signals"])
    assert len(signals["nudity"]) == 1                     # recorded for the dashboard, but not a rejection
