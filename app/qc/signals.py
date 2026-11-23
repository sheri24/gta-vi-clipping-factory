"""Objective, model-free measurements of a clip's raw footage (one ffmpeg pass) + scoring."""

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from app.recording.media import NO_WINDOW


@dataclass
class MediaSignals:
    duration: float
    black_seconds: float
    frozen_seconds: float
    silent_seconds: float
    mean_volume_db: float | None

    @property
    def black_frac(self) -> float:
        return self.black_seconds / self.duration if self.duration else 1.0

    @property
    def frozen_frac(self) -> float:
        return self.frozen_seconds / self.duration if self.duration else 1.0

    @property
    def silent_frac(self) -> float:
        return self.silent_seconds / self.duration if self.duration else 1.0


def parse_ffmpeg_detectors(stderr: str, duration: float) -> MediaSignals:
    black = sum(float(m) for m in re.findall(r"black_duration:\s*([\d.]+)", stderr))
    frozen = sum(float(m) for m in re.findall(r"lavfi\.freezedetect\.freeze_duration:\s*([\d.]+)", stderr))
    silent = sum(float(m) for m in re.findall(r"silence_duration:\s*([\d.]+)", stderr))
    # freeze/silence still open at EOF have a start but no duration line
    for kind, total_attr in (("freezedetect.freeze_start", "frozen"), ("silence_start", "silent")):
        starts = re.findall(rf"{kind}:\s*([\d.]+)", stderr)
        ends = re.findall(r"freeze_end" if kind.startswith("freeze") else r"silence_end", stderr)
        if len(starts) > len(ends):
            tail = max(0.0, duration - float(starts[-1]))
            if total_attr == "frozen":
                frozen += tail
            else:
                silent += tail
    vol = re.search(r"mean_volume:\s*(-?[\d.]+) dB", stderr)
    return MediaSignals(duration, min(black, duration), min(frozen, duration), min(silent, duration),
                        float(vol.group(1)) if vol else None)


def measure(video: Path, duration: float, ffmpeg_bin: str = "ffmpeg", offset: float | None = None) -> MediaSignals:
    """Black/frozen/silent analysis of `video`, or of [offset, offset+duration] of it (a clip's raw source)."""
    window = ["-ss", f"{offset:.3f}", "-t", f"{duration:.3f}"] if offset is not None else []
    p = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-nostats", *window, "-i", str(video),
         "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=0.003:d=2",
         "-af", "silencedetect=noise=-45dB:d=1,volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"signal pass failed: {p.stderr.strip()[-300:]}")
    return parse_ffmpeg_detectors(p.stderr, duration)


@dataclass
class Signals:
    media: MediaSignals
    spike_ratio: float
    spike_z: float
    words_per_second: float
    word_confidence: float | None     # mean whisper word probability; proxy for caption accuracy

    def to_dict(self) -> dict:
        d = asdict(self)
        d["media"].update(black_frac=round(self.media.black_frac, 3), frozen_frac=round(self.media.frozen_frac, 3),
                          silent_frac=round(self.media.silent_frac, 3))
        return d


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def hard_rejects(s: Signals) -> list[str]:
    reasons = []
    if s.media.black_frac > 0.30:
        reasons.append(f"black screen {s.media.black_frac:.0%} of the clip")
    if s.media.frozen_frac > 0.50:
        reasons.append(f"frozen video {s.media.frozen_frac:.0%} of the clip")
    if s.media.silent_frac > 0.90 or (s.media.mean_volume_db is not None and s.media.mean_volume_db < -50):
        reasons.append("no usable audio")
    return reasons


def objective_score(s: Signals) -> float:
    """0-10. Mostly 'how hard did chat react', nudged by speech and penalised by dead air."""
    chat = 0.6 * clamp01((s.spike_ratio - 1.6) / (4.0 - 1.6)) + 0.4 * clamp01((s.spike_z - 3.5) / (10 - 3.5))
    speech = clamp01(s.words_per_second / 2.0)                  # a streamer reacting talks ~2 words/s
    score = 10 * (0.70 * chat + 0.30 * speech)
    score -= 4 * clamp01((s.media.silent_frac - 0.4) / 0.5)    # dead air
    score -= 3 * clamp01(s.media.frozen_frac / 0.5)
    if s.word_confidence is not None and s.words_per_second > 0.5 and s.word_confidence < 0.5:
        score -= 1                                              # likely garbled captions
    return round(max(0.0, min(10.0, score)), 2)
