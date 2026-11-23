import pytest

from app import channels as ch
from app.youtube.api import QuotaExceeded

UC = "UCWsDFcIhY2DBi3GB5uykGXA"


def channel_item(handle="@ishowspeed"):
    return {"id": UC, "snippet": {"title": "IShowSpeed", "customUrl": handle}}


def test_add_channel_by_handle_resolves_id(db, api, web, fake_yt):
    fake_yt.channels["forHandle:@IShowSpeed"] = channel_item()
    row = ch.add_channel(db, api, web, "https://www.youtube.com/@IShowSpeed", permission_confirmed=True)
    assert row["channel_id"] == UC and row["title"] == "IShowSpeed"
    assert row["permission_confirmed"] == 1 and row["enabled"] == 1 and row["record_enabled"] == 1
    assert api.ledger.used() == 1  # channels.list costs 1 unit


def test_duplicate_channel_rejected_even_via_different_url(db, api, web, fake_yt):
    fake_yt.channels["forHandle:@IShowSpeed"] = channel_item()
    fake_yt.channels[f"id:{UC}"] = channel_item()
    ch.add_channel(db, api, web, "@IShowSpeed")
    with pytest.raises(ch.ChannelExists):
        ch.add_channel(db, api, web, f"https://www.youtube.com/channel/{UC}")


def test_update_and_remove(db, api, web, fake_yt):
    fake_yt.channels["forHandle:@IShowSpeed"] = channel_item()
    ch.add_channel(db, api, web, "@IShowSpeed")
    row = ch.update_channel(db, UC, enabled=False, notes="paused")
    assert row["enabled"] == 0 and row["notes"] == "paused"
    assert ch.remove_channel(db, UC) == []
    assert ch.list_channels(db) == []
    with pytest.raises(LookupError):
        ch.remove_channel(db, UC)


def test_removing_a_channel_ends_its_live_stream_so_footage_gets_cleaned(db, api, web, fake_yt):
    from app.db import utcnow
    fake_yt.channels["forHandle:@IShowSpeed"] = channel_item()
    ch.add_channel(db, api, web, "@IShowSpeed")
    db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v1', ?, 'live', ?)",
               (UC, utcnow()))
    db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v0', ?, 'ended', ?)",
               (UC, utcnow()))
    [ended] = ch.remove_channel(db, UC)
    assert ended["video_id"] == "v1"
    row = db.one("SELECT status, ended_at FROM live_sessions WHERE video_id = 'v1'")
    assert row["status"] == "ended" and row["ended_at"]


def test_local_budget_blocks_calls_before_hitting_google(db, api, fake_yt):
    api.ledger.daily_budget = 2
    api.get_videos(["a"])
    api.get_videos(["b"])
    with pytest.raises(QuotaExceeded):
        api.get_videos(["c"])
    assert len(fake_yt.requests) == 2


def test_videos_list_batches_50_ids_per_unit(db, api, fake_yt):
    api.get_videos([f"v{i:02d}" for i in range(120)])
    assert len(fake_yt.requests) == 3
    assert api.ledger.used() == 3
