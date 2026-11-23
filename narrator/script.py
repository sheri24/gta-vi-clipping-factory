"""Facts → a 30-40 s list-style script, written in small steps so a small local model stays accurate:

  1. rate every fact (surprising + specific), keep the best 5
  2. one spoken line per fact, names/places/numbers kept exactly, in an excited spoken style
  3. check each line against its fact; anything added or changed is rewritten
  4. a hook and a closing question, written from the chosen lines

The first version wrote the whole script in one go and mixed facts up ("complete 91 missions to unlock the cheat
code system"); one small task per call fixed that. Each line keeps its fact number for the footage search."""

import json
from pathlib import Path

import httpx

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "gemma4:e2b"
NAMES = ("Leonida, Vice City, Port Gellhorn, Ambrosia, Kelly County, Grassrivers, Leonida Keys, Mount Kalaga, "
         "Jason, Lucia, Michael, Trevor, Franklin, CJ, Brian, Dexter")
STYLE = ("Sound like a friend telling you something cool: short sentences, natural rhythm, '!' or '...' where you'd "
         "stress or pause. No filler like 'did you know' or 'in this video'. Don't open with an exclamation "
         "(no 'Wait', 'OMG', 'Dude', 'Yo', 'Listen'): start with the thing itself.")
OPENERS = ("wait", "omg", "dude", "yo", "listen", "okay", "ok", "guys", "bro", "whoa", "wow")
SHOW_ECHO = ("concrete scene", "a camera sees", "gameplay scene")


def tidy(say: str) -> str:
    """Drop an exclamation opener the model used anyway ("Wait! ...", "OMG, ...")."""
    words = say.strip().split(maxsplit=1)
    if len(words) == 2 and words[0].strip("!,.…").lower() in OPENERS:
        rest = words[1].lstrip(" ,!.…")
        return rest[:1].upper() + rest[1:]
    return say.strip()


def ask(prompt: str, schema: dict, temperature: float = 0.6, tries: int = 3) -> dict:
    """One small JSON task. Small models now and then return cut-off or malformed JSON: retry, a bit warmer."""
    last = None
    for attempt in range(tries):
        r = httpx.post(OLLAMA, timeout=600, json={
            "model": MODEL, "stream": False, "format": schema,
            "options": {"temperature": temperature + 0.1 * attempt, "num_ctx": 4096, "num_predict": 1200},
            "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        try:
            data = json.loads(r.json()["message"]["content"])
            if all(k in data for k in schema.get("required", [])):
                return data
        except ValueError as e:
            last = e
    raise RuntimeError(f"the model gave no usable answer after {tries} tries ({last})")


def describe(f: dict) -> str:
    return f"{f['name']}: {f['what']} Where: {f['where']}. How: {f['how']}"


RATINGS = Path(__file__).resolve().parents[1] / "data" / "narrator" / "research" / "ratings.json"


def pick_facts(facts: list[dict], n: int = 5) -> list[dict]:
    schema = {"type": "object", "properties": {"surprise": {"type": "integer"}, "specific": {"type": "integer"}},
              "required": ["surprise", "specific"]}
    cache = json.loads(RATINGS.read_text(encoding="utf-8")) if RATINGS.exists() else {}
    scored = []
    for f in facts:
        key = describe(f)
        if key in cache:
            r = cache[key]
        else:
            r = cache[key] = ask(f"""Rate this GTA 6 secret for a Shorts video, 1-10 each:
surprise: would a GTA fan think "no way, I didn't know that"?
specific: does it say clearly what it is AND where to find it? (vague or confusing = low)
Secret: {describe(f)}
Reply with JSON only.""", schema, temperature=0.1)
        bonus = 1.0 if len(f.get("sources", [])) > 1 else 0.0      # several videos mention it
        scored.append((r["surprise"] + r["specific"] + bonus, f))
    RATINGS.parent.mkdir(parents=True, exist_ok=True)
    RATINGS.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:n]]


def fact_line(f: dict) -> dict:
    schema = {"type": "object", "properties": {"say": {"type": "string"}, "show": {"type": "string"}},
              "required": ["say", "show"]}
    line = ask(f"""Turn this GTA 6 secret into ONE spoken line for a viral YouTube Short (12-22 words).
{STYLE}
Say WHERE it is and WHAT you get or see: the payoff must be in the line.
Keep every name, place and number exactly as written. Add nothing the secret doesn't say.
Correct spellings of GTA 6 names: {NAMES}.
Secret: {describe(f)}

Also "show": the gameplay scene to show while it's spoken: a concrete scene a camera sees in GTA 6
(e.g. "a speedboat parked in a suburban front yard"). Never a menu, phone screen or text.
Reply with JSON only.""", schema)
    check = ask(f"""Fact: {describe(f)}
Spoken line: {line['say']}
Is anything wrong with the line: does it claim something the fact doesn't say, change a name, place or number,
or leave out what you get or see (the payoff)? If so, rewrite it: only what the fact says, with the payoff,
12-22 words, same lively style, no exclamation opener. Reply with JSON only.""",
                {"type": "object", "properties": {"ok": {"type": "boolean"}, "fixed": {"type": "string"}},
                 "required": ["ok", "fixed"]}, temperature=0.1)
    if not check["ok"] and check["fixed"].strip():
        line["say"] = check["fixed"].strip()
    line["say"] = tidy(line["say"])
    if any(e in line["show"].lower() for e in SHOW_ECHO) or len(line["show"].split()) < 3:
        line["show"] = f"{f['name']}, {f['where']}"         # the model echoed the instructions: use the fact itself
    return line


def write_script(facts: list[dict], n: int = 5) -> dict:
    chosen = pick_facts(facts, n)
    lines = []
    for f in chosen:
        line = fact_line(f)
        line["fact"] = facts.index(f) + 1
        lines.append(line)
    body = "\n".join(l["say"] for l in lines)
    edges = ask(f"""These are the lines of a GTA 6 secrets Short:
{body}

Write:
- hook: the first line, max 12 words, makes people stop scrolling. It teases the WHOLE list and must not
  mention any single secret (e.g. "Ninety-nine percent of players walked right past these. Did you?",
  "GTA 6 is hiding things most players will never find."). {STYLE}
- question: the last line, max 10 words, asks the viewer something about these secrets.
- title: max 60 characters, no hashtags.
- hook_show: a gameplay scene for the hook (e.g. "a car speeding through neon Vice City at night").
Reply with JSON only.""", {"type": "object", "properties": {
        "hook": {"type": "string"}, "question": {"type": "string"}, "title": {"type": "string"},
        "hook_show": {"type": "string"}}, "required": ["hook", "question", "title", "hook_show"]})
    hook = tidy(edges["hook"])
    hook_words = {w.lower().strip(".,!?") for w in hook.split() if len(w) > 3}
    if any(len(hook_words & {w.lower().strip(".,!?") for w in l["say"].split()}) >= 3 for l in lines):
        hook = "Ninety-nine percent of players walked right past these. Did you?"   # it repeated a fact
    return {"title": edges["title"],
            "lines": [{"say": hook, "show": edges["hook_show"], "fact": 0, "role": "hook"}]
            + [{**l, "role": "fact"} for l in lines]
            + [{"say": edges["question"], "show": "quick cuts of sunny Vice City streets", "fact": 0, "role": "question"}]}
