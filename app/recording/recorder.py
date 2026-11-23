"""Recording: one Recorder thread per live session, supervised by RecorderManager.

Pipeline per part:   source (streamlink | yt-dlp) --stdout MPEG-TS-->  ffmpeg -c copy -f segment
  * stream copy (no re-encode) → negligible CPU
  * -copyts keeps YouTube's PTS (seconds since broadcast start) so time anchors stay valid
  * a crash loses at most the segment being written; earlier segments are already final
"""

import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from app.db import Database, utcnow
from app.procs import kill_tree
from app.recording.media import NO_WINDOW, probe
from app.recording.timeline import Anchor, pts_to_utc, sample_anchor

log = logging.getLogger(__name__)

SourceFactory = Callable[[dict, str], list[str]]   # (session, backend) -> argv writing TS to stdout
BACKENDS = ("streamlink", "ytdlp")


def watch_url(session: dict) -> str:
    return f"https://www.youtube.com/watch?v={session['video_id']}"


def default_source_factory(settings) -> SourceFactory:
    def build(session: dict, backend: str) -> list[str]:
        url = watch_url(session)
        if backend == "streamlink":
            return [settings.streamlink_bin, "--stdout", "--loglevel", "warning",
                    "--stream-timeout", "60", url, settings.record_quality]
        return [settings.ytdlp_bin, "--no-update", "--no-part", "-q",
                "-f", "best[protocol^=m3u8][height<=720]/best[height<=720]/best", "-o", "-", url]
    return build


def latest_anchor(db: Database, session_id: int) -> Anchor | None:
    row = db.one("SELECT pts, utc FROM time_anchors WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                 (session_id,))
    return Anchor(row["pts"], datetime.fromisoformat(row["utc"])) if row else None


def backfill_utc(db: Database, session_id: int) -> int:
    """Fill utc_start for segments indexed before the first anchor arrived."""
    anchor = latest_anchor(db, session_id)
    if not anchor:
        return 0
    rows = db.query("SELECT id, pts_start FROM segments WHERE session_id = ? AND utc_start IS NULL",
                    (session_id,))
    for r in rows:
        db.execute("UPDATE segments SET utc_start = ? WHERE id = ?",
                   (pts_to_utc(r["pts_start"], anchor).isoformat(), r["id"]))
    return len(rows)


def disk_free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 1024**3


class Recorder(threading.Thread):
    def __init__(self, db: Database, session: dict, out_dir: Path, source_factory: SourceFactory,
                 ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe", segment_seconds: int = 60,
                 is_live: Callable[[], bool] | None = None, index_interval: float = 3.0,
                 min_healthy_seconds: float = 120.0):
        super().__init__(name=f"recorder-{session['video_id']}", daemon=True)
        self.db = db
        self.session = session
        self.out_dir = out_dir
        self.source_factory = source_factory
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin
        self.segment_seconds = segment_seconds
        self.is_live = is_live or self._session_is_live
        self.index_interval = index_interval
        self.min_healthy_seconds = min_healthy_seconds
        self.stop_event = threading.Event()
        self.backend = BACKENDS[0]
        self._known: set[str] = set()

    # -- helpers -------------------------------------------------------------------

    def _session_is_live(self) -> bool:
        row = self.db.one("SELECT status FROM live_sessions WHERE id = ?", (self.session["id"],))
        return bool(row) and row["status"] == "live"

    def _next_seq(self) -> int:
        nums = [int(p.stem.split("_")[1]) for p in self.out_dir.glob("seg_*.ts")]
        row = self.db.one("SELECT MAX(seq) m FROM segments WHERE session_id = ?", (self.session["id"],))
        return max(nums + [row["m"] or 0]) + 1

    def index_segments(self, recording_id: int | None, final: bool) -> int:
        """Register finished segment files. While ffmpeg runs, the newest file is still open."""
        files = sorted(self.out_dir.glob("seg_*.ts"))
        if not final:
            files = files[:-1]
        anchor = latest_anchor(self.db, self.session["id"])
        added = 0
        for f in files:
            key = str(f)
            if key in self._known:
                continue
            self._known.add(key)
            try:
                p = probe(f, self.ffprobe_bin)
            except Exception as e:  # truncated last segment after a hard kill, etc.
                self.db.log_error("recorder.index", str(e), key)
                continue
            utc = pts_to_utc(p.start_time, anchor).isoformat() if anchor else None
            cur = self.db.execute(
                """INSERT INTO segments (session_id, recording_id, seq, path, pts_start, duration,
                                         utc_start, size_bytes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (path) DO NOTHING""",
                (self.session["id"], recording_id, int(f.stem.split("_")[1]), key, p.start_time,
                 p.duration, utc, f.stat().st_size, utcnow()),
            )
            added += cur.rowcount
        return added

    # -- one recorder run ------------------------------------------------------------

    def _run_part(self, backend: str) -> tuple[int | None, str | None]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        row = self.db.one("SELECT COALESCE(MAX(part), 0) + 1 AS p FROM recordings WHERE session_id = ?",
                          (self.session["id"],))
        part = row["p"]
        rec_id = self.db.execute(
            "INSERT INTO recordings (session_id, part, backend, status, started_at) VALUES (?, ?, ?, 'running', ?)",
            (self.session["id"], part, backend, utcnow()),
        ).lastrowid

        src_log = open(self.out_dir / f"part{part:03d}.{backend}.log", "wb")
        ff_log = open(self.out_dir / f"part{part:03d}.ffmpeg.log", "wb")
        ffmpeg_cmd = [
            self.ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-copyts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(self.segment_seconds), "-segment_format", "mpegts",
            "-segment_start_number", str(self._next_seq()),
            str(self.out_dir / "seg_%06d.ts"),
        ]
        log.info("recording %s part %d via %s", self.session["video_id"], part, backend)
        src = subprocess.Popen(self.source_factory(self.session, backend), stdout=subprocess.PIPE,
                               stderr=src_log, creationflags=NO_WINDOW)
        ff = subprocess.Popen(ffmpeg_cmd, stdin=src.stdout, stderr=ff_log, creationflags=NO_WINDOW)
        src.stdout.close()  # ffmpeg owns the read end now; lets src get SIGPIPE if ffmpeg dies

        try:
            while ff.poll() is None:
                if self.stop_event.wait(self.index_interval):
                    kill_tree(src)  # whole tree, or the real streamlink keeps writing; ffmpeg then sees EOF
                    try:
                        ff.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        ff.kill()
                    break
                self.index_segments(rec_id, final=False)
                if src.poll() is not None and ff.poll() is None:
                    try:
                        ff.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        ff.kill()
        finally:
            kill_tree(src)
            src_code = src.wait(timeout=20)
            src_log.close()
            ff_log.close()

        self.index_segments(rec_id, final=True)
        stopped = self.stop_event.is_set()
        err = None
        if not stopped and src_code != 0:
            err = (self.out_dir / f"part{part:03d}.{backend}.log").read_text(errors="replace")[-500:]
        self.db.execute(
            "UPDATE recordings SET status = ?, stopped_at = ?, exit_code = ?, error = ? WHERE id = ?",
            ("stopped" if stopped or src_code == 0 else "failed", utcnow(), src_code, err, rec_id),
        )
        return src_code, err

    # -- supervisor loop -------------------------------------------------------------

    def run(self) -> None:
        failures = 0
        while not self.stop_event.is_set() and self.is_live():
            # Swap backend after every 2 consecutive short-lived runs.
            self.backend = BACKENDS[(failures // 2) % len(BACKENDS)]
            started = time.monotonic()
            try:
                code, err = self._run_part(self.backend)
            except Exception as e:
                log.exception("recorder part crashed")
                self.db.log_error("recorder", str(e), self.session["video_id"])
                code, err = -1, str(e)
            if self.stop_event.is_set():
                break
            healthy = time.monotonic() - started >= self.min_healthy_seconds
            failures = 0 if healthy else failures + 1
            if err:
                self.db.log_error("recorder.part", f"{self.backend} exited {code}", err[-300:])
            delay = 2 if healthy else min(60, 5 * 2 ** min(failures - 1, 4))
            log.info("recorder %s: restarting in %ss (failures=%d)", self.session["video_id"], delay, failures)
            self.stop_event.wait(delay)
        log.info("recorder %s finished", self.session["video_id"])

    def stop(self) -> None:
        self.stop_event.set()


class RecorderManager:
    def __init__(self, db: Database, settings, source_factory: SourceFactory | None = None,
                 hls_url_resolver: Callable[[dict], str] | None = None, http: httpx.Client | None = None):
        self.db = db
        self.settings = settings
        self.source_factory = source_factory or default_source_factory(settings)
        self.hls_url_resolver = hls_url_resolver or self._streamlink_hls_url
        self.http = http or httpx.Client(timeout=20)
        self.recorders: dict[int, Recorder] = {}
        self.lock = threading.Lock()

    def _streamlink_hls_url(self, session: dict) -> str:
        out = subprocess.run([self.settings.streamlink_bin, "--stream-url", watch_url(session),
                              self.settings.record_quality], capture_output=True, text=True,
                             timeout=60, creationflags=NO_WINDOW)
        url = out.stdout.strip()
        if out.returncode != 0 or not url.startswith("http"):
            raise RuntimeError(f"could not resolve HLS URL: {(out.stdout + out.stderr)[-300:]}")
        return url

    # -- gating -------------------------------------------------------------------------

    def _can_record(self, session: dict) -> str | None:
        """Returns a reason string if we must NOT record, else None."""
        ch = self.db.one("SELECT enabled, record_enabled FROM channels WHERE channel_id = ?",
                         (session["channel_id"],))
        if not ch or not ch["enabled"]:
            return "channel disabled or removed"
        if not ch["record_enabled"]:
            return "recording not enabled for this channel"
        free = disk_free_gb(self.settings.recordings_dir)
        if free < self.settings.min_free_gb:
            return f"only {free:.1f} GB free (< {self.settings.min_free_gb} GB)"
        return None

    # -- monitor listeners -------------------------------------------------------------

    def on_session_started(self, session: dict) -> None:
        with self.lock:
            existing = self.recorders.get(session["id"])
            if existing and existing.is_alive():
                return
            if reason := self._can_record(session):
                log.info("not recording %s: %s", session["video_id"], reason)
                if reason.startswith("only"):
                    self.db.log_error("recorder.disk", reason, session["video_id"])
                return
            rec = Recorder(self.db, session, self.settings.recordings_dir / session["video_id"],
                           self.source_factory, self.settings.ffmpeg_bin, self.settings.ffprobe_bin,
                           self.settings.segment_seconds)
            self.recorders[session["id"]] = rec
            rec.start()
        threading.Thread(target=self.sample_anchor_for, args=(session,), daemon=True).start()

    def on_session_ended(self, session: dict) -> None:
        with self.lock:
            rec = self.recorders.pop(session["id"], None)
        if rec:
            rec.stop()

    def resume(self) -> None:
        """At app start: restart recorders for sessions still marked live."""
        for row in self.db.query("SELECT * FROM live_sessions WHERE status = 'live'"):
            self.on_session_started(dict(row))

    def stop_all(self, timeout: float = 25) -> None:
        with self.lock:
            recs = list(self.recorders.values())
            self.recorders.clear()
        for r in recs:
            r.stop()
        for r in recs:
            r.join(timeout)

    def is_recording(self, session_id: int) -> bool:
        rec = self.recorders.get(session_id)
        return bool(rec and rec.is_alive())

    # -- time anchors ----------------------------------------------------------------------

    def sample_anchor_for(self, session: dict, attempts: int = 3, retry_wait: float = 5.0) -> Anchor | None:
        # YouTube occasionally drops a connection; a missed anchor otherwise waits 10 min for the next run.
        for attempt in range(1, attempts + 1):
            try:
                anchor = sample_anchor(self.hls_url_resolver(session), self.http, self.settings.ffprobe_bin)
                break
            except Exception as e:  # never affects recording
                if attempt == attempts:
                    self.db.log_error("recorder.anchor", str(e), session["video_id"])
                    return None
                time.sleep(retry_wait * attempt)
        self.db.execute(
            """INSERT INTO time_anchors (session_id, pts, utc, sampled_at, edge_lag_s, target_duration)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session["id"], anchor.pts, anchor.utc.isoformat(), utcnow(), anchor.edge_lag, anchor.target_duration),
        )
        backfill_utc(self.db, session["id"])
        return anchor

    def sample_anchors(self) -> None:
        for rec in list(self.recorders.values()):
            if rec.is_alive():
                self.sample_anchor_for(rec.session)
