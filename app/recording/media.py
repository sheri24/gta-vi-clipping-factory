"""ffprobe helpers."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Don't flash a console window per ffprobe call on Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class ProbeResult:
    start_time: float
    duration: float


def probe(source: Path | bytes, ffprobe_bin: str = "ffprobe") -> ProbeResult:
    """Read container start_time/duration from a file path or raw MPEG-TS bytes."""
    cmd = [ffprobe_bin, "-v", "error", "-show_entries", "format=start_time,duration", "-of", "json"]
    if isinstance(source, bytes):
        out = subprocess.run(cmd + ["-i", "pipe:0"], input=source, capture_output=True,
                             timeout=30, creationflags=NO_WINDOW)
    else:
        out = subprocess.run(cmd + [str(source)], capture_output=True, timeout=30,
                             creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.decode(errors='replace')[:300]}")
    fmt = json.loads(out.stdout).get("format", {})
    if "start_time" not in fmt:
        raise RuntimeError("ffprobe returned no start_time")
    return ProbeResult(float(fmt["start_time"]), float(fmt.get("duration") or 0.0))
