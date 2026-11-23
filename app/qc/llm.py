"""Vision-model judgement via a local Ollama server (no paid APIs)."""

import base64
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.recording.media import NO_WINDOW

CRITERIA = {                 # name: weight in the LLM score
    "excitement": 0.30,
    "reaction": 0.20,
    "gameplay": 0.15,
    "standalone": 0.15,
    "visual_quality": 0.10,
    "clean_start_end": 0.10,
}

SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "integer", "minimum": 0, "maximum": 10} for k in CRITERIA},
        "gta_on_screen": {"type": "boolean"},
        "funny": {"type": "integer", "minimum": 0, "maximum": 10},
        "summary": {"type": "string"},
        "reject": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": [*CRITERIA, "gta_on_screen", "funny", "summary", "reject", "reason"],
}

PROMPT = """You are a strict editor choosing YouTube Shorts from GTA 6 livestreams.
The frames are evenly spaced through one {duration:.0f}-second clip, in order.

Stream: "{stream_title}" by {channel}
{note}What the streamer says (auto transcript, may contain errors): {transcript}
Live chat while this happened (viewers reacting in real time):
{chat}

Score each from 0 to 10:
- excitement: would a stranger scrolling Shorts stop and watch?
- reaction: is there a strong, funny or surprised streamer/chat reaction?
- gameplay: is something interesting/unexpected happening in the game (crash, chase, fail, stunt, glitch)?
- standalone: does it make sense with no other context?
- visual_quality: is the picture clear (not menus, loading screens, black or static)?
- clean_start_end: does it start and end at sensible points rather than mid-action cut-offs?

Also report (for information; they don't decide anything on their own):
gta_on_screen: true if GTA 6 is on screen in most frames: gameplay, cutscenes, the pause menu or map, credits,
  a post-credits scene, a loading screen. False only if the screen mostly shows the streamer's desktop, stream
  tools (giveaway, poll, alerts), a different game, or only the webcam.
funny: 0 to 10, how funny this is to a stranger who doesn't know the streamer (10 = laugh out loud).

summary: one plain sentence saying what happens in the clip.
reject: true if this should never be posted (boring, menus/loading, nothing happens, offensive content).
reason: one short sentence explaining your verdict.
Be honest: most clips are average. Reply with JSON only."""


@dataclass
class LlmVerdict:
    scores: dict[str, int]
    summary: str
    reject: bool
    reason: str
    gta_on_screen: bool = True
    funny: int = 0

    @property
    def score(self) -> float:
        return round(sum(self.scores[k] * w for k, w in CRITERIA.items()), 2)


def sample_frames(source: Path, offset: float, duration: float, n: int, ffmpeg_bin: str = "ffmpeg") -> list[bytes]:
    """n JPEG frames (640 px wide, 16:9) evenly spaced inside the clip window."""
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(n):
            t = offset + duration * (i + 0.5) / n
            out = Path(tmp) / f"f{i}.jpg"
            p = subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0, t - 3):.3f}",
                                "-i", str(source), "-ss", f"{min(3, t):.3f}", "-frames:v", "1", "-vf", "scale=640:-2",
                                "-q:v", "4", str(out)], capture_output=True, timeout=60, creationflags=NO_WINDOW)
            if p.returncode == 0 and out.exists():
                frames.append(out.read_bytes())
    return frames


def format_chat(chat: list[dict], limit: int = 40) -> str:
    if not chat:
        return "(no chat captured)"
    return "\n".join(f"- {m['m'][:80]}" for m in chat[:limit])


class OllamaJudge:
    def __init__(self, url: str, model: str, timeout: float = 600, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.model = model
        self.http = http or httpx.Client(timeout=timeout)

    def available(self) -> bool:
        try:
            tags = self.http.get(f"{self.url}/api/tags", timeout=5).json()
        except Exception:
            return False
        return any(m.get("name") == self.model or m.get("model") == self.model for m in tags.get("models", []))

    def judge(self, frames: list[bytes], *, duration: float, stream_title: str, channel: str,
              transcript: str, chat: list[dict], note: str | None = None) -> LlmVerdict:
        note_line = (f'The channel owner watched this clip and says: "{note.strip()[:500]}". This is correct: trust it '
                     "over your own reading of the frames.\n") if note and note.strip() else ""
        prompt = PROMPT.format(duration=duration, stream_title=stream_title or "?", channel=channel or "?",
                               transcript=(transcript or "(no speech)")[:1200], chat=format_chat(chat),
                               note=note_line)
        r = self.http.post(f"{self.url}/api/chat", json={
            "model": self.model, "stream": False, "format": SCHEMA,
            "options": {"temperature": 0.2},
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(f).decode() for f in frames]}],
        })
        r.raise_for_status()
        data = json.loads(r.json()["message"]["content"])
        scores = {k: max(0, min(10, int(data.get(k, 0)))) for k in CRITERIA}
        return LlmVerdict(scores, str(data.get("summary", "")).strip(), bool(data.get("reject", False)),
                          str(data.get("reason", "")).strip(), bool(data.get("gta_on_screen", True)),
                          max(0, min(10, int(data.get("funny", 0)))))
