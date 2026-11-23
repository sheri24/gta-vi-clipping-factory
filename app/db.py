"""SQLite access and schema migrations.

Migrations are append-only: each entry runs once, tracked by PRAGMA user_version.
Later phases add their tables by appending to MIGRATIONS.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1 — Phase 2: channels, live detection, quota, errors
    """
    CREATE TABLE channels (
        id                   INTEGER PRIMARY KEY,
        channel_id           TEXT NOT NULL UNIQUE,
        handle               TEXT,
        title                TEXT,
        source_url           TEXT NOT NULL,
        enabled              INTEGER NOT NULL DEFAULT 1,
        permission_confirmed INTEGER NOT NULL DEFAULT 0,
        notes                TEXT,
        created_at           TEXT NOT NULL,
        last_checked_at      TEXT
    );

    -- Every video ID we've seen per channel, so finished VODs aren't re-queried each poll.
    CREATE TABLE videos_seen (
        video_id        TEXT PRIMARY KEY,
        channel_id      TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
        first_seen_at   TEXT NOT NULL,
        last_state      TEXT NOT NULL DEFAULT 'unknown',  -- live | upcoming | none | missing | unknown
        last_checked_at TEXT
    );

    -- One row per broadcast. UNIQUE(video_id) is what prevents duplicate sessions.
    CREATE TABLE live_sessions (
        id                INTEGER PRIMARY KEY,
        video_id          TEXT NOT NULL UNIQUE,
        channel_id        TEXT NOT NULL,
        title             TEXT,
        status            TEXT NOT NULL CHECK (status IN ('live', 'ended')),
        live_chat_id      TEXT,
        actual_start_at   TEXT,
        actual_end_at     TEXT,
        detected_at       TEXT NOT NULL,
        last_seen_live_at TEXT,
        ended_at          TEXT,
        missed_checks     INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX ix_live_sessions_status ON live_sessions(status);

    -- Units spent per Pacific-time day (YouTube resets quota at midnight PT) and bucket.
    CREATE TABLE quota_usage (
        day    TEXT NOT NULL,
        bucket TEXT NOT NULL,
        units  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, bucket)
    );

    CREATE TABLE errors (
        id        INTEGER PRIMARY KEY,
        at        TEXT NOT NULL,
        component TEXT NOT NULL,
        message   TEXT NOT NULL,
        details   TEXT
    );
    """,
    # 2 — Phase 3: recordings, segments, timeline anchors, storage cleanup
    """
    -- One row per recorder run ("part"). A restart after a network drop starts a new part.
    CREATE TABLE recordings (
        id          INTEGER PRIMARY KEY,
        session_id  INTEGER NOT NULL REFERENCES live_sessions(id),
        part        INTEGER NOT NULL,
        backend     TEXT NOT NULL,                 -- streamlink | ytdlp
        status      TEXT NOT NULL CHECK (status IN ('running', 'stopped', 'failed')),
        started_at  TEXT NOT NULL,
        stopped_at  TEXT,
        exit_code   INTEGER,
        error       TEXT,
        UNIQUE (session_id, part)
    );

    -- Each finished 60 s .ts file. pts_* are the stream's own timestamps (seconds since broadcast start).
    CREATE TABLE segments (
        id            INTEGER PRIMARY KEY,
        session_id    INTEGER NOT NULL REFERENCES live_sessions(id),
        recording_id  INTEGER REFERENCES recordings(id),
        seq           INTEGER NOT NULL,
        path          TEXT NOT NULL UNIQUE,
        pts_start     REAL NOT NULL,
        duration      REAL NOT NULL,
        utc_start     TEXT,                        -- filled from the nearest time anchor
        size_bytes    INTEGER NOT NULL,
        created_at    TEXT NOT NULL,
        pinned        INTEGER NOT NULL DEFAULT 0,  -- >0 = a clip still needs it; never deleted
        deleted_at    TEXT,
        UNIQUE (session_id, seq)
    );
    CREATE INDEX ix_segments_session ON segments(session_id, pts_start);

    -- (pts, utc) pairs from HLS EXT-X-PROGRAM-DATE-TIME: utc = anchor.utc + (pts - anchor.pts)
    CREATE TABLE time_anchors (
        id          INTEGER PRIMARY KEY,
        session_id  INTEGER NOT NULL REFERENCES live_sessions(id),
        pts         REAL NOT NULL,
        utc         TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT 'hls_pdt',
        sampled_at  TEXT NOT NULL
    );

    ALTER TABLE live_sessions ADD COLUMN storage_cleaned_at TEXT;
    """,
    # 3 — Phase 4: live chat
    """
    -- Raw messages, deduplicated per session by YouTube's message ID (both sources replay history
    -- on reconnect). published_us = microseconds since epoch on YouTube's clock.
    CREATE TABLE chat_messages (
        id           INTEGER PRIMARY KEY,
        session_id   INTEGER NOT NULL REFERENCES live_sessions(id),
        msg_id       TEXT NOT NULL,
        published_us INTEGER NOT NULL,
        kind         TEXT NOT NULL,   -- text | paid | sticker | membership | gift
        author_id    TEXT,
        author_name  TEXT,
        text         TEXT,
        source       TEXT NOT NULL,   -- ytdlp | official
        UNIQUE (session_id, msg_id)
    );
    CREATE INDEX ix_chat_messages_time ON chat_messages(session_id, published_us);

    -- Per-bucket activity (default 10 s), keyed by YouTube publish time, not arrival time.
    CREATE TABLE chat_buckets (
        session_id     INTEGER NOT NULL REFERENCES live_sessions(id),
        bucket_start   INTEGER NOT NULL,   -- epoch seconds (UTC), multiple of bucket size
        messages       INTEGER NOT NULL,   -- text + paid + sticker
        unique_authors INTEGER NOT NULL,
        paid           INTEGER NOT NULL,
        memberships    INTEGER NOT NULL,
        gifts          INTEGER NOT NULL,
        PRIMARY KEY (session_id, bucket_start)
    );

    -- One row per collector run, for the dashboard and debugging source failures.
    CREATE TABLE chat_runs (
        id          INTEGER PRIMARY KEY,
        session_id  INTEGER NOT NULL REFERENCES live_sessions(id),
        source      TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        stopped_at  TEXT,
        messages    INTEGER NOT NULL DEFAULT 0,
        result      TEXT,
        error       TEXT
    );
    """,
    # 4 — Phase 5: spike detection + clip windows
    """
    -- Seconds between something happening on stream and chat reacting to it
    -- (viewer stream latency + human reaction time). Tunable per channel.
    ALTER TABLE channels ADD COLUMN chat_delay_seconds REAL NOT NULL DEFAULT 12;

    -- Local recording/clipping is the user's choice; permission_confirmed now gates PUBLISHING only.
    ALTER TABLE channels ADD COLUMN record_enabled INTEGER NOT NULL DEFAULT 0;

    -- One row per detected burst of chat. All *_utc are YouTube-clock epoch seconds.
    -- UNIQUE(session_id, start_bucket) makes re-detection idempotent: a burst that keeps growing
    -- across detector runs updates its own row instead of creating a duplicate.
    CREATE TABLE spikes (
        id               INTEGER PRIMARY KEY,
        session_id       INTEGER NOT NULL REFERENCES live_sessions(id),
        start_bucket     INTEGER NOT NULL,   -- first bucket above threshold
        peak_bucket      INTEGER NOT NULL,
        end_bucket       INTEGER NOT NULL,   -- last bucket above threshold
        peak_authors     INTEGER NOT NULL,
        peak_messages    INTEGER NOT NULL,
        baseline_authors REAL NOT NULL,      -- rolling median before the burst
        z_score          REAL NOT NULL,      -- robust z of the peak bucket
        ratio            REAL NOT NULL,      -- peak / baseline
        chat_delay_s     REAL NOT NULL,      -- delay used for this estimate
        event_utc        REAL NOT NULL,      -- estimated moment on stream = start_bucket - delay
        clip_start_utc   REAL NOT NULL,
        clip_end_utc     REAL NOT NULL,
        clip_start_pts   REAL,               -- same window on the recording's clock (needs an anchor)
        clip_end_pts     REAL,
        detected_at      TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
        UNIQUE (session_id, start_bucket)
    );
    CREATE INDEX ix_spikes_session ON spikes(session_id, event_utc);
    """,
    # 5 — Phase 6: clips
    """
    -- Lifecycle: pending → cutting → cut → (Phase 7+) … ; failed after repeated errors.
    -- Clips outlive their session's footage: source.ts keeps the exact window (+context) for re-renders.
    CREATE TABLE clips (
        id                INTEGER PRIMARY KEY,
        session_id        INTEGER NOT NULL REFERENCES live_sessions(id),
        spike_id          INTEGER REFERENCES spikes(id),
        window_start_pts  REAL NOT NULL,
        window_end_pts    REAL NOT NULL,
        window_start_utc  REAL NOT NULL,
        window_end_utc    REAL NOT NULL,
        status            TEXT NOT NULL DEFAULT 'pending',
        attempts          INTEGER NOT NULL DEFAULT 0,
        source_path       TEXT,
        source_start_pts  REAL,
        vertical_path     TEXT,
        layout            TEXT,
        duration          REAL,
        error             TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );
    CREATE INDEX ix_clips_status ON clips(status, id);
    CREATE INDEX ix_clips_session ON clips(session_id, window_start_pts);

    ALTER TABLE spikes ADD COLUMN clip_id INTEGER REFERENCES clips(id);

    -- 9:16 layout per channel: 'blur' (whole frame over blurred fill) or 'split' (facecam top, gameplay bottom)
    ALTER TABLE channels ADD COLUMN layout TEXT NOT NULL DEFAULT 'blur';
    -- facecam rectangle as fractions of the source frame: "x,y,w,h" (e.g. "0.75,0.70,0.25,0.30")
    ALTER TABLE channels ADD COLUMN facecam_rect TEXT;
    """,
    # 6 — Phase 7: transcription + captions
    """
    CREATE TABLE transcripts (
        clip_id        INTEGER PRIMARY KEY REFERENCES clips(id),
        language       TEXT,
        language_prob  REAL,
        text           TEXT NOT NULL,
        words          TEXT NOT NULL,      -- JSON [{"start","end","word","prob"}], seconds from clip start
        model          TEXT NOT NULL,
        seconds_taken  REAL,
        created_at     TEXT NOT NULL
    );
    ALTER TABLE clips ADD COLUMN captions_path TEXT;
    ALTER TABLE clips ADD COLUMN final_path TEXT;
    -- force a transcription language per channel (e.g. 'hi', 'en'); NULL = auto-detect
    ALTER TABLE channels ADD COLUMN language TEXT;
    """,
    # 7 — Phase 8: quality control
    """
    -- What chat said during the spike, snapshotted at cut time (raw chat is deleted with the footage).
    ALTER TABLE clips ADD COLUMN chat_sample TEXT;
    ALTER TABLE clips ADD COLUMN score REAL;

    CREATE TABLE evaluations (
        clip_id          INTEGER PRIMARY KEY REFERENCES clips(id),
        signals          TEXT NOT NULL,     -- JSON objective measurements
        objective_score  REAL NOT NULL,     -- 0-10
        llm_model        TEXT,
        llm_result       TEXT,              -- JSON scores + summary from the vision model
        llm_score        REAL,              -- 0-10
        final_score      REAL NOT NULL,
        decision         TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
        reasons          TEXT NOT NULL,     -- JSON list of human-readable reasons
        seconds_taken    REAL,
        created_at       TEXT NOT NULL
    );
    """,
    # 8 — measured stream latency → per-session chat delay
    """
    ALTER TABLE time_anchors ADD COLUMN edge_lag_s REAL;
    ALTER TABLE time_anchors ADD COLUMN target_duration REAL;
    -- 1 = derive chat delay from measured stream latency; 0 = always use chat_delay_seconds
    ALTER TABLE channels ADD COLUMN chat_delay_auto INTEGER NOT NULL DEFAULT 1;
    """,
    # 9 — Phase 9: publishing
    """
    -- One row per (clip, platform): UNIQUE makes duplicate uploads impossible.
    -- status: queued → uploading → published | failed (after retries) | cancelled
    CREATE TABLE publish_queue (
        id               INTEGER PRIMARY KEY,
        clip_id          INTEGER NOT NULL REFERENCES clips(id),
        platform         TEXT NOT NULL DEFAULT 'youtube',
        status           TEXT NOT NULL DEFAULT 'queued',
        priority         REAL NOT NULL DEFAULT 0,      -- QC score: best clips go first
        title            TEXT,
        description      TEXT,
        tags             TEXT,                         -- JSON list
        metadata_source  TEXT,                         -- llm | template
        privacy          TEXT,
        attempts         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at  TEXT,
        remote_id        TEXT,                         -- YouTube video ID once uploaded
        remote_url       TEXT,
        error            TEXT,
        queued_at        TEXT NOT NULL,
        published_at     TEXT,
        updated_at       TEXT NOT NULL,
        UNIQUE (clip_id, platform)
    );
    CREATE INDEX ix_publish_status ON publish_queue(status, priority);

    -- Every upload attempt, successful or not (publishing history).
    CREATE TABLE publish_attempts (
        id           INTEGER PRIMARY KEY,
        queue_id     INTEGER NOT NULL REFERENCES publish_queue(id),
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        outcome      TEXT,       -- published | retry | failed
        error        TEXT
    );
    """,
    # 10 — automatic facecam detection per stream
    """
    ALTER TABLE live_sessions ADD COLUMN facecam_rect TEXT;          -- "x,y,w,h" fractions, NULL = none found
    ALTER TABLE live_sessions ADD COLUMN facecam_confidence REAL;
    ALTER TABLE live_sessions ADD COLUMN facecam_method TEXT;        -- borders | face-expand | none
    ALTER TABLE live_sessions ADD COLUMN facecam_checked_at TEXT;
    ALTER TABLE live_sessions ADD COLUMN facecam_attempts INTEGER NOT NULL DEFAULT 0;
    -- 'auto' = split screen when a facecam is detected, blur otherwise. Channels nobody configured move to auto.
    UPDATE channels SET layout = 'auto' WHERE layout = 'blur' AND facecam_rect IS NULL;
    """,
    # 11 — delete a clip's local files once it is on YouTube (paths are cleared, history is kept)
    """
    ALTER TABLE clips ADD COLUMN files_deleted_at TEXT;
    """,
    # 12 — what YouTube actually applied: unaudited API projects get public uploads locked to private
    """
    ALTER TABLE publish_queue ADD COLUMN privacy_actual TEXT;
    ALTER TABLE publish_queue ADD COLUMN privacy_checked_at TEXT;
    """,
    # 13 — alerts already sent (Telegram), so restarts never repeat one
    """
    CREATE TABLE notifications (
        key      TEXT PRIMARY KEY,      -- e.g. 'uploaded:12', 'live:<video_id>'
        sent_at  TEXT NOT NULL
    );
    """,
    # 14 — past-stream (VOD) mode: clip uploaded streams from their chat replay
    """
    -- One row per finished stream we looked at, including skipped ones, so a scan never re-queries them.
    -- status: queued → analyzing → clipping → done | waiting_chat (retry later) | no_chat | skipped | failed
    CREATE TABLE vod_jobs (
        id               INTEGER PRIMARY KEY,
        video_id         TEXT NOT NULL UNIQUE,
        channel_id       TEXT NOT NULL,
        title            TEXT,
        started_at       TEXT,               -- liveStreamingDetails.actualStartTime = VOD second 0
        ended_at         TEXT,
        duration_s       REAL,
        origin           TEXT NOT NULL,      -- scan | link
        status           TEXT NOT NULL DEFAULT 'queued',
        session_id       INTEGER REFERENCES live_sessions(id),
        offset_utc       REAL,               -- epoch seconds of VOD second 0, measured from the chat replay
        chat_delay_s     REAL,
        chat_messages    INTEGER,
        spikes           INTEGER,
        clips            INTEGER,
        attempts         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at  TEXT,
        error            TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    );
    CREATE INDEX ix_vod_jobs_status ON vod_jobs(status, ended_at);

    -- live: cut from local recording segments, pts = stream PTS.  vod: downloaded section, pts = seconds into the VOD.
    ALTER TABLE clips ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'live';
    -- which detector pass found the spike; the live planner and the VOD planner each take their own
    ALTER TABLE spikes ADD COLUMN source TEXT NOT NULL DEFAULT 'live';
    -- why a spike was not clipped, e.g. chat typing a giveaway keyword or a poll option
    ALTER TABLE spikes ADD COLUMN skip_reason TEXT;
    ALTER TABLE channels ADD COLUMN vod_enabled INTEGER NOT NULL DEFAULT 1;
    """,
    # 15 — what the operator says is on screen; the AI review and the title writer trust it over the frames
    """
    ALTER TABLE clips ADD COLUMN review_note TEXT;
    """,
    # 16 — when the clip's whole window was scanned for nudity; nothing uploads without it
    """
    ALTER TABLE clips ADD COLUMN nudity_checked_at TEXT;
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper: one connection, serialised by a lock (FastAPI threads + scheduler thread)."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.lock = threading.RLock()
        self.migrate()

    def migrate(self) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                self.conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i}; COMMIT;")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def mark_interrupted(self) -> None:
        """At startup nothing can still be running: rows left 'running' belong to a dead process."""
        now = utcnow()
        self.execute("UPDATE recordings SET status = 'failed', stopped_at = ?, error = 'interrupted (app restart)' "
                     "WHERE status = 'running'", (now,))
        self.execute("UPDATE chat_runs SET stopped_at = ?, result = 'interrupted' WHERE stopped_at IS NULL", (now,))
        self.execute("UPDATE clips SET status = 'pending', updated_at = ? WHERE status = 'cutting'", (now,))
        self.execute("UPDATE clips SET status = 'cut', updated_at = ? WHERE status = 'captioning'", (now,))
        self.execute("UPDATE clips SET status = 'transcribed', updated_at = ? WHERE status = 'evaluating'", (now,))
        self.execute("UPDATE clips SET status = 'passed', updated_at = ? WHERE status = 'rendering'", (now,))
        # An upload cut off mid-way never produced a video ID; YouTube discards incomplete uploads.
        self.execute("UPDATE publish_queue SET status = 'queued', updated_at = ? WHERE status = 'uploading'", (now,))
        self.execute("UPDATE vod_jobs SET status = 'queued', updated_at = ? WHERE status = 'analyzing'", (now,))

    def log_error(self, component: str, message: str, details: str | None = None) -> None:
        self.execute(
            "INSERT INTO errors (at, component, message, details) VALUES (?, ?, ?, ?)",
            (utcnow(), component, message, details),
        )

    def close(self) -> None:
        self.conn.close()
