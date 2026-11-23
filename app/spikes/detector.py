"""Chat spike detection against a rolling, per-stream baseline.

Signal: distinct chatters per bucket (one spammer can't fake it; a crowd reacting can).
Baseline: median of the surrounding chat, past 5 min AND the next 3 min, skipping +/-60 s around the
bucket itself. Looking both ways matters: when we join a stream in the middle of a hype moment there
is no "before" (measured on Lacy's GTA 6 finale: a past-only baseline missed the shooting entirely).
Clips wait for the moment to finish anyway, so the extra ~3 min of latency costs nothing.
A bucket is "hot" when, versus that median:
    z     = (x - median) / scale  >= z_min        (robust z; scale from MAD with a Poisson floor)
    x / median                    >= ratio_min
    x                             >= min_authors  (ignore noise on tiny streams)
Consecutive hot buckets (allowing gaps up to merge_gap) form one spike.

Clip window: chat reacts AFTER the moment (viewer latency + typing time), so
    event_utc = spike start - chat_delay;   clip = [event - pre, event + post]
"""

import bisect
import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db import Database, utcnow
from app.recording.recorder import latest_anchor
from app.recording.timeline import estimate_chat_delay, utc_to_pts


@dataclass(frozen=True)
class Params:
    bucket_seconds: int = 10
    baseline_buckets: int = 30        # look back 5 min of 10 s buckets
    future_buckets: int = 18          # and ahead 3 min
    guard_buckets: int = 6            # skip +/-60 s around the bucket (the moment itself)
    min_history_buckets: int = 12     # need this many baseline buckets to judge at all
    z_min: float = 3.5
    ratio_min: float = 1.6
    min_authors: int = 5
    merge_gap_buckets: int = 2        # 20 s


@dataclass
class Spike:
    start_bucket: int
    peak_bucket: int
    end_bucket: int
    peak_authors: int
    peak_messages: int
    baseline_authors: float
    z_score: float
    ratio: float


def robust_z(x: float, history: list[float]) -> tuple[float, float]:
    """Returns (z, median). MAD alone collapses to 0 on steady chat, so floor the scale by
    sqrt(median) (Poisson noise of counts) and 1."""
    med = statistics.median(history)
    mad = statistics.median(abs(h - med) for h in history)
    scale = max(1.4826 * mad, math.sqrt(med), 1.0)
    return (x - med) / scale, med


def detect(buckets: list[dict], p: Params) -> list[Spike]:
    """buckets: ascending by bucket_start, each with bucket_start, unique_authors, messages.
    Missing buckets (collector gaps) are simply absent; they never count as zero activity."""
    spikes: list[Spike] = []
    current: Spike | None = None
    last_hot: int | None = None
    bs = p.bucket_seconds
    times = [b["bucket_start"] for b in buckets]   # ascending: the baseline window is a slice
    for b in buckets:
        t = b["bucket_start"]
        # Time-based window (collector gaps just mean fewer buckets), excluding the moment itself.
        lo = bisect.bisect_left(times, t - p.baseline_buckets * bs)
        hi = bisect.bisect_right(times, t + p.future_buckets * bs)
        history = [h["unique_authors"] for h in buckets[lo:hi] if abs(h["bucket_start"] - t) > p.guard_buckets * bs]
        if len(history) < p.min_history_buckets:
            continue
        x = b["unique_authors"]
        z, med = robust_z(x, history)
        ratio = x / med if med > 0 else math.inf
        hot = z >= p.z_min and ratio >= p.ratio_min and x >= p.min_authors
        gap_ok = last_hot is not None and t - last_hot <= (p.merge_gap_buckets + 1) * p.bucket_seconds
        if hot:
            if current is not None and gap_ok:
                current.end_bucket = t
                if x > current.peak_authors:
                    current.peak_bucket, current.peak_authors = t, x
                    current.peak_messages, current.z_score, current.ratio = b["messages"], z, ratio
            else:
                current = Spike(t, t, t, x, b["messages"], med, z, ratio)
                spikes.append(current)
            last_hot = t
        elif current is not None and not gap_ok:
            current = None
    return spikes


# -- prompted chat: giveaways and polls ------------------------------------------------------------------
# The biggest chat bursts of a stream are often the streamer ASKING chat to type something: a giveaway
# keyword ("gta" x126 of 163 messages on Lacy's finale) or a poll option ("marathon"/"end", 620 of 667).
# Real hype is varied ("LMAO", "NO WAY", "he really did that"); its spam is reaction words, which we ignore,
# or a copypasta sentence. What chat is told to type is short: one or two words.
REACTION = re.compile(r"^(w|l|lol|lmf?ao|kekw?|omg|no|nah|yes|ye|wtf|bruh|pog\w*|g+|f|rip|hah?|ha(ha)+|xd|wow|"
                      r"yo|sheesh|clip it|clip|[^a-z0-9]*)$")
CHORUS_SHARE = 0.5


def _norm(text: str) -> str:
    text = re.sub(r":[a-z0-9_\-]+:", " ", text.lower())          # emoji shortcodes
    text = re.sub(r"(.)\1{2,}", r"\1", text)                    # "shooot" -> "shot", "noooo" -> "no"
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", text)).strip()


def _prompt_like(t: str) -> bool:
    return not REACTION.match(t) and len(t.split()) <= 2


def chorus_share(texts: list[str], top: int = 2) -> float:
    """Share of messages that are the same one or two short non-reaction phrases (0-1)."""
    norm = [t for t in (_norm(x) for x in texts) if t]
    if not norm:
        return 0.0
    counts = Counter(t for t in norm if _prompt_like(t))
    return sum(n for _, n in counts.most_common(top)) / len(norm)


def prompted_reason(db: Database, spike) -> str | None:
    """Why this burst is chat doing what it was told rather than reacting, or None."""
    rows = db.query("""SELECT text FROM chat_messages WHERE session_id = ? AND kind IN ('text', 'paid')
                          AND text IS NOT NULL AND published_us BETWEEN ? AND ?""",
                    (spike["session_id"], spike["start_bucket"] * 1_000_000, (spike["end_bucket"] + 10) * 1_000_000))
    texts = [r["text"] for r in rows]
    share = chorus_share(texts)
    if len(texts) >= 10 and share >= CHORUS_SHARE:
        phrase = Counter(t for t in (_norm(x) for x in texts) if t and _prompt_like(t)).most_common(1)[0][0]
        return f'{share:.0%} of chat typed the same thing ("{phrase[:30]}"): a giveaway or poll, not a reaction'
    return None


class SpikeDetector:
    def __init__(self, db: Database, params: Params, pre_seconds: float = 15, post_seconds: float = 25,
                 settle_seconds: int = 20, lookback_minutes: float = 8):
        self.db = db
        self.p = params
        self.pre = pre_seconds
        self.post = post_seconds
        self.settle = settle_seconds          # newest buckets may still receive late messages
        self.lookback = lookback_minutes

    def run(self, session_id: int, final: bool = False, lookback_minutes: float | None = None,
            chat_delay: float | None = None, source: str = "live") -> list[int]:
        """Detect over recent buckets and upsert spikes. Returns ids of spikes created or updated.
        chat_delay overrides the measured/channel delay (past streams have no live latency samples);
        source tags new spikes so the live and past-stream planners each take only their own.

        Spikes stay 'open' (provisional) until their 3-minute "after" baseline window has fully arrived,
        then become 'closed' and eligible for clipping. Because the baseline looks ahead, a provisional
        spike can shift or vanish as data arrives; open spikes no longer detected are deleted."""
        lookback = lookback_minutes if lookback_minutes is not None else self.lookback
        bs = self.p.bucket_seconds
        newest = self.db.one("SELECT MAX(bucket_start) m FROM chat_buckets WHERE session_id = ?",
                             (session_id,))["m"]
        if newest is None:
            return []
        upto = newest if final else newest - self.settle
        since = upto - int(lookback * 60) - self.p.baseline_buckets * bs
        rows = [dict(r) for r in self.db.query(
            """SELECT bucket_start, unique_authors, messages FROM chat_buckets
                WHERE session_id = ? AND bucket_start BETWEEN ? AND ? ORDER BY bucket_start""",
            (session_id, since, upto))]
        # Only spikes starting inside the lookback (not the warm-up history) are ours to write.
        first_owned = upto - int(lookback * 60)
        delay = chat_delay if chat_delay is not None else self._chat_delay(session_id)
        anchor = latest_anchor(self.db, session_id)
        touched = []
        found = []
        for s in detect(rows, self.p):
            if s.start_bucket < first_owned:
                continue
            found.append(s.start_bucket)
            event = s.start_bucket - delay
            c0, c1 = event - self.pre, event + self.post
            pts0 = pts1 = None
            if anchor:
                pts0 = utc_to_pts(datetime.fromtimestamp(c0, tz=timezone.utc), anchor)
                pts1 = utc_to_pts(datetime.fromtimestamp(c1, tz=timezone.utc), anchor)
            settled = (self.p.future_buckets + self.p.merge_gap_buckets + 1) * bs
            closed = final or s.end_bucket < upto - settled
            now = utcnow()
            cur = self.db.execute(
                """INSERT INTO spikes (session_id, start_bucket, peak_bucket, end_bucket, peak_authors,
                       peak_messages, baseline_authors, z_score, ratio, chat_delay_s, event_utc,
                       clip_start_utc, clip_end_utc, clip_start_pts, clip_end_pts, detected_at, updated_at, status,
                       source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (session_id, start_bucket) DO UPDATE SET
                       peak_bucket = excluded.peak_bucket, end_bucket = excluded.end_bucket,
                       peak_authors = excluded.peak_authors, peak_messages = excluded.peak_messages,
                       z_score = excluded.z_score, ratio = excluded.ratio,
                       clip_start_pts = COALESCE(excluded.clip_start_pts, clip_start_pts),
                       clip_end_pts = COALESCE(excluded.clip_end_pts, clip_end_pts),
                       updated_at = excluded.updated_at, status = excluded.status
                   WHERE spikes.status = 'open'
                   RETURNING id""",
                (session_id, s.start_bucket, s.peak_bucket, s.end_bucket, s.peak_authors, s.peak_messages,
                 s.baseline_authors, s.z_score, s.ratio, delay, event, c0, c1, pts0, pts1, now, now,
                 "closed" if closed else "open", source),
            )
            row = cur.fetchone()
            if row:
                touched.append(row[0])
        # provisional spikes that more data has disproved
        stale = [r["id"] for r in self.db.query(
            "SELECT id, start_bucket FROM spikes WHERE session_id = ? AND status = 'open' AND start_bucket >= ?",
            (session_id, first_owned)) if r["start_bucket"] not in found]
        for sid in stale:
            self.db.execute("DELETE FROM spikes WHERE id = ?", (sid,))
        return touched

    def _chat_delay(self, session_id: int) -> float:
        """Measured from the stream's own latency when possible (median of recent anchor samples),
        else the channel's manual chat_delay_seconds."""
        row = self.db.one(
            """SELECT c.chat_delay_seconds d, c.chat_delay_auto auto FROM live_sessions s
                 JOIN channels c ON c.channel_id = s.channel_id WHERE s.id = ?""", (session_id,))
        if not row:
            return 12.0
        if row["auto"]:
            samples = [estimate_chat_delay(r["edge_lag_s"], r["target_duration"]) for r in self.db.query(
                """SELECT edge_lag_s, target_duration FROM time_anchors
                    WHERE session_id = ? AND edge_lag_s IS NOT NULL ORDER BY id DESC LIMIT 5""", (session_id,))]
            if samples:
                return statistics.median(samples)
        return row["d"]
