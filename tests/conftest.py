import json

import httpx
import pytest

from app.db import Database
from app.youtube.api import QuotaLedger, YouTubeApi


def video_item(vid: str, channel_id: str, content: str = "live", ended: bool = False,
               chat_id: str | None = "chat-1") -> dict:
    details: dict = {"actualStartTime": "2026-11-22T15:00:00Z"}
    if content == "live" and chat_id:
        details["activeLiveChatId"] = chat_id
    if ended:
        details["actualEndTime"] = "2026-11-22T19:00:00Z"
    return {
        "id": vid,
        "snippet": {"channelId": channel_id, "title": f"title {vid}", "liveBroadcastContent": content},
        "liveStreamingDetails": details,
    }


class FakeYouTube:
    """Stands in for the Data API: tests set .videos / .channels; records every request."""

    def __init__(self):
        self.videos: dict[str, dict] = {}
        self.channels: dict[str, dict] = {}  # keyed by "id:X" / "forHandle:@x"
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p = request.url.params
        if request.url.path.endswith("/videos"):
            ids = p["id"].split(",")
            return httpx.Response(200, json={"items": [self.videos[i] for i in ids if i in self.videos]})
        if request.url.path.endswith("/channels"):
            for key in ("id", "forHandle", "forUsername"):
                if key in p and f"{key}:{p[key]}" in self.channels:
                    return httpx.Response(200, json={"items": [self.channels[f"{key}:{p[key]}"]]})
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, text=json.dumps({"error": "unexpected"}))


class FakeWeb:
    def __init__(self):
        self.rss: dict[str, list[str]] = {}
        self.live_page: dict[str, str | None] = {}
        self.fail_rss = False
        self.fail_live_page = False

    def rss_video_ids(self, channel_id):
        if self.fail_rss:
            raise ConnectionError("rss down")
        return list(self.rss.get(channel_id, []))

    def live_page_video_id(self, channel_id):
        if self.fail_live_page:
            raise ConnectionError("youtube unreachable")
        return self.live_page.get(channel_id)

    def resolve_custom_url(self, url):
        raise ValueError("not in test")


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def fake_yt():
    return FakeYouTube()


@pytest.fixture
def api(db, fake_yt):
    http = httpx.Client(transport=httpx.MockTransport(fake_yt.handler))
    return YouTubeApi("test-key", QuotaLedger(db, daily_budget=100), http=http)


@pytest.fixture
def web():
    return FakeWeb()
