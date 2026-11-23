"""Map recording timestamps (PTS) to real UTC time.

YouTube's live HLS playlist tags every ~2 s segment with #EXT-X-PROGRAM-DATE-TIME (UTC,
YouTube's clock). Its MPEG-TS PTS count seconds since broadcast start. Downloading ONE
segment and probing its PTS gives an anchor (pts, utc); then for any recorded frame:

    utc = anchor.utc + (pts - anchor.pts)

This deliberately ignores the laptop clock (measured ~4-5 s off in Phase 1).
Chat messages' publishedAt is also YouTube-side, so chat and video share a clock.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from app.recording.media import probe


@dataclass(frozen=True)
class Anchor:
    pts: float
    utc: datetime
    # How far YouTube's live edge trails real time when sampled (server Date header - newest PDT), and
    # the playlist's segment length (2 s = low-latency stream, 5 s = normal latency). Together they
    # predict how late viewers, and therefore chat, see each moment.
    edge_lag: float | None = None
    target_duration: float | None = None


@dataclass(frozen=True)
class PlaylistSegment:
    uri: str
    duration: float
    pdt: datetime | None


def parse_media_playlist(text: str) -> list[PlaylistSegment]:
    """Parse an HLS media playlist; each PROGRAM-DATE-TIME applies to the next segment URI."""
    segments: list[PlaylistSegment] = []
    pdt: datetime | None = None
    duration = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00"))
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",")[0])
        elif not line.startswith("#"):
            segments.append(PlaylistSegment(line, duration, pdt))
            pdt, duration = None, 0.0
    return segments


def pts_to_utc(pts: float, anchor: Anchor) -> datetime:
    return anchor.utc + timedelta(seconds=pts - anchor.pts)


def utc_to_pts(utc: datetime, anchor: Anchor) -> float:
    return anchor.pts + (utc - anchor.utc).total_seconds()


def sample_anchor(hls_url: str, http: httpx.Client, ffprobe_bin: str = "ffprobe") -> Anchor:
    """Fetch the playlist, download its newest dated segment (~0.5 MB at 720p) and probe its PTS."""
    resp = http.get(hls_url)
    resp.raise_for_status()
    dated = [s for s in parse_media_playlist(resp.text) if s.pdt is not None]
    if not dated:
        raise RuntimeError("playlist has no EXT-X-PROGRAM-DATE-TIME tags")
    seg = dated[-1]
    edge_lag = None
    if resp.headers.get("date"):   # Google's clock, 1 s resolution: good enough for a ~10-40 s lag
        edge_lag = (parsedate_to_datetime(resp.headers["date"]) - seg.pdt).total_seconds()
    data = http.get(seg.uri)
    data.raise_for_status()
    return Anchor(pts=probe(data.content, ffprobe_bin).start_time, utc=seg.pdt, edge_lag=edge_lag,
                  target_duration=target_duration(resp.text))


def target_duration(playlist: str) -> float | None:
    for line in playlist.splitlines():
        if line.startswith("#EXT-X-TARGETDURATION:"):
            return float(line.split(":", 1)[1])
    return None


REACTION_SECONDS = 3.0


def estimate_chat_delay(edge_lag: float, target: float | None) -> float:
    """Seconds from a moment (PDT clock) to chat reacting: the live edge trails by edge_lag, players sit
    about one segment behind the edge, then people need a few seconds to type.
    Measured on Lacy's finale: 31.8 + 5 + 3 = 39.8 s predicted vs ~39 s observed."""
    return round(max(3.0, min(120.0, edge_lag + (target or 2.0) + REACTION_SECONDS)), 1)
