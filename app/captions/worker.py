"""Two jobs around the AI review:
  cut → captioning → transcribed      Whisper only; the AI review needs the words, not a render
  passed → rendering → approved       the 9:16 edit with captions, only for clips the AI passed
"""

import json
import logging
import time
from dataclasses import asdict, replace
from pathlib import Path

from app.captions.ass import CaptionStyle, Word, build_ass, subtitles_filter
from app.clips.render import render_vertical
from app.db import Database, utcnow

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def caption_style(settings) -> CaptionStyle:
    return CaptionStyle(font=settings.caption_font, size=settings.caption_size, color=settings.caption_color,
                        highlight=settings.caption_highlight, outline=settings.caption_outline,
                        margin_v=settings.caption_margin_v, words_per_line=settings.caption_words_per_line,
                        uppercase=settings.caption_uppercase)


class CaptionWorker:
    def __init__(self, db: Database, transcriber, render_opts_for, style: CaptionStyle,
                 credit_enabled: bool = True, ffmpeg_bin: str = "ffmpeg"):
        self.db = db
        self.transcriber = transcriber
        self.render_opts_for = render_opts_for
        self.style = style
        self.credit_enabled = credit_enabled
        self.ffmpeg_bin = ffmpeg_bin
        self.model_backoff_seconds = 600.0
        self._model_retry_at = 0.0
        self.split_margin_v = 1180    # px from bottom: caption baseline just under the 640 px camera band

    def _set(self, clip_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE clips SET {cols} WHERE id = ?", (*fields.values(), clip_id))

    def run_all(self) -> list[int]:
        # live clips first; at most one past-stream clip per run so fresh live moments never wait long
        pending = self.db.query("SELECT * FROM clips WHERE status = 'cut' ORDER BY source_kind = 'vod', id")
        if not pending:
            return []
        # A missing model (e.g. Hugging Face rate-limits the first download) is not the clip's fault:
        # leave clips waiting instead of burning their retry attempts.
        if hasattr(self.transcriber, "ensure_loaded"):
            if time.monotonic() < self._model_retry_at:
                return []
            try:
                self.transcriber.ensure_loaded()
            except Exception as e:
                self._model_retry_at = time.monotonic() + self.model_backoff_seconds
                self.db.log_error("captions.model", f"whisper model unavailable: {str(e)[:150]}")
                return []
        done = []
        for clip in pending:
            if self.process(dict(clip)):
                done.append(clip["id"])
            if clip["source_kind"] == "vod":
                break
        return done

    def render_all(self) -> list[int]:
        """Render the Shorts the AI review passed. Live clips first; one past-stream clip per run."""
        done = []
        for clip in self.db.query("SELECT * FROM clips WHERE status = 'passed' ORDER BY source_kind = 'vod', id"):
            clip = dict(clip)
            self._set(clip["id"], status="rendering", attempts=clip["attempts"] + 1)
            try:
                t = self.db.one("SELECT words FROM transcripts WHERE clip_id = ?", (clip["id"],))
                self.render_final(clip, [Word(**w) for w in json.loads(t["words"])] if t else [])
                self._set(clip["id"], status="approved", attempts=0, error=None)
                log.info("clip %d rendered and approved", clip["id"])
                done.append(clip["id"])
            except Exception as e:
                log.exception("rendering clip %d failed", clip["id"])
                final_try = clip["attempts"] + 1 >= MAX_ATTEMPTS
                self._set(clip["id"], status="failed" if final_try else "passed", error=str(e)[-500:])
                self.db.log_error("captions.render", str(e)[:200], str(clip["id"]))
            if clip["source_kind"] == "vod":
                break
        return done

    def style_for(self, layout: str) -> CaptionStyle:
        # Split screen: the lower part is gameplay, so captions sit on the camera/gameplay seam instead.
        return replace(self.style, margin_v=self.split_margin_v) if layout == "split" else self.style

    def render_final(self, clip: dict, words: list[Word]) -> Path:
        """Captions + layout → final.mp4, from the clip's lossless source.ts. Used for first renders and
        re-renders (new layout/style) without transcribing again."""
        ch = self.db.one("""SELECT c.handle, c.title FROM live_sessions s
                              JOIN channels c ON c.channel_id = s.channel_id WHERE s.id = ?""", (clip["session_id"],))
        source = Path(clip["source_path"])
        offset = clip["window_start_pts"] - clip["source_start_pts"]
        duration = clip["window_end_pts"] - clip["window_start_pts"]
        opts = self.render_opts_for(clip)
        credit = None
        if self.credit_enabled and ch:
            credit = ch["handle"] if ch["handle"] and ch["handle"].startswith("@") else f"@{ch['title'].strip()}"
        ass_path = source.parent / "captions.ass"
        ass_path.write_text(build_ass(words, self.style_for(opts.layout), credit, duration), encoding="utf-8")
        final = source.parent / "final.mp4"
        render_vertical(source, final, offset, duration, opts, extra_filters=subtitles_filter(ass_path))
        self._set(clip["id"], captions_path=str(ass_path), final_path=str(final), layout=opts.layout)
        return final

    def rerender(self, clip_id: int) -> Path:
        clip = dict(self.db.one("SELECT * FROM clips WHERE id = ?", (clip_id,)))
        t = self.db.one("SELECT words FROM transcripts WHERE clip_id = ?", (clip_id,))
        if not clip.get("source_path") or not t:
            raise ValueError("clip has no source or transcript yet")
        words = [Word(**w) for w in json.loads(t["words"])]
        return self.render_final(clip, words)

    def process(self, clip: dict) -> bool:
        cid = clip["id"]
        ch = self.db.one("""SELECT c.handle, c.title, c.language FROM live_sessions s
                              JOIN channels c ON c.channel_id = s.channel_id WHERE s.id = ?""",
                         (clip["session_id"],))
        self._set(cid, status="captioning", attempts=clip["attempts"] + 1)
        try:
            source = Path(clip["source_path"])
            offset = clip["window_start_pts"] - clip["source_start_pts"]
            duration = clip["window_end_pts"] - clip["window_start_pts"]
            tr = self.transcriber.transcribe_clip(source, offset, duration, ch["language"] if ch else None,
                                                  self.ffmpeg_bin)
            self.db.execute(
                """INSERT OR REPLACE INTO transcripts (clip_id, language, language_prob, text, words, model,
                                                       seconds_taken, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, tr.language, tr.language_prob, tr.text, json.dumps([asdict(w) for w in tr.words]),
                 getattr(self.transcriber, "model_name", "?"), tr.seconds_taken, utcnow()))

            self._set(cid, status="transcribed", attempts=0, error=None)
            log.info("clip %d transcribed (%s, %d words, whisper %.1fs)", cid, tr.language, len(tr.words),
                     tr.seconds_taken)
            return True
        except Exception as e:
            log.exception("captioning clip %d failed", cid)
            final_try = clip["attempts"] + 1 >= MAX_ATTEMPTS
            self._set(cid, status="failed" if final_try else "cut", error=str(e)[-500:])
            self.db.log_error("captions", str(e)[:200], str(cid))
            return False
