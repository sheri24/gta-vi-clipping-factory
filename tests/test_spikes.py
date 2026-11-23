import random
from datetime import datetime, timezone

from app.db import utcnow
from app.recording.timeline import Anchor
from app.spikes.detector import Params, SpikeDetector, detect, robust_z

T0 = 1_795_382_700   # epoch seconds, bucket-aligned
P = Params()


def series(authors: list[int], messages: list[int] | None = None, start=T0) -> list[dict]:
    messages = messages or [a + a // 4 for a in authors]
    return [{"bucket_start": start + i * 10, "unique_authors": a, "messages": m}
            for i, (a, m) in enumerate(zip(authors, messages))]


def noisy(n, mean, seed=1):
    rng = random.Random(seed)
    return [max(0, round(rng.gauss(mean, mean ** 0.5))) for _ in range(n)]


# -- pure detection --------------------------------------------------------------------

def test_robust_z_has_poisson_floor_on_flat_chat():
    z, med = robust_z(36, [30] * 30)        # MAD = 0; scale floors at sqrt(30)
    assert med == 30 and abs(z - 6 / 30 ** 0.5) < 1e-9


def test_detects_single_burst_with_peak():
    authors = noisy(30, 30) + [80, 120, 95] + noisy(10, 30, seed=2)
    [s] = detect(series(authors), P)
    assert s.start_bucket == T0 + 300 and s.peak_bucket == T0 + 310 and s.end_bucket == T0 + 320
    assert s.peak_authors == 120 and s.ratio > 3


def test_spammer_raises_messages_but_not_authors_no_spike():
    authors = [30] * 40
    messages = [38] * 30 + [400, 400] + [38] * 8
    assert detect(series(authors, messages), P) == []


def test_needs_history_before_judging():
    assert detect(series([5] * 5 + [200]), P) == []


def test_gradual_growth_is_not_a_spike():
    authors = [20 + i for i in range(60)]          # stream getting busier over 10 min
    assert detect(series(authors), P) == []


def test_quiet_stream_noise_ignored_by_min_authors():
    authors = [1] * 30 + [4, 1, 1]                # 4x baseline but only 4 people
    assert detect(series(authors), P) == []


def test_close_bursts_merge_far_bursts_separate():
    base = noisy(30, 30)
    merged = detect(series(base + [100, 30, 30, 110] + [30] * 5), P)        # 20 s gap → one moment
    assert len(merged) == 1 and merged[0].end_bucket == T0 + 330
    separate = detect(series(base + [100] + [30] * 6 + [110] + [30] * 3), P)  # 60 s gap → two
    assert len(separate) == 2


def test_baseline_ignores_the_spike_itself():
    """A long burst must stay one spike instead of fading as its own buckets enter the baseline."""
    authors = noisy(30, 30) + [100] * 12 + [30] * 5
    [s] = detect(series(authors), P)
    assert s.end_bucket == T0 + 300 + 11 * 10


def test_joining_mid_hype_still_detected():
    """We start collecting while chat is already exploding: no 'before' exists, the 'after' is quiet."""
    spikes = detect(series([26, 24, 25, 27, 23, 25, 24, 26] + noisy(30, 6, seed=3)), P)
    assert len(spikes) >= 1 and spikes[0].start_bucket <= T0 + 30


# Real 10 s distinct-chatter counts from Lacy's GTA 6 finale (2026-11-22, from 22:20:20 UTC).
# Chat reacted to Jason and Lucia getting shot ("NOOOO", "THEY DEAD") from ~22:21:00 to 22:22:30.
# The original past-only baseline found nothing here.
LACY_FINALE = [31, 25, 22, 27, 18, 23, 15, 16, 11, 23, 25, 26, 21, 19, 7, 7, 8, 10, 5, 5, 8, 6, 5, 1, 4, 2, 8, 8,
               3, 2, 5, 7, 8, 7, 9, 11, 10, 7, 6, 4, 5, 5, 7, 10, 5, 9, 5, 3, 4, 5, 6, 5, 8, 3, 3, 2, 1, 7, 4, 1,
               8, 2, 5, 1, 4, 1, 3]


def test_regression_lacy_finale_shooting_is_detected():
    spikes = detect(series(LACY_FINALE), P)
    starts = [s.start_bucket - T0 for s in spikes]
    assert starts and starts[0] <= 40                  # first spike in the reaction (22:20:20 + <=40 s)
    assert all(t <= 140 for t in starts)               # and nothing in the quiet tail afterwards


def test_collector_gap_is_not_zero_activity():
    rows = series(noisy(30, 30))
    after_gap = series([31, 29, 30], start=T0 + 600)   # 4 min with no buckets, then normal again
    assert detect(rows + after_gap, P) == []


# -- detector writing to the DB ---------------------------------------------------------------

def setup_session(db, delay=12):
    db.execute("INSERT INTO channels (channel_id, title, source_url, created_at, chat_delay_seconds) VALUES ('UC1','x','u',?,?)",
               (utcnow(), delay))
    return db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC1','live',?)",
                      (utcnow(),)).lastrowid


def write_buckets(db, sid, rows):
    for r in rows:
        db.execute("""INSERT OR REPLACE INTO chat_buckets
                      (session_id, bucket_start, messages, unique_authors, paid, memberships, gifts)
                      VALUES (?, ?, ?, ?, 0, 0, 0)""", (sid, r["bucket_start"], r["messages"], r["unique_authors"]))


def test_clip_window_steps_back_by_chat_delay_and_maps_to_pts(db):
    sid = setup_session(db, delay=12)
    # anchor: broadcast started 5 h before T0 (pts = seconds since start, as measured in Phase 3)
    db.execute("INSERT INTO time_anchors (session_id, pts, utc, sampled_at) VALUES (?, ?, ?, ?)",
               (sid, 18000.0, datetime.fromtimestamp(T0, tz=timezone.utc).isoformat(), utcnow()))
    write_buckets(db, sid, series(noisy(30, 30) + [120] + [30] * 6))
    det = SpikeDetector(db, P, pre_seconds=15, post_seconds=25)

    det.run(sid, final=True)

    k = db.one("SELECT * FROM spikes")
    spike_start = T0 + 300
    assert k["event_utc"] == spike_start - 12
    assert (k["clip_start_utc"], k["clip_end_utc"]) == (spike_start - 27, spike_start + 13)
    assert k["clip_start_pts"] == 18000 + (spike_start - 27 - T0)
    assert k["status"] == "closed"


def test_measured_latency_sets_chat_delay_unless_disabled(db):
    sid = setup_session(db, delay=12)
    det = SpikeDetector(db, P)
    assert det._chat_delay(sid) == 12                          # nothing measured yet → channel value
    for lag in (31.0, 31.8, 33.0):
        db.execute("""INSERT INTO time_anchors (session_id, pts, utc, sampled_at, edge_lag_s, target_duration)
                      VALUES (?, 0, '2026-11-22T22:00:00+00:00', ?, ?, 5)""", (sid, utcnow(), lag))
    assert det._chat_delay(sid) == 39.8                         # median of 39.0 / 39.8 / 41.0
    db.execute("UPDATE channels SET chat_delay_auto = 0")
    assert det._chat_delay(sid) == 12


def test_growing_spike_updates_same_row_then_closes(db):
    sid = setup_session(db)
    det = SpikeDetector(db, P, settle_seconds=0)
    base = noisy(30, 30)
    write_buckets(db, sid, series(base + [90]))
    det.run(sid)
    assert db.one("SELECT COUNT(*) n, MAX(status) s FROM spikes")["s"] == "open"

    write_buckets(db, sid, series(base + [90, 140]))
    det.run(sid)
    row = db.one("SELECT COUNT(*) n, peak_authors p FROM spikes")
    assert (row["n"], row["p"]) == (1, 140)

    write_buckets(db, sid, series(base + [90, 140] + [30] * 6))
    det.run(sid)
    assert db.one("SELECT status FROM spikes")["status"] == "open"     # 3-min "after" window not complete

    write_buckets(db, sid, series(base + [90, 140] + [30] * 25))
    det.run(sid)
    assert db.one("SELECT status FROM spikes")["status"] == "closed"


def test_provisional_spike_no_longer_detected_is_removed_but_closed_kept(db):
    sid = setup_session(db)
    write_buckets(db, sid, series([30] * 40))             # flat chat: nothing to detect
    for start, status in ((T0 + 300, "open"), (T0 + 200, "closed")):
        db.execute("""INSERT INTO spikes (session_id, start_bucket, peak_bucket, end_bucket, peak_authors, peak_messages,
                      baseline_authors, z_score, ratio, chat_delay_s, event_utc, clip_start_utc, clip_end_utc,
                      detected_at, updated_at, status) VALUES (?, ?, ?, ?, 90, 90, 30, 5, 3, 12, 0, 0, 0, ?, ?, ?)""",
                   (sid, start, start, start, utcnow(), utcnow(), status))
    SpikeDetector(db, P, settle_seconds=0).run(sid)
    assert [r["status"] for r in db.query("SELECT status FROM spikes")] == ["closed"]


def test_closed_spike_is_never_rewritten(db):
    sid = setup_session(db)
    det = SpikeDetector(db, P)
    write_buckets(db, sid, series(noisy(30, 30) + [120] + [30] * 6))
    det.run(sid, final=True)
    before = dict(db.one("SELECT * FROM spikes"))
    assert det.run(sid, final=True) == []
    assert dict(db.one("SELECT * FROM spikes")) == before
