"""Past-stream (VOD) mode: discovery, chat-replay analysis, clip planning, live-first cutting."""

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.chat.store import ChatStore
from app.chat.ytdlp_source import LineParser
from app.clips.pipeline import ClipWorker, plan_clips, render_options_factory
from app.clips.render import render_vertical
from app.config import Settings
from app.db import utcnow
from app.main import spike_params
from app.publish.queue import enqueue_approved
from app.spikes.detector import SpikeDetector, chorus_share
from app.vod import VodPolicy, VodScanner, VodWorker, skip_reason
from app.youtube.urls import parse_video_id
from tests.conftest import video_item
from tests.test_clips import needs_ffmpeg, pixel

CH = "UCWsDFcIhY2DBi3GB5uykGXA"
VID = "vodvid00001"
START = datetime(2026, 11, 22, 15, tzinfo=timezone.utc)      # video_item()'s actualStartTime
T0 = START.timestamp()
NOW = datetime(2026, 11, 22, 20, tzinfo=timezone.utc)        # 1 h after video_item()'s actualEndTime


# -- fixtures ----------------------------------------------------------------------------------------

def replay_line(mid: str, offset_s: float, author: str, text: str = "lol") -> str:
    """A chat-replay line as yt-dlp writes it for a finished stream (offset INSIDE the action)."""
    body = {"id": mid, "timestampUsec": str(int((T0 + offset_s) * 1e6)), "authorExternalChannelId": author,
            "authorName": {"simpleText": f"@{author}"}, "message": {"runs": [{"text": text}]}}
    return json.dumps({"replayChatItemAction": {
        "actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": body}}}],
        "videoOffsetTimeMsec": str(int(offset_s * 1000))}})


def replay(minutes: int = 40, bursts: dict[int, tuple[int, int]] | None = None,
           burst_text: dict[int, str] | None = None) -> list[str]:
    """6 distinct chatters per 10 s, plus bursts {start_second: (seconds, chatters)}. Everyone types
    "lol" unless burst_text says what that burst typed."""
    bursts = bursts if bursts is not None else {1200: (30, 40), 1800: (20, 25)}
    burst_text = burst_text or {}
    lines, n = [], 0
    for b in range(0, minutes * 60, 10):
        authors, text = 6, "lol"
        for s0, (length, k) in bursts.items():
            if s0 <= b < s0 + length:
                authors, text = k, burst_text.get(s0, "lol")
        for i in range(authors):
            n += 1
            lines.append(replay_line(f"m{n}", b + i * 0.2 + 0.1, f"UC{i:03d}", text))
    return lines


class FakeTools:
    def __init__(self):
        self.streams: dict[str, list[str]] = {}
        self.infos: dict[str, dict] = {}
        self.replays: dict[str, list[str]] = {}
        self.fail_streams = False
        self.calls: list[str] = []

    def list_streams(self, channel_id, limit):
        if self.fail_streams:
            raise RuntimeError("youtube unreachable")
        return self.streams.get(channel_id, [])[:limit]

    def info(self, video_id):
        self.calls.append(f"info:{video_id}")
        return self.infos.get(video_id, {"title": "t", "duration": 2400, "live_status": "was_live", "has_chat": True})

    def chat_replay(self, video_id, out_base: Path):
        self.calls.append(f"chat:{video_id}")
        lines = self.replays.get(video_id)
        if lines is None:
            return None
        path = Path(f"{out_base}.live_chat.json")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def add_channel(db, channel_id=CH, record=1, vod=1, delay_auto=1):
    db.execute("""INSERT INTO channels (channel_id, title, source_url, created_at, record_enabled, vod_enabled,
                                         chat_delay_auto) VALUES (?, 'Lacy', 'u', ?, ?, ?, ?)""",
               (channel_id, utcnow(), record, vod, delay_auto))


def finished_stream(vid=VID, title="GTA 6 MARATHON DAY 4", end="2026-11-22T19:00:00Z"):
    item = video_item(vid, CH, "none", ended=True)
    item["snippet"]["title"] = title
    item["liveStreamingDetails"]["actualEndTime"] = end
    return item


def queue_job(db, vid=VID, status="queued", origin="scan", ended="2026-11-22T19:00:00Z"):
    return db.execute(
        """INSERT INTO vod_jobs (video_id, channel_id, title, started_at, ended_at, duration_s, origin, status,
                                 created_at, updated_at) VALUES (?, ?, 'GTA 6', ?, ?, 2400, ?, ?, ?, ?)""",
        (vid, CH, "2026-11-22T15:00:00Z", ended, origin, status, utcnow(), utcnow())).lastrowid


def make_worker(db, tools, tmp_path, paused=lambda: None, **policy):
    store = ChatStore(db, 10)
    spikes = SpikeDetector(db, spike_params(Settings(_env_file=None)), 15, 25)
    return VodWorker(db, tools, store, spikes, VodPolicy(**policy), tmp_path / "vod", paused=paused)


# -- links and filters ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["BXuVBw88ELc", "https://www.youtube.com/watch?v=BXuVBw88ELc&t=120s",
                                  "youtu.be/BXuVBw88ELc", "https://www.youtube.com/live/BXuVBw88ELc?si=x",
                                  "https://m.youtube.com/watch?feature=share&v=BXuVBw88ELc"])
def test_parse_video_id(text):
    assert parse_video_id(text) == "BXuVBw88ELc"


@pytest.mark.parametrize("text", ["https://www.youtube.com/@LacyHimself", "https://vimeo.com/123", "hello"])
def test_parse_video_id_rejects_non_videos(text):
    with pytest.raises(ValueError):
        parse_video_id(text)


def test_skip_reasons():
    p = VodPolicy()
    end = NOW - timedelta(hours=1)
    assert skip_reason("🌴PLAYING GTA 6 FOR THE FIRST TIME🌴", end, 3 * 3600, p, NOW) is None
    assert skip_reason("GTA VI FINALE", end, 3 * 3600, p, NOW) is None
    assert "GTA 6" in skip_reason("Minecraft hardcore", end, 3 * 3600, p, NOW)
    assert "72 h" in skip_reason("GTA 6", NOW - timedelta(hours=80), 3 * 3600, p, NOW)
    assert "20 min" in skip_reason("GTA 6", end, 600, p, NOW)
    assert skip_reason("Minecraft", end, 3 * 3600, VodPolicy(title_keywords=()), NOW) is None


def test_replay_lines_are_dated_like_live_ones():
    """The offset sits inside the action in replay files; gifts (no timestamp of their own) need it."""
    gift = json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": "61000", "actions": [
        {"addChatItemAction": {"item": {"giftMessageViewModel": {"id": "g1", "text": {"content": "sent"}}}}}]}})
    parser = LineParser()
    parser.parse(replay_line("a", 60, "UC1"))
    [g] = parser.parse(gift)
    assert g.kind == "gift" and g.published_us == int((T0 + 61) * 1e6)


# -- discovery ---------------------------------------------------------------------------------------------

def test_scan_queues_finished_gta_streams_and_never_requeries(db, api, fake_yt):
    add_channel(db)
    tools = FakeTools()
    tools.streams[CH] = ["vodvid00001", "vodvid00002", "vodvid00003", "livevid0001", "oldvid00001"]
    fake_yt.videos.update({
        "vodvid00001": finished_stream("vodvid00001"),
        "vodvid00002": finished_stream("vodvid00002", title="Just chatting"),
        "vodvid00003": finished_stream("vodvid00003", end="2026-11-22T15:10:00Z"),   # 10 min long
        "livevid0001": video_item("livevid0001", CH, "live"),
        "oldvid00001": finished_stream("oldvid00001", end="2026-11-18T19:00:00Z"),
    })
    scanner = VodScanner(db, api, tools, VodPolicy())

    assert len(scanner.scan(NOW)) == 4                 # the live one has no job yet
    jobs = {r["video_id"]: dict(r) for r in db.query("SELECT * FROM vod_jobs")}
    assert jobs["vodvid00001"]["status"] == "queued" and jobs["vodvid00001"]["duration_s"] == 4 * 3600
    assert jobs["vodvid00002"]["error"] == "title doesn't mention GTA 6"
    assert jobs["vodvid00003"]["status"] == jobs["oldvid00001"]["status"] == "skipped"
    assert api.ledger.used() == 1                      # one batched videos.list

    scanner.scan(NOW)                                  # only the still-live stream is asked about again
    assert fake_yt.requests[-1].url.params["id"] == "livevid0001"


def test_scan_skips_channels_with_past_streams_off_and_survives_failures(db, api, fake_yt):
    add_channel(db, vod=0)
    add_channel(db, channel_id="UC" + "x" * 22)
    tools = FakeTools()
    tools.streams[CH] = ["vodvid00001"]
    tools.fail_streams = True
    assert VodScanner(db, api, tools, VodPolicy()).scan(NOW) == []
    assert db.one("SELECT COUNT(*) n FROM errors WHERE component = 'vod.scan'")["n"] == 1   # only the enabled one
    assert not fake_yt.requests


def test_link_needs_a_tracked_channel_and_a_finished_stream(db, api, fake_yt):
    scanner = VodScanner(db, api, FakeTools(), VodPolicy())
    fake_yt.videos[VID] = finished_stream(title="Minecraft", end="2026-11-01T19:00:00Z")   # old, off-topic
    with pytest.raises(ValueError, match="as a streamer first"):
        scanner.add_link(f"https://youtu.be/{VID}", NOW)
    add_channel(db)
    upload = finished_stream("upload00001")
    del upload["liveStreamingDetails"]
    fake_yt.videos["upload00001"] = upload
    with pytest.raises(ValueError, match="regular upload"):
        scanner.add_link("upload00001", NOW)
    fake_yt.videos["livevid0001"] = video_item("livevid0001", CH, "live")
    with pytest.raises(ValueError, match="hasn't ended"):
        scanner.add_link("livevid0001", NOW)

    job = scanner.add_link(f"https://www.youtube.com/watch?v={VID}", NOW)   # links bypass age/title filters
    assert job["status"] == "queued" and job["origin"] == "link"
    with pytest.raises(ValueError, match="already in the list"):
        scanner.add_link(VID, NOW)


def test_link_requeues_a_skipped_stream(db, api):
    add_channel(db)
    jid = queue_job(db, status="skipped")
    job = VodScanner(db, api, FakeTools(), VodPolicy()).add_link(VID, NOW)
    assert job["id"] == jid and job["status"] == "queued" and job["origin"] == "link"
    assert not api.ledger.used()


# -- analysis ----------------------------------------------------------------------------------------------

def test_chat_replay_becomes_vod_clips_at_the_right_second(db, tmp_path):
    add_channel(db)
    jid = queue_job(db)
    tools = FakeTools()
    tools.replays[VID] = replay()
    worker = make_worker(db, tools, tmp_path)

    assert worker.run_once(NOW) == jid

    job = db.one("SELECT * FROM vod_jobs WHERE id = ?", (jid,))
    assert job["status"] == "clipping" and job["clips"] == 2 and job["error"] is None
    assert abs(job["offset_utc"] - T0) < 0.01           # VOD second 0 measured from the replay itself
    assert job["chat_delay_s"] == 35                      # never measured live: the default
    session = db.one("SELECT * FROM live_sessions WHERE id = ?", (job["session_id"],))
    assert session["status"] == "ended" and session["storage_cleaned_at"]   # nothing on disk to clean
    clips = db.query("SELECT * FROM clips ORDER BY window_start_pts")
    assert [c["source_kind"] for c in clips] == ["vod", "vod"]
    # chat burst at VOD 1200 s, chat reacts 35 s late → moment at 1165 s → window [1150, 1190]
    assert (clips[0]["window_start_pts"], clips[0]["window_end_pts"]) == pytest.approx((1150, 1190))
    assert clips[0]["window_start_utc"] == pytest.approx(T0 + 1150)
    assert not (tmp_path / "vod" / VID).exists()          # chat file deleted once it's in the database
    assert db.one("SELECT COUNT(*) n FROM chat_messages")["n"] > 0   # kept until the clips are cut
    assert plan_clips(db) == []                           # the live planner leaves past-stream spikes alone


def test_strongest_moments_only_and_measured_delay_wins(db, tmp_path):
    add_channel(db)
    old = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('older', ?, 'ended', ?)",
                     (CH, utcnow())).lastrowid
    db.execute("""INSERT INTO time_anchors (session_id, pts, utc, sampled_at, edge_lag_s, target_duration)
                  VALUES (?, 0, ?, ?, 31.8, 5)""", (old, utcnow(), utcnow()))
    queue_job(db)
    tools = FakeTools()
    tools.replays[VID] = replay()
    make_worker(db, tools, tmp_path, max_clips=1).run_once(NOW)
    [clip] = db.query("SELECT * FROM clips")
    assert clip["window_start_pts"] == pytest.approx(1200 - 39.8 - 15)   # the 40-chatter burst, Lacy's 39.8 s lag


def test_giveaway_burst_is_not_clipped(db, tmp_path):
    """Measured on Lacy's finale: the biggest bursts were a giveaway keyword and a poll, not moments."""
    add_channel(db)
    queue_job(db)
    tools = FakeTools()
    tools.replays[VID] = replay(burst_text={1200: "GTA"})
    make_worker(db, tools, tmp_path).run_once(NOW)
    [clip] = db.query("SELECT * FROM clips")
    assert clip["window_start_pts"] == pytest.approx(1800 - 35 - 15)        # the real reaction
    reason = db.one("SELECT skip_reason FROM spikes WHERE clip_id IS NULL AND skip_reason IS NOT NULL")["skip_reason"]
    assert "giveaway or poll" in reason and '"gta"' in reason


def test_weak_bursts_are_left_out_of_past_streams(db, tmp_path):
    add_channel(db)
    queue_job(db)
    tools = FakeTools()
    tools.replays[VID] = replay(bursts={1200: (30, 40), 1800: (20, 18)})
    make_worker(db, tools, tmp_path, min_peak_chatters=20).run_once(NOW)
    assert db.one("SELECT COUNT(*) n FROM clips")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM spikes WHERE skip_reason LIKE 'only 18 chatters%'")["n"] == 1


@pytest.mark.parametrize("texts,prompted", [
    (["gta"] * 126 + ["W"] * 4 + ["gta6"] * 4 + [f"msg {i}" for i in range(29)], True),        # giveaway keyword
    (["marathon"] * 312 + ["END"] * 308 + [f"msg {i}" for i in range(47)], True),             # poll
    (["take"] * 9 + ["shoot"] * 5 + ["deal"] * 4 + [f"msg {i}" for i in range(88)], False),   # reacting to a choice
    (["W"] * 80 + ["WWWW"] * 20 + ["LMAOOO"] * 30 + ["no way he did that"] * 3, False),       # hype spam
    (["GG"] * 90 + ["GGGG"] * 10 + ["gg wp"] * 5, False),
    (["NOOOOO"] * 50 + [":face-blue-wide-eyes:"] * 30 + ["bro"] * 5, False),
    (["NOOOO HE GOT SHOT 💀"] * 60, False),                                                   # copypasta hype
])
def test_chorus_share_tells_prompts_from_reactions(texts, prompted):
    assert (chorus_share(texts) >= 0.5) is prompted


def test_moment_already_clipped_live_is_not_clipped_twice(db, tmp_path):
    add_channel(db)
    sid = db.execute("""INSERT INTO live_sessions (video_id, channel_id, status, detected_at, storage_cleaned_at)
                        VALUES (?, ?, 'ended', ?, ?)""", (VID, CH, utcnow(), utcnow())).lastrowid
    db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                                     status, created_at, updated_at) VALUES (?, 18000, 18040, ?, ?, 'published', ?, ?)""",
               (sid, T0 + 1150, T0 + 1190, utcnow(), utcnow()))
    jid = queue_job(db)
    tools = FakeTools()
    tools.replays[VID] = replay()
    make_worker(db, tools, tmp_path).run_once(NOW)
    assert db.one("SELECT session_id FROM vod_jobs WHERE id = ?", (jid,))["session_id"] == sid   # reused
    vod = db.query("SELECT * FROM clips WHERE source_kind = 'vod'")
    assert len(vod) == 1 and vod[0]["window_start_pts"] == pytest.approx(1800 - 35 - 15)   # only the second burst


def test_no_chat_replay_retries_then_gives_up(db, tmp_path):
    add_channel(db)
    jid = queue_job(db)
    tools = FakeTools()
    tools.infos[VID] = {"title": "t", "duration": 2400, "live_status": "was_live", "has_chat": False}
    worker = make_worker(db, tools, tmp_path, chat_retries=2, chat_retry_minutes=60)
    worker.run_once(NOW)
    job = db.one("SELECT * FROM vod_jobs WHERE id = ?", (jid,))
    assert job["status"] == "waiting_chat" and job["next_attempt_at"] == "2026-11-22T21:00:00+00:00"
    assert worker.run_once(NOW + timedelta(minutes=30)) is None          # not due yet
    worker.run_once(NOW + timedelta(minutes=61))
    assert db.one("SELECT status FROM vod_jobs WHERE id = ?", (jid,))["status"] == "no_chat"


def test_errors_retry_then_fail(db, tmp_path):
    add_channel(db)
    jid = queue_job(db)
    tools = FakeTools()
    tools.info = lambda vid: (_ for _ in ()).throw(RuntimeError("Sign in to confirm you're not a bot"))
    worker = make_worker(db, tools, tmp_path)
    for i in range(3):
        worker.run_once(NOW + timedelta(hours=i))
    job = db.one("SELECT * FROM vod_jobs WHERE id = ?", (jid,))
    assert job["status"] == "failed" and "not a bot" in job["error"]


def test_internet_outage_never_uses_up_attempts(db, tmp_path):
    add_channel(db)
    jid = queue_job(db)
    tools = FakeTools()
    tools.info = lambda vid: (_ for _ in ()).throw(
        RuntimeError("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>"))
    worker = make_worker(db, tools, tmp_path)
    for i in range(10):                                   # a 10-hour outage
        worker.run_once(NOW + timedelta(hours=i))
    job = db.one("SELECT * FROM vod_jobs WHERE id = ?", (jid,))
    assert job["status"] == "queued" and job["attempts"] == 0 and "no internet" in job["error"]
    del tools.info                                        # internet is back
    tools.replays[VID] = replay()
    assert worker.run_once(NOW + timedelta(hours=11)) == jid
    assert db.one("SELECT status FROM vod_jobs WHERE id = ?", (jid,))["status"] == "clipping"


def test_waits_while_live_one_stream_at_a_time_and_cleans_up(db, tmp_path):
    add_channel(db)
    first = queue_job(db, vid="vodvid00001", ended="2026-11-22T19:00:00Z")
    second = queue_job(db, vid="vodvid00002", ended="2026-11-21T19:00:00Z")
    tools = FakeTools()
    tools.replays = {"vodvid00001": replay(), "vodvid00002": replay()}
    reason = ["TGG is live"]
    worker = make_worker(db, tools, tmp_path, paused=lambda: reason[0])
    assert worker.run_once(NOW) is None and not tools.calls

    reason[0] = None
    assert worker.run_once(NOW) == first                  # newest first
    assert worker.run_once(NOW) is None                   # its clips aren't cut yet
    db.execute("UPDATE clips SET status = 'cut'")
    assert worker.run_once(NOW) == second
    assert db.one("SELECT status FROM vod_jobs WHERE id = ?", (first,))["status"] == "done"
    sid = db.one("SELECT session_id FROM vod_jobs WHERE id = ?", (first,))["session_id"]
    assert db.one("SELECT COUNT(*) n FROM chat_messages WHERE session_id = ?", (sid,))["n"] == 0


def test_stream_recorded_live_waits_for_its_own_cleanup(db, tmp_path):
    add_channel(db)
    db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES (?, ?, 'ended', ?)",
               (VID, CH, utcnow()))
    queue_job(db)
    tools = FakeTools()
    tools.replays[VID] = replay()
    worker = make_worker(db, tools, tmp_path)
    assert worker.run_once(NOW) is None
    db.execute("UPDATE live_sessions SET storage_cleaned_at = ?", (utcnow(),))
    assert worker.run_once(NOW) is not None


def test_pasted_link_goes_before_newer_streams(db, tmp_path):
    add_channel(db)
    queue_job(db, vid="vodvid00001", ended="2026-11-22T19:00:00Z")
    link = queue_job(db, vid="vodvid00002", ended="2026-11-10T19:00:00Z", origin="link")
    tools = FakeTools()
    tools.replays = {"vodvid00001": replay(), "vodvid00002": replay()}
    assert make_worker(db, tools, tmp_path).run_once(NOW) == link


# -- cutting -------------------------------------------------------------------------------------------------

def vod_clip(db, start=998.0, end=1004.0, attempts=0):
    sid = db.one("SELECT id FROM live_sessions WHERE video_id = ?", (VID,))
    sid = sid["id"] if sid else db.execute(
        "INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES (?, ?, 'ended', ?)",
        (VID, CH, utcnow())).lastrowid
    return db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc,
                                            window_end_utc, created_at, updated_at, source_kind, attempts)
                         VALUES (?, ?, ?, ?, ?, ?, ?, 'vod', ?)""",
                      (sid, start, end, T0 + start, T0 + end, utcnow(), utcnow(), attempts)).lastrowid


def colour_fetch(calls: list, switch_at: float = 1000.0):
    """Stands in for the section download: the 'VOD' is red before second `switch_at`, blue after."""
    def fetch(video_id, start, end, out: Path) -> Path:
        calls.append((video_id, start, end))
        red, blue = switch_at - start, end - switch_at
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"color=red:s=1280x720:r=30:d={red}",
                        "-f", "lavfi", "-i", f"color=blue:s=1280x720:r=30:d={blue}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={red + blue}",
                        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out)], check=True)
        return out
    return fetch


@needs_ffmpeg
def test_vod_clip_is_cut_from_a_downloaded_section_at_the_right_second(db, tmp_path):
    add_channel(db)
    cid = vod_clip(db)
    calls, probed = [], []
    worker = ClipWorker(db, tmp_path / "clips", vod_fetch=colour_fetch(calls),
                        after_vod_fetch=lambda clip, src: probed.append(src))
    assert worker.run_all() == [cid]
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "cut", clip["error"]
    assert calls == [(VID, 995.0, 1007.0)]                    # window ± 3 s, not the whole stream
    assert clip["source_start_pts"] == pytest.approx(995.0)   # in VOD seconds, like the window
    assert probed and Path(clip["source_path"]).name == "source.mp4"
    # the final render's offset math (window - source start) lands on the right second of the VOD
    vertical = tmp_path / "vertical.mp4"
    render_vertical(Path(clip["source_path"]), vertical, clip["window_start_pts"] - clip["source_start_pts"],
                    clip["duration"], render_options_factory(db, Settings(_env_file=None))(dict(clip)))
    assert pixel(vertical, 0.5) == "red"        # VOD 998.5 s
    assert pixel(vertical, 4.0) == "blue"       # VOD 1002 s


def test_live_clips_first_one_download_per_run_and_pause(db, tmp_path):
    add_channel(db)
    v1, v2 = vod_clip(db), vod_clip(db, 2000, 2040)
    fetched, paused = [], [True]

    def fake_cut(clip):
        fetched.append(clip["id"])
        db.execute("UPDATE clips SET status = 'cut' WHERE id = ?", (clip["id"],))

    worker = ClipWorker(db, tmp_path, vod_fetch=lambda *a: None, vod_paused=lambda: paused[0])
    worker._cut_vod = fake_cut
    assert worker.run_all() == [] and not fetched             # someone is live
    paused[0] = False
    assert worker.run_all() == [v1]                           # one download, then back to check for live clips
    assert worker.run_all() == [v2]


def test_failed_download_waits_before_retrying(db, tmp_path):
    add_channel(db)
    cid = vod_clip(db)

    def broken(*a):
        raise RuntimeError("HTTP Error 403")

    worker = ClipWorker(db, tmp_path / "clips", vod_fetch=broken)
    worker.run_all()
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "pending" and clip["attempts"] == 1 and "403" in clip["error"]
    assert worker.run_all() == []                             # not straight away: the network may be down


def test_download_outage_keeps_the_clip_waiting(db, tmp_path):
    add_channel(db)
    cid = vod_clip(db)

    def offline(*a):
        raise RuntimeError("Unable to download: [Errno 11001] getaddrinfo failed")

    worker = ClipWorker(db, tmp_path / "clips", vod_fetch=offline)
    for _ in range(5):
        worker.run_all()
        db.execute("UPDATE clips SET updated_at = '2000-01-01T00:00:00+00:00'")   # skip the 5-minute wait
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    assert clip["status"] == "pending" and clip["attempts"] == 0 and "no internet" in clip["error"]


def test_live_clip_outranks_equal_vod_clip_in_upload_queue(db):
    add_channel(db)
    db.execute("UPDATE channels SET permission_confirmed = 1")
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('live1', ?, 'ended', ?)",
                     (CH, utcnow())).lastrowid
    for kind in ("live", "vod"):
        db.execute("""INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                                         status, score, source_kind, created_at, updated_at, nudity_checked_at)
                      VALUES (?, 0, 40, 0, 40, 'approved', 8.0, ?, ?, ?, ?)""", (sid, kind, utcnow(), utcnow(), utcnow()))
    enqueue_approved(db)
    rows = db.query("SELECT x.source_kind, q.priority FROM publish_queue q JOIN clips x ON x.id = q.clip_id")
    assert {r["source_kind"]: r["priority"] for r in rows} == {"live": 8.0, "vod": 7.0}
