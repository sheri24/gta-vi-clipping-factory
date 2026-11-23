"""Telegram bot alerts (free). One-time setup:  .venv\\Scripts\\python -m app.notify.telegram

1. In Telegram, message @BotFather → /newbot → copy the token into .env as TELEGRAM_BOT_TOKEN=…
2. Open your new bot in Telegram and send it any message (e.g. "hi").
3. Run the command above: it finds your chat, saves it to data/telegram.json and sends a test alert.
"""

import json
import logging
import sys
from pathlib import Path

import httpx

from app.private_files import write_private

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
CAPTION_MAX = 1024     # Telegram limit for media captions
TEXT_MAX = 4096


class TelegramError(Exception):
    pass


def chat_file(data_dir: Path) -> Path:
    return data_dir / "telegram.json"


def saved_chat_id(data_dir: Path) -> str | None:
    p = chat_file(data_dir)
    return json.loads(p.read_text(encoding="utf-8")).get("chat_id") if p.exists() else None


class Telegram:
    def __init__(self, token: str, chat_id: str | None, http: httpx.Client | None = None):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip() if chat_id else None
        self.http = http or httpx.Client(timeout=60)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, **kwargs) -> dict:
        try:
            r = self.http.post(f"{API}/bot{self.token}/{method}", **kwargs)
            data = r.json()
        except Exception as e:      # never put the URL (it contains the token) in the message
            raise TelegramError(f"Telegram {method} failed: {type(e).__name__}") from None
        if not data.get("ok"):
            raise TelegramError(f"Telegram {method}: {data.get('description', r.status_code)}")
        return data["result"]

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        """HTML-formatted message; buttons are (label, url) pairs shown under it."""
        body = {"chat_id": self.chat_id, "text": text[:TEXT_MAX], "parse_mode": "HTML",
                "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]}
        self._call("sendMessage", json=body)

    def send_video(self, path: Path, caption: str, buttons: list[tuple[str, str]] | None = None) -> None:
        data = {"chat_id": self.chat_id, "caption": caption[:CAPTION_MAX], "parse_mode": "HTML",
                "supports_streaming": "true"}
        if buttons:
            data["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]})
        with open(path, "rb") as f:
            self._call("sendVideo", data=data, files={"video": (path.name, f, "video/mp4")})

    def find_chat(self) -> tuple[str, str]:
        """(chat_id, name) of whoever last messaged the bot."""
        updates = self._call("getUpdates")
        for u in reversed(updates):
            chat = (u.get("message") or u.get("edited_message") or {}).get("chat")
            if chat:
                name = chat.get("username") or chat.get("first_name") or chat.get("title") or "you"
                return str(chat["id"]), name
        raise TelegramError("no messages yet: open your bot in Telegram, send it 'hi', then run this again")


def main() -> int:
    from app.config import get_settings

    s = get_settings()
    if not s.telegram_bot_token:
        print("Add TELEGRAM_BOT_TOKEN=... to .env first (get it from @BotFather → /newbot).")
        return 1
    bot = Telegram(s.telegram_bot_token, None)
    try:
        chat_id, name = bot.find_chat()
        bot.chat_id = chat_id
        bot.send("✅ <b>Clip Factory alerts are on.</b>\nYou'll hear from me when a streamer goes live, "
                 "a clip is approved or uploaded, or something needs you.")
    except TelegramError as e:
        print(e)
        return 1
    write_private(chat_file(s.data_dir), json.dumps({"chat_id": chat_id, "name": name}))
    print(f"Connected to {name}. Test message sent. Restart the app to start sending alerts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
