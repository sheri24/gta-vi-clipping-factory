"""Publishing queue: approved + permitted clips wait in a bank and go out on a posting strategy.

Gates, in order: master switch → publisher ready (signed in) → interval since last upload →
daily cap (Pacific-time day, like YouTube's quota) → the strategy picks what (if anything) goes now:
  * breaking lane: a very strong, very fresh live clip posts at once (that's how you beat other clippers)
  * otherwise one post per slot, spread over the audience's evening; the clip with the best
    score, discounted for age and for repeating the last post's streamer
  * clips whose moment is too old are dropped ('expired'): everyone has posted it by then
Without a strategy (no slots configured) the queue posts best-first as soon as gates allow.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db import Database, utcnow
from app.publish.metadata import clip_context
from app.publish.youtube import LOCKED_PRIVATE_HELP, PublishError
from app.youtube.api import PACIFIC

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
BACKOFF_MINUTES = (5, 15, 60)
NETWORK_RETRY_MINUTES = 5     # internet down: try again this often, without using up an attempt
PRIVACY_CHECK_AFTER = timedelta(minutes=10)   # YouTube may lock a video once processing finishes


@dataclass(frozen=True)
class PostingStrategy:
    slots: tuple[time, ...] = (time(12), time(14, 30), time(17), time(19), time(21), time(23))
    tz: ZoneInfo = ZoneInfo("America/New_York")    # the audience's clock (these streamers: mostly US)
    breaking_score: float = 8.0
    breaking_minutes: float = 120                   # moment this recent + score this high = post now
    max_age_hours: float = 96                       # older moments are dropped (past streams: up to 72 h old)
    per_stream_per_day: int = 2
    freshness_per_day: float = 1.0                  # score points lost per day since the moment
    same_channel_penalty: float = 1.0               # variety: avoid the same streamer twice in a row

    @classmethod
    def from_settings(cls, s) -> "PostingStrategy | None":
        slots = tuple(time.fromisoformat(t.strip()) for t in s.publish_slots.split(",") if t.strip())
        if not slots:
            return None
        return cls(slots=tuple(sorted(slots)), tz=ZoneInfo(s.publish_slots_timezone),
                   breaking_score=s.publish_breaking_score, breaking_minutes=s.publish_breaking_minutes,
                   max_age_hours=s.publish_max_age_hours, per_stream_per_day=s.publish_max_per_stream_per_day)

    def _at(self, day, t: time) -> datetime:
        return datetime.combine(day, t, tzinfo=self.tz).astimezone(timezone.utc)

    def last_slot(self, now: datetime) -> datetime:
        """The most recent slot at or before `now` (yesterday's last one before today's first)."""
        today = now.astimezone(self.tz).date()
        times = [self._at(today - timedelta(days=d), t) for d in (1, 0) for t in self.slots]
        return max(t for t in times if t <= now)

    def next_slot(self, now: datetime) -> datetime:
        today = now.astimezone(self.tz).date()
        return min(t for d in (0, 1) for t in (self._at(today + timedelta(days=d), s) for s in self.slots) if t > now)

    def day_start(self, now: datetime) -> datetime:
        return self._at(now.astimezone(self.tz).date(), time(0))


def next_pacific_midnight(now: datetime) -> datetime:
    local = now.astimezone(PACIFIC)
    return (local + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0).astimezone(timezone.utc)


VOD_PRIORITY_PENALTY = 1.0   # a fresh live moment beats an equally good one from a past stream


def priority(score: float | None, source_kind: str) -> float:
    return (score or 0) - (VOD_PRIORITY_PENALTY if source_kind == "vod" else 0)


def enqueue_approved(db: Database, platform: str = "youtube") -> list[int]:
    """Queue approved clips whose channel has publishing permission. UNIQUE(clip, platform) = no duplicates."""
    rows = db.query(
        """SELECT x.id, x.score, x.source_kind FROM clips x
             JOIN live_sessions s ON s.id = x.session_id
             JOIN channels c ON c.channel_id = s.channel_id
            WHERE x.status = 'approved' AND c.permission_confirmed = 1 AND x.nudity_checked_at IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM publish_queue q WHERE q.clip_id = x.id AND q.platform = ?)""",
        (platform,))
    now = utcnow()
    for r in rows:
        db.execute("""INSERT OR IGNORE INTO publish_queue (clip_id, platform, priority, queued_at, updated_at)
                      VALUES (?, ?, ?, ?, ?)""", (r["id"], platform, priority(r["score"], r["source_kind"]), now, now))
    return [r["id"] for r in rows]


def blocked_by_permission(db: Database) -> list[dict]:
    """Approved clips that won't be queued because the creator hasn't agreed: shown on the dashboard."""
    return [dict(r) for r in db.query(
        """SELECT x.id clip_id, c.title channel, x.score FROM clips x
             JOIN live_sessions s ON s.id = x.session_id JOIN channels c ON c.channel_id = s.channel_id
            WHERE x.status = 'approved' AND c.permission_confirmed = 0 ORDER BY x.score DESC""")]


class PublishQueue:
    def __init__(self, db: Database, publisher, writer, *, enabled: bool, privacy: str, interval_minutes: int,
                 max_per_day: int, strategy: PostingStrategy | None = None):
        self.db = db
        self.strategy = strategy
        self.publisher = publisher
        self.writer = writer
        self.enabled = enabled
        self.privacy = privacy
        self.interval = timedelta(minutes=interval_minutes)
        self.max_per_day = max_per_day

    # -- gating -----------------------------------------------------------------------------

    def published_today(self, now: datetime) -> int:
        start = now.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        return self.db.one("SELECT COUNT(*) n FROM publish_queue WHERE status = 'published' AND published_at >= ?",
                           (start.isoformat(timespec="seconds"),))["n"]

    def why_not_now(self, now: datetime | None = None) -> str | None:
        """Why nothing goes out right now (for the dashboard), or None when an upload may happen."""
        now = now or datetime.now(timezone.utc)
        reason = self._gate(now)
        if reason or not self.strategy or self._breaking(now) or not self._slot_used(now):
            return reason
        nxt = self.strategy.next_slot(now)
        return (f"next posting slot {nxt.astimezone(self.strategy.tz):%H:%M} US time "
                f"({nxt.astimezone():%H:%M} here)")

    def _gate(self, now: datetime) -> str | None:
        if not self.enabled:
            return "publishing disabled (PUBLISH_ENABLED=false)"
        ok, reason = self.publisher.ready()
        if not ok:
            return reason
        last = self.db.one("SELECT MAX(published_at) t FROM publish_queue WHERE status = 'published'")["t"]
        if last and now - datetime.fromisoformat(last) < self.interval:
            return f"next upload after {(datetime.fromisoformat(last) + self.interval).isoformat(timespec='minutes')}"
        if self.published_today(now) >= self.max_per_day:
            return f"daily cap of {self.max_per_day} reached"
        return None

    def next_item(self, now: datetime) -> dict | None:
        if self.strategy:
            return self._breaking(now) or (None if self._slot_used(now) else self._best_for_slot(now))
        row = self.db.one(
            """SELECT q.* FROM publish_queue q JOIN clips x ON x.id = q.clip_id
                WHERE q.status = 'queued' AND x.nudity_checked_at IS NOT NULL
                 AND (q.next_attempt_at IS NULL OR q.next_attempt_at <= ?)
               ORDER BY q.priority DESC, q.queued_at LIMIT 1""", (now.isoformat(timespec="seconds"),))
        return dict(row) if row else None

    # -- posting strategy -------------------------------------------------------------------------

    def _candidates(self, now: datetime) -> list[dict]:
        rows = self.db.query(
            """SELECT q.*, x.score, x.source_kind, x.window_start_utc moment_utc, x.session_id, s.channel_id
                 FROM publish_queue q JOIN clips x ON x.id = q.clip_id JOIN live_sessions s ON s.id = x.session_id
                WHERE q.status = 'queued' AND x.nudity_checked_at IS NOT NULL
                  AND (q.next_attempt_at IS NULL OR q.next_attempt_at <= ?)""",
            (now.isoformat(timespec="seconds"),))
        return [{**dict(r), "age_hours": (now.timestamp() - r["moment_utc"]) / 3600} for r in rows]

    def _breaking(self, now: datetime) -> dict | None:
        st = self.strategy
        hot = [c for c in self._candidates(now) if c["source_kind"] == "live" and (c["score"] or 0) >= st.breaking_score
               and c["age_hours"] * 60 <= st.breaking_minutes]
        return max(hot, key=lambda c: c["score"], default=None)

    def _slot_used(self, now: datetime) -> bool:
        last = self.db.one("SELECT MAX(published_at) t FROM publish_queue WHERE status = 'published'")["t"]
        return bool(last) and datetime.fromisoformat(last) >= self.strategy.last_slot(now)

    def _best_for_slot(self, now: datetime) -> dict | None:
        st = self.strategy
        last = self.db.one("""SELECT s.channel_id FROM publish_queue q JOIN clips x ON x.id = q.clip_id
                                JOIN live_sessions s ON s.id = x.session_id
                               WHERE q.status = 'published' ORDER BY q.published_at DESC LIMIT 1""")
        day = st.day_start(now).isoformat(timespec="seconds")
        posted_today = {r["session_id"]: r["n"] for r in self.db.query(
            """SELECT x.session_id, COUNT(*) n FROM publish_queue q JOIN clips x ON x.id = q.clip_id
                WHERE q.status = 'published' AND q.published_at >= ? GROUP BY x.session_id""", (day,))}

        def worth(c: dict) -> float:
            return (c["priority"] - st.freshness_per_day * max(0.0, c["age_hours"]) / 24
                    - (st.same_channel_penalty if last and c["channel_id"] == last["channel_id"] else 0))

        ok = [c for c in self._candidates(now) if posted_today.get(c["session_id"], 0) < st.per_stream_per_day]
        return max(ok, key=worth, default=None)

    def expire_old(self, now: datetime) -> int:
        """Drop queued clips whose moment is older than the strategy allows."""
        if not self.strategy:
            return 0
        n = 0
        for c in self.db.query("""SELECT q.id, x.window_start_utc m FROM publish_queue q
                                    JOIN clips x ON x.id = q.clip_id WHERE q.status = 'queued'"""):
            age = (now.timestamp() - c["m"]) / 3600
            if age > self.strategy.max_age_hours:
                self._set(c["id"], status="expired",
                          error=f"not posted: the moment was {age / 24:.1f} days old (limit {self.strategy.max_age_hours:g} h)")
                n += 1
        return n

    # -- work -------------------------------------------------------------------------------------

    def _set(self, qid: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE publish_queue SET {cols} WHERE id = ?", (*fields.values(), qid))

    def ensure_metadata(self, item: dict) -> dict:
        if item["title"]:
            return item
        md = self.writer.write(clip_context(self.db, item["clip_id"]))
        self._set(item["id"], title=md.title, description=md.description, tags=json.dumps(md.tags),
                  metadata_source=md.source)
        return dict(self.db.one("SELECT * FROM publish_queue WHERE id = ?", (item["id"],)))

    def run_once(self, now: datetime | None = None) -> dict | None:
        """Enqueue new approvals, then publish at most one item if all gates allow. Returns the item touched."""
        now = now or datetime.now(timezone.utc)
        enqueue_approved(self.db, self.publisher.platform)
        if self.enabled:
            self.verify_privacy(now)
        self.expire_old(now)
        if self._gate(now):
            return None
        item = self.next_item(now)
        if not item:
            return None
        return self.publish(item, now)

    def publish(self, item: dict, now: datetime) -> dict:
        qid, prior_attempts = item["id"], item["attempts"]
        attempt_id = self.db.execute("INSERT INTO publish_attempts (queue_id, started_at) VALUES (?, ?)",
                                     (qid, utcnow())).lastrowid
        self._set(qid, status="uploading", attempts=prior_attempts + 1)
        try:
            item = self.ensure_metadata(item)
            clip = self.db.one("SELECT final_path FROM clips WHERE id = ?", (item["clip_id"],))
            result = self.publisher.upload(Path(clip["final_path"]), item["title"], item["description"],
                                           json.loads(item["tags"] or "[]"), self.privacy)
        except PublishError as e:
            return self._failed(qid, item["clip_id"], prior_attempts, attempt_id, e, now)
        except Exception as e:
            return self._failed(qid, item["clip_id"], prior_attempts, attempt_id, PublishError(str(e), "retry"), now)
        # `now` (not the wall clock) so interval/cap checks and history share one clock
        self._set(qid, status="published", remote_id=result.remote_id, remote_url=result.url, privacy=self.privacy,
                  privacy_actual=result.privacy, published_at=now.isoformat(timespec="seconds"), error=None,
                  next_attempt_at=None)
        self._note_privacy(qid, item["clip_id"], self.privacy, result.privacy)
        self.db.execute("UPDATE clips SET status = 'published', updated_at = ? WHERE id = ?", (utcnow(), item["clip_id"]))
        self.db.execute("UPDATE publish_attempts SET finished_at = ?, outcome = 'published' WHERE id = ?",
                        (utcnow(), attempt_id))
        log.info("published clip %d → %s (%s)", item["clip_id"], result.url, self.privacy)
        return dict(self.db.one("SELECT * FROM publish_queue WHERE id = ?", (qid,)))

    def verify_privacy(self, now: datetime) -> None:
        """Re-read the privacy of recent non-private uploads once YouTube has processed them."""
        check = getattr(self.publisher, "privacy_of", None)
        if not check or not self.publisher.ready()[0]:
            return
        rows = self.db.query(
            """SELECT id, clip_id, remote_id, privacy FROM publish_queue
                WHERE status = 'published' AND privacy != 'private' AND privacy_checked_at IS NULL
                  AND remote_id IS NOT NULL AND published_at <= ? ORDER BY id LIMIT 3""",
            ((now - PRIVACY_CHECK_AFTER).isoformat(timespec="seconds"),))
        for r in rows:
            try:
                actual = check(r["remote_id"])
            except Exception as e:
                log.warning("privacy check of %s failed: %s", r["remote_id"], e)
                return
            self._set(r["id"], privacy_actual=actual, privacy_checked_at=utcnow())
            self._note_privacy(r["id"], r["clip_id"], r["privacy"], actual)

    def _note_privacy(self, qid: int, clip_id: int, requested: str, actual: str | None) -> None:
        if actual and actual != requested:
            log.warning("clip %d: asked for %s, YouTube applied %s", clip_id, requested, actual)
            self._set(qid, error=LOCKED_PRIVATE_HELP if actual == "private" else f"YouTube applied {actual}")
            self.db.log_error("publish.privacy", f"clip {clip_id} is {actual} on YouTube, not {requested}", str(clip_id))

    def _failed(self, qid: int, clip_id: int, prior_attempts: int, attempt_id: int, e: PublishError,
                now: datetime) -> dict:
        attempts = prior_attempts + 1
        if e.kind == "network":
            # Internet down: keep it queued without using an attempt; it still counts for this slot once it goes.
            self._set(qid, status="queued", attempts=prior_attempts, error=str(e)[:500],
                      next_attempt_at=(now + timedelta(minutes=NETWORK_RETRY_MINUTES)).isoformat(timespec="seconds"))
            self.db.execute("UPDATE publish_attempts SET finished_at = ?, outcome = 'retry', error = ? WHERE id = ?",
                            (utcnow(), str(e)[:500], attempt_id))
            log.warning("upload of clip %d waiting for the internet: %s", clip_id, str(e)[:120])
            return dict(self.db.one("SELECT * FROM publish_queue WHERE id = ?", (qid,)))
        if e.kind == "quota":
            # Not the clip's fault: wait for YouTube's daily reset, don't burn an attempt.
            status, retry_at, attempts = "queued", next_pacific_midnight(now), prior_attempts
        elif e.kind == "auth":
            # Needs the user to sign in again; keep it queued and retry hourly.
            status, retry_at, attempts = "queued", now + timedelta(hours=1), prior_attempts
        elif e.kind == "fatal" or attempts >= MAX_ATTEMPTS:
            status, retry_at = "failed", None
        else:
            status, retry_at = "queued", now + timedelta(minutes=BACKOFF_MINUTES[min(attempts, len(BACKOFF_MINUTES)) - 1])
        self._set(qid, status=status, attempts=attempts, error=str(e)[:500],
                  next_attempt_at=retry_at.isoformat(timespec="seconds") if retry_at else None)
        self.db.execute("UPDATE publish_attempts SET finished_at = ?, outcome = ?, error = ? WHERE id = ?",
                        (utcnow(), "failed" if status == "failed" else "retry", str(e)[:500], attempt_id))
        self.db.log_error(f"publish.{e.kind}", str(e)[:200], str(clip_id))
        return dict(self.db.one("SELECT * FROM publish_queue WHERE id = ?", (qid,)))
