"""Quota-free signals from public YouTube pages: the channel RSS feed and the /live page.

These only *suggest* candidate video IDs. videos.list is the source of truth for live state.
"""

import re

import defusedxml.ElementTree as ET
import httpx

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
LIVE_URL = "https://www.youtube.com/channel/{channel_id}/live"
NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

# Browser-like headers; SOCS skips the EU cookie-consent interstitial.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "SOCS=CAI",
}

CANONICAL_WATCH_RE = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"'
)
CANONICAL_CHANNEL_RE = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'
)


class YouTubeWeb:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=20, headers=BROWSER_HEADERS, follow_redirects=True)

    def rss_video_ids(self, channel_id: str) -> list[str]:
        """Latest ~15 uploads/broadcasts, ordered by creation time (NOT by live-ness)."""
        resp = self.http.get(RSS_URL.format(channel_id=channel_id))
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        return [el.text for el in root.iterfind("atom:entry/yt:videoId", NS) if el.text]

    def live_page_video_id(self, channel_id: str) -> str | None:
        """If the channel is live, /live's canonical link is the watch URL of the stream."""
        resp = self.http.get(LIVE_URL.format(channel_id=channel_id))
        resp.raise_for_status()
        m = CANONICAL_WATCH_RE.search(resp.text)
        return m.group(1) if m else None

    def resolve_custom_url(self, path_url: str) -> str:
        """Resolve legacy /c/name URLs (no API support) to a channel ID via the page's canonical link."""
        resp = self.http.get(path_url)
        resp.raise_for_status()
        m = CANONICAL_CHANNEL_RE.search(resp.text)
        if not m:
            raise ValueError(f"Could not find channel ID on {path_url}")
        return m.group(1)
