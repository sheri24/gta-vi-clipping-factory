"""Zero-quota live chat via yt-dlp's live_chat "subtitle" downloader.

yt-dlp follows the chat in real time and appends one JSON object per line to
<out>.live_chat.json.part. We tail that file. Unofficial (reads YouTube's web endpoints):
it can break when YouTube changes, which is why the official source exists as fallback.
"""

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

from app.chat.store import ChatMessage, ChatStore
from app.procs import kill_tree
from app.recording.media import NO_WINDOW

log = logging.getLogger(__name__)

RENDERER_KINDS = {
    "liveChatTextMessageRenderer": "text",
    "liveChatPaidMessageRenderer": "paid",
    "liveChatPaidStickerRenderer": "sticker",
    "liveChatMembershipItemRenderer": "membership",
}


def runs_to_text(message: dict | None) -> str:
    out = []
    for run in (message or {}).get("runs", []):
        if "text" in run:
            out.append(run["text"])
        elif "emoji" in run:
            e = run["emoji"]
            out.append((e.get("shortcuts") or [e.get("emojiId", "")])[0])
    return "".join(out)


class LineParser:
    """Stateful: gifts carry no timestamp, so we date them from this run's capture start
    (derived from any timestamped message: t0 = timestampUsec - videoOffsetTimeMsec)."""

    def __init__(self):
        self.run_t0_us: int | None = None

    def parse(self, line: str) -> list[ChatMessage]:
        return self.parse_obj(json.loads(line))

    def parse_obj(self, obj: dict) -> list[ChatMessage]:
        # Live lines carry the offset at the top level; chat-replay lines (past streams) inside the action.
        replay = obj.get("replayChatItemAction", {})
        offset_us = int(obj.get("videoOffsetTimeMsec") or replay.get("videoOffsetTimeMsec") or 0) * 1000
        out: list[ChatMessage] = []
        gifts: list[dict] = []
        for act in obj.get("replayChatItemAction", {}).get("actions", []):
            item = act.get("addChatItemAction", {}).get("item", {})
            for renderer, body in item.items():
                if renderer in RENDERER_KINDS and "timestampUsec" in body:
                    ts = int(body["timestampUsec"])
                    if self.run_t0_us is None and offset_us > 0:
                        self.run_t0_us = ts - offset_us
                    out.append(ChatMessage(
                        msg_id=body["id"], published_us=ts, kind=RENDERER_KINDS[renderer],
                        author_id=body.get("authorExternalChannelId"),
                        author_name=(body.get("authorName") or {}).get("simpleText"),
                        text=runs_to_text(body.get("message") or body.get("headerSubtext")),
                    ))
                elif renderer == "giftMessageViewModel":
                    gifts.append(body)
        # offset 0 = replayed history from before this run started: no way to date those gifts.
        if gifts and self.run_t0_us is not None and offset_us > 0:
            for g in gifts:
                out.append(ChatMessage(
                    msg_id=g["id"], published_us=self.run_t0_us + offset_us, kind="gift",
                    author_name=(g.get("authorName") or {}).get("content", "").strip() or None,
                    text=(g.get("text") or {}).get("content"),
                ))
        return out


class FileTailer:
    """Read complete new lines from a file another process is appending to.
    Re-opens on every read so we never hold a handle (Windows would block yt-dlp's final rename)."""

    def __init__(self, path: Path):
        self.path = path
        self.pos = 0
        self.partial = b""

    def read_lines(self) -> list[str]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                chunk = f.read()
        except FileNotFoundError:
            return []
        self.pos += len(chunk)
        data = self.partial + chunk
        *lines, self.partial = data.split(b"\n")
        return [ln.decode("utf-8", errors="replace") for ln in lines if ln.strip()]


class YtDlpChatSource:
    name = "ytdlp"

    def __init__(self, ytdlp_bin: str, work_dir: Path, poll_seconds: float = 1.0,
                 rotate_seconds: float = 3600, cmd_builder=None):
        self.ytdlp_bin = ytdlp_bin
        self.work_dir = work_dir
        self.poll_seconds = poll_seconds
        self.rotate_seconds = rotate_seconds   # restart hourly so the temp file never grows huge
        self.cmd_builder = cmd_builder or self._cmd

    def _cmd(self, session: dict, out_base: Path) -> list[str]:
        return [self.ytdlp_bin, "--no-update", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                "-o", str(out_base), f"https://www.youtube.com/watch?v={session['video_id']}"]

    def run(self, session: dict, store: ChatStore, stop: threading.Event) -> tuple[str, int, str | None]:
        """Returns (result, messages_added, error). result: stopped | rotated | exited."""
        out_dir = self.work_dir / session["video_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_base = out_dir / f"run{int(time.time())}"
        err_path = out_dir / f"{out_base.name}.err.txt"
        tailer = FileTailer(Path(f"{out_base}.live_chat.json.part"))
        parser = LineParser()
        added = 0
        with open(err_path, "wb") as err:
            proc = subprocess.Popen(self.cmd_builder(session, out_base), stdout=subprocess.DEVNULL,
                                    stderr=err, creationflags=NO_WINDOW)
        started = time.monotonic()
        result = "exited"
        try:
            while True:
                exited = proc.poll() is not None
                lines = tailer.read_lines()
                if exited and not lines:  # yt-dlp may have renamed .part → .json at the end
                    final = Path(f"{out_base}.live_chat.json")
                    if final.exists() and tailer.path != final:
                        tailer.path = final
                        lines = tailer.read_lines()
                batch = []
                for ln in lines:
                    try:
                        batch.extend(parser.parse(ln))
                    except (ValueError, KeyError) as e:
                        log.debug("skipping unparseable chat line: %s", e)
                added += store.add(session["id"], batch, self.name)
                if exited:
                    break
                if stop.wait(self.poll_seconds):
                    result = "stopped"
                    break
                if time.monotonic() - started > self.rotate_seconds:
                    result = "rotated"
                    break
        finally:
            kill_tree(proc)
        error = None
        if result == "exited" and proc.returncode != 0:
            error = err_path.read_text(errors="replace")[-500:] or f"yt-dlp exit {proc.returncode}"
        for f in out_dir.glob(f"{out_base.name}*"):   # messages are in the DB now
            f.unlink(missing_ok=True)
        return result, added, error
