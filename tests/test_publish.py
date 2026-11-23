import json
from datetime import datetime, timedelta, timezone

import httpx

from app.db import utcnow
from app.publish.metadata import (MetadataWriter, assemble, chat_digest, clean_tags, clean_text, pick_title,
                                  template_creative)
from app.publish.queue import PublishQueue, enqueue_approved, next_pacific_midnight
from app.publish.youtube import PublishError, UploadResult

NOW = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)    # 10:00 Pacific
BASE = ["gta 6", "gta vi", "shorts"]


# -- metadata ------------------------------------------------------------------------------

CTX = {"channel": "Lacy", "handle": "@lacyhimself", "video_id": "BXuVBw88ELc", "stream_title": "GTA VI FINALE",
       "transcript": "It's you, man. Come on out! Nooooo!", "chat": ["NOOOO", "THEY DEAD", "RIP"],
       "summary": "chaotic moments", "offset_seconds": 23_150}


def test_clean_text_limits_and_strips_forbidden_chars():
    assert clean_text('  "Jason <finally> gets\n shot"  ', 100) == "Jason finally gets shot"
    long = clean_text("word " * 40, 30)
    assert len(long) <= 30 and long.endswith("…")


def test_tags_dedupe_lowercase_and_budget():
    tags = clean_tags(["GTA 6", "#Lacy", "gta 6", "x" * 70], BASE, ["lacy"])
    assert tags == ["gta 6", "lacy", "gta vi", "shorts"]
    many = clean_tags([f"tag number {i}" for i in range(100)], BASE, [])
    assert sum(len(t) + 3 for t in many) <= 500


def test_assemble_always_credits_source_with_timestamp():
    md = assemble({"title": "Lacy watches Jason get shot 😭", "description": "The finale goes wrong.",
                   "tags": ["gta 6 ending"]}, CTX, BASE, "llm")
    assert md.title == "Lacy watches Jason get shot 😭"
    assert "🎥 Clip from Lacy's livestream (@lacyhimself)" in md.description
    assert "https://www.youtube.com/watch?v=BXuVBw88ELc&t=23150s" in md.description
    assert md.description.endswith("#GTA6 #Lacy #Shorts")
    assert md.tags[0] == "gta 6 ending" and "lacy" in md.tags


def test_channel_name_whitespace_and_title_capitalisation():
    md = assemble({"title": "lacy's intense finale", "description": "", "tags": []}, CTX | {"channel": "Lacy "},
                  BASE, "llm")
    assert md.title == "Lacy's intense finale"
    assert "Clip from Lacy's livestream" in md.description and "#Lacy " in md.description


def test_template_fallback_is_used_when_model_fails():
    def broken(request):
        return httpx.Response(500, text="model crashed")
    writer = MetadataWriter("http://ollama", "m", BASE, http=httpx.Client(transport=httpx.MockTransport(broken)))
    md = writer.write(CTX)
    assert md.source == "template" and "Lacy" in md.title and "watch?v=BXuVBw88ELc" in md.description


def test_model_output_is_parsed_and_cleaned():
    def ok(request):
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        assert "THEY DEAD" in prompt and body["format"]["required"]
        return httpx.Response(200, json={"message": {"content": json.dumps(
            {"titles": ["Lacy's chaotic reaction during the stream 😱", "Jason <got> SHOT in the GTA 6 finale 😭"],
             "hook": "Lacy can't believe it.", "question": "Did you see it coming?",
             "hashtags": ["GTA6Ending", "#Shorts", "Lacy"], "tags": ["Lacy", "GTA 6 Ending"]})}})
    md = MetadataWriter("http://ollama", "m", BASE, http=httpx.Client(transport=httpx.MockTransport(ok))).write(CTX)
    # the generic one lost; the winner didn't name the streamer, so the name goes in front
    assert md.source == "llm" and md.title == "Lacy: Jason got SHOT in the GTA 6 finale 😭"
    assert [c for c, _ in md.candidates][1].startswith("Lacy's chaotic")
    assert md.tags[:2] == ["lacy", "gta 6 ending"]
    assert md.description.startswith("Lacy can't believe it.\n\nDid you see it coming?")
    assert md.description.endswith("#GTA6 #Lacy #GTA6Ending #Shorts")            # no duplicate #Lacy/#Shorts


LACY_CHAT = ["lol", "noooo", "Noooooooo", "BONNIE AND CLYDE", ":face-green-smiling::face-green-smiling:",
             "NOOOOO", "Bad honour ending", "NOOOO", "He got the psycho ending", "Why would you pick swim",
             "BRO SURRENDER", "Bro you surrender", "GG", "GG", "unequip ur gun jackassd", "NOOOOOOOOOOO"]


def test_chat_digest_separates_facts_from_noise():
    said, reactions = chat_digest(LACY_CHAT)
    assert reactions[0] == ("nooo", 5) and ("GG", 2) in reactions
    assert "Why would you pick swim" in said and "Bad honour ending" in said
    assert "BRO SURRENDER" in said and "Bro you surrender" not in said      # same words: kept once
    assert not any(":face" in m for m in said)


def test_specific_titles_beat_generic_ones():
    ctx = {"channel": "Lacy", "transcript": "Come on out! You got a weapon! Nooo!", "chat": LACY_CHAT}
    best, scored = pick_title([
        "Lacy's chaotic reaction during the gta 6 finale 😱",
        "LACY GETS THE WORST ENDING EVER!!!",
        "Lacy refused to surrender and got the bad ending 💀",
        "Chat begged him to surrender… he didn't 💀",                 # copied style example
        "x" * 90,
    ], ctx)
    assert best == "Lacy refused to surrender and got the bad ending 💀"
    assert scored[-1][1] < 0 and len(scored) == 5


def test_hashtags_capped_at_five():
    md = assemble({"titles": ["Lacy picks swim"], "hook": "h", "question": "q",
                   "hashtags": ["GTA6Ending", "BadEnding", "Swim", "Finale"], "tags": []}, CTX, BASE, "llm")
    assert md.description.splitlines()[-1] == "#GTA6 #Lacy #GTA6Ending #BadEnding #Shorts"


# -- queue ----------------------------------------------------------------------------------------

class FakePublisher:
    platform = "youtube"

    def __init__(self, fail=None, signed_in=True, applied=None, later=None):
        self.fail, self.signed_in, self.uploads = list(fail or []), signed_in, []
        self.applied, self.later = applied, later      # privacy in the insert response / on re-check

    def ready(self):
        return (True, "ok") if self.signed_in else (False, "not signed in")

    def upload(self, video, title, description, tags, privacy):
        if self.fail:
            raise self.fail.pop(0)
        self.uploads.append((str(video), title, privacy))
        return UploadResult(f"yt{len(self.uploads)}", f"https://youtube.com/shorts/yt{len(self.uploads)}", self.applied)

    def privacy_of(self, remote_id):
        return self.later


class FakeWriter:
    def write(self, ctx):
        return assemble({"title": f"{ctx['channel']} moment", "description": "d", "tags": []}, ctx, BASE, "llm")


def setup(db, permission=1, clips=((7.5, "approved"),)):
    db.execute("INSERT INTO channels (channel_id, title, handle, source_url, created_at, permission_confirmed) "
               "VALUES ('UC1','Lacy','@lacyhimself','u',?,?)", (utcnow(), permission))
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, title, status, detected_at, actual_start_at) "
                     "VALUES ('vid','UC1','GTA VI FINALE','ended',?, '2026-11-22T16:00:00Z')", (utcnow(),)).lastrowid
    ids = []
    for score, status in clips:
        ids.append(db.execute(
            """INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc, status,
                                  score, final_path, created_at, updated_at, nudity_checked_at)
               VALUES (?, 0, 40, 1795385000, 1795385040, ?, ?, ?, ?, ?, ?)""",
            (sid, status, score, f"final{score}.mp4", utcnow(), utcnow(), utcnow())).lastrowid)
    return ids


def make_queue(db, pub, **kw):
    opts = dict(enabled=True, privacy="private", interval_minutes=30, max_per_day=6) | kw
    return PublishQueue(db, pub, FakeWriter(), **opts)


def test_only_permitted_approved_clips_are_queued(db):
    setup(db, permission=0)
    assert enqueue_approved(db) == []
    db.execute("UPDATE channels SET permission_confirmed = 1")
    assert len(enqueue_approved(db)) == 1
    assert enqueue_approved(db) == []                  # never queued twice


def test_rejected_and_unapproved_clips_never_published(db):
    setup(db, clips=((9.0, "rejected"), (8.0, "captioned")))
    pub = FakePublisher()
    assert make_queue(db, pub).run_once(NOW) is None and pub.uploads == []


def test_publishes_best_first_then_respects_interval(db):
    a, b = setup(db, clips=((6.5, "approved"), (8.2, "approved")))
    pub = FakePublisher()
    q = make_queue(db, pub)
    item = q.run_once(NOW)
    assert item["clip_id"] == b and item["status"] == "published" and item["remote_id"] == "yt1"
    assert pub.uploads[0][2] == "private"
    assert db.one("SELECT status FROM clips WHERE id = ?", (b,))["status"] == "published"
    # within 30 min of the last upload: nothing
    assert q.run_once(NOW + timedelta(minutes=10)) is None
    assert "next upload after" in q.why_not_now(datetime.now(timezone.utc))


def test_daily_cap(db):
    setup(db, clips=tuple((5.0 + i, "approved") for i in range(4)))
    pub = FakePublisher()
    q = make_queue(db, pub, interval_minutes=0, max_per_day=2)
    now = datetime.now(timezone.utc)
    q.run_once(now), q.run_once(now), q.run_once(now)
    assert len(pub.uploads) == 2 and "daily cap" in q.why_not_now(now)


def test_metadata_generated_once_and_stored(db):
    setup(db)
    q = make_queue(db, FakePublisher())
    item = q.run_once(NOW)
    assert item["title"] == "Lacy moment" and item["metadata_source"] == "llm"
    assert "watch?v=vid&t=" in item["description"]


def test_transient_failure_backs_off_then_succeeds(db):
    setup(db)
    pub = FakePublisher(fail=[PublishError("socket closed", "retry")])
    q = make_queue(db, pub, interval_minutes=0)
    item = q.run_once(NOW)
    assert item["status"] == "queued" and item["attempts"] == 1 and item["next_attempt_at"] > NOW.isoformat()
    assert q.run_once(NOW + timedelta(minutes=1)) is None             # still backing off
    item = q.run_once(NOW + timedelta(minutes=6))
    assert item["status"] == "published"
    outcomes = [r["outcome"] for r in db.query("SELECT outcome FROM publish_attempts ORDER BY id")]
    assert outcomes == ["retry", "published"]


def test_gives_up_after_three_attempts(db):
    setup(db)
    q = make_queue(db, FakePublisher(fail=[PublishError("boom", "retry")] * 3), interval_minutes=0)
    t = NOW
    for _ in range(3):
        item = q.run_once(t)
        t += timedelta(hours=2)
    assert item["status"] == "failed" and "boom" in item["error"]


def test_quota_waits_for_pacific_reset_without_using_an_attempt(db):
    setup(db)
    q = make_queue(db, FakePublisher(fail=[PublishError("quotaExceeded", "quota")]))
    item = q.run_once(NOW)
    assert item["status"] == "queued" and item["attempts"] == 0
    assert item["next_attempt_at"] == next_pacific_midnight(NOW).isoformat(timespec="seconds")
    assert next_pacific_midnight(NOW) == datetime(2026, 11, 24, 8, 5, tzinfo=timezone.utc)   # 00:05 PST


def test_not_signed_in_or_disabled_does_nothing(db):
    setup(db)
    assert make_queue(db, FakePublisher(signed_in=False)).run_once(NOW) is None
    assert make_queue(db, FakePublisher(), enabled=False).run_once(NOW) is None
    assert db.one("SELECT status FROM publish_queue")["status"] == "queued"   # still queued, waiting


def test_sign_in_status_tracks_testing_mode_expiry(tmp_path):
    from app.publish.youtube import record_sign_in, sign_in_info_path, sign_in_status
    token = tmp_path / "youtube_token.json"
    assert sign_in_status(token)["signed_in"] is False
    token.write_text("{}")
    record_sign_in(token, "GTA Moments", "UCx")
    st = sign_in_status(token)
    assert st["signed_in"] and st["channel"] == "GTA Moments" and st["renew_soon"] is False
    info = json.loads(sign_in_info_path(token).read_text())
    info["signed_in_at"] = (datetime.now(timezone.utc) - timedelta(days=6, hours=12)).isoformat()
    sign_in_info_path(token).write_text(json.dumps(info))
    assert sign_in_status(token)["renew_soon"] is True


def test_publisher_not_ready_when_sign_in_cannot_refresh(tmp_path):
    from app.publish.youtube import YouTubePublisher
    token = tmp_path / "youtube_token.json"
    pub = YouTubePublisher(token)
    assert pub.ready()[0] is False                                # never signed in
    token.write_text(json.dumps({"token": "x", "refresh_token": "dead", "client_id": "c", "client_secret": "s",
                                 "token_uri": "http://127.0.0.1:9/token", "expiry": "2020-01-01T00:00:00Z"}))
    ok, reason = YouTubePublisher(token).ready()                  # expired + refresh fails → sign in again
    assert ok is False and "app.publish.auth" in reason


def test_interrupted_upload_requeued_on_startup(db):
    setup(db)
    enqueue_approved(db)
    db.execute("UPDATE publish_queue SET status = 'uploading'")
    db.mark_interrupted()
    assert db.one("SELECT status FROM publish_queue")["status"] == "queued"


def test_public_upload_locked_to_private_is_flagged(db):
    setup(db)
    pub = FakePublisher(applied="public", later="private")       # accepted as public, locked after processing
    q = make_queue(db, pub, privacy="public")
    item = q.run_once(NOW)
    assert item["privacy"] == "public" and item["privacy_actual"] == "public" and item["error"] is None
    q.run_once(NOW + timedelta(minutes=5))                       # too early to re-check
    assert db.one("SELECT privacy_checked_at FROM publish_queue")["privacy_checked_at"] is None
    q.run_once(NOW + timedelta(minutes=15))
    row = db.one("SELECT * FROM publish_queue")
    assert row["privacy_actual"] == "private" and "audit" in row["error"]
    assert db.one("SELECT component FROM errors")["component"] == "publish.privacy"
    q.run_once(NOW + timedelta(minutes=30))
    assert db.one("SELECT COUNT(*) n FROM errors")["n"] == 1      # checked once, not every minute


def test_private_uploads_are_never_rechecked(db):
    setup(db)
    q = make_queue(db, FakePublisher(applied="private", later="private"))
    q.run_once(NOW)
    q.run_once(NOW + timedelta(hours=1))
    assert db.one("SELECT error FROM publish_queue")["error"] is None


def test_title_cleanup_labels_quotes_and_profanity():
    ctx = {"channel": "Lacy", "transcript": "It's you, man. Come on out!", "chat": ["Surrender dumass", "gg fkn eazy"]}
    quoted = "\"It's you, man.\" Lacy's last words in GTA 6"
    best, scored = pick_title(["CHAT'S VERDICT: Did Lacy really not surrender?", quoted,
                               "It's you, man.\" Lacy before the ending", "Lacy's dumbass ending in GTA 6"], ctx)
    titles = dict(scored)
    assert "Did Lacy really not surrender?" in titles                     # label stripped
    assert quoted in titles                                               # quote-led title kept intact
    assert "\"It's you, man.\" Lacy before the ending" in titles          # missing opening quote added
    assert titles["Lacy's dumbass ending in GTA 6"] < min(v for k, v in titles.items() if "dumbass" not in k)
    said, _ = chat_digest(ctx["chat"])
    assert not any("dumass" in m or "fkn" in m for m in said)
    assert clean_tags(["gta 6 shit", "lacy", "class pass"], [], []) == ["lacy", "class pass"]


def test_operator_note_is_in_the_title_prompt():
    from app.publish.metadata import build_prompt
    ctx = dict(channel="Lacy", duration=40, stream_title="GTA VI FINALE", transcript="", chat=[], summary="",
               note="post-credits scene")
    assert '(trust this most): "post-credits scene"' in build_prompt(ctx)
    assert "trust this most" not in build_prompt({**ctx, "note": ""})


# -- posting strategy ----------------------------------------------------------------------------------

from app.publish.queue import PostingStrategy   # noqa: E402

# NOW = 18:00 UTC = 13:00 US Eastern: the 12:00 slot has passed, next is 14:30 (19:30 UTC)


def bank(db, clips):
    """clips: [(score, hours_since_moment, session_video_id, kind)] → approved, permitted, queued."""
    db.execute("INSERT OR IGNORE INTO channels (channel_id, title, source_url, created_at, permission_confirmed) "
               "VALUES ('UC1','Lacy','u',?,1)", (utcnow(),))
    ids = []
    for score, hours, vid, kind in clips:
        row = db.one("SELECT id FROM live_sessions WHERE video_id = ?", (vid,))
        sid = row["id"] if row else db.execute(
            "INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES (?, 'UC1', 'ended', ?)",
            (vid, utcnow())).lastrowid
        moment = NOW.timestamp() - hours * 3600
        ids.append(db.execute(
            """INSERT INTO clips (session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                                  status, score, final_path, source_kind, created_at, updated_at, nudity_checked_at)
               VALUES (?, 0, 40, ?, ?, 'approved', ?, 'f.mp4', ?, ?, ?, ?)""",
            (sid, moment, moment + 40, score, kind, utcnow(), utcnow(), utcnow())).lastrowid)
    enqueue_approved(db)
    return ids


def test_slots_follow_the_audience_clock():
    st = PostingStrategy()
    assert st.last_slot(NOW) == datetime(2026, 11, 23, 17, 0, tzinfo=timezone.utc)      # 12:00 ET
    assert st.next_slot(NOW) == datetime(2026, 11, 23, 19, 30, tzinfo=timezone.utc)     # 14:30 ET
    early = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)                           # 05:00 ET
    assert st.last_slot(early) == datetime(2026, 11, 23, 4, 0, tzinfo=timezone.utc)     # yesterday 23:00 ET


def test_one_post_per_slot_then_waits_for_the_next(db):
    bank(db, [(7.0, 2, "s1", "live"), (7.5, 2, "s2", "live")])
    pub = FakePublisher()
    q = make_queue(db, pub, strategy=PostingStrategy())
    assert q.run_once(NOW)["status"] == "published"
    later = NOW + timedelta(minutes=45)
    assert q.run_once(later) is None                                   # 12:00 slot already used
    assert "next posting slot 14:30 US time" in q.why_not_now(later)
    assert q.run_once(datetime(2026, 11, 23, 19, 31, tzinfo=timezone.utc))["status"] == "published"
    assert len(pub.uploads) == 2


def test_breaking_live_moment_skips_the_wait(db):
    bank(db, [(7.0, 2, "s1", "live")])
    pub = FakePublisher()
    q = make_queue(db, pub, strategy=PostingStrategy())
    q.run_once(NOW)                                                     # uses the 12:00 slot
    soon = NOW + timedelta(minutes=40)
    [hot] = bank(db, [(8.6, 0.3, "s2", "live")])                        # huge moment 18 minutes ago
    item = q.run_once(soon)
    assert item["clip_id"] == hot and item["status"] == "published"
    [vod] = bank(db, [(9.0, 0.3, "s3", "vod")])                         # past streams never jump the queue
    assert q.run_once(soon + timedelta(minutes=40)) is None


def test_fresh_clip_beats_slightly_better_old_one_and_old_ones_expire(db):
    old, fresh, ancient = bank(db, [(8.0, 60, "s1", "live"), (7.5, 1, "s2", "live"), (9.5, 120, "s3", "live")])
    q = make_queue(db, FakePublisher(), strategy=PostingStrategy())
    assert q.run_once(NOW)["clip_id"] == fresh                          # 7.5 now beats 8.0 from 2.5 days ago
    gone = db.one("SELECT status, error FROM publish_queue WHERE clip_id = ?", (ancient,))
    assert gone["status"] == "expired" and "5.0 days old" in gone["error"]


def test_at_most_two_posts_per_stream_per_day(db):
    bank(db, [(8.0, 1, "s1", "live"), (7.9, 1, "s1", "live"), (7.8, 1, "s1", "live")])
    pub = FakePublisher()
    q = make_queue(db, pub, strategy=PostingStrategy())
    for t in (NOW, datetime(2026, 11, 23, 19, 31, tzinfo=timezone.utc), datetime(2026, 11, 23, 22, 1, tzinfo=timezone.utc)):
        q.run_once(t)
    assert len(pub.uploads) == 2


def test_internet_outage_at_a_slot_keeps_the_clip_queued_without_using_attempts(db):
    bank(db, [(7.5, 2, "s1", "live")])
    down = PublishError("upload interrupted: Unable to find the server at youtube.googleapis.com", "network")
    pub = FakePublisher(fail=[down] * 20)
    q = make_queue(db, pub, strategy=PostingStrategy())
    t = NOW
    for _ in range(20):                                   # 100 minutes offline, a try every 5 minutes
        q.run_once(t)
        t += timedelta(minutes=5)
    row = db.one("SELECT status, attempts, error FROM publish_queue")
    assert row["status"] == "queued" and row["attempts"] == 0 and "Unable to find the server" in row["error"]
    assert q.run_once(t)["status"] == "published"         # back online: the 12:00 slot's post still goes out


def test_laptop_asleep_through_slots_posts_once_on_wake(db):
    bank(db, [(7.0, 1, "s1", "live"), (7.1, 1, "s2", "live"), (7.2, 1, "s3", "live")])
    pub = FakePublisher()
    q = make_queue(db, pub, strategy=PostingStrategy())
    wake = datetime(2026, 11, 24, 0, 30, tzinfo=timezone.utc)     # 19:30 ET: slept through 12:00, 14:30, 17:00, 19:00
    q.run_once(wake)
    q.run_once(wake + timedelta(minutes=1))
    assert len(pub.uploads) == 1                                  # one catch-up post, not a burst
    q.run_once(datetime(2026, 11, 24, 2, 1, tzinfo=timezone.utc))  # 21:00 ET slot
    assert len(pub.uploads) == 2


def test_offline_sign_in_refresh_is_a_network_problem_not_a_sign_out(tmp_path, monkeypatch):
    import app.publish.youtube as yt
    from google.auth.exceptions import TransportError

    class Creds:
        valid, expired, refresh_token = False, True, "r"

        def refresh(self, request):
            raise TransportError("HTTPSConnectionPool: Max retries exceeded ([Errno 11001] getaddrinfo failed)")

    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    from google.oauth2.credentials import Credentials
    monkeypatch.setattr(Credentials, "from_authorized_user_file", lambda *a, **k: Creds())
    try:
        yt.load_credentials(token)
    except PublishError as e:
        assert e.kind == "network" and "no internet" in str(e)
    else:
        raise AssertionError("expected PublishError")



def test_titles_name_the_streamer():
    from app.publish.metadata import names_streamer, pick_title, streamer_name
    assert streamer_name("Kai Cenat Live") == "Kai Cenat" and streamer_name("IShowSpeed") == "IShowSpeed"
    assert names_streamer("Kai picks the bad ending", "Kai Cenat Live")
    assert not names_streamer("Kaitlyn wins the race", "Kai Cenat Live")
    ctx = dict(channel="Kai Cenat Live", chat=[], transcript="club home betrayal ending")
    best, _ = pick_title(["What betrayal set up the GTA 6 bad ending?", "Kai Cenat picks club over home 💀"], ctx)
    assert best == "Kai Cenat picks club over home 💀"                 # the named one wins
    best, _ = pick_title(["What betrayal set up the GTA 6 bad ending?"], ctx)
    assert best == "Kai Cenat: What betrayal set up the GTA 6 bad ending?"   # nobody named him: prefixed
    best, scored = pick_title(["13 requests for club + 8 for home filled the chat.",
                               "Kai Cenat can't pick between club and home"], ctx)
    assert best.startswith("Kai Cenat can't")                           # chat-count titles lose


def test_hashtag_uses_the_streamer_name():
    from app.publish.metadata import assemble
    ctx = dict(channel="Kai Cenat Live", handle="@KaiCenatLive", video_id="v", offset_seconds=60, chat=[],
               transcript="", stream_title="GTA 6", summary="")
    md = assemble({"titles": ["Kai Cenat picks the bad ending"], "hook": "h", "question": "q", "hashtags": [],
                   "tags": []}, ctx, ["gta 6"], "llm")
    assert "#KaiCenat " in md.description and "#KaiCenatLive" not in md.description
    assert "kai cenat" in md.tags
