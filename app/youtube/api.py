"""YouTube Data API v3 client (API-key only) with a local quota ledger.

Costs per call (developers.google.com/youtube/v3/determine_quota_cost):
channels.list = 1, videos.list = 1 (up to 50 IDs). We never call search.list (100).
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.db import Database
from app.youtube.urls import ChannelRef

API_BASE = "https://www.googleapis.com/youtube/v3"
PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_BUCKET = "default"


class QuotaExceeded(Exception):
    """Our local budget is spent, or Google returned quotaExceeded."""


class YouTubeApiError(Exception):
    pass


@dataclass
class ChannelInfo:
    channel_id: str
    title: str
    handle: str | None


class QuotaLedger:
    def __init__(self, db: Database, daily_budget: int):
        self.db = db
        self.daily_budget = daily_budget

    @staticmethod
    def today() -> str:
        return datetime.now(PACIFIC).date().isoformat()

    def used(self, bucket: str = DEFAULT_BUCKET) -> int:
        row = self.db.one(
            "SELECT units FROM quota_usage WHERE day = ? AND bucket = ?", (self.today(), bucket)
        )
        return row["units"] if row else 0

    def spend(self, units: int, bucket: str = DEFAULT_BUCKET) -> None:
        if self.used(bucket) + units > self.daily_budget:
            raise QuotaExceeded(f"Local daily budget of {self.daily_budget} units reached")
        self.db.execute(
            """INSERT INTO quota_usage (day, bucket, units) VALUES (?, ?, ?)
               ON CONFLICT (day, bucket) DO UPDATE SET units = units + excluded.units""",
            (self.today(), bucket, units),
        )

    def mark_exhausted(self, bucket: str = DEFAULT_BUCKET) -> None:
        """Google says we're out — stop spending until the Pacific-time reset."""
        self.db.execute(
            """INSERT INTO quota_usage (day, bucket, units) VALUES (?, ?, ?)
               ON CONFLICT (day, bucket) DO UPDATE SET units = excluded.units""",
            (self.today(), bucket, self.daily_budget),
        )


class YouTubeApi:
    def __init__(self, api_key: str, ledger: QuotaLedger, http: httpx.Client | None = None):
        self.api_key = api_key
        self.ledger = ledger
        self.http = http or httpx.Client(timeout=20)

    def _get(self, resource: str, params: dict, cost: int = 1) -> dict:
        if not self.api_key:
            raise YouTubeApiError("YOUTUBE_API_KEY is not set (see docs/setup-google-cloud.md)")
        self.ledger.spend(cost)
        resp = self.http.get(f"{API_BASE}/{resource}", params={**params, "key": self.api_key})
        if resp.status_code == 403 and "quotaExceeded" in resp.text:
            self.ledger.mark_exhausted()
            raise QuotaExceeded("YouTube reported quotaExceeded")
        if resp.status_code != 200:
            raise YouTubeApiError(f"{resource} HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def resolve_channel(self, ref: ChannelRef) -> ChannelInfo:
        if ref.kind == "custom":
            raise YouTubeApiError("/c/ URLs can't be resolved via the API; use resolve via web first")
        param = {"id": "id", "handle": "forHandle", "username": "forUsername"}[ref.kind]
        data = self._get("channels", {"part": "snippet", param: ref.value})
        items = data.get("items") or []
        if not items:
            raise YouTubeApiError(f"No channel found for {ref.kind} {ref.value!r}")
        item = items[0]
        return ChannelInfo(
            channel_id=item["id"],
            title=item["snippet"]["title"],
            handle=item["snippet"].get("customUrl"),
        )

    def get_videos(self, video_ids: list[str]) -> dict[str, dict]:
        """Returns {video_id: item} for IDs that still exist; missing IDs are private/deleted."""
        out: dict[str, dict] = {}
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            data = self._get(
                "videos",
                {"part": "snippet,liveStreamingDetails", "id": ",".join(batch), "maxResults": 50},
            )
            for item in data.get("items", []):
                out[item["id"]] = item
        return out
