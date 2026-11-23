"""Facts from several research videos → one ranked list: the same secret told by different videos is merged, and
secrets more videos mention rank first. Input: the extraction step's facts.json {video_id: {"items": [...]}}.
Output: [{"name", "what", "where", "how", "keywords", "sources": [video ids]}], best first."""

import json
import re
import sys
from pathlib import Path

STOP = set("""a an the and or of to in on at for with from by is are be it its this that you your if as up into out
gta gta6 vi 6 game secret secrets easter egg eggs hidden find found get go use can will there their which where how""".split())


def toks(item: dict) -> set[str]:
    text = " ".join([item.get("name", ""), item.get("where", ""), " ".join(item.get("keywords", []))]).lower()
    return {w for w in re.findall(r"[a-z0-9$']+", text) if w not in STOP and len(w) > 2}


def rank(facts: dict) -> list[dict]:
    clusters: list[dict] = []
    for vid, d in facts.items():
        for it in d["items"]:
            t = toks(it)
            best, score = None, 0.0
            for c in clusters:
                j = len(t & c["toks"]) / max(1, min(len(t), len(c["toks"])))
                if j > score:
                    best, score = c, j
            if best is not None and score >= 0.5:
                best["items"].append((vid, it))
                best["toks"] |= t
            else:
                clusters.append({"toks": set(t), "items": [(vid, it)]})
    out = []
    for c in clusters:
        sources = sorted({v for v, _ in c["items"]})
        main = max((it for _, it in c["items"]), key=lambda it: len(it.get("how", "")) + len(it.get("where", "")))
        keywords = sorted({k for _, it in c["items"] for k in it.get("keywords", [])})
        out.append({**{k: main.get(k, "") for k in ("name", "what", "where", "how")}, "keywords": keywords,
                    "sources": sources})
    out.sort(key=lambda f: (-len(f["sources"]), -len(f["how"]) - len(f["where"])))
    return out


if __name__ == "__main__":
    ranked = rank(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    Path(sys.argv[2]).write_text(json.dumps(ranked, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(ranked)} facts, {sum(len(f['sources']) > 1 for f in ranked)} in 2+ videos")
