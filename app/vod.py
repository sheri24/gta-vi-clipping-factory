"""Past-stream (VOD) mode: clip finished streams from their chat replay while nobody is live.

A finished stream on YouTube still has everything the live pipeline uses, in a different shape:
  * Chat replay: every live message with its original timestamp. yt-dlp downloads 6 h of it in about
    a minute with no API quota. Second 0 of the replay is the stream's actualStartTime (measured on
    Lacy's GTA VI finale: 0.3 s apart), so (chat time - offset_utc) = seconds into the VOD.
  * Footage: only the window around each moment is downloaded (an HLS section: about 1 min and 11 MB
    for a 46 s window), never the 10-hour file.
A VOD becomes an ended live_sessions row. Its chat goes through the same buckets and spike detector,
and its clips through the same cut → captions → QC → publish stages; only the time base differs
(clip pts = seconds into the VOD).

Live always wins: the worker pauses while anything is recording, and the cutter, captioner and QC take
live clips first and at most one VOD clip per round.
"""

import json
import logging
import shutil
import statistics
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.chat.store import ChatStore
from app.chat.ytdlp_source import LineParser
from app.db import Database, utcnow
from app.net import is_network_error
from app.procs import kill_tree
from app.recording.media import NO_WINDOW
from app.recording.timeline import estimate_chat_delay
from app.spikes.detector import SpikeDetector, prompted_reason
from app.youtube.api import QuotaExceeded, YouTubeApi, YouTubeApiError
from app.youtube.urls import parse_video_id

log = logging.getLogger(__name__)

INSERT_BATCH = 20_000      # chat messages per executemany
ANALYZE_ATTEMPTS = 3       # info/chat-download failures before a job is marked failed
RETRY_MINUTES = 15
ACTIVE = ("queued", "analyzing", "clipping", "waiting_chat")


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def ffmpeg_location(ffmpeg_bin: str) -> str | None:
    """Folder of the real ffmpeg for yt-dlp's --ffmpeg-location. WinGet puts a symlink on PATH, and
    yt-dlp handed the symlink's folder fails (measured: exit code 3436169992), so resolve it."""
    found = shutil.which(ffmpeg_bin)
    return str(Path(found).resolve().parent) if found else None


# -- yt-dlp ----------------------------------------------------------------------------------------

class VodTools:
    """The few yt-dlp calls VOD mode needs. `runner(cmd, timeout) -> (returncode, stdout, stderr)` is
    injectable so tests never touch the network."""

    def __init__(self, ytdlp_bin: str, ffmpeg_bin: str = "ffmpeg", fmt: str = "b", runner=None):
        self.ytdlp_bin = ytdlp_bin
        self.ffmpeg_dir = ffmpeg_location(ffmpeg_bin)
        self.fmt = fmt
        self.runner = runner or self._run

    @staticmethod
    def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        # Popen + kill_tree rather than subprocess.run: on timeout, yt-dlp's ffmpeg child must die too.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise RuntimeError(f"yt-dlp timed out after {timeout:.0f} s")
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def _call(self, args: list[str], timeout: float) -> str:
        code, out, err = self.runner([self.ytdlp_bin, "--no-update", "--no-warnings", *args], timeout)
        if code != 0:
            lines = [ln for ln in err.splitlines() if ln.strip()]
            raise RuntimeError((lines[-1] if lines else f"yt-dlp exit {code}")[:300])
        return out

    def list_streams(self, channel_id: str, limit: int) -> list[str]:
        """Newest entries of the channel's Live tab (finished, live and upcoming), free."""
        out = self._call(["--flat-playlist", "--playlist-end", str(limit), "--print", "id",
                          f"https://www.youtube.com/channel/{channel_id}/streams"], timeout=120)
        return [ln.strip() for ln in out.splitlines() if len(ln.strip()) == 11]

    def info(self, video_id: str) -> dict:
        d = json.loads(self._call(["-J", "--skip-download", f"https://www.youtube.com/watch?v={video_id}"],
                                  timeout=180))
        return {"title": d.get("title"), "duration": d.get("duration"), "live_status": d.get("live_status"),
                "has_chat": "live_chat" in (d.get("subtitles") or {})}

    def chat_replay(self, video_id: str, out_base: Path) -> Path | None:
        """Download the whole chat replay (one JSON object per line). None if YouTube has none."""
        self._call(["--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", str(out_base),
                    f"https://www.youtube.com/watch?v={video_id}"], timeout=45 * 60)
        path = Path(f"{out_base}.live_chat.json")
        return path if path.exists() and path.stat().st_size else None

    def download_section(self, video_id: str, start: float, end: float, out: Path) -> Path:
        args = ["--no-progress", "-q", "-f", self.fmt, "--download-sections", f"*{start:.2f}-{end:.2f}",
                "--merge-output-format", "mp4", "--force-overwrites", "-o", str(out)]
        if self.ffmpeg_dir:
            args += ["--ffmpeg-location", self.ffmpeg_dir]
        self._call([*args, f"https://www.youtube.com/watch?v={video_id}"], timeout=15 * 60)
        if not out.exists() or not out.stat().st_size:
            raise RuntimeError("yt-dlp finished but wrote no video")
        return out


# -- policy ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VodPolicy:
    max_age_hours: float = 72
    min_minutes: float = 20
    title_keywords: tuple[str, ...] = ("gta 6", "gta vi", "gta6", "gtavi", "grand theft auto")
    max_clips: int = 6
    min_peak_chatters: int = 10   # weaker bursts are left out: uploads are capped, so only strong moments
    default_chat_delay: float = 35
    chat_retry_minutes: int = 60
    chat_retries: int = 6
    streams_per_channel: int = 6
    min_overlap_seconds: float = 10

    @classmethod
    def from_settings(cls, s) -> "VodPolicy":
        return cls(max_age_hours=s.vod_max_age_hours, min_minutes=s.vod_min_minutes,
                   title_keywords=tuple(k.strip().lower() for k in s.vod_title_keywords.split(",") if k.strip()),
                   max_clips=s.vod_max_clips, min_peak_chatters=s.vod_min_peak_chatters,
                   default_chat_delay=s.vod_chat_delay_seconds,
                   chat_retry_minutes=s.vod_chat_retry_minutes, chat_retries=s.vod_chat_retries,
                   streams_per_channel=s.vod_streams_per_channel, min_overlap_seconds=s.clip_min_overlap_seconds)


def skip_reason(title: str, ended: datetime, duration_s: float | None, policy: VodPolicy,
                now: datetime) -> str | None:
    """Why an automatically found stream isn't worth clipping, or None. Pasted links bypass this."""
    if policy.title_keywords and not any(k in (title or "").lower() for k in policy.title_keywords):
        return "title doesn't mention GTA 6"
    if now - ended > timedelta(hours=policy.max_age_hours):
        return f"ended more than {policy.max_age_hours:g} h ago"
    if duration_s is not None and duration_s < policy.min_minutes * 60:
        return f"shorter than {policy.min_minutes:g} min"
    return None


# -- discovery ----------------------------------------------------------------------------------------

class VodScanner:
    """Finds finished streams on tracked channels: the Live tab via yt-dlp (free), then one batched
    videos.list (1 quota unit per 50) for start/end times. Every stream looked at gets a vod_jobs row,
    skipped ones included, so it's never queried again."""

    def __init__(self, db: Database, api: YouTubeApi, tools: VodTools, policy: VodPolicy):
        self.db = db
        self.api = api
        self.tools = tools
        self.policy = policy

    def scan(self, now: datetime | None = None) -> list[int]:
        now = now or datetime.now(timezone.utc)
        new: list[str] = []
        for c in self.db.query("SELECT channel_id FROM channels WHERE vod_enabled = 1 AND record_enabled = 1"):
            try:
                ids = self.tools.list_streams(c["channel_id"], self.policy.streams_per_channel)
            except Exception as e:     # one channel failing (network, YouTube change) never stops the others
                self.db.log_error("vod.scan", str(e)[:200], c["channel_id"])
                continue
            new += [v for v in ids if v not in new and not self.db.one("SELECT 1 FROM vod_jobs WHERE video_id = ?", (v,))]
        if not new:
            return []
        try:
            items = self.api.get_videos(new)
        except (QuotaExceeded, YouTubeApiError, httpx.HTTPError) as e:
            self.db.log_error("vod.scan", str(e)[:200])
            return []
        created = []
        for vid in new:
            if vid in items and (job_id := self._insert(items[vid], "scan", now)):
                created.append(job_id)
        if created:
            log.info("past streams: %d new", len(created))
        return created

    def add_link(self, url: str, now: datetime | None = None) -> dict:
        """Queue one stream by its link, ignoring the age/title filters. Raises ValueError for the UI."""
        now = now or datetime.now(timezone.utc)
        vid = parse_video_id(url)
        job = self.db.one("SELECT * FROM vod_jobs WHERE video_id = ?", (vid,))
        if job and job["status"] in ACTIVE:
            raise ValueError("That stream is already in the list")
        if job and job["status"] == "done":
            raise ValueError(f"Already done: {job['clips'] or 0} clip(s) were made from that stream")
        if job:   # skipped / failed / no_chat: the user is asking for it explicitly
            self.db.execute("""UPDATE vod_jobs SET status = 'queued', origin = 'link', attempts = 0, error = NULL,
                                   next_attempt_at = NULL, updated_at = ? WHERE id = ?""", (utcnow(), job["id"]))
            return dict(self.db.one("SELECT * FROM vod_jobs WHERE id = ?", (job["id"],)))
        try:
            item = self.api.get_videos([vid]).get(vid)
        except (QuotaExceeded, YouTubeApiError, httpx.HTTPError) as e:
            raise ValueError(f"Couldn't ask YouTube about that video: {e}")
        if not item:
            raise ValueError("YouTube doesn't show that video (private, deleted or members-only)")
        sn = item["snippet"]
        if not self.db.one("SELECT 1 FROM channels WHERE channel_id = ?", (sn["channelId"],)):
            raise ValueError(f"Add {sn.get('channelTitle') or 'that channel'} as a streamer first: "
                             "clips are only made for channels you track")
        details = item.get("liveStreamingDetails") or {}
        if not details:
            raise ValueError("That's a regular upload, not a past live stream. Only past streams have the "
                             "chat replay used to find the moments")
        if not details.get("actualEndTime"):
            raise ValueError("That stream hasn't ended yet. If it's live it is being recorded now; "
                             "otherwise it's picked up automatically once it ends")
        job_id = self._insert(item, "link", now, apply_policy=False)
        return dict(self.db.one("SELECT * FROM vod_jobs WHERE id = ?", (job_id,)))

    def _insert(self, item: dict, origin: str, now: datetime, apply_policy: bool = True) -> int | None:
        d = item.get("liveStreamingDetails") or {}
        if not d.get("actualEndTime"):
            return None                    # still live or upcoming: the next scan looks again
        ended = _iso(d["actualEndTime"])
        duration = (ended - _iso(d["actualStartTime"])).total_seconds() if d.get("actualStartTime") else None
        title = item["snippet"].get("title") or ""
        reason = skip_reason(title, ended, duration, self.policy, now) if apply_policy else None
        stamp = utcnow()
        cur = self.db.execute(
            """INSERT INTO vod_jobs (video_id, channel_id, title, started_at, ended_at, duration_s, origin, status,
                                     error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (video_id) DO NOTHING""",
            (item["id"], item["snippet"]["channelId"], title, d.get("actualStartTime"), d["actualEndTime"],
             duration, origin, "skipped" if reason else "queued", reason, stamp, stamp))
        return cur.lastrowid if cur.rowcount else None


# -- processing -----------------------------------------------------------------------------------------

class VodWorker:
    """One stream at a time, newest first (pasted links before everything): chat replay → buckets →
    spikes → up to max_clips clips. The clips are then cut by the ClipWorker like any other; once
    they're all cut the stream's raw chat is deleted (each clip keeps its own chat sample)."""

    def __init__(self, db: Database, tools: VodTools, store: ChatStore, spikes: SpikeDetector,
                 policy: VodPolicy, work_dir: Path, paused: Callable[[], str | None] = lambda: None):
        self.db = db
        self.tools = tools
        self.store = store
        self.spikes = spikes
        self.policy = policy
        self.work_dir = work_dir
        self.paused = paused          # () -> reason to wait (someone is live, disk full) or None

    def _set(self, job_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE vod_jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def run_once(self, now: datetime | None = None) -> int | None:
        """Finish streams whose clips are all cut, then analyse the next one. Returns its job id."""
        now = now or datetime.now(timezone.utc)
        self.finish_jobs()
        if self.paused():
            return None
        if self.db.one("SELECT 1 FROM vod_jobs WHERE status = 'clipping'"):
            return None      # one stream at a time, so its clips come out before the next stream's
        job = self.db.one(
            """SELECT j.* FROM vod_jobs j JOIN channels c ON c.channel_id = j.channel_id
                WHERE c.vod_enabled = 1 AND c.record_enabled = 1 AND j.status IN ('queued', 'waiting_chat')
                  AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?)
                  -- a stream we recorded live: wait until its own end-of-stream cleanup has run,
                  -- or that cleanup would delete the replay chat we're about to load
                  AND NOT EXISTS (SELECT 1 FROM live_sessions s WHERE s.video_id = j.video_id
                                     AND (s.status = 'live' OR s.storage_cleaned_at IS NULL))
                ORDER BY j.origin = 'link' DESC, j.ended_at DESC LIMIT 1""",
            (now.isoformat(timespec="seconds"),))
        if not job:
            return None
        self.process(dict(job), now)
        return job["id"]

    def process(self, job: dict, now: datetime) -> None:
        jid, vid = job["id"], job["video_id"]
        job["attempts"] += 1
        self._set(jid, status="analyzing", attempts=job["attempts"], error=None)
        work = self.work_dir / vid
        try:
            info = self.tools.info(vid)
            if info["live_status"] in ("is_live", "is_upcoming", "post_live"):
                # post_live = YouTube is still processing the archive; chat replay comes after
                self._set(jid, status="waiting_chat", next_attempt_at=self._later(now, 30), attempts=job["attempts"] - 1,
                          error="YouTube is still processing the stream")
                return
            if not info["has_chat"]:
                self._no_chat(job, now)
                return
            work.mkdir(parents=True, exist_ok=True)
            path = self.tools.chat_replay(vid, work / "chat")
            if path is None:
                self._no_chat(job, now)
                return
            sid = self._ensure_session(job)
            added, origins = self._ingest(sid, path)
            total = self.db.one("SELECT COUNT(*) n FROM chat_messages WHERE session_id = ?", (sid,))["n"]
            if not total:
                self._no_chat(job, now)
                return
            offset = statistics.median(origins) if origins else _iso(job["started_at"]).timestamp()
            duration = float(info["duration"] or job["duration_s"] or 0)
            self.store.aggregate(sid, lookback_seconds=int(duration) + 7200)
            delay = self.chat_delay(sid, job["channel_id"])
            self.spikes.run(sid, final=True, lookback_minutes=duration / 60 + 120, chat_delay=delay, source="vod")
            made = self.plan(sid, offset, duration)
            n_spikes = self.db.one("SELECT COUNT(*) n FROM spikes WHERE session_id = ? AND source = 'vod'", (sid,))["n"]
            log.info("past stream %s: %d chat messages, %d spikes, %d clips (delay %.1f s)",
                     vid, total, n_spikes, len(made), delay)
            self._set(jid, status="clipping" if made else "done", session_id=sid, offset_utc=offset,
                      chat_delay_s=delay, chat_messages=total, spikes=n_spikes, duration_s=duration,
                      clips=self.db.one("SELECT COUNT(*) n FROM clips WHERE session_id = ? AND source_kind = 'vod'",
                                        (sid,))["n"])
            if not made:
                self._cleanup(sid, vid)
        except Exception as e:
            if is_network_error(e):
                log.warning("past stream %s: no internet (%s); trying again later", vid, str(e)[:120])
                self._set(jid, status="queued", attempts=job["attempts"] - 1, error="no internet; trying again",
                          next_attempt_at=self._later(now, RETRY_MINUTES))
                return
            log.exception("past stream %s failed", vid)
            self.db.log_error("vod", str(e)[:200], vid)
            final = job["attempts"] >= ANALYZE_ATTEMPTS
            self._set(jid, status="failed" if final else "queued", error=str(e)[:300],
                      next_attempt_at=None if final else self._later(now, RETRY_MINUTES))
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)    # the chat is in the database now

    @staticmethod
    def _later(now: datetime, minutes: float) -> str:
        return (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def _no_chat(self, job: dict, now: datetime) -> None:
        if job["attempts"] >= self.policy.chat_retries:
            self._set(job["id"], status="no_chat", error="YouTube has no chat replay for this stream")
        else:
            self._set(job["id"], status="waiting_chat", next_attempt_at=self._later(now, self.policy.chat_retry_minutes),
                      error="chat replay isn't available yet")

    def _ensure_session(self, job: dict) -> int:
        """The stream's session: the one we recorded live, or a new ended one. New ones are created
        already 'cleaned' (there's no footage on disk); this worker deletes their chat itself."""
        row = self.db.one("SELECT id FROM live_sessions WHERE video_id = ?", (job["video_id"],))
        if row:
            return row["id"]
        now = utcnow()
        return self.db.execute(
            """INSERT INTO live_sessions (video_id, channel_id, title, status, actual_start_at, actual_end_at,
                                          detected_at, ended_at, storage_cleaned_at)
               VALUES (?, ?, ?, 'ended', ?, ?, ?, ?, ?)""",
            (job["video_id"], job["channel_id"], job["title"], job["started_at"], job["ended_at"], now,
             job["ended_at"] or now, now)).lastrowid

    def _ingest(self, session_id: int, path: Path) -> tuple[int, list[float]]:
        """Load the replay into chat_messages. Also returns, per message, timestamp - offset: the epoch
        second of VOD second 0 (they agree to ~0.1 s; the median is our clock mapping)."""
        parser = LineParser()
        batch, origins, added = [], [], 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    msgs = parser.parse_obj(obj)
                except (ValueError, KeyError, TypeError):
                    continue
                off_ms = int((obj.get("replayChatItemAction") or {}).get("videoOffsetTimeMsec") or 0)
                if off_ms > 0:     # 0 = chat from the waiting room before the stream started
                    origins += [m.published_us / 1e6 - off_ms / 1000 for m in msgs if m.kind != "gift"]
                batch += msgs
                if len(batch) >= INSERT_BATCH:
                    added += self.store.add(session_id, batch, "replay")
                    batch = []
        added += self.store.add(session_id, batch, "replay")
        return added, origins

    def chat_delay(self, session_id: int, channel_id: str) -> float:
        """How long chat takes to react: a manual channel setting wins; else what we measured when this
        stream (or the channel's latest stream) was recorded live; else a default from past measurements."""
        ch = self.db.one("SELECT chat_delay_seconds, chat_delay_auto FROM channels WHERE channel_id = ?",
                         (channel_id,))
        if ch and not ch["chat_delay_auto"]:
            return ch["chat_delay_seconds"]
        rows = self.db.query("""SELECT edge_lag_s, target_duration FROM time_anchors
                                 WHERE session_id = ? AND edge_lag_s IS NOT NULL ORDER BY id DESC LIMIT 5""",
                             (session_id,))
        if not rows:
            rows = self.db.query(
                """SELECT a.edge_lag_s, a.target_duration FROM time_anchors a JOIN live_sessions s ON s.id = a.session_id
                    WHERE s.channel_id = ? AND a.edge_lag_s IS NOT NULL ORDER BY a.id DESC LIMIT 5""", (channel_id,))
        if rows:
            return statistics.median(estimate_chat_delay(r["edge_lag_s"], r["target_duration"]) for r in rows)
        return self.policy.default_chat_delay

    def plan(self, session_id: int, offset_utc: float, duration: float) -> list[int]:
        """The strongest spikes become clips (window in seconds into the VOD). A moment we already
        clipped live is linked to that clip instead, unless that clip failed (e.g. footage missing)."""
        budget = self.policy.max_clips - self.db.one(
            "SELECT COUNT(*) n FROM clips WHERE session_id = ? AND source_kind = 'vod'", (session_id,))["n"]
        made: list[int] = []
        for k in self.db.query("""SELECT * FROM spikes WHERE session_id = ? AND source = 'vod' AND status = 'closed'
                                     AND clip_id IS NULL AND skip_reason IS NULL ORDER BY z_score DESC""", (session_id,)):
            if len(made) >= budget:
                break
            reason = (f"only {k['peak_authors']} chatters at the peak" if k["peak_authors"] < self.policy.min_peak_chatters
                      else prompted_reason(self.db, k))
            if reason:
                self.db.execute("UPDATE spikes SET skip_reason = ? WHERE id = ?", (reason, k["id"]))
                continue
            start, end = k["clip_start_utc"] - offset_utc, k["clip_end_utc"] - offset_utc
            if start < 0 or (duration and end > duration + 1):
                # waiting-room chat before the stream started, or after the archive ends
                self.db.execute("UPDATE spikes SET skip_reason = ? WHERE id = ?",
                                ("outside the video (chat before the stream started)" if start < 0
                                 else "outside the video (after the archive ends)", k["id"]))
                continue
            dup = self.db.one(
                """SELECT id FROM clips WHERE session_id = ? AND status != 'failed'
                     AND MIN(window_end_utc, ?) - MAX(window_start_utc, ?) >= ? ORDER BY id LIMIT 1""",
                (session_id, k["clip_end_utc"], k["clip_start_utc"], self.policy.min_overlap_seconds))
            if dup:
                self.db.execute("UPDATE spikes SET clip_id = ? WHERE id = ?", (dup["id"], k["id"]))
                continue
            now = utcnow()
            clip_id = self.db.execute(
                """INSERT INTO clips (session_id, spike_id, window_start_pts, window_end_pts, window_start_utc,
                                      window_end_utc, created_at, updated_at, source_kind)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'vod')""",
                (session_id, k["id"], start, end, k["clip_start_utc"], k["clip_end_utc"], now, now)).lastrowid
            self.db.execute("UPDATE spikes SET clip_id = ? WHERE id = ?", (clip_id, k["id"]))
            made.append(clip_id)
        return made

    def finish_jobs(self) -> list[int]:
        done = []
        for j in self.db.query("SELECT * FROM vod_jobs WHERE status = 'clipping'"):
            waiting = self.db.one("""SELECT COUNT(*) n FROM clips WHERE session_id = ? AND source_kind = 'vod'
                                       AND status IN ('pending', 'cutting')""", (j["session_id"],))["n"]
            if waiting:
                continue
            self._cleanup(j["session_id"], j["video_id"])
            self._set(j["id"], status="done")
            done.append(j["id"])
        return done

    def _cleanup(self, session_id: int, video_id: str) -> None:
        self.store.delete_session_messages(session_id)
        shutil.rmtree(self.work_dir / video_id, ignore_errors=True)


def facecam_probe(db: Database, faces, ffmpeg_bin: str = "ffmpeg") -> Callable[[dict, Path], None]:
    """Facecam detection for past streams, which have no recording segments to sample: run it on the
    first downloaded sections (frames every 4 s) until a camera is found or we give up."""
    from app.clips.facecam import MAX_ATTEMPTS, detect_facecam, sample_frames

    def probe(clip: dict, source: Path) -> None:
        s = db.one("SELECT facecam_rect, facecam_attempts FROM live_sessions WHERE id = ?", (clip["session_id"],))
        if not s or s["facecam_rect"] or s["facecam_attempts"] >= MAX_ATTEMPTS:
            return
        try:
            result = detect_facecam(sample_frames([source], every_seconds=4, ffmpeg_bin=ffmpeg_bin), faces)
        except Exception as e:     # never blocks clipping; blur layout is the fallback
            log.exception("facecam detection failed")
            db.log_error("clips.facecam", str(e)[:200], str(clip["session_id"]))
            result = None
        db.execute("""UPDATE live_sessions SET facecam_rect = ?, facecam_confidence = ?, facecam_method = ?,
                          facecam_checked_at = ?, facecam_attempts = facecam_attempts + 1 WHERE id = ?""",
                   (result.rect_str if result else None, result.confidence if result else None,
                    result.method if result else "none", utcnow(), clip["session_id"]))
        log.info("facecam for past stream session %d: %s", clip["session_id"], result or "none found")

    return probe
