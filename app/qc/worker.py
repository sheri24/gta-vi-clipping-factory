"""transcribed → evaluating → passed | rejected, judged on the raw source before any editing.
passed clips are rendered next (captions worker) and then become approved.
"""

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from app.db import Database, utcnow
from app.qc.llm import OllamaJudge, sample_frames
from app.qc.signals import Signals, hard_rejects, measure, objective_score

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def decide(obj: float, llm_score: float | None, llm_reject: bool, hard: list[str], min_score: float,
           llm_weight: float) -> tuple[float, str, list[str]]:
    reasons = list(hard)
    final = obj if llm_score is None else round(llm_weight * llm_score + (1 - llm_weight) * obj, 2)
    if llm_reject:
        reasons.append("vision model flagged it as not postable")
    if final < min_score:
        reasons.append(f"score {final:.1f} below {min_score:.1f}")
    return final, ("rejected" if reasons else "approved"), reasons


class QcWorker:
    def __init__(self, db: Database, judge: OllamaJudge, frames: int = 6, min_score: float = 6.0,
                 llm_weight: float = 0.6, ffmpeg_bin: str = "ffmpeg"):
        self.db = db
        self.judge = judge
        self.frames = frames
        self.min_score = min_score
        self.llm_weight = llm_weight
        self.ffmpeg_bin = ffmpeg_bin
        self.backoff_seconds = 300.0
        self._retry_at = 0.0

    def _set(self, clip_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE clips SET {cols} WHERE id = ?", (*fields.values(), clip_id))

    def run_all(self) -> list[int]:
        # live clips first; at most one past-stream clip per run so fresh live moments never wait long
        # 'captioned' = clips from before the AI review moved ahead of rendering
        pending = self.db.query("""SELECT * FROM clips WHERE status IN ('transcribed', 'captioned')
                                    ORDER BY source_kind = 'vod', id""")
        if not pending or time.monotonic() < self._retry_at:
            return []
        # No judge, no verdict: clips wait (never auto-approved without QC).
        if not self.judge.available():
            self._retry_at = time.monotonic() + self.backoff_seconds
            self.db.log_error("qc.ollama", f"Ollama or model '{self.judge.model}' unavailable; clips waiting")
            return []
        done = []
        for c in pending:
            if self.process(dict(c)):
                done.append(c["id"])
            if c["source_kind"] == "vod":
                break
        return done

    def process(self, clip: dict) -> bool:
        cid = clip["id"]
        self._set(cid, status="evaluating", attempts=clip["attempts"] + 1)
        t0 = time.monotonic()
        try:
            ctx = self.db.one(
                """SELECT s.title stream_title, c.title channel, k.ratio, k.z_score, t.text transcript, t.words
                     FROM clips x JOIN live_sessions s ON s.id = x.session_id
                     JOIN channels c ON c.channel_id = s.channel_id
                     LEFT JOIN spikes k ON k.id = x.spike_id
                     LEFT JOIN transcripts t ON t.clip_id = x.id
                    WHERE x.id = ?""", (cid,))
            duration = clip["window_end_pts"] - clip["window_start_pts"]
            offset = clip["window_start_pts"] - clip["source_start_pts"]
            words = json.loads(ctx["words"] or "[]")
            probs = [w["prob"] for w in words if w.get("prob") is not None]
            signals = Signals(
                media=measure(Path(clip["source_path"]), duration, self.ffmpeg_bin, offset=offset),
                spike_ratio=ctx["ratio"] or 1.0, spike_z=ctx["z_score"] or 0.0,
                words_per_second=len(words) / duration if duration else 0.0,
                word_confidence=sum(probs) / len(probs) if probs else None,
            )
            hard = hard_rejects(signals)
            obj = objective_score(signals)
            verdict = None
            if not hard:   # don't spend a minute of CPU on a black/frozen/silent clip
                frames = sample_frames(Path(clip["source_path"]), offset, duration, self.frames, self.ffmpeg_bin)
                verdict = self.judge.judge(frames, duration=duration, stream_title=ctx["stream_title"],
                                           channel=ctx["channel"], transcript=ctx["transcript"],
                                           chat=json.loads(clip.get("chat_sample") or "[]"),
                                           note=clip.get("review_note"))
            final, decision, reasons = decide(obj, verdict.score if verdict else None,
                                              bool(verdict and verdict.reject), hard, self.min_score,
                                              self.llm_weight)
            self.db.execute(
                """INSERT OR REPLACE INTO evaluations (clip_id, signals, objective_score, llm_model, llm_result,
                       llm_score, final_score, decision, reasons, seconds_taken, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, json.dumps(signals.to_dict()), obj, self.judge.model if verdict else None,
                 json.dumps(asdict(verdict)) if verdict else None, verdict.score if verdict else None,
                 final, decision, json.dumps(reasons), time.monotonic() - t0, utcnow()))
            if decision == "approved" and not clip.get("final_path"):
                decision = "passed"          # rendered next, then approved
            self._set(cid, status=decision, score=final, attempts=0, error=None)
            log.info("clip %d %s (score %.1f): %s", cid, decision, final, "; ".join(reasons) or "ok")
            return True
        except Exception as e:
            log.exception("QC of clip %d failed", cid)
            final_try = clip["attempts"] + 1 >= MAX_ATTEMPTS
            self._set(cid, status="failed" if final_try else clip["status"], error=str(e)[-500:])
            self.db.log_error("qc", str(e)[:200], str(cid))
            return False
