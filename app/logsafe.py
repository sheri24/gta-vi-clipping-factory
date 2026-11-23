"""Keep secrets out of logs. httpx logs every request URL at INFO, and Data API URLs carry ?key=…"""

import logging
import re
from pathlib import Path

_PATTERNS = [
    (re.compile(r"([?&](?:key|access_token|refresh_token|client_secret)=)[^&\s\"']+"), r"\1REDACTED"),
    (re.compile(r"(\"(?:refresh_token|access_token|client_secret|token)\"\s*:\s*\")[^\"]+"), r"\1REDACTED"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "REDACTED_API_KEY"),
    (re.compile(r"\bbot\d{6,}:[0-9A-Za-z_\-]{30,}"), "botREDACTED"),          # Telegram bot token in URLs
    (re.compile(r"\bya29\.[0-9A-Za-z_\-.]+"), "REDACTED_OAUTH_TOKEN"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


def tail(path: Path, lines: int = 200, chunk: int = 64 * 1024) -> list[str]:
    """Last `lines` lines of a (possibly large) log, redacted, without reading the whole file."""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(chunk, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
    text = data.decode("utf-8", errors="replace")
    return [redact(line) for line in text.splitlines()[-lines:]]


def scrub_file(path: Path) -> int:
    """Redact secrets already written to an old log. Returns how many lines changed."""
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    cleaned = [redact(line) for line in lines]
    changed = sum(a != b for a, b in zip(lines, cleaned))
    if changed:
        path.write_text("".join(cleaned), encoding="utf-8")
    return changed
