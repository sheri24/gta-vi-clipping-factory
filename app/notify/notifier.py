"""Turns pipeline events into alerts. Runs every 30 s: scans the database for things that happened
since the app started and sends each one once (the notifications table remembers what was sent,
so a restart never repeats an alert)."""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from html import escape

from app.db import Database, utcnow
from app.publish.youtube import sign_in_status
from app.recording.recorder import disk_free_gb

log = logging.getLogger(__name__)
LOOKBACK = timedelta(minutes=15)     # on startup, also report what happened just before
FAILURE_PAUSE = 300                  # seconds to wait after Telegram refuses (bad token, offline)


class Senders:
    """Every configured channel (Telegram, ntfy…). An alert counts as sent if any of them delivered it."""

    def __init__(self, senders: list):
        self.senders = [x for x in senders if x.enabled]

    @property
    def enabled(self) -> bool:
        return bool(self.senders)

    def send(self, text: str, buttons=None) -> None:
        errors = []
        for x in self.senders:
            try:
                x.send(text, buttons)
            except Exception as e:
                errors.append(str(e))
        if errors and len(errors) == len(self.senders):
            raise RuntimeError("; ".join(errors))


def studio_url(video_id: str) -> str:
    return f"https://studio.youtube.com/video/{video_id}/edit"


class Notifier:
    def __init__(self, db: Database, sender, settings, publish_queue=None, notify_live: bool = True):
        self.db = db
        self.sender = sender                     # Telegram (or anything with send / enabled)
        self.s = settings
        self.queue = publish_queue
        self.notify_live = notify_live
        self.since = (datetime.now(timezone.utc) - LOOKBACK).isoformat(timespec="seconds")
        self._paused_until = 0.0

    # -- bookkeeping -------------------------------------------------------------------------------

    def _sent(self, key: str) -> bool:
        return self.db.one("SELECT 1 FROM notifications WHERE key = ?", (key,)) is not None

    def _emit(self, key: str, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        if self._sent(key) or time.monotonic() < self._paused_until:
            return False
        try:
            self.sender.send(text, buttons)
        except Exception as e:
            log.warning("alert %s not sent: %s", key, e)
            self._paused_until = time.monotonic() + FAILURE_PAUSE
            return False
        self.db.execute("INSERT OR IGNORE INTO notifications (key, sent_at) VALUES (?, ?)", (key, utcnow()))
        return True

    def run(self) -> int:
        if not self.sender.enabled:
            return 0
        sent = 0
        for check in (self._live, self._approved, self._uploaded, self._problems):
            try:
                sent += check()
            except Exception:
                log.exception("alert check %s failed", check.__name__)
        return sent

    # -- events -----------------------------------------------------------------------------------------

    def _live(self) -> int:
        if not self.notify_live:
            return 0
        n = 0
        for r in self.db.query(
                """SELECT s.video_id, s.title, c.title channel, c.record_enabled FROM live_sessions s
                     JOIN channels c ON c.channel_id = s.channel_id
                    WHERE s.status = 'live' AND s.detected_at >= ?""", (self.since,)):
            what = "Recording and watching chat for hype moments." if r["record_enabled"] else \
                "Recording is off for this channel, so no clips."
            n += self._emit(f"live:{r['video_id']}",
                            f"🔴 <b>{escape(r['channel'])} is live</b>\n{escape(r['title'] or '')}\n\n{what}",
                            [("Watch stream", f"https://www.youtube.com/watch?v={r['video_id']}")])
        return n

    def _approved(self) -> int:
        n = 0
        for r in self.db.query(
                """SELECT x.id, x.score, x.status, c.title channel, c.permission_confirmed, e.llm_result
                     FROM evaluations e JOIN clips x ON x.id = e.clip_id
                     JOIN live_sessions s ON s.id = x.session_id JOIN channels c ON c.channel_id = s.channel_id
                    WHERE e.decision = 'approved' AND e.created_at >= ?""", (self.since,)):
            summary = (json.loads(r["llm_result"]) if r["llm_result"] else {}).get("summary", "")
            if not r["permission_confirmed"]:
                nxt = "⛔ Not uploading: this channel doesn't have <b>Upload allowed</b> on."
            elif not (self.queue and self.queue.enabled):
                nxt = "⛔ Not uploading: uploads are off (PUBLISH_ENABLED=false)."
            else:
                wait = self.queue.why_not_now()
                nxt = f"⏳ Uploading {'as soon as possible' if not wait else 'when possible: ' + escape(wait)}."
            n += self._emit(f"approved:{r['id']}",
                            f"✂️ <b>New clip from {escape(r['channel'])}</b> (score {r['score']:.1f})\n"
                            f"{escape(summary)}\n\n{nxt}",
                            [("Review on dashboard", "http://127.0.0.1:8000/#clips")])
        return n

    def _uploaded(self) -> int:
        n = 0
        for r in self.db.query(
                """SELECT q.id, q.remote_id, q.remote_url, q.title, q.privacy, q.privacy_actual, c.title channel
                     FROM publish_queue q JOIN clips x ON x.id = q.clip_id
                     JOIN live_sessions s ON s.id = x.session_id JOIN channels c ON c.channel_id = s.channel_id
                    WHERE q.status = 'published' AND q.published_at >= ?""", (self.since,)):
            privacy = r["privacy_actual"] or r["privacy"]
            if privacy == "public":
                head, todo = "✅ <b>Posted publicly</b>", ""
            else:
                head = f"⚠️ <b>Uploaded as {escape(privacy.upper())}</b>, nobody can see it yet"
                todo = "\n\n👉 Tap <b>Open in Studio</b> → Visibility → <b>Public</b> while it's still fresh."
            n += self._emit(f"uploaded:{r['id']}",
                            f"{head}\n<b>{escape(r['title'] or '')}</b>\nfrom {escape(r['channel'])}{todo}",
                            [("Open in Studio", studio_url(r["remote_id"])), ("Watch", r["remote_url"])])
        return n

    def _problems(self) -> int:
        n = 0
        for r in self.db.query(
                """SELECT id, remote_id, title FROM publish_queue WHERE privacy_actual = 'private'
                     AND privacy != 'private' AND privacy_checked_at >= ?""", (self.since,)):
            n += self._emit(f"locked:{r['id']}",
                            f"🔒 <b>YouTube locked an upload to private</b>\n{escape(r['title'] or '')}\n\n"
                            "Uploads from an unaudited API project can't be public. Try making it public in "
                            "Studio; if that's blocked too, post this clip by hand.",
                            [("Open in Studio", studio_url(r["remote_id"]))])
        for r in self.db.query("SELECT id, clip_id, error FROM publish_queue WHERE status = 'failed' AND updated_at >= ?",
                               (self.since,)):
            n += self._emit(f"failed:{r['id']}",
                            f"❌ <b>Upload of clip {r['clip_id']} failed</b> after retries\n{escape((r['error'] or '')[:300])}",
                            [("Open dashboard", "http://127.0.0.1:8000/#publishing")])
        sign_in = sign_in_status(self.s.youtube_token_path)
        if sign_in.get("renew_soon"):
            n += self._emit(f"signin:{sign_in['expires_around']}",
                            "🔑 <b>YouTube sign-in expires within a day.</b> Uploads stop after that.\n"
                            "On the laptop run:\n<code>.venv\\Scripts\\python -m app.publish.auth</code>")
        free = disk_free_gb(self.s.recordings_dir)
        if free < self.s.min_free_gb:
            n += self._emit(f"disk:{datetime.now(timezone.utc).date()}",
                            f"💾 <b>Disk almost full</b>: {free:.1f} GB free. New recordings are paused "
                            f"below {self.s.min_free_gb:g} GB.")
        return n
