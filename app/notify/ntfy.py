"""ntfy.sh phone alerts (free, no account). One-time setup:  .venv\\Scripts\\python -m app.notify.ntfy

1. Install the free "ntfy" app on your phone (Play Store / App Store).
2. Run the command above: it creates a private, random topic name, saves it to data/ntfy.json
   and sends a test alert.
3. In the ntfy app tap + and subscribe to that exact topic name. Then restart Clip Factory.

The topic name is the only key: anyone who knows it can read the alerts, so it's long and random.
"""

import json
import re
import secrets
import sys
from pathlib import Path

import httpx

from app.private_files import write_private

SERVER = "https://ntfy.sh"
URGENT = ("⚠️", "🔒", "❌", "🔑", "💾")     # alerts that need you: high priority (sound even on quiet mode)


class NtfyError(Exception):
    pass


def topic_file(data_dir: Path) -> Path:
    return data_dir / "ntfy.json"


def saved_topic(data_dir: Path) -> str | None:
    p = topic_file(data_dir)
    return json.loads(p.read_text(encoding="utf-8")).get("topic") if p.exists() else None


def plain(html: str) -> str:
    """The notifier writes Telegram-style HTML; ntfy shows plain text."""
    text = re.sub(r"<[^>]+>", "", html)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#x27;", "'").replace("&amp;", "&")


class Ntfy:
    def __init__(self, topic: str | None, server: str = SERVER, http: httpx.Client | None = None):
        self.topic = (topic or "").strip()
        self.server = server.rstrip("/")
        self.http = http or httpx.Client(timeout=30)

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        title, _, body = plain(text).partition("\n")
        msg = {"topic": self.topic, "title": title.strip()[:250], "message": body.strip()[:3500] or " ",
               "priority": 4 if title.startswith(URGENT) else 3}
        if buttons:
            msg["click"] = buttons[0][1]
            msg["actions"] = [{"action": "view", "label": label[:40], "url": url, "clear": True}
                              for label, url in buttons[:3]]
        try:
            r = self.http.post(self.server, json=msg)       # JSON publish: emoji-safe, unlike headers
        except Exception as e:
            raise NtfyError(f"ntfy publish failed: {type(e).__name__}") from None
        if r.status_code >= 300:
            raise NtfyError(f"ntfy publish failed: HTTP {r.status_code} {r.text[:120]}")


def main() -> int:
    from app.config import get_settings

    s = get_settings()
    topic = s.ntfy_topic or saved_topic(s.data_dir) or f"clipfactory-{secrets.token_urlsafe(12).replace('_', 'x').replace('-', 'y')}"
    try:
        Ntfy(topic, s.ntfy_server).send("✅ Clip Factory alerts are on\nYou'll hear from me when a streamer goes "
                                        "live, a clip is approved or uploaded, or something needs you.")
    except NtfyError as e:
        print(e)
        return 1
    if not s.ntfy_topic:
        write_private(topic_file(s.data_dir), json.dumps({"topic": topic}))   # the topic is the only key
    print(f"\nYour private topic:  {topic}\n")
    print("In the ntfy app: tap +, enter that topic exactly, Subscribe. (The test alert is already waiting there.)")
    print("Then restart Clip Factory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
