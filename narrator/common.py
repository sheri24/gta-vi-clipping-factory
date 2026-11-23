"""Paths, tools and small helpers shared by the narrator steps."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "narrator"                 # git-ignored, like the rest of data/
CACHE = DATA / "cache"                            # storyboards, CLIP index, downloaded shots
MODELS = DATA / "models"
VENV_BIN = Path(sys.executable).parent
YTDLP = str(VENV_BIN / "yt-dlp.exe") if (VENV_BIN / "yt-dlp.exe").exists() else "yt-dlp"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

for d in (DATA, CACHE, MODELS):
    d.mkdir(parents=True, exist_ok=True)


def ffmpeg_bin() -> str:
    """The real ffmpeg (WinGet installs a symlink that yt-dlp can't use as --ffmpeg-location)."""
    found = shutil.which("ffmpeg")
    return str(Path(found).resolve()) if found else "ffmpeg"


def ytdlp(*args: str, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run([YTDLP, "--no-update", "--no-warnings", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)


def video_info(video_id: str) -> dict:
    """yt-dlp's JSON for a video, cached (it's what storyboards and durations come from)."""
    path = CACHE / "info" / f"{video_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    p = ytdlp("-J", "--skip-download", f"https://www.youtube.com/watch?v={video_id}", timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"yt-dlp -J {video_id}: {(p.stderr.strip().splitlines() or ['?'])[-1][:200]}")
    d = json.loads(p.stdout)
    keep = {k: d.get(k) for k in ("id", "title", "channel", "duration", "view_count", "upload_date")}
    keep["storyboards"] = [{k: f.get(k) for k in ("format_id", "width", "height", "columns", "rows", "fps", "fragments")}
                           for f in d.get("formats", []) if f.get("format_id", "").startswith("sb")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep), encoding="utf-8")
    return keep


def download_section(video_id: str, start: float, end: float, out: Path, height: int = 720) -> Path:
    """Only [start, end] of a video, via HLS (fast; DASH sections stall)."""
    fmt = (f"bv*[height<={height}][protocol^=m3u8]+ba[protocol^=m3u8]/b[height<={height}][protocol^=m3u8]"
           f"/bv*[height<={height}]+ba/b")
    p = ytdlp("--no-progress", "-q", "-f", fmt, "--download-sections", f"*{max(0, start):.2f}-{end:.2f}",
              "--merge-output-format", "mp4", "--force-overwrites", "--ffmpeg-location", str(Path(ffmpeg_bin()).parent),
              "-o", str(out), f"https://www.youtube.com/watch?v={video_id}", timeout=900)
    if p.returncode != 0 or not out.exists():
        raise RuntimeError(f"section {video_id} {start:.0f}-{end:.0f}: {(p.stderr.strip().splitlines() or ['?'])[-1][:200]}")
    return out


def probe_duration(path: Path) -> float:
    out = subprocess.run([str(Path(ffmpeg_bin()).with_name("ffprobe.exe")), "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout.strip()
    return float(out or 0)
