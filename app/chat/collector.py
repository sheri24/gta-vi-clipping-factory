"""One ChatCollector thread per live session; sources tried in configured order with fallback."""

import logging
import threading
import time
from collections.abc import Callable

from app.chat.store import ChatStore
from app.db import Database, utcnow

log = logging.getLogger(__name__)

HEALTHY_SECONDS = 120     # a run at least this long resets the failure count
SWITCH_AFTER = 3          # consecutive short runs before moving to the next source
PRIMARY_RETRY = 300       # on a fallback source (e.g. the quota-costing official API), retry the first one this often


class CutoffStore:
    """Both sources replay recent history on connect, and their message IDs differ (measured:
    0 overlap), so ID dedupe can't catch cross-source repeats. Drop anything not newer than
    what we already had when this run started."""

    def __init__(self, store: ChatStore, cutoff_us: int | None):
        self.store = store
        self.cutoff_us = cutoff_us

    def add(self, session_id, messages, source) -> int:
        if self.cutoff_us is not None:
            messages = [m for m in messages if m.published_us > self.cutoff_us]
        return self.store.add(session_id, messages, source)


class ChatCollector(threading.Thread):
    def __init__(self, db: Database, store: ChatStore, session: dict, sources: list,
                 is_live: Callable[[], bool] | None = None, healthy_seconds: float = HEALTHY_SECONDS,
                 primary_retry_seconds: float = PRIMARY_RETRY):
        super().__init__(name=f"chat-{session['video_id']}", daemon=True)
        self.db = db
        self.store = store
        self.session = session
        self.sources = sources
        self.is_live = is_live or self._session_is_live
        self.healthy_seconds = healthy_seconds
        self.stop_event = threading.Event()
        self.idx = 0
        self.primary_retry = primary_retry_seconds
        self._run_stop: threading.Event | None = None   # ends just the current source run

    def _session_is_live(self) -> bool:
        row = self.db.one("SELECT status, live_chat_id FROM live_sessions WHERE id = ?", (self.session["id"],))
        if row and row["live_chat_id"]:
            self.session["live_chat_id"] = row["live_chat_id"]   # monitor may fill it in later
        return bool(row) and row["status"] == "live"

    @property
    def source(self):
        return self.sources[self.idx]

    def _advance(self) -> None:
        self.idx = (self.idx + 1) % len(self.sources)
        log.info("chat %s: switching to %s", self.session["video_id"], self.source.name)

    def run(self) -> None:
        failures = 0
        while not self.stop_event.is_set() and self.is_live():
            src = self.source
            if hasattr(src, "available") and not src.available():
                if all(hasattr(s, "available") and not s.available() for s in self.sources):
                    self.stop_event.wait(60)   # nothing usable right now
                    continue
                self._advance()
                continue
            run_id = self.db.execute(
                "INSERT INTO chat_runs (session_id, source, started_at) VALUES (?, ?, ?)",
                (self.session["id"], src.name, utcnow())).lastrowid
            started = time.monotonic()
            cutoff = self.db.one("SELECT MAX(published_us) m FROM chat_messages WHERE session_id = ?",
                                 (self.session["id"],))["m"]
            run_stop = self._run_stop = threading.Event()
            if self.stop_event.is_set():
                run_stop.set()
            # After an outage we may have fallen back to a source that costs quota; don't stay there.
            timer = threading.Timer(self.primary_retry, run_stop.set) if self.idx != 0 else None
            if timer:
                timer.daemon = True
                timer.start()
            try:
                result, added, error = src.run(self.session, CutoffStore(self.store, cutoff), run_stop)
            except Exception as e:
                log.exception("chat source crashed")
                result, added, error = "error", 0, str(e)
            finally:
                if timer:
                    timer.cancel()
            self.db.execute("UPDATE chat_runs SET stopped_at = ?, messages = ?, result = ?, error = ? WHERE id = ?",
                            (utcnow(), added, result, error, run_id))
            if self.stop_event.is_set():
                break
            if run_stop.is_set():
                log.info("chat %s: trying %s again", self.session["video_id"], self.sources[0].name)
                self.idx, failures = 0, 0
                continue
            if error:
                self.db.log_error(f"chat.{src.name}", f"{result}: {error[:200]}", self.session["video_id"])
            healthy = time.monotonic() - started >= self.healthy_seconds
            failures = 0 if healthy else failures + 1
            if result == "quota" or failures >= SWITCH_AFTER:
                self._advance()
                failures = 0
            self.stop_event.wait(1 if healthy else min(30, 2 * 2 ** failures))

    def stop(self) -> None:
        self.stop_event.set()
        if self._run_stop:
            self._run_stop.set()


class ChatManager:
    def __init__(self, db: Database, store: ChatStore, source_factory: Callable[[], list]):
        self.db = db
        self.store = store
        self.source_factory = source_factory   # fresh source objects per collector
        self.collectors: dict[int, ChatCollector] = {}
        self.lock = threading.Lock()

    def on_session_started(self, session: dict) -> None:
        with self.lock:
            existing = self.collectors.get(session["id"])
            if existing and existing.is_alive():
                return
            ch = self.db.one("SELECT enabled FROM channels WHERE channel_id = ?", (session["channel_id"],))
            if not ch or not ch["enabled"]:
                return
            c = ChatCollector(self.db, self.store, dict(session), self.source_factory())
            self.collectors[session["id"]] = c
            c.start()

    def on_session_ended(self, session: dict) -> None:
        with self.lock:
            c = self.collectors.pop(session["id"], None)
        if c:
            c.stop()
        self.store.aggregate(session["id"])   # final buckets

    def resume(self) -> None:
        for row in self.db.query("SELECT * FROM live_sessions WHERE status = 'live'"):
            self.on_session_started(dict(row))

    def aggregate_all(self) -> None:
        for sid, c in list(self.collectors.items()):
            if c.is_alive():
                self.store.aggregate(sid)

    def stop_all(self, timeout: float = 20) -> None:
        with self.lock:
            cs = list(self.collectors.values())
            self.collectors.clear()
        for c in cs:
            c.stop()
        for c in cs:
            c.join(timeout)

    def active(self) -> list[dict]:
        return [{"video_id": c.session["video_id"], "source": c.source.name}
                for c in self.collectors.values() if c.is_alive()]
