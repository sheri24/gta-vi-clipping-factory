import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.db import utcnow
from app.logsafe import redact
from app.notify.notifier import Notifier
from app.notify.telegram import Telegram, TelegramError

TOKEN = "123456789:AAH" + "x" * 32


class FakeSender:
    enabled = True

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, text, buttons=None):
        if self.fail:
            raise TelegramError("Telegram sendMessage: Unauthorized")
        self.sent.append((text, buttons or []))


def settings(tmp_path):
    return SimpleNamespace(youtube_token_path=tmp_path / "token.json", recordings_dir=tmp_path, min_free_gb=0)


def world(db, permission=1):
    db.execute("""INSERT INTO channels (channel_id, title, source_url, created_at, record_enabled, permission_confirmed)
                  VALUES ('UC1', 'IShowSpeed', 'u', ?, 1, ?)""", (utcnow(), permission))
    db.execute("""INSERT INTO live_sessions (id, video_id, channel_id, title, status, detected_at)
                  VALUES (1, 'live1', 'UC1', 'GTA 6 <day 3>', 'live', ?)""", (utcnow(),))
    db.execute("""INSERT INTO clips (id, session_id, window_start_pts, window_end_pts, window_start_utc, window_end_utc,
                                     status, score, created_at, updated_at) VALUES (1, 1, 0, 40, 0, 40, 'approved', 7.4, ?, ?)""",
               (utcnow(), utcnow()))
    db.execute("""INSERT INTO evaluations (clip_id, signals, objective_score, llm_result, final_score, decision, reasons,
                                           created_at) VALUES (1, '{}', 6, ?, 7.4, 'approved', '[]', ?)""",
               (json.dumps({"summary": "He crashes into the cops."}), utcnow()))


def test_live_and_approved_alerts_sent_once(db, tmp_path):
    world(db, permission=0)
    sender = FakeSender()
    n = Notifier(db, sender, settings(tmp_path))
    assert n.run() == 2
    live, clip = sender.sent
    assert "IShowSpeed is live" in live[0] and "&lt;day 3&gt;" in live[0]          # HTML-escaped
    assert "score 7.4" in clip[0] and "Upload allowed" in clip[0]                  # says why it won't upload
    assert Notifier(db, sender, settings(tmp_path)).run() == 0                     # restart: no repeats


def test_private_upload_alert_has_studio_button(db, tmp_path):
    world(db)
    db.execute("""INSERT INTO publish_queue (clip_id, status, title, privacy, remote_id, remote_url, queued_at,
                                             published_at, updated_at)
                  VALUES (1, 'published', 'Speed vs the cops', 'public', 'abc', 'https://youtube.com/shorts/abc', ?, ?, ?)""",
               (utcnow(), utcnow(), utcnow()))
    db.execute("UPDATE publish_queue SET privacy_actual = 'private', privacy_checked_at = ?", (utcnow(),))
    sender = FakeSender()
    Notifier(db, sender, settings(tmp_path), notify_live=False).run()
    texts = [t for t, _ in sender.sent]
    uploaded = next(m for m in sender.sent if "Uploaded as PRIVATE" in m[0])
    assert ("Open in Studio", "https://studio.youtube.com/video/abc/edit") in uploaded[1]
    assert any("locked an upload to private" in t for t in texts)
    assert not any("is live" in t for t in texts)


def test_old_events_are_not_replayed_on_first_start(db, tmp_path):
    world(db)
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    db.execute("UPDATE live_sessions SET detected_at = ?", (long_ago,))
    db.execute("UPDATE evaluations SET created_at = ?", (long_ago,))
    assert Notifier(db, FakeSender(), settings(tmp_path)).run() == 0


def test_telegram_failure_pauses_and_retries_later(db, tmp_path):
    world(db)
    n = Notifier(db, FakeSender(fail=True), settings(tmp_path))
    assert n.run() == 0
    assert db.one("SELECT COUNT(*) c FROM notifications")["c"] == 0                # nothing marked as sent
    n.sender, n._paused_until = FakeSender(), 0                                    # token fixed, pause over
    assert n.run() == 2


def test_signin_expiry_alert(db, tmp_path):
    from app.publish.youtube import record_sign_in, sign_in_info_path
    s = settings(tmp_path)
    s.youtube_token_path.write_text("{}")
    record_sign_in(s.youtube_token_path, "Hidden GTA", "UCx")
    info = json.loads(sign_in_info_path(s.youtube_token_path).read_text())
    info["signed_in_at"] = (datetime.now(timezone.utc) - timedelta(days=6, hours=12)).isoformat()
    sign_in_info_path(s.youtube_token_path).write_text(json.dumps(info))
    sender = FakeSender()
    Notifier(db, sender, s).run()
    assert any("sign-in expires" in t for t, _ in sender.sent)


def test_telegram_client_formats_requests_and_hides_token():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"message": {"chat": {"id": 42, "first_name": "Ahmer"}}}]})
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    bot = Telegram(TOKEN, None, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert bot.find_chat() == ("42", "Ahmer")
    bot.chat_id = "42"
    bot.send("<b>hi</b>", [("Open", "https://x")])
    body = json.loads(calls[-1].content)
    assert body["parse_mode"] == "HTML" and body["reply_markup"]["inline_keyboard"] == [[{"text": "Open", "url": "https://x"}]]
    with pytest.raises(TelegramError) as e:
        bot._call("sendVideo")
    assert TOKEN not in str(e.value)
    assert TOKEN not in redact(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage")


def test_ntfy_sends_plain_text_with_buttons_and_priority():
    from app.notify.ntfy import Ntfy
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    n = Ntfy("clipfactory-secret", http=httpx.Client(transport=httpx.MockTransport(handler)))
    n.send("⚠️ <b>Uploaded as PRIVATE</b>, nobody can see it\n<b>Speed &amp; the cops</b>",
           [("Open in Studio", "https://studio.youtube.com/video/abc/edit"), ("Watch", "https://youtube.com/shorts/abc")])
    b = bodies[0]
    assert b["topic"] == "clipfactory-secret" and b["priority"] == 4
    assert b["title"] == "⚠️ Uploaded as PRIVATE, nobody can see it" and b["message"] == "Speed & the cops"
    assert b["click"] == "https://studio.youtube.com/video/abc/edit"
    assert b["actions"][0] == {"action": "view", "label": "Open in Studio",
                               "url": "https://studio.youtube.com/video/abc/edit", "clear": True}
    n.send("🔴 IShowSpeed is live\nday 3")
    assert bodies[1]["priority"] == 3 and "actions" not in bodies[1]


def test_alert_counts_as_sent_if_any_channel_delivers(db, tmp_path):
    from app.notify.notifier import Senders
    world(db)
    blocked, ok = FakeSender(fail=True), FakeSender()
    assert Notifier(db, Senders([blocked, ok]), settings(tmp_path)).run() == 2
    assert len(ok.sent) == 2
    assert Notifier(db, Senders([FakeSender(fail=True)]), settings(tmp_path)).run() == 0   # already sent: no retry
