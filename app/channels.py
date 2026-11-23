"""Channel management: resolve a pasted URL to a channel ID and store it."""

from app.clips.render import parse_rect
from app.db import Database, utcnow
from app.youtube.api import YouTubeApi
from app.youtube.urls import ChannelRef, parse_channel_ref
from app.youtube.web import YouTubeWeb


class ChannelExists(Exception):
    pass


def add_channel(db: Database, api: YouTubeApi, web: YouTubeWeb, url: str,
                permission_confirmed: bool = False, notes: str | None = None, record_enabled: bool = True) -> dict:
    ref = parse_channel_ref(url)  # ValueError on garbage
    if ref.kind == "custom":
        ref = ChannelRef("id", web.resolve_custom_url(f"https://www.youtube.com/c/{ref.value}"))
    info = api.resolve_channel(ref)

    if db.one("SELECT 1 FROM channels WHERE channel_id = ?", (info.channel_id,)):
        raise ChannelExists(f"{info.title} ({info.channel_id}) is already added")
    db.execute(
        """INSERT INTO channels (channel_id, handle, title, source_url, permission_confirmed, notes, created_at, layout,
                                 record_enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'auto', ?)""",
        (info.channel_id, info.handle, info.title.strip(), url.strip(), int(permission_confirmed), notes, utcnow(),
         int(record_enabled)),
    )
    return get_channel(db, info.channel_id)


def get_channel(db: Database, channel_id: str) -> dict | None:
    row = db.one("SELECT * FROM channels WHERE channel_id = ?", (channel_id,))
    return dict(row) if row else None


def list_channels(db: Database) -> list[dict]:
    rows = db.query(
        """SELECT c.*,
                  (SELECT s.video_id FROM live_sessions s
                    WHERE s.channel_id = c.channel_id AND s.status = 'live'
                    ORDER BY s.detected_at DESC LIMIT 1) AS live_video_id
             FROM channels c ORDER BY c.created_at"""
    )
    return [dict(r) for r in rows]


def update_channel(db: Database, channel_id: str, **fields) -> dict | None:
    allowed = {"enabled", "record_enabled", "vod_enabled", "permission_confirmed", "chat_delay_seconds", "chat_delay_auto", "layout",
               "facecam_rect", "notes"}
    if fields.get("layout") not in (None, "auto", "blur", "split"):
        raise ValueError("layout must be 'auto', 'blur' or 'split'")
    if fields.get("facecam_rect"):
        parse_rect(fields["facecam_rect"])   # ValueError if malformed
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if sets:
        cols = ", ".join(f"{k} = ?" for k in sets)
        db.execute(f"UPDATE channels SET {cols} WHERE channel_id = ?",
                   (*[int(v) if isinstance(v, bool) else v for v in sets.values()], channel_id))
    return get_channel(db, channel_id)


def remove_channel(db: Database, channel_id: str) -> list[dict]:
    """Delete the channel and end its open streams. Returns those sessions so the caller can stop
    their recorder/chat. Sessions themselves are kept (clips reference them); once ended, the normal
    end-of-stream cleanup deletes their footage. Raises LookupError if the channel doesn't exist."""
    if not db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,)).rowcount:
        raise LookupError("channel not found")
    live = [dict(r) for r in db.query("SELECT * FROM live_sessions WHERE channel_id = ? AND status = 'live'",
                                      (channel_id,))]
    db.execute("UPDATE live_sessions SET status = 'ended', ended_at = ? WHERE channel_id = ? AND status = 'live'",
               (utcnow(), channel_id))
    return live
