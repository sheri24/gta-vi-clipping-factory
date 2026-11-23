"""FFmpeg rendering of 9:16 Shorts from a 16:9 source."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.recording.media import NO_WINDOW

OUT_W, OUT_H = 1080, 1920


@dataclass(frozen=True)
class RenderOptions:
    layout: str = "blur"                     # blur | split
    facecam_rect: str | None = None          # "x,y,w,h" fractions, required for split
    encoder: str = "libx264"
    crf: int = 20
    loudness_lufs: float = -14
    ffmpeg_bin: str = "ffmpeg"


def parse_rect(rect: str) -> tuple[float, float, float, float]:
    x, y, w, h = (float(v) for v in rect.split(","))
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x + 1e-9 and 0 < h <= 1 - y + 1e-9):
        raise ValueError(f"facecam_rect out of range: {rect}")
    return x, y, w, h


def video_filter(opts: RenderOptions) -> str:
    """Returns a filter_complex graph from [0:v] to [v]."""
    if opts.layout == "split":
        if not opts.facecam_rect:
            raise ValueError("split layout needs facecam_rect")
        x, y, w, h = parse_rect(opts.facecam_rect)
        top_h = 640                          # facecam band; gameplay gets the remaining 1280 px
        bot_h = OUT_H - top_h
        return (
            f"[0:v]split=2[cam][game];"
            f"[cam]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
            f"scale={OUT_W}:{top_h}:force_original_aspect_ratio=increase,crop={OUT_W}:{top_h},setsar=1[top];"
            # centre crop of the gameplay at the bottom band's aspect ratio (keeps the action, which sits mid-frame)
            f"[game]crop=ih*{OUT_W}/{bot_h}:ih,scale={OUT_W}:{bot_h},setsar=1[bot];"
            f"[top][bot]vstack,format=yuv420p[v]"
        )
    # blur: full frame (nothing cut off) centred over a blurred, zoomed copy.
    # Blur at 1/4 size then upscale: same look, ~16x cheaper than blurring at 1080x1920.
    return (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={OUT_W // 4}:{OUT_H // 4}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W // 4}:{OUT_H // 4},boxblur=8:2,scale={OUT_W}:{OUT_H},setsar=1[bg];"
        f"[fgsrc]scale={OUT_W}:-2,setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )


def encoder_args(opts: RenderOptions) -> list[str]:
    if opts.encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(opts.crf), "-look_ahead", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(opts.crf), "-profile:v", "high"]


COARSE_SEEK_MARGIN = 4.0   # > YouTube's 2 s keyframe interval


def render_vertical(source: Path, out: Path, offset: float, duration: float, opts: RenderOptions,
                    extra_filters: str = "", audio: bool = True) -> None:
    """Trim [offset, offset+duration] (seconds from the start of `source`) and render 9:16.
    extra_filters: appended to the video chain after layout (e.g. subtitles in Phase 7), as ',filter…'.

    Two-step seek: a fast input seek to a few seconds BEFORE the window (MPEG-TS input seeking can land
    past the keyframe it needs and output no video at all), then a frame-exact trim in the filter graph."""
    coarse = max(0.0, offset - COARSE_SEEK_MARGIN)
    fine = offset - coarse
    trim_v = f"[0:v]trim=start={fine:.3f}:duration={duration:.3f},setpts=PTS-STARTPTS[vt];"
    graph = trim_v + video_filter(opts).replace("[0:v]", "[vt]", 1)
    if extra_filters:
        graph = graph.replace("format=yuv420p[v]", f"format=yuv420p{extra_filters}[v]")
    maps = ["-map", "[v]"]
    if audio:
        graph += (f";[0:a:0]atrim=start={fine:.3f}:duration={duration:.3f},asetpts=PTS-STARTPTS,"
                  f"loudnorm=I={opts.loudness_lufs}:TP=-1.5:LRA=11,aresample=48000[a]")
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    cmd = [
        opts.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{coarse:.3f}", "-i", str(source),
        "-filter_complex", graph, *maps, *encoder_args(opts),
        "-movflags", "+faststart", str(out),
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg render failed: {p.stderr.strip()[-600:]}")
