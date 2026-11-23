"""Read models and operator actions for the local dashboard (served at /)."""

import time
from datetime import datetime, timezone

from app.db import Database, utcnow
from app.publish.youtube import sign_in_status
from app.recording.recorder import disk_free_gb
from app.recording.retention import folder_bytes, storage_summary

TRACE_MINUTES = 30
BUSY = ("cutting", "captioning", "evaluating", "rendering")


def overview(services: dict) -> dict:
    """Everything the dashboard's top half needs, in one request (polled every few seconds)."""
    s, db = services["settings"], services["db"]
    recording = {sid for sid, r in services["recorders"].recorders.items() if r.is_alive()}
    chatting = {sid: c.source.name for sid, c in services["chat"].collectors.items() if c.is_alive()}
    storage = storage_summary(db, s.recordings_dir)
    q = services["publish_queue"]
    return {
        "now": time.time(),
        "system": {
            "api_key_configured": bool(services["api"].api_key),
            "quota_used": services["ledger"].used(), "quota_budget": services["ledger"].daily_budget,
            "disk_free_gb": round(disk_free_gb(s.recordings_dir), 1), "min_free_gb": s.min_free_gb,
            "recordings_gb": round(storage["recordings_bytes"] / 1024**3, 2),
            "clips_gb": round(folder_bytes(s.clips_dir) / 1024**3, 2),
            "purge_after_publish": s.clip_purge_after_publish, "purge_grace_minutes": s.clip_purge_grace_minutes,
            "buffer_minutes": s.buffer_minutes, "poll_seconds": s.poll_interval_seconds,
        },
        "publishing": {
            "enabled": q.enabled, "privacy": q.privacy, "waiting_because": q.why_not_now(),
            "published_today": q.published_today(datetime.now(timezone.utc)), "max_per_day": q.max_per_day,
            "queued": db.one("SELECT COUNT(*) n FROM publish_queue WHERE status IN ('queued', 'uploading')")["n"],
            "youtube": sign_in_status(s.youtube_token_path),
        },
        "clip_counts": {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM clips GROUP BY status")},
        "channels": [channel_card(db, dict(c), recording, chatting, s.chat_bucket_seconds)
                     for c in db.query("SELECT * FROM channels ORDER BY title COLLATE NOCASE")],
        "errors": [dict(r) for r in db.query("SELECT * FROM errors ORDER BY id DESC LIMIT 8")],
        "vod": vod_overview(db, s, services["vod_paused"]()),
    }


def channel_card(db: Database, c: dict, recording: set, chatting: dict, bucket_seconds: int) -> dict:
    # a live stream beats a newer row from a past stream being clipped
    session = db.one("""SELECT * FROM live_sessions WHERE channel_id = ?
                         ORDER BY status = 'live' DESC, detected_at DESC LIMIT 1""",
                     (c["channel_id"],))
    card = {**c, "session": None, "trace": None, "spikes": []}
    if not session:
        return card
    sid = session["id"]
    card["session"] = {
        "video_id": session["video_id"], "title": session["title"], "status": session["status"],
        "started_at": session["actual_start_at"] or session["detected_at"], "ended_at": session["actual_end_at"] or session["ended_at"],
        "recording": sid in recording, "chat_source": chatting.get(sid),
        "facecam_method": session["facecam_method"], "facecam_rect": session["facecam_rect"],
        "clips": db.one("SELECT COUNT(*) n FROM clips WHERE session_id = ?", (sid,))["n"],
    }
    if session["status"] != "live":
        return card
    n = TRACE_MINUTES * 60 // bucket_seconds
    rows = db.query("""SELECT bucket_start, unique_authors, messages FROM chat_buckets WHERE session_id = ?
                        ORDER BY bucket_start DESC LIMIT ?""", (sid, n))
    rows = list(reversed(rows))
    # compact [t, authors, messages] triples: ~180 points for 30 min
    card["trace"] = {"bucket_seconds": bucket_seconds, "points": [[r[0], r[1], r[2]] for r in rows]}
    if rows:
        card["spikes"] = [dict(r) for r in db.query(
            """SELECT id, start_bucket, peak_bucket, end_bucket, peak_authors, ratio, z_score, event_utc,
                      status, clip_id FROM spikes WHERE session_id = ? AND end_bucket >= ? ORDER BY start_bucket""",
            (sid, rows[0][0]))]
    return card


def clip_action(db: Database, clip_id: int, action: str, note: str | None = None) -> dict:
    """Operator overrides. approve/reject skip or overrule the AI; requeue resumes a held or failed
    clip from the first stage it hasn't finished. Raises ValueError with a message for the UI."""
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if not clip:
        raise LookupError("clip not found")
    status = clip["status"]
    if status == "published":
        raise ValueError("this clip is already on YouTube; manage it in YouTube Studio")
    if status in BUSY:
        raise ValueError(f"the clip is {status} right now; try again when that finishes")
    now = utcnow()
    transcribed = db.one("SELECT 1 FROM transcripts WHERE clip_id = ?", (clip_id,)) is not None
    if action == "approve":
        if clip["final_path"]:
            status = "approved"
        elif clip["source_path"] and transcribed:
            status = "passed"         # overruling the AI: render it, then it's approved
        elif clip["files_deleted_at"]:
            raise ValueError("this clip's files were deleted; use Resume to fetch it again first")
        else:
            raise ValueError("the clip isn't transcribed yet; resume it first")
        db.execute("UPDATE clips SET status = ?, error = NULL, updated_at = ? WHERE id = ?", (status, now, clip_id))
    elif action == "reject":
        db.execute("UPDATE clips SET status = 'rejected', updated_at = ? WHERE id = ?", (now, clip_id))
        db.execute("""UPDATE publish_queue SET status = 'cancelled', updated_at = ?
                       WHERE clip_id = ? AND status IN ('queued', 'failed')""", (now, clip_id))
    elif action == "requeue":
        if note is not None:     # "Review again with this note": the AI reads it on the next pass
            db.execute("UPDATE clips SET review_note = ? WHERE id = ?", (note.strip()[:500] or None, clip_id))
        stage = ("transcribed" if clip["source_path"] and transcribed
                 else "cut" if clip["source_path"] else "pending")
        db.execute("UPDATE clips SET status = ?, attempts = 0, error = NULL, updated_at = ? WHERE id = ?",
                   (stage, now, clip_id))
    else:
        raise ValueError(f"unknown action {action!r}")
    return dict(db.one("SELECT * FROM clips WHERE id = ?", (clip_id,)))


VOD_LIST = 15
VOD_RANK = """CASE j.status WHEN 'analyzing' THEN 0 WHEN 'clipping' THEN 1 WHEN 'queued' THEN 2
                            WHEN 'waiting_chat' THEN 3 ELSE 4 END"""


def vod_overview(db: Database, settings, paused_because: str | None) -> dict:
    """Past streams: what's being worked on, what's next (in processing order), what was done."""
    jobs = db.query(
        f"""SELECT j.*, c.title channel_title,
                   (SELECT COUNT(*) FROM clips x WHERE x.session_id = j.session_id AND x.source_kind = 'vod'
                       AND x.status NOT IN ('pending', 'cutting')) clips_cut
              FROM vod_jobs j LEFT JOIN channels c ON c.channel_id = j.channel_id
             WHERE j.status != 'skipped' OR j.origin = 'link'
             ORDER BY {VOD_RANK}, j.origin = 'link' DESC,
                      CASE WHEN {VOD_RANK} <= 3 THEN j.ended_at END DESC, j.updated_at DESC
             LIMIT ?""", (VOD_LIST,))
    return {
        "enabled": settings.vod_enabled, "paused_because": paused_because,
        "scan_minutes": settings.vod_scan_minutes, "max_age_hours": settings.vod_max_age_hours,
        "max_clips": settings.vod_max_clips,
        "skipped": db.one("SELECT COUNT(*) n FROM vod_jobs WHERE status = 'skipped' AND origin = 'scan'")["n"],
        "jobs": [dict(r) for r in jobs],
    }


def vod_action(db: Database, job_id: int, action: str) -> dict:
    job = db.one("SELECT * FROM vod_jobs WHERE id = ?", (job_id,))
    if not job:
        raise LookupError("past stream not found")
    now = utcnow()
    if action == "retry":
        if job["status"] not in ("failed", "no_chat", "skipped", "waiting_chat"):
            raise ValueError(f"it's {job['status']} right now")
        db.execute("""UPDATE vod_jobs SET status = 'queued', attempts = 0, error = NULL, next_attempt_at = NULL,
                          updated_at = ? WHERE id = ?""", (now, job_id))
    elif action == "skip":
        if job["status"] not in ("queued", "waiting_chat"):
            raise ValueError(f"only waiting streams can be skipped (this one is {job['status']})")
        db.execute("UPDATE vod_jobs SET status = 'skipped', error = 'skipped by you', updated_at = ? WHERE id = ?",
                   (now, job_id))
    else:
        raise ValueError(f"unknown action {action!r}")
    return dict(db.one("SELECT * FROM vod_jobs WHERE id = ?", (job_id,)))
