"""The 9:16 edit: each line's shot cropped to 1080x1920 with a slow push-in, joined by animated transitions with a
whoosh, the voice, optional background music that ducks under the voice, word captions, and the channel logo."""

import subprocess
import sys
from pathlib import Path

from narrator.common import NO_WINDOW, ROOT, ffmpeg_bin

sys.path.insert(0, str(ROOT))
from app.captions.ass import CaptionStyle, Word, build_ass, subtitles_filter  # noqa: E402  (read-only reuse)

W, H, FPS = 1080, 1920, 30
GAP = 0.12                     # pause between lines
T = 0.3                        # transition length
TRANSITIONS = ["slideleft", "zoomin", "slideup", "circleopen", "smoothleft", "wipeleft", "radial", "slidedown"]


def run(args: list[str]) -> None:
    p = subprocess.run([ffmpeg_bin(), "-v", "error", "-y", *args], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr.strip()[-600:]}")


def estimate_words(text: str, start: float, duration: float) -> list[Word]:
    """Kokoro gives no word timings: spread the line's duration over its words by length."""
    tokens = text.split()
    weights = [len(t) + 2 for t in tokens]
    total = sum(weights) or 1
    out, t = [], start
    for tok, w in zip(tokens, weights):
        d = duration * w / total
        out.append(Word(t, t + d, " " + tok))
        t += d
    return out


def line_video(shot: Path, seconds: float, out: Path) -> Path:
    """16:9 footage → 9:16: fill the height (+15%, so the game's own subtitles at the bottom are cut off), take the
    centre (streamer facecams sit at the edges), slow push-in."""
    frames = max(1, int(seconds * FPS))
    big = int(H * 1.15)
    vf = (f"scale=-2:{big},crop={W}:{H}:(iw-{W})/2:(ih-{H})*0.2,setsar=1,"
          f"zoompan=z='min(1+0.06*on/{frames},1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}")
    run(["-stream_loop", "-1", "-i", str(shot), "-t", f"{seconds:.3f}", "-vf", vf, "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)])
    return out


def whoosh(out: Path) -> Path:
    """A transition whoosh made from filtered noise: no sound file, nothing to license."""
    run(["-f", "lavfi", "-i", "anoisesrc=d=0.5:c=pink:a=0.6:r=48000",
         "-af", "highpass=f=250,lowpass=f=5000,afade=t=in:d=0.18:curve=exp,afade=t=out:st=0.18:d=0.32,volume=0.9",
         "-ac", "2", str(out)])
    return out


LOGO_W = 170                   # channel logo width on the 1080-wide frame
LOGO_XY = (40, 150)            # top-left, below the phone's status bar; Shorts puts its buttons on the right
LOGO_ALPHA = 0.9
# Music sits ~10 dB under the voice while it speaks and comes back up between lines. The first version (volume 0.22,
# threshold 0.02, ratio 8) buried it ~20 dB under: inaudible on a phone.
DUCK = "threshold=0.05:ratio=4:attack=20:release=400"


def logo_png(src: Path, work: Path) -> Path:
    """The logo cropped to its visible pixels (the file has a lot of empty transparent canvas) and scaled down."""
    from PIL import Image
    im = Image.open(src).convert("RGBA")
    im = im.crop(im.getchannel("A").getbbox() or (0, 0, *im.size))
    im = im.resize((LOGO_W, round(im.height * LOGO_W / im.width)), Image.LANCZOS)
    out = work / "logo.png"
    im.save(out)
    return out


def assemble(lines: list[str], voices: list[tuple[Path, float]], shots: list[Path], out: Path, work: Path,
             music: Path | None = None, music_volume: float = 0.45, logo: Path | None = None,
             music_start: float = 0.0) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    segs = [dur + GAP for _, dur in voices]
    parts = [line_video(shot, seg + T, work / f"v{i:02d}.mp4") for i, (shot, seg) in enumerate(zip(shots, segs))]

    # video: chain xfade transitions; line i starts at the sum of the earlier segments, like its voice
    inputs, chain, last = [], [], "0:v"
    for p in parts:
        inputs += ["-i", str(p)]
    offset = 0.0
    for i in range(1, len(parts)):
        offset += segs[i - 1]
        label = f"x{i}"
        chain.append(f"[{last}][{i}:v]xfade=transition={TRANSITIONS[(i - 1) % len(TRANSITIONS)]}:duration={T}:"
                     f"offset={offset - T / 2:.3f}[{label}]")
        last = label
    video = work / "video.mp4"
    if chain:
        run([*inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "18", "-pix_fmt", "yuv420p", str(video)])
    else:
        video = parts[0]

    # voice: line WAVs back to back with the same gaps
    voice = work / "voice.wav"
    vin, labels = [], []
    for i, (wav, _) in enumerate(voices):
        vin += ["-i", str(wav)]
        labels.append(f"[{i}:a]aresample=48000,apad=pad_dur={GAP}[a{i}]")
    run([*vin, "-filter_complex", ";".join(labels) + ";" + "".join(f"[a{i}]" for i in range(len(voices)))
         + f"concat=n={len(voices)}:v=0:a=1,aformat=channel_layouts=stereo[out]", "-map", "[out]", str(voice)])

    # sound: voice + a whoosh at every cut + (optional) music ducked under the voice
    total = sum(segs) + T
    sfx = whoosh(work / "whoosh.wav")
    cuts = [sum(segs[:i]) - T / 2 for i in range(1, len(segs))]
    ain = ["-i", str(voice)] + sum((["-i", str(sfx)] for _ in cuts), [])
    graph = [f"[0:a]asplit=2[v][vkey]"]
    mix = ["[v]"]
    for k, c in enumerate(cuts, start=1):
        graph.append(f"[{k}:a]adelay={int(max(0, c - 0.15) * 1000)}:all=1,volume=0.55[w{k}]")
        mix.append(f"[w{k}]")
    if music:
        m = len(cuts) + 1
        ain += ["-stream_loop", "-1", "-ss", f"{music_start:.2f}", "-i", str(music)]   # e.g. start on the beat drop
        graph.append(f"[{m}:a]aresample=48000,aformat=channel_layouts=stereo,volume={music_volume},"
                     f"afade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5[mu]")
        graph.append(f"[mu][vkey]sidechaincompress={DUCK}[duck]")
        mix.append("[duck]")
    else:
        graph.append("[vkey]anullsink")
    graph.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first,"
                 "loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    audio = work / "audio.wav"
    run([*ain, "-filter_complex", ";".join(graph), "-map", "[aout]", "-t", f"{total:.3f}", str(audio)])

    # captions (+ channel logo) burned in, audio muxed
    words, t = [], 0.0
    for line, seg, (_, dur) in zip(lines, segs, voices):
        words += estimate_words(line, t, dur)
        t += seg
    ass = work / "captions.ass"
    ass.write_text(build_ass(words, CaptionStyle(margin_v=760, size=92), None, total), encoding="utf-8")
    graph = f"[0:v]{subtitles_filter(ass).lstrip(',')}[cap]"
    extra = []
    if logo:
        extra = ["-i", str(logo_png(logo, work))]
        graph += (f";[2:v]format=rgba,colorchannelmixer=aa={LOGO_ALPHA}[lg];"
                  f"[cap][lg]overlay={LOGO_XY[0]}:{LOGO_XY[1]}[vout]")
    else:
        graph = graph.replace("[cap]", "[vout]")
    run(["-i", str(video), "-i", str(audio), *extra, "-filter_complex", graph, "-map", "[vout]", "-map", "1:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-shortest",
         "-movflags", "+faststart", str(out)])
    return out
