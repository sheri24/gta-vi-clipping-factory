"""B-roll for each script line: GTA 6 footage that shows what the line says.

Where a shot comes from, in order:
  1. a specific fact ("Michael's boat in Jason's yard"): the moment a research video shows it, found through that
     video's transcript timestamps, then checked by CLIP
  2. our own streamers' past streams (channels with upload permission in the clip factory's database)
  3. YouTube search results for the line's visual: long GTA 6 gameplay videos only; never Shorts, never videos
     about other GTA games or comparisons
Frames that look more like an older GTA game, a title card or a comparison screen than like the line are skipped.
At most one shot per source video in a Short, always cropped, narrated over and captioned: transformed.
"""

import json
import re
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from narrator.common import CACHE, DATA, NO_WINDOW, ROOT, download_section, ffmpeg_bin, ytdlp
from narrator.visual_index import embed_images, embed_text, load_index, search

OWN_BONUS = 0.01          # prefer our own streamers when a match is about as good
FACT_BONUS = 0.03         # prefer the exact moment a research video shows the fact
NEGATIVE_VISUALS = ["old low-poly GTA San Andreas graphics", "GTA 5 gameplay from 2013",
                    "a YouTube title card with big text", "a split screen comparison of two video games",
                    "a person talking to a webcam"]
OTHER_GAMES = re.compile(r"san andreas|gta\s*(v|5|iv|4|3|iii)\b|gtav|vice city stories|liberty city|red dead|rdr|"
                         r"\bvs\.?\b|versus|comparison|compared|remaster|trilogy|mod\b|mods\b", re.I)
RESEARCH = DATA / "research"   # transcripts of the research videos (Whisper words.json or YouTube .vtt)


@dataclass
class Shot:
    query: str
    video_id: str
    start: float           # seconds into the source video
    duration: float
    score: float
    source: str            # fact | own | youtube
    path: str


def own_videos() -> list[str]:
    """Past streams from channels whose creators allowed uploads (read-only look at the clip factory's DB)."""
    db = sqlite3.connect(f"file:{ROOT / 'data' / 'clipper.db'}?mode=ro", uri=True)
    rows = db.execute("""SELECT DISTINCT j.video_id FROM vod_jobs j JOIN channels c ON c.channel_id = j.channel_id
                          WHERE c.permission_confirmed = 1 AND j.status IN ('done', 'clipping')""").fetchall()
    return [r[0] for r in rows]


def youtube_candidates(query: str, n: int = 6) -> list[str]:
    """Long GTA 6 gameplay videos for a visual (free yt-dlp search, no API quota), minus other games/comparisons."""
    path = CACHE / "search" / (re.sub(r"[^a-z0-9]+", "_", query.lower())[:80] + ".v2.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    p = ytdlp(f"ytsearch{n * 3}:GTA 6 {query} gameplay", "--flat-playlist", "--print", "%(id)s|%(duration)s|%(title)s",
              timeout=120)
    ids = []
    for line in p.stdout.splitlines():
        vid, dur, title = (line.split("|", 2) + ["", ""])[:3]
        if dur in ("NA", "None", "") or not 120 <= float(dur) <= 4 * 3600:   # no Shorts, no 10 h streams
            continue
        if OTHER_GAMES.search(title):
            continue
        ids.append(vid)
    ids = ids[:n]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids), encoding="utf-8")
    return ids


# -- the exact moment a research video shows a fact ---------------------------------------------------------

def transcript_words(video_id: str) -> list[tuple[float, str]]:
    """(seconds, word) for a research video: Whisper's words.json, or YouTube auto-caption cues."""
    wj = RESEARCH / f"{video_id}.words.json"
    if wj.exists():
        return [(w["t"], w["w"].strip().lower()) for w in json.loads(wj.read_text(encoding="utf-8"))]
    vtt = next(RESEARCH.glob(f"{video_id}*.vtt"), None)
    if not vtt:
        return []
    out, t = [], 0.0
    for line in vtt.read_text(encoding="utf-8").splitlines():
        m = re.match(r"(\d+):(\d+):(\d+\.\d+) -->", line)
        if m:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        elif line.strip() and "-->" not in line and not line.startswith(("WEBVTT", "Kind:", "Language:")):
            out += [(t, w.lower()) for w in re.findall(r"[A-Za-z0-9']+", re.sub(r"<[^>]+>", "", line))]
    return out


def fact_moments(video_id: str, keywords: list[str], window: float = 20.0) -> list[float]:
    """Where in the video its narrator talks about these keywords most densely (the footage there shows it)."""
    words = transcript_words(video_id)
    keys = {w for k in keywords for w in re.findall(r"[a-z0-9']+", k.lower()) if len(w) > 3}
    hits = [t for t, w in words if w.strip(".,!?") in keys]
    if not hits:
        return []
    best = max(hits, key=lambda t: sum(1 for h in hits if t <= h <= t + window))
    return [best]


# -- footage helpers ---------------------------------------------------------------------------------------

def frames(video: Path, fps: float = 2.0) -> list[Image.Image]:
    w, h = 384, 216
    raw = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", str(video), "-vf", f"fps={fps},scale={w}:{h}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, creationflags=NO_WINDOW).stdout
    size = w * h * 3
    return [Image.frombytes("RGB", (w, h), raw[i:i + size]) for i in range(0, len(raw) - size + 1, size)]


def refine(video_id: str, t: float, q: np.ndarray, neg: np.ndarray, length: float, out_dir: Path,
           before: float = 4.0, after: float = 8.0) -> tuple[float, float, Path, float]:
    """Download a window around a candidate moment; pick the best `length` seconds in it by CLIP
    (frames that look like a negative visual count as 0). Returns (start in source, score, wide file, wide start)."""
    lo = max(0.0, t - before)
    wide = download_section(video_id, lo, lo + before + length + after, out_dir / f"{video_id}_{int(lo)}_wide.mp4")
    fr = frames(wide)
    if len(fr) < 4:
        return lo, 0.0, wide, lo
    e = embed_images(fr)
    s = e @ q
    s = np.where((e @ neg.T).max(axis=1) >= s, 0.0, s)
    win = max(1, int(length * 2))
    best = max(range(0, max(1, len(s) - win + 1)), key=lambda i: float(s[i:i + win].mean()))
    return lo + best / 2, float(s[best:best + win].mean()), wide, lo


def cut(src: Path, offset: float, length: float, out: Path) -> Path:
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-ss", f"{offset:.2f}", "-i", str(src), "-t", f"{length:.2f}",
                    "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out)], check=True,
                   creationflags=NO_WINDOW)
    return out


def find_shots(lines: list[dict], lengths: list[float], use_youtube: bool = True, log=print) -> list[Shot]:
    """lines: [{"show": visual, "fact": {"keywords": [...], "sources": [video ids]} or None}], one shot per line,
    each as long as its spoken line (so footage never loops)."""
    out_dir = CACHE / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    http = httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    own = own_videos()
    neg = embed_text(NEGATIVE_VISUALS)
    used: set[str] = set()
    shots: list[Shot] = []
    for qi, (line, length) in enumerate(zip(lines, lengths)):
        query = line["show"]
        q = embed_text([f"a screenshot of GTA 6 gameplay showing {query}"])[0]
        best = None                                      # (score, video, start, source, wide, wide_start)
        fact = line.get("fact")
        for vid in (fact or {}).get("sources", []):
            if vid in used:
                continue
            for t in fact_moments(vid, fact.get("keywords", [])):
                start, score, wide, lo = refine(vid, t, q, neg, length, out_dir, before=2, after=14)
                score += FACT_BONUS
                if best is None or score > best[0]:
                    if best:
                        best[4].unlink(missing_ok=True)
                    best = (score, vid, start, "fact", wide, lo)
                else:
                    wide.unlink(missing_ok=True)
        cands: list[tuple[float, str, float, str]] = []
        for source, vids in (("own", own), ("youtube", youtube_candidates(query) if use_youtube else [])):
            for vid in vids:
                if vid in used:
                    continue
                try:
                    idx = load_index(vid, http)
                except Exception as e:
                    log(f"   index {vid} failed: {e}")
                    continue
                if idx is None:
                    continue
                for t, s in search(idx, q, top=1, negatives=neg):
                    cands.append((s + (OWN_BONUS if source == "own" else 0), vid, t, source))
        for score, vid, t, source in sorted(cands, reverse=True)[:2]:     # verify the top 2 on real footage
            if best is not None and score <= best[0] - 0.02:
                break
            start, fscore, wide, lo = refine(vid, t, q, neg, length, out_dir)
            fscore += OWN_BONUS if source == "own" else 0
            if best is None or fscore > best[0]:
                if best:
                    best[4].unlink(missing_ok=True)
                best = (fscore, vid, start, source, wide, lo)
            else:
                wide.unlink(missing_ok=True)
        if best is None:
            log(f"[{qi}] {query}: no footage found")
            continue
        score, vid, start, source, wide, wide_start = best
        shot = cut(wide, start - wide_start, length, out_dir / f"shot{qi:02d}_{vid}_{int(start)}.mp4")
        wide.unlink(missing_ok=True)
        used.add(vid)
        shots.append(Shot(query, vid, round(start, 1), length, round(score, 3), source, str(shot)))
        log(f"[{qi}] {query[:70]}: {source} {vid} @ {start:.0f}s (score {score:.3f})")
    http.close()
    (CACHE / "last_shots.json").write_text(json.dumps([asdict(s) for s in shots], indent=1), encoding="utf-8")
    return shots
