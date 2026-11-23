"""Voiceover with Kokoro-82M (Apache-2.0 model, MIT runtime), offline on the CPU.
Model files come from the kokoro-onnx GitHub releases (Hugging Face is blocked on this network)."""

from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from narrator.common import MODELS

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {"kokoro-v1.0.int8.onnx": "model", "voices-v1.0.bin": "voices"}
SAMPLE_RATE = 24000
_tts = None


def _ensure_models() -> None:
    for name in FILES:
        path = MODELS / name
        if path.exists() and path.stat().st_size > 1_000_000:
            continue
        tmp = path.with_suffix(".part")
        with httpx.stream("GET", RELEASE + name, follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.replace(path)


def tts():
    global _tts
    if _tts is None:
        _ensure_models()
        from kokoro_onnx import Kokoro
        _tts = Kokoro(str(MODELS / "kokoro-v1.0.int8.onnx"), str(MODELS / "voices-v1.0.bin"))
    return _tts


def trim_silence(samples: np.ndarray, threshold: float = 0.01, pad: int = 1200) -> np.ndarray:
    loud = np.flatnonzero(np.abs(samples) > threshold)
    if not len(loud):
        return samples
    return samples[max(0, loud[0] - pad): loud[-1] + pad]


def speak(lines: list[str], out_dir: Path, voice: str = "am_puck", speed: float | list[float] = 1.0
          ) -> list[tuple[Path, float]]:
    """One WAV per line (so each line's footage can match its length). `speed` can differ per line (a punchier
    hook). Returns (path, seconds)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    speeds = speed if isinstance(speed, list) else [speed] * len(lines)
    result = []
    for i, text in enumerate(lines):
        samples, sr = tts().create(text, voice=voice, speed=speeds[i], lang="en-us" if voice[0] == "a" else "en-gb")
        samples = trim_silence(np.asarray(samples, np.float32))
        path = out_dir / f"line{i:02d}.wav"
        sf.write(path, samples, sr)
        result.append((path, len(samples) / sr))
    return result
