"""Spike → clip planning and the clip-cutting worker."""

import json
import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.clips.render import RenderOptions
from app.db import Database, utcnow
from app.spikes.detector import prompted_reason
from app.net import is_network_error
from app.recording.media import probe
from app.recording.recorder import latest_anchor
from app.recording.timeline import utc_to_pts

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
GAP_TOLERANCE = 1.0   # seconds of missing footage tolerated between consecutive segments
SECTION_PAD = 3.0     # past streams: seconds downloaded either side of the window (keyframe slack)
DOWNLOAD_RETRY_MINUTES = 5   # a failed past-stream download (network) waits this long before retrying


def plan_clips(db: Database, min_overlap: float = 10.0) -> list[int]:
    """Turn closed spikes into clip rows. A spike whose window overlaps an existing clip of the
    same session by >= min_overlap seconds is linked to that clip instead (no duplicate clips).
    Spikes without a time anchor yet are left for a later run."""
    created = []
    spikes = db.query(
        """SELECT k.* FROM spikes k
             JOIN live_sessions s ON s.id = k.session_id
             JOIN channels c ON c.channel_id = s.channel_id
            WHERE k.status = 'closed' AND k.clip_id IS NULL AND c.record_enabled = 1 AND k.source = 'live'
              AND k.skip_reason IS NULL
            ORDER BY k.event_utc""")
    for k in spikes:
        if reason := prompted_reason(db, k):
            db.execute("UPDATE spikes SET skip_reason = ? WHERE id = ?", (reason, k["id"]))
            continue
        start_pts, end_pts = k["clip_start_pts"], k["clip_end_pts"]
        if start_pts is None:
            anchor = latest_anchor(db, k["session_id"])
            if not anchor:
                continue
            start_pts = utc_to_pts(datetime.fromtimestamp(k["clip_start_utc"], tz=timezone.utc), anchor)
            end_pts = utc_to_pts(datetime.fromtimestamp(k["clip_end_utc"], tz=timezone.utc), anchor)
        dup = db.one(
            """SELECT id FROM clips WHERE session_id = ?
                 AND MIN(window_end_pts, ?) - MAX(window_start_pts, ?) >= ?
               ORDER BY id LIMIT 1""",
            (k["session_id"], end_pts, start_pts, min_overlap))
        if dup:
            db.execute("UPDATE spikes SET clip_id = ? WHERE id = ?", (dup["id"], k["id"]))
            continue
        now = utcnow()
        clip_id = db.execute(
            """INSERT INTO clips (session_id, spike_id, window_start_pts, window_end_pts, window_start_utc,
                                  window_end_utc, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (k["session_id"], k["id"], start_pts, end_pts, k["clip_start_utc"], k["clip_end_utc"], now, now),
        ).lastrowid
        db.execute("UPDATE spikes SET clip_id = ? WHERE id = ?", (clip_id, k["id"]))
        created.append(clip_id)
    return created


def covering_segments(db: Database, session_id: int, start: float, end: float) -> tuple[list, str]:
    """Returns (segments, state) where state is 'ok' | 'waiting' | 'missing'."""
    segs = [dict(r) for r in db.query(
        """SELECT * FROM segments WHERE session_id = ? AND deleted_at IS NULL
              AND pts_start < ? AND pts_start + duration > ? ORDER BY seq""",
        (session_id, end, start))]
    session_live = db.one("SELECT status FROM live_sessions WHERE id = ?", (session_id,))["status"] == "live"
    if not segs:
        newest = db.one("SELECT MAX(pts_start + duration) e FROM segments WHERE session_id = ? AND deleted_at IS NULL",
                        (session_id,))["e"]
        # window is still in the future of what's been written → wait for the recorder
        if session_live and (newest is None or newest < end):
            return [], "waiting"
        return [], "missing"
    if segs[0]["pts_start"] > start + GAP_TOLERANCE:
        return segs, "missing"               # recording started after the moment
    for a, b in zip(segs, segs[1:]):
        if b["pts_start"] - (a["pts_start"] + a["duration"]) > GAP_TOLERANCE:
            return segs, "missing"           # recorder restart gap inside the window
    last_end = segs[-1]["pts_start"] + segs[-1]["duration"]
    if last_end < end - GAP_TOLERANCE:
        return segs, "waiting" if session_live else "missing"
    return segs, "ok"


class ClipWorker:
    """Secures each pending clip's source footage. Live clips come from the local recording; past-stream
    clips ('vod') from a section download (vod_fetch). Nothing is rendered here: the 9:16 edit waits until
    the AI review has passed the clip, so rejected clips never cost a render.
    Live clips always go first, and each run does at most one download, so a backlog of past-stream clips
    never delays a fresh live moment by more than one clip."""

    def __init__(self, db: Database, clips_dir: Path, ffprobe_bin: str = "ffprobe",
                 vod_fetch: Callable[[str, float, float, Path], Path] | None = None,
                 vod_paused: Callable[[], bool] = lambda: False,
                 after_vod_fetch: Callable[[dict, Path], None] | None = None):
        self.db = db
        self.clips_dir = clips_dir
        self.ffprobe_bin = ffprobe_bin
        self.vod_fetch = vod_fetch               # (video_id, start_s, end_s, out_path) -> out_path
        self.vod_paused = vod_paused
        self.after_vod_fetch = after_vod_fetch   # e.g. facecam detection on the first downloaded section

    def _set(self, clip_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE clips SET {cols} WHERE id = ?", (*fields.values(), clip_id))

    def run_once(self) -> int | None:
        """Process the oldest ready clip, live ones first. Returns its id if one was cut."""
        retry_before = (datetime.now(timezone.utc) - timedelta(minutes=DOWNLOAD_RETRY_MINUTES)).isoformat(timespec="seconds")
        for clip in self.db.query("SELECT * FROM clips WHERE status = 'pending' ORDER BY source_kind = 'vod', id"):
            if clip["source_kind"] == "vod":
                if self.vod_fetch is None or self.vod_paused():
                    continue
                if clip["error"] and clip["updated_at"] > retry_before:
                    continue        # a failed download waits a few minutes (the internet may be down)
                self._cut_vod(dict(clip))
                return clip["id"]
            segs, state = covering_segments(self.db, clip["session_id"], clip["window_start_pts"],
                                            clip["window_end_pts"])
            if state == "waiting":
                continue
            if state == "missing":
                self._set(clip["id"], status="failed", error="footage not recorded or already deleted")
                continue
            self._cut(dict(clip), segs)
            return clip["id"]
        return None

    def run_all(self) -> list[int]:
        done = []
        while (cid := self.run_once()) is not None:
            done.append(cid)
            if self.db.one("SELECT source_kind FROM clips WHERE id = ?", (cid,))["source_kind"] == "vod":
                break       # one download per run; the next run checks for live clips first
        return done

    def _cut(self, clip: dict, segs: list[dict]) -> None:
        def from_segments(out_dir: Path) -> tuple[Path, float]:
            source = out_dir / "source.ts"
            # MPEG-TS segments from one ffmpeg run concatenate byte-for-byte: lossless, instant,
            # and the stream's own PTS survive, so the window offset stays exact.
            with open(source, "wb") as dst:
                for s in segs:
                    with open(s["path"], "rb") as src:
                        shutil.copyfileobj(src, dst)
            return source, probe(source, self.ffprobe_bin).start_time
        self._make(clip, from_segments)

    def _cut_vod(self, clip: dict) -> None:
        def from_download(out_dir: Path) -> tuple[Path, float]:
            start = max(0.0, clip["window_start_pts"] - SECTION_PAD)
            source = self.vod_fetch(clip["video_id"], start, clip["window_end_pts"] + SECTION_PAD,
                                    out_dir / "source.mp4")
            if self.after_vod_fetch:
                try:
                    self.after_vod_fetch(clip, source)
                except Exception:
                    log.exception("after-download hook failed for clip %d", clip["id"])
            # file time 0 = VOD second `start`: keep source_start in VOD seconds like the clip window
            return source, start + probe(source, self.ffprobe_bin).start_time
        self._make(clip, from_download)

    def _make(self, clip: dict, build_source: Callable[[Path], tuple[Path, float]]) -> None:
        cid = clip["id"]
        video_id = self.db.one("SELECT video_id FROM live_sessions WHERE id = ?", (clip["session_id"],))["video_id"]
        clip["video_id"] = video_id
        out_dir = self.clips_dir / video_id / f"clip{cid:05d}"
        self._set(cid, status="cutting", attempts=clip["attempts"] + 1)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            source, source_start = build_source(out_dir)
            duration = clip["window_end_pts"] - clip["window_start_pts"]
            self._set(cid, status="cut", attempts=0, source_path=str(source), source_start_pts=source_start,
                      duration=duration, error=None, chat_sample=json.dumps(chat_sample(self.db, clip)))
            log.info("clip %d source ready (%.0f s)", cid, duration)
        except Exception as e:
            if clip.get("source_kind") == "vod" and is_network_error(e):
                log.warning("clip %d: no internet (%s); trying again later", cid, str(e)[:120])
                self._set(cid, status="pending", attempts=clip["attempts"], error=f"no internet, will retry: {str(e)[-200:]}")
                return
            log.exception("clip %d failed", cid)
            final = clip["attempts"] + 1 >= MAX_ATTEMPTS
            self._set(cid, status="failed" if final else "pending", error=str(e)[-500:])
            self.db.log_error("clips.cut", str(e)[:200], str(cid))


def chat_sample(db: Database, clip: dict, limit: int = 60) -> list[dict]:
    """Chat messages from just before the spike to shortly after it: the best description of
    'what just happened' we have. Snapshotted because raw chat is deleted with the footage."""
    k = db.one("SELECT start_bucket, end_bucket FROM spikes WHERE id = ?", (clip.get("spike_id"),)) \
        if clip.get("spike_id") else None
    lo, hi = (k["start_bucket"] - 5, k["end_bucket"] + 20) if k else (clip["window_start_utc"], clip["window_end_utc"])
    rows = db.query(
        """SELECT published_us, author_name, text FROM chat_messages
            WHERE session_id = ? AND kind IN ('text', 'paid') AND text IS NOT NULL AND text != ''
              AND published_us BETWEEN ? AND ? ORDER BY published_us LIMIT ?""",
        (clip["session_id"], int(lo * 1e6), int(hi * 1e6), limit))
    return [{"t": round(r["published_us"] / 1e6 - lo, 1), "a": r["author_name"], "m": r["text"]} for r in rows]


def choose_layout(channel_layout: str | None, manual_rect: str | None, detected_rect: str | None) -> tuple[str, str | None]:
    """Manual rectangle beats detection; 'blur' forces blur; split needs *some* rectangle."""
    rect = manual_rect or detected_rect
    if channel_layout == "blur" or not rect:
        return "blur", None
    return "split", rect          # 'auto' or 'split' with a rectangle available


def render_options_factory(db: Database, settings):
    def for_clip(clip: dict) -> RenderOptions:
        row = db.one("""SELECT c.layout, c.facecam_rect manual, s.facecam_rect detected FROM live_sessions s
                          JOIN channels c ON c.channel_id = s.channel_id WHERE s.id = ?""", (clip["session_id"],))
        layout, rect = choose_layout(row["layout"], row["manual"], row["detected"]) if row else ("blur", None)
        return RenderOptions(layout=layout, facecam_rect=rect, encoder=settings.render_encoder,
                             crf=settings.render_crf, loudness_lufs=settings.loudness_lufs,
                             ffmpeg_bin=settings.ffmpeg_bin)
    return for_clip
