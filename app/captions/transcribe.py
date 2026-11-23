"""Local speech-to-text with faster-whisper (MIT), CPU int8."""

import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.captions.ass import Word
from app.recording.media import NO_WINDOW

log = logging.getLogger(__name__)


@dataclass
class Transcript:
    language: str | None
    language_prob: float | None
    text: str
    words: list[Word]
    seconds_taken: float


def extract_audio(source: Path, offset: float, duration: float, out_wav: Path, ffmpeg_bin: str = "ffmpeg") -> None:
    """16 kHz mono WAV of exactly the clip window (Whisper's native input format)."""
    # output-side seek: decodes from the start (cheap for audio) but is exact and never misses
    p = subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                        "-ss", f"{offset:.3f}", "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                        str(out_wav)], capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"audio extract failed: {p.stderr.strip()[-300:]}")


class Transcriber:
    """Loads the model lazily, once; the first call downloads it (~460 MB for 'small')."""

    def __init__(self, model_name: str, models_dir: Path, threads: int = 8, hf_token: str = "", beam_size: int = 1):
        self.model_name = model_name
        self.models_dir = models_dir
        self.threads = threads
        self.hf_token = hf_token
        self.beam_size = beam_size   # 1 = greedy; 5 was ~5.7 min per 40 s clip with openai-whisper on this CPU
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel   # heavy import: only when first needed
                if self.hf_token:
                    os.environ.setdefault("HF_TOKEN", self.hf_token)   # read by huggingface_hub downloads
                log.info("loading whisper model %s", self.model_name)
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                           cpu_threads=self.threads, download_root=str(self.models_dir))
        return self._model

    def ensure_loaded(self) -> None:
        self._load()

    def transcribe(self, wav: Path, language: str | None = None) -> Transcript:
        model = self._load()
        t0 = time.monotonic()
        segments, info = model.transcribe(
            str(wav), language=language, word_timestamps=True, beam_size=self.beam_size,
            vad_filter=True,                      # skip music/gameplay noise instead of hallucinating text
            condition_on_previous_text=False,     # short clips: avoids repetition loops
        )
        words: list[Word] = []
        texts: list[str] = []
        for seg in segments:                      # generator: work happens here
            texts.append(seg.text.strip())
            for w in seg.words or []:
                words.append(Word(w.start, w.end, w.word, round(w.probability, 3)))
        return Transcript(info.language, info.language_probability, " ".join(t for t in texts if t),
                          words, time.monotonic() - t0)

    def transcribe_clip(self, source: Path, offset: float, duration: float, language: str | None = None,
                        ffmpeg_bin: str = "ffmpeg") -> Transcript:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            extract_audio(source, offset, duration, wav, ffmpeg_bin)
            return self.transcribe(wav, language)


class OpenAIWhisperTranscriber(Transcriber):
    """Official openai-whisper on PyTorch CPU. Weights download from OpenAI's CDN (not Hugging Face)
    and the library verifies their SHA-256. ~2-3x slower than faster-whisper; no built-in VAD."""

    def _load(self):
        with self._lock:
            if self._model is None:
                import torch
                import whisper   # heavy import: only when first needed
                torch.set_num_threads(self.threads)
                log.info("loading openai-whisper model %s", self.model_name)
                self._model = whisper.load_model(self.model_name, device="cpu", download_root=str(self.models_dir))
        return self._model

    def transcribe(self, wav: Path, language: str | None = None) -> Transcript:
        model = self._load()
        t0 = time.monotonic()
        result = model.transcribe(
            str(wav), language=language, word_timestamps=True, fp16=False,   # fp16 is GPU-only
            condition_on_previous_text=False,
            # greedy unless configured; whisper still falls back to sampling if a segment looks garbled
            beam_size=self.beam_size if self.beam_size > 1 else None,
            best_of=self.beam_size if self.beam_size > 1 else None,
        )
        words: list[Word] = []
        texts: list[str] = []
        for seg in result.get("segments", []):
            # Without VAD, gameplay noise can produce invented text: drop segments Whisper itself
            # thinks are probably not speech.
            if seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -0.8:
                continue
            texts.append(seg["text"].strip())
            for w in seg.get("words", []):
                words.append(Word(w["start"], w["end"], w["word"], round(w.get("probability", 0.0), 3)))
        return Transcript(result.get("language"), None, " ".join(t for t in texts if t), words,
                          time.monotonic() - t0)


def make_transcriber(settings) -> Transcriber:
    cls = OpenAIWhisperTranscriber if settings.whisper_backend == "openai" else Transcriber
    return cls(settings.whisper_model, settings.models_dir, settings.whisper_threads, settings.hf_token,
               settings.whisper_beam_size)
