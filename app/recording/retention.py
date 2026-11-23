"""Storage cleanup.

Three rules, run every minute:
  1. Rolling buffer — while a stream is live, keep only the last `buffer_minutes` of segments.
  2. End-of-stream wipe — `grace_minutes` after a session ends (and its recorder has stopped),
     delete all of its remaining footage.
  3. Posted clips — a while after a clip is published, delete its local folder (the Short lives
     on YouTube; transcript, scores and the link stay in the database).
  4. Rejected clips — a while after the AI (or you) rejected a clip, delete its local folder too.
Segments with pinned > 0 are never deleted: Phase 6 pins the segments a clip is being cut
from and unpins them once the clip file exists, so viral moments always survive cleanup.
"""

import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import Database, utcnow

log = logging.getLogger(__name__)

# A segment is protected while any clip that still needs it hasn't been cut yet.
NOT_NEEDED_BY_CLIP = """NOT EXISTS (
    SELECT 1 FROM clips c WHERE c.session_id = s.session_id AND c.status IN ('pending', 'cutting')
       AND c.source_kind = 'live' AND c.window_start_pts < s.pts_start + s.duration AND c.window_end_pts > s.pts_start)"""


def _delete_segment(db: Database, seg_id: int, path: str) -> int:
    p = Path(path)
    size = p.stat().st_size if p.exists() else 0
    p.unlink(missing_ok=True)
    db.execute("UPDATE segments SET deleted_at = ? WHERE id = ?", (utcnow(), seg_id))
    return size


def enforce_rolling_buffer(db: Database, buffer_minutes: int, now: datetime | None = None) -> int:
    """Delete unpinned segments of LIVE sessions older than the buffer. Returns bytes freed."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=buffer_minutes)).isoformat(timespec="seconds")
    rows = db.query(
        """SELECT s.id, s.path FROM segments s JOIN live_sessions l ON l.id = s.session_id
            WHERE l.status = 'live' AND s.deleted_at IS NULL AND s.pinned = 0 AND s.created_at < ?
              AND """ + NOT_NEEDED_BY_CLIP,
        (cutoff,),
    )
    return sum(_delete_segment(db, r["id"], r["path"]) for r in rows)


def cleanup_ended_sessions(db: Database, grace_minutes: int, is_recording: Callable[[int], bool],
                           recordings_dir: Path, now: datetime | None = None,
                           on_cleaned: Callable[[int], None] | None = None) -> int:
    """Wipe footage of sessions that ended more than `grace_minutes` ago. Returns bytes freed."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=grace_minutes)).isoformat(timespec="seconds")
    freed = 0
    sessions = db.query(
        """SELECT id, video_id FROM live_sessions
            WHERE status = 'ended' AND storage_cleaned_at IS NULL AND ended_at < ?""",
        (cutoff,),
    )
    for s in sessions:
        if is_recording(s["id"]):
            continue  # recorder still flushing
        rows = db.query("SELECT id, path FROM segments s WHERE session_id = ? AND deleted_at IS NULL AND pinned = 0 AND "
                        + NOT_NEEDED_BY_CLIP, (s["id"],))
        freed += sum(_delete_segment(db, r["id"], r["path"]) for r in rows)
        still_needed = db.one("SELECT COUNT(*) n FROM segments WHERE session_id = ? AND deleted_at IS NULL",
                              (s["id"],))["n"]
        if still_needed:
            continue  # a clip is still waiting to be cut; try again next run
        session_dir = recordings_dir / s["video_id"]
        if session_dir.exists():
            for leftover in session_dir.iterdir():   # part logs, stray partial segments
                freed += leftover.stat().st_size
                leftover.unlink()
            session_dir.rmdir()
        db.execute("UPDATE live_sessions SET storage_cleaned_at = ? WHERE id = ?", (utcnow(), s["id"]))
        if on_cleaned:
            on_cleaned(s["id"])
        log.info("cleaned storage for ended session %s", s["video_id"])
    return freed


def storage_summary(db: Database, recordings_dir: Path) -> dict:
    total = sum(f.stat().st_size for f in recordings_dir.rglob("*") if f.is_file()) if recordings_dir.exists() else 0
    row = db.one("SELECT COUNT(*) n, COALESCE(SUM(pinned > 0), 0) p FROM segments WHERE deleted_at IS NULL")
    return {"recordings_bytes": total, "live_segments": row["n"], "pinned_segments": row["p"]}


def purge_published_clips(db: Database, clips_dir: Path, grace_minutes: int, now: datetime | None = None) -> int:
    """Delete the folders of clips published more than `grace_minutes` ago. Returns bytes freed.
    The grace period leaves time to notice a failed YouTube processing and re-upload."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=grace_minutes)).isoformat(timespec="seconds")
    root = clips_dir.resolve()
    rows = db.query(
        """SELECT c.id, c.source_path, c.vertical_path, c.final_path, c.captions_path FROM clips c
             JOIN publish_queue q ON q.clip_id = c.id AND q.status = 'published'
            WHERE c.status = 'published' AND c.files_deleted_at IS NULL AND q.published_at <= ?""", (cutoff,))
    freed = sum(_delete_clip_files(db, dict(r), root) for r in rows)
    for r in rows:
        log.info("clip %d is on YouTube; deleted its local files", r["id"])
    return freed


def purge_rejected_clips(db: Database, clips_dir: Path, grace_minutes: int, now: datetime | None = None) -> int:
    """Delete the folders of clips rejected more than `grace_minutes` ago (time to look and overrule).
    Resume fetches a past-stream clip again; a live clip's footage is gone once the stream is cleaned up."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=grace_minutes)).isoformat(timespec="seconds")
    rows = db.query("""SELECT id, source_path, vertical_path, final_path, captions_path FROM clips
                        WHERE status = 'rejected' AND files_deleted_at IS NULL AND updated_at <= ?
                          AND COALESCE(source_path, vertical_path, final_path) IS NOT NULL""", (cutoff,))
    freed = sum(_delete_clip_files(db, dict(r), clips_dir.resolve()) for r in rows)
    if rows:
        log.info("deleted the files of %d rejected clip(s)", len(rows))
    return freed


def _delete_clip_files(db: Database, r: dict, root: Path) -> int:
    """Remove a clip's folder (only inside the clips folder) and clear its paths. Returns bytes freed."""
    freed = 0
    files = [Path(p) for p in (r["source_path"], r["vertical_path"], r["final_path"], r["captions_path"]) if p]
    folders = {f.resolve().parent for f in files}
    for folder in folders:
        if not folder.is_relative_to(root) or folder == root:
            log.warning("clip %d: not deleting %s (outside %s)", r["id"], folder, root)
            continue
        if folder.exists():
            freed += sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
            shutil.rmtree(folder)
        parent = folder.parent              # clips/<video_id>/ once its last clip is gone
        if parent != root and parent.is_relative_to(root) and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    # updated_at is left alone: it still says when the clip was published or rejected
    db.execute("""UPDATE clips SET files_deleted_at = ?, source_path = NULL, vertical_path = NULL,
                      final_path = NULL, captions_path = NULL WHERE id = ?""", (utcnow(), r["id"]))
    return freed


def folder_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0
