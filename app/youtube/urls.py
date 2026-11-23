"""Parse the many shapes of YouTube channel references users paste in."""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, unquote, urlparse

CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
HANDLE_RE = re.compile(r"^@[\w.\-·]{3,100}$")


@dataclass(frozen=True)
class ChannelRef:
    kind: Literal["id", "handle", "username", "custom"]
    value: str


def parse_channel_ref(text: str) -> ChannelRef:
    """Accepts a channel URL, '@handle', or a bare 'UC…' ID.

    Raises ValueError for anything that isn't a channel (e.g. a single video link).
    """
    text = text.strip()
    if CHANNEL_ID_RE.match(text):
        return ChannelRef("id", text)
    if HANDLE_RE.match(text):
        return ChannelRef("handle", text)

    if "://" not in text:
        text = "https://" + text
    url = urlparse(text)
    host = (url.hostname or "").lower()
    if not (host == "youtube.com" or host.endswith(".youtube.com")):
        raise ValueError(f"Not a YouTube URL: {text}")

    parts = [unquote(p) for p in url.path.split("/") if p]
    if not parts:
        raise ValueError("URL has no channel path")
    first = parts[0]

    if first.startswith("@") and HANDLE_RE.match(first):
        return ChannelRef("handle", first)
    if first == "channel" and len(parts) > 1 and CHANNEL_ID_RE.match(parts[1]):
        return ChannelRef("id", parts[1])
    if first == "user" and len(parts) > 1:
        return ChannelRef("username", parts[1])
    if first == "c" and len(parts) > 1:
        return ChannelRef("custom", parts[1])
    if first in ("watch", "live", "shorts", "embed") or host == "youtu.be":
        raise ValueError("That's a video link — paste the channel URL (e.g. youtube.com/@name) instead")
    raise ValueError(f"Unrecognised channel URL: {text}")


VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


def parse_video_id(text: str) -> str:
    """Video ID from a watch / youtu.be / live / shorts / embed link, or a bare 11-character ID."""
    text = text.strip()
    if VIDEO_ID_RE.match(text):
        return text
    if "://" not in text:
        text = "https://" + text
    url = urlparse(text)
    host = (url.hostname or "").lower()
    parts = [p for p in url.path.split("/") if p]
    candidate = None
    if host == "youtu.be" and parts:
        candidate = parts[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parts[:1] == ["watch"]:
            candidate = (parse_qs(url.query).get("v") or [None])[0]
        elif len(parts) > 1 and parts[0] in ("live", "shorts", "embed", "v"):
            candidate = parts[1]
    else:
        raise ValueError(f"Not a YouTube URL: {text}")
    if not candidate or not VIDEO_ID_RE.match(candidate):
        raise ValueError("That isn't a link to a single video (paste the stream's watch link)")
    return candidate
