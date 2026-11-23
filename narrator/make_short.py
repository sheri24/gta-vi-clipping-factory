"""Make one narrated Short end to end:

    narrator\\.venv\\Scripts\\python -m narrator.make_short <facts_ranked.json> [--voice am_puck] [--music file.mp3]
        [--music-start 19] [--logo data/narrator/brand/logo.png] [--facts-count 8]

Re-edit a finished Short (new music level, logo, transitions) from its saved voices and shots, in ~2 minutes:

    narrator\\.venv\\Scripts\\python -m narrator.make_short --reedit data/narrator/out/<name>.json --music ... --music-start 19

facts_ranked.json comes from narrator/research.py. Output: data/narrator/out/<name>.mp4 plus <name>.json (script,
shots and source videos, for the description).
"""

import argparse
import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

from narrator.broll import find_shots
from narrator.common import DATA, probe_duration
from narrator.edit import GAP, T, assemble
from narrator.script import write_script
from narrator.voice import speak

sys.stdout.reconfigure(encoding="utf-8")
LOGO = DATA / "brand" / "logo.png"


def main() -> Path:
    ap = argparse.ArgumentParser()
    ap.add_argument("facts", nargs="?")
    ap.add_argument("--facts-count", type=int, default=8)
    ap.add_argument("--voice", default="am_puck")
    ap.add_argument("--speed", type=float, default=1.03, help="facts; the hook is a bit faster, the question a bit slower")
    ap.add_argument("--music", type=Path)
    ap.add_argument("--music-volume", type=float, default=0.45)
    ap.add_argument("--music-start", type=float, default=0.0, help="seconds into the track, e.g. the beat drop")
    ap.add_argument("--logo", type=Path, default=LOGO if LOGO.exists() else None, help="'' for none")
    ap.add_argument("--script", type=Path, help="reuse a script JSON instead of writing a new one")
    ap.add_argument("--reedit", type=Path, help="a finished Short's .json: redo only the edit")
    a = ap.parse_args()
    logo = a.logo if a.logo and str(a.logo) else None
    t0 = time.monotonic()

    if a.reedit:
        meta = json.loads(a.reedit.read_text(encoding="utf-8"))
        name = a.reedit.stem
        work = DATA / "work" / name
        lines = [l["say"].strip() for l in meta["script"]["lines"]]
        wavs = [work / "voice" / f"line{i:02d}.wav" for i in range(len(lines))]
        voices = [(w, probe_duration(w)) for w in wavs]
        out = a.reedit.with_suffix(".mp4")
        assemble(lines, voices, [Path(s["path"]) for s in meta["shots"]], out, work / "edit", music=a.music,
                 music_volume=a.music_volume, logo=logo, music_start=a.music_start)
        print(f"RE-EDITED in {(time.monotonic() - t0) / 60:.1f} min: {out}")
        return out

    if not a.facts:
        ap.error("give a facts file, or --reedit")
    facts = json.loads(Path(a.facts).read_text(encoding="utf-8"))[:a.facts_count]
    script = json.loads(a.script.read_text(encoding="utf-8")) if a.script else write_script(facts)
    name = re.sub(r"[^a-z0-9]+", "_", script["title"].lower()).strip("_")[:50] or "short"
    work = DATA / "work" / name
    work.mkdir(parents=True, exist_ok=True)
    (work / "script.json").write_text(json.dumps(script, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = [l["say"].strip() for l in script["lines"]]
    print(f"SCRIPT ({time.monotonic() - t0:.0f}s): {script['title']}")
    for l in script["lines"]:
        print(f"  [{l['fact']}] SAY: {l['say']}\n      SHOW: {l['show']}")

    speeds = [{"hook": a.speed + 0.09, "question": a.speed - 0.03}.get(l.get("role"), a.speed) for l in script["lines"]]
    voices = speak(lines, work / "voice", voice=a.voice, speed=speeds)
    print(f"VOICE ({a.voice}): {sum(d for _, d in voices):.1f} s over {len(voices)} lines")

    shot_specs = [{"show": l["show"],
                   "fact": ({"keywords": facts[l["fact"] - 1]["keywords"] + [facts[l["fact"] - 1]["name"]],
                             "sources": facts[l["fact"] - 1]["sources"]} if l["fact"] else None)}
                  for l in script["lines"]]
    shots = find_shots(shot_specs, [d + GAP + T + 0.2 for _, d in voices])
    if len(shots) < len(lines):
        raise SystemExit("not enough footage found for every line")

    out = DATA / "out" / f"{name}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    assemble(lines, voices, [Path(s.path) for s in shots], out, work / "edit", music=a.music,
             music_volume=a.music_volume, logo=logo, music_start=a.music_start)
    sources = sorted({(s.source, s.video_id) for s in shots})
    out.with_suffix(".json").write_text(json.dumps({"script": script, "voice": a.voice,
                                                    "shots": [asdict(s) for s in shots],
                                                    "sources": [f"https://youtu.be/{v} ({src})" for src, v in sources]},
                                                   indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"DONE in {(time.monotonic() - t0) / 60:.1f} min: {out}")
    return out


if __name__ == "__main__":
    main()
