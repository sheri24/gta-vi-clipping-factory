"""Build Advanced SubStation Alpha (.ass) captions: word-highlight karaoke + channel credit.

libass (inside FFmpeg's `subtitles` filter) renders these onto the 1080x1920 frame.
"""

from dataclasses import dataclass
from pathlib import Path

from app.clips.render import OUT_H, OUT_W


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    prob: float | None = None     # whisper word probability (QC uses it as a caption-accuracy proxy)


@dataclass(frozen=True)
class CaptionStyle:
    font: str = "Arial Black"
    size: int = 84
    color: str = "FFFFFF"
    highlight: str = "FFE11A"
    outline: int = 7
    margin_v: int = 470
    words_per_line: int = 3
    uppercase: bool = True


def ass_color(rgb_hex: str) -> str:
    """ASS colours are &HAABBGGRR."""
    r, g, b = rgb_hex[0:2], rgb_hex[2:4], rgb_hex[4:6]
    return f"&H00{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = round(t * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape(text: str) -> str:
    # braces start override blocks; backslash starts escapes; newlines must be \N
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def chunk_words(words: list[Word], per_line: int, max_gap: float = 0.6) -> list[list[Word]]:
    """Group words into caption lines: at most `per_line` words, and break at pauses."""
    chunks: list[list[Word]] = []
    for w in words:
        if not w.text.strip():
            continue
        if chunks and len(chunks[-1]) < per_line and w.start - chunks[-1][-1].end <= max_gap:
            chunks[-1].append(w)
        else:
            chunks.append([w])
    return chunks


def build_ass(words: list[Word], style: CaptionStyle, credit: str | None = None, duration: float = 0.0) -> str:
    hi = ass_color(style.highlight)
    base = ass_color(style.color)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{style.font},{style.size},{base},{base},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
        f"{style.outline},2,2,60,60,{style.margin_v},1",
        f"Style: Credit,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,7,40,40,170,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if credit and duration > 0:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(duration)},Credit,,0,0,0,,{escape(credit)}")
    for chunk in chunk_words(words, style.words_per_line):
        texts = [escape(w.text.strip().upper() if style.uppercase else w.text.strip()) for w in chunk]
        # one event per word: the whole line stays up, the spoken word lights up
        for i, w in enumerate(chunk):
            start = w.start
            end = chunk[i + 1].start if i + 1 < len(chunk) else max(w.end, w.start + 0.25)
            parts = [f"{{\\c{hi}}}{t}{{\\c{base}}}" if j == i else t for j, t in enumerate(texts)]
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{' '.join(parts)}")
    return "\n".join(lines) + "\n"


def subtitles_filter(ass_path: Path) -> str:
    """FFmpeg filter-arg escaping for a Windows path: forward slashes, escape the drive colon, quote."""
    p = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f",subtitles='{p}'"
