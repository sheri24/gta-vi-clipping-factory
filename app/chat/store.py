"""Persist chat messages and roll them up into fixed-size time buckets."""

from dataclasses import dataclass

from app.db import Database

KINDS = ("text", "paid", "sticker", "membership", "gift")


@dataclass(frozen=True)
class ChatMessage:
    msg_id: str
    published_us: int          # YouTube clock, microseconds since epoch
    kind: str
    author_id: str | None = None
    author_name: str | None = None
    text: str | None = None


class ChatStore:
    def __init__(self, db: Database, bucket_seconds: int = 10):
        self.db = db
        self.bucket_seconds = bucket_seconds

    def add(self, session_id: int, messages: list[ChatMessage], source: str) -> int:
        """Insert messages; duplicates (same msg_id in this session) are ignored. Returns new rows."""
        if not messages:
            return 0
        with self.db.lock:
            before = self.db.conn.total_changes
            self.db.conn.executemany(
                """INSERT OR IGNORE INTO chat_messages
                       (session_id, msg_id, published_us, kind, author_id, author_name, text, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(session_id, m.msg_id, m.published_us, m.kind, m.author_id, m.author_name, m.text, source)
                 for m in messages],
            )
            return self.db.conn.total_changes - before

    def aggregate(self, session_id: int, lookback_seconds: int = 120) -> int:
        """Recompute recent buckets. Window is measured from the newest message (YouTube clock),
        so late-arriving messages still land in the right bucket and the laptop clock is unused."""
        newest = self.db.one("SELECT MAX(published_us) m FROM chat_messages WHERE session_id = ?",
                             (session_id,))["m"]
        if newest is None:
            return 0
        width_us = self.bucket_seconds * 1_000_000
        since = (newest - lookback_seconds * 1_000_000) // width_us * width_us
        cur = self.db.execute(
            f"""INSERT INTO chat_buckets
                    (session_id, bucket_start, messages, unique_authors, paid, memberships, gifts)
                SELECT session_id, (published_us / {width_us}) * {self.bucket_seconds},
                       SUM(kind IN ('text', 'paid', 'sticker')),
                       COUNT(DISTINCT CASE WHEN kind IN ('text', 'paid', 'sticker') THEN author_id END),
                       SUM(kind IN ('paid', 'sticker')),
                       SUM(kind = 'membership'),
                       SUM(kind = 'gift')
                  FROM chat_messages
                 WHERE session_id = ? AND published_us >= ?
                 GROUP BY 1, 2
                ON CONFLICT (session_id, bucket_start) DO UPDATE SET
                    messages = excluded.messages, unique_authors = excluded.unique_authors,
                    paid = excluded.paid, memberships = excluded.memberships, gifts = excluded.gifts""",
            (session_id, since),
        )
        return cur.rowcount

    def buckets(self, session_id: int, limit: int = 360) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM chat_buckets WHERE session_id = ? ORDER BY bucket_start DESC LIMIT ?",
            (session_id, limit))
        return [dict(r) for r in reversed(rows)]

    def delete_session_messages(self, session_id: int) -> int:
        """Raw messages go with the footage; buckets (tiny) are kept for history."""
        return self.db.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,)).rowcount
