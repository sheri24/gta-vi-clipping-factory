"""Nudity check: no Short with sexual nudity ever reaches YouTube (it's removed, and strikes hit the channel).

GTA 6 has nude story scenes. The AI review sees only 6 still frames, so a few seconds of nudity at the end of a clip
slipped through once (a TGG cutscene, caught by the owner before upload). This scans the clip's whole window, two
frames a second, with NudeNet (exposed genitals, buttocks, breasts, anus), on the full frame and on four
overlapping quarters: at 320 px a person in a wide shot is too small for the model, a quarter makes them 2.8x bigger.

NudeNet: https://github.com/notAI-tech/nudenet. Its package metadata says MIT but the license files it ships are
AGPL-3.0; the owner chose it knowing that (see CLAUDE.md). Runs offline (the 10 MB model is inside the package).
"""

import logging
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from app.recording.media import NO_WINDOW

log = logging.getLogger(__name__)

SEXUAL = {"MALE_GENITALIA_EXPOSED": "exposed genitals", "FEMALE_GENITALIA_EXPOSED": "exposed genitals",
          "FEMALE_BREAST_EXPOSED": "exposed breasts", "BUTTOCKS_EXPOSED": "exposed buttocks",
          "ANUS_EXPOSED": "exposed anus"}
W, H = 1280, 720          # frames are scaled to this before splitting into views


@dataclass(frozen=True)
class NudityHit:
    t: float              # seconds from the clip start
    label: str
    score: float
    view: str             # full | tl | tr | bl | br

    def describe(self) -> str:
        return f"{SEXUAL[self.label]} at 0:{int(self.t):02d} ({self.score:.2f})"


def views(frame: np.ndarray):
    """The full frame plus four 60% quarters that overlap by 20%, so nothing sits only on a seam."""
    h, w = frame.shape[:2]
    yield "full", frame
    th, tw = int(h * 0.6), int(w * 0.6)
    for name, y, x in (("tl", 0, 0), ("tr", 0, w - tw), ("bl", h - th, 0), ("br", h - th, w - tw)):
        yield name, frame[y:y + th, x:x + tw]


class NudityScanner:
    """Measured on 30 real clips: the one with a nude cutscene had 11 frames scoring >= 0.4 (max 0.66); the false
    alarms (a shoulder, a tattooed arm, a cat thumbnail, a face on a pink pillow) had exactly ONE frame each
    (max 0.52). So a clip is nude when >= `min_frames` frames (1.5 s) hit, or any single hit is >= `sure_score`."""

    def __init__(self, threshold: float = 0.4, fps: float = 2.0, ffmpeg_bin: str = "ffmpeg", detector=None,
                 min_frames: int = 3, sure_score: float = 0.75):
        self.threshold = threshold
        self.min_frames = min_frames
        self.sure_score = sure_score
        self.fps = fps
        self.ffmpeg_bin = ffmpeg_bin
        self._detector = detector          # injectable for tests; NudeNet is loaded on first use

    @property
    def detector(self):
        if self._detector is None:
            from nudenet import NudeDetector
            self._detector = NudeDetector()
        return self._detector

    def frames(self, video: Path, offset: float, duration: float):
        """(seconds into the window, BGR frame) at `fps`, straight from ffmpeg: no temp files."""
        p = subprocess.run(
            [self.ffmpeg_bin, "-v", "error", "-ss", f"{max(0.0, offset):.3f}", "-t", f"{duration:.3f}", "-i", str(video),
             "-vf", f"fps={self.fps},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            capture_output=True, timeout=600, creationflags=NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError(f"couldn't read frames for the nudity check: {p.stderr.decode(errors='replace')[-200:]}")
        size = W * H * 3
        for i in range(len(p.stdout) // size):
            yield i / self.fps, np.frombuffer(p.stdout[i * size:(i + 1) * size], np.uint8).reshape(H, W, 3)

    def scan(self, video: Path, offset: float, duration: float) -> list[NudityHit]:
        hits = []
        for t, frame in self.frames(video, offset, duration):
            for name, view in views(frame):
                for d in self.detector.detect(np.ascontiguousarray(view)):
                    if d["class"] in SEXUAL and d["score"] >= self.threshold:
                        hits.append(NudityHit(round(t, 1), d["class"], round(float(d["score"]), 2), name))
        return hits

    def is_nude(self, hits: list[NudityHit]) -> bool:
        """Sustained (several frames) or certain (one very confident hit): single-frame blips are false alarms."""
        return len({h.t for h in hits}) >= self.min_frames or any(h.score >= self.sure_score for h in hits)


def summarize(hits: list[NudityHit]) -> str:
    worst = max(hits, key=lambda h: h.score)
    frames = len({h.t for h in hits})
    return f"nudity: {worst.describe()}" + (f", on {frames} frames" if frames > 1 else "")


def hits_json(hits: list[NudityHit]) -> list[dict]:
    return [asdict(h) for h in hits[:20]]
