"""Live-stream monitor.

Each poll:
  1. Gather candidate video IDs per channel from free sources (RSS feed, /live page).
  2. Confirm state with ONE batched videos.list call (1 unit per 50 IDs).
  3. Open/refresh/close rows in live_sessions and notify listeners (recorder, chat — later phases).
"""

import logging
from collections.abc import Callable

import httpx
from dataclasses import dataclass, field

from app.db import Database, utcnow
from app.youtube.api import QuotaExceeded, YouTubeApi, YouTubeApiError
from app.youtube.web import YouTubeWeb

log = logging.getLogger(__name__)

# How many consecutive inconclusive checks before we give up on a session.
MAX_MISSED_CHECKS = 3

SessionListener = Callable[[dict], None]


def classify(item: dict | None) -> str:
    """Map a videos.list item to 'live' | 'upcoming' | 'none' | 'missing'."""
    if item is None:
        return "missing"
    details = item.get("liveStreamingDetails") or {}
    if details.get("actualEndTime"):
        return "none"
    content = item.get("snippet", {}).get("liveBroadcastContent", "none")
    return content if content in ("live", "upcoming") else "none"


def next_session_status(missed_checks: int, item: dict | None) -> tuple[str, int]:
    """Decide whether a session we believe is live is still live.

    Args:
        missed_checks: consecutive inconclusive checks so far for this session.
        item: the videos.list item for the session's video, or None if YouTube
              didn't return it (video went private, was deleted, or a transient API gap).

    Returns:
        (status, missed_checks) where status is 'live' or 'ended'.

    Signals available on item:
        item["liveStreamingDetails"].get("actualEndTime")   -> set once YouTube considers it over
        item["snippet"]["liveBroadcastContent"]              -> 'live' | 'upcoming' | 'none'
    """
    state = classify(item)
    if state == "live":
        return "live", 0
    if item is not None and (item.get("liveStreamingDetails") or {}).get("actualEndTime"):
        return "ended", missed_checks
    # Inconclusive (missing, or not-live without an end time): tolerate a few blips
    # so a transient API gap doesn't cut a recording short.
    missed = missed_checks + 1
    return ("ended" if missed >= MAX_MISSED_CHECKS else "live"), missed


@dataclass
class PollResult:
    channels_checked: int = 0
    candidates: int = 0
    started: list[str] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Monitor:
    def __init__(self, db: Database, api: YouTubeApi, web: YouTubeWeb, live_page_probe: bool = True):
        self.db = db
        self.api = api
        self.web = web
        self.live_page_probe = live_page_probe
        self.on_started: list[SessionListener] = []
        self.on_ended: list[SessionListener] = []

    # -- step 1: candidates from free sources ---------------------------------

    def _collect_candidates(self, channel_id: str, result: PollResult) -> bool:
        """Record candidate IDs; True if at least one free source answered for this channel."""
        ids: list[str] = []
        answered = False
        try:
            ids += self.web.rss_video_ids(channel_id)
            answered = True
        except Exception as e:  # network/parse — the /live probe may still work
            result.errors.append(f"rss {channel_id}: {e}")
            self.db.log_error("monitor.rss", str(e), channel_id)
        if self.live_page_probe:
            try:
                if vid := self.web.live_page_video_id(channel_id):
                    ids.append(vid)
                answered = True
            except Exception as e:
                result.errors.append(f"live-page {channel_id}: {e}")
                self.db.log_error("monitor.live_page", str(e), channel_id)
        now = utcnow()
        for vid in ids:
            self.db.execute(
                "INSERT OR IGNORE INTO videos_seen (video_id, channel_id, first_seen_at) VALUES (?, ?, ?)",
                (vid, channel_id, now),
            )
        return answered

    def _ids_to_check(self) -> list[str]:
        rows = self.db.query(
            """SELECT v.video_id FROM videos_seen v
                 JOIN channels c ON c.channel_id = v.channel_id AND c.enabled = 1
                WHERE v.last_state IN ('unknown', 'live', 'upcoming')
               UNION
               SELECT video_id FROM live_sessions WHERE status = 'live'"""
        )
        return [r["video_id"] for r in rows]

    # -- step 3: session bookkeeping -------------------------------------------

    def _open_or_refresh(self, item: dict, result: PollResult) -> None:
        vid = item["id"]
        details = item.get("liveStreamingDetails") or {}
        now = utcnow()
        # A session we marked 'ended' while YouTube still says live (channel removed and re-added,
        # a long outage, a manual fix) is reopened; it already exists, so a plain INSERT can't.
        cur = self.db.execute(
            """INSERT INTO live_sessions
                   (video_id, channel_id, title, status, live_chat_id, actual_start_at,
                    detected_at, last_seen_live_at)
               VALUES (?, ?, ?, 'live', ?, ?, ?, ?)
               ON CONFLICT (video_id) DO UPDATE SET
                   status = 'live', ended_at = NULL, actual_end_at = NULL, missed_checks = 0,
                   storage_cleaned_at = NULL, last_seen_live_at = excluded.last_seen_live_at,
                   title = excluded.title, live_chat_id = COALESCE(excluded.live_chat_id, live_chat_id)
               WHERE live_sessions.status = 'ended'""",
            (vid, item["snippet"]["channelId"], item["snippet"]["title"],
             details.get("activeLiveChatId"), details.get("actualStartTime"), now, now),
        )
        if cur.rowcount:
            result.started.append(vid)
            session = dict(self.db.one("SELECT * FROM live_sessions WHERE video_id = ?", (vid,)))
            log.info("LIVE: %s (%s)%s", session["title"], vid,
                     " (reopened)" if session["detected_at"] != now else "")
            for listener in self.on_started:
                self._notify(listener, session)

    def _update_live_sessions(self, items: dict[str, dict], result: PollResult) -> None:
        for row in self.db.query("SELECT * FROM live_sessions WHERE status = 'live'"):
            item = items.get(row["video_id"])
            status, missed = next_session_status(row["missed_checks"], item)
            now = utcnow()
            if status == "live":
                details = (item or {}).get("liveStreamingDetails") or {}
                self.db.execute(
                    """UPDATE live_sessions SET missed_checks = ?,
                           last_seen_live_at = CASE WHEN ? = 0 THEN ? ELSE last_seen_live_at END,
                           live_chat_id = COALESCE(?, live_chat_id)
                        WHERE id = ?""",
                    (missed, missed, now, details.get("activeLiveChatId"), row["id"]),
                )
                continue
            end_at = ((item or {}).get("liveStreamingDetails") or {}).get("actualEndTime")
            self.db.execute(
                """UPDATE live_sessions SET status = 'ended', missed_checks = ?,
                       actual_end_at = ?, ended_at = ? WHERE id = ?""",
                (missed, end_at, now, row["id"]),
            )
            result.ended.append(row["video_id"])
            log.info("ENDED: %s (%s)", row["title"], row["video_id"])
            session = dict(self.db.one("SELECT * FROM live_sessions WHERE id = ?", (row["id"],)))
            for listener in self.on_ended:
                self._notify(listener, session)

    def _notify(self, listener: SessionListener, session: dict) -> None:
        # A failing listener (e.g. recorder) must never break monitoring.
        try:
            listener(session)
        except Exception as e:
            log.exception("session listener failed")
            self.db.log_error("monitor.listener", str(e), session["video_id"])

    # -- the poll ----------------------------------------------------------------

    def poll(self) -> PollResult:
        result = PollResult()
        channels = self.db.query("SELECT channel_id FROM channels WHERE enabled = 1")
        answered = [ch["channel_id"] for ch in channels if self._collect_candidates(ch["channel_id"], result)]
        result.channels_checked = len(channels)

        ids = self._ids_to_check()
        result.candidates = len(ids)
        if not ids:
            # Nothing new to confirm is the usual quiet poll; it still counts as a check.
            self._mark_checked(answered)
            return result
        try:
            items = self.api.get_videos(ids)
        except (QuotaExceeded, YouTubeApiError, httpx.HTTPError) as e:
            # Leave sessions untouched: no data (quota, API error, internet down) is not evidence
            # that a stream ended. Recorder and chat keep retrying on their own meanwhile.
            result.errors.append(str(e))
            self.db.log_error("monitor.api", str(e))
            return result

        now = utcnow()
        for vid in ids:
            item = items.get(vid)
            self.db.execute(
                "UPDATE videos_seen SET last_state = ?, last_checked_at = ? WHERE video_id = ?",
                (classify(item), now, vid),
            )
            if classify(item) == "live":
                self._open_or_refresh(item, result)
        self._update_live_sessions(items, result)
        self._mark_checked(answered)
        return result

    def _mark_checked(self, channel_ids: list[str]) -> None:
        # Only channels YouTube actually answered for: during an outage the dashboard's
        # "checked N min ago" should visibly go stale rather than pretend all is well.
        now = utcnow()
        for channel_id in channel_ids:
            self.db.execute("UPDATE channels SET last_checked_at = ? WHERE channel_id = ?", (now, channel_id))
