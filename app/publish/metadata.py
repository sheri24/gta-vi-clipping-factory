"""Title / description / tags for a Short.

The local model writes the creative parts from what actually happened (transcript + what chat said).
It proposes several titles in different hook styles; code scores them and keeps the most specific one.
Credit, source link, hashtags and limits come from code, so they can never be dropped or invented.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.db import Database

TITLE_MAX = 100          # YouTube hard limit
TITLE_TARGET = 70        # Shorts titles get cut off on phones beyond ~70
TAGS_MAX_CHARS = 450     # YouTube limit is 500 incl. separators
MAX_HASHTAGS = 5         # more than 15 and YouTube ignores all of them; a few focused ones read better

SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5},
        "hook": {"type": "string"},
        "question": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titles", "hook", "question", "hashtags", "tags"],
}

# House style. Invented examples (not real clips) so the model learns the voice, not the facts.
STYLE_EXAMPLES = [
    "Chat begged him to surrender… he didn't 💀",
    "He trusted Lucia. Big mistake 😭",
    "Speed got 5 stars in under a minute",
    "\"Why is he still following me?!\" 💀",
    "Nobody expected the getaway car to do this",
]

PROMPT = """You write titles for viral YouTube Shorts. The clip is {duration} seconds from {channel}'s GTA 6 livestream.

WHAT HAPPENED (use only these facts):
- Stream title: "{stream_title}"
- {channel} says (auto transcript, may have errors): "{transcript}"
- Chat messages that describe the moment: {said}
- Chat's loudest reactions (message × count): {reactions}
- A vision model's rough description (vague, lowest priority): {summary}
{note}
Write 4 different titles, one per hook style:
1. WHAT HAPPENED: the concrete event, with stakes ("…refused to surrender and got the bad ending").
2. CHAT'S VERDICT: what chat was screaming or begging for.
3. QUOTE: {channel}'s most dramatic words in quotes, plus a short punch.
4. CURIOSITY: makes people need to watch, without lying about what happens.

Title rules: 25-60 characters. Name {channel} or GTA 6 in at least two titles. Be specific: use the real
nouns and verbs from the facts above. At most ONE emoji, at the end. No hashtags. Never use the words
chaotic, epic, insane, crazy, reaction, moment, livestream, compilation. Never invent events that the
facts don't show. No swearing, no numbers about chat. Don't copy these style examples, just match their energy:
{examples}

Also write:
- hook: one sentence (max 110 characters) that sets up the moment for someone scrolling, specific and true.
- question: a short question that makes viewers comment ("Would you have surrendered?").
- hashtags: 2 topical hashtags without the # sign (e.g. GTA6Ending). Not the streamer's name, not "Shorts".
- tags: 6-10 lowercase search phrases people would type to find this.
Reply with JSON only."""

GENERIC = ("chaotic", "epic", "insane", "crazy", "reaction", "moment", "moments", "livestream", "stream",
           "compilation", "during", "you won't believe", "must watch", "omg")
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")
STOP = set("""a an the and or but so to of in on at for with is it its it's this that was were be been am are
i you he she they we me my your his her their our him them us do did does not no yes oh just like get got
what why how who when where all out up down off over then than there here too very really bro man lol lmao
gg w l im i'm dont don't u ur yo ya yeah ok okay""".split())


PROFANE = re.compile(r"\b(f+u+c*k+\w*|fkn|fk|sh+i+t+\w*|bitch\w*|dumb?ass\w*|dumass\w*|jackass\w*|ass(hole)?s?|"
                     r"dick\w*|retard\w*|whore\w*|cunt\w*|nigg\w*|fag\w*)\b", re.I)
HOOK_LABEL = re.compile(r"^\s*(\d[.)]\s*)?(what happened|chat'?s verdict|quote|curiosity|title)\s*[:\-–]\s*", re.I)


def unlabel(title: str) -> str:
    """'CHAT'S VERDICT: Did Lacy…' → 'Did Lacy…' (small models echo the prompt's hook-style names)."""
    return HOOK_LABEL.sub("", title)


def balance_quotes(title: str) -> str:
    if title.count('"') % 2 == 1:
        i = title.find('"')
        title = '"' + title if i > 0 else title[:i] + title[i + 1:]
    return title


@dataclass
class Metadata:
    title: str
    description: str
    tags: list[str]
    source: str          # llm | template
    candidates: list[tuple[str, float]] = field(default_factory=list)   # every title considered, with its score


def clean_text(s: str, limit: int) -> str:
    s = s.replace("<", "").replace(">", "")          # YouTube rejects angle brackets
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 1 and s[0] == s[-1] == '"' and s.count('"') == 2:
        s = s[1:-1].strip()                          # the whole thing was wrapped in quotes
    if len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"
    return s


def clean_tags(tags: list[str], base: list[str], extra: list[str]) -> list[str]:
    out, seen, total = [], set(), 0
    for t in [*tags, *extra, *base]:
        t = re.sub(r"[<>#,]", "", str(t)).strip().lower()
        if not t or t in seen or len(t) > 60 or PROFANE.search(t):
            continue
        cost = len(t) + (2 if " " in t else 0) + 1      # YouTube counts quotes around multi-word tags
        if total + cost > TAGS_MAX_CHARS:
            break
        seen.add(t)
        out.append(t)
        total += cost
    return out


def hashtag(name: str) -> str:
    return "#" + re.sub(r"[^0-9A-Za-z]", "", name)


def source_link(video_id: str, offset_seconds: int | None) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    return f"{url}&t={offset_seconds}s" if offset_seconds and offset_seconds > 0 else url


# -- chat digest ----------------------------------------------------------------------------------

def normalize_chat(text: str) -> str:
    """':face-purple-crying:' codes out, 'Noooooooo' → 'nooo', whitespace squeezed."""
    text = re.sub(r":[a-z0-9_\-]+:", " ", text, flags=re.I)
    text = re.sub(r"(.)\1{2,}", r"\1\1\1", text)
    return re.sub(r"\s+", " ", text).strip()


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOP and len(w) > 2]


def chat_digest(messages: list[str], max_said: int = 15, max_reactions: int = 6) -> tuple[list[str], list[tuple[str, int]]]:
    """(messages that say something, most repeated short reactions with counts).
    Chat is the best free description of a moment: repetition measures intensity, while the longer,
    unique messages carry the facts ("why would you pick swim")."""
    cleaned = [m for m in (normalize_chat(PROFANE.sub("", x)) for x in messages) if m]
    shown: dict[str, str] = {}                      # first spelling seen: keeps CAPS, which carry intensity
    for m in cleaned:
        shown.setdefault(m.lower(), m)
    reactions = Counter(m.lower() for m in cleaned if len(words(m)) <= 1)
    seen, said = set(), []
    for m in sorted((m for m in cleaned if is_informative(m)), key=lambda m: -len(set(words(m)))):
        key = " ".join(sorted(set(words(m))))
        if key not in seen:
            seen.add(key)
            said.append(m[:90])
    return said[:max_said], [(shown[m], n) for m, n in reactions.most_common(max_reactions)]


INTERJECTIONS = {"what", "damn", "dang", "bruh", "nahh", "yooo", "noooo", "wtff", "lmfao", "hahaha", "bozo"}


def is_informative(message: str) -> bool:
    """Says something about the moment: two real words, or one real word that isn't a stretched-out
    shout ('surrender' yes, 'NOOOO' / 'RIPPPP' no)."""
    ws = words(message)
    if len(ws) >= 2:
        return True
    return len(ws) == 1 and len(ws[0]) >= 5 and not re.search(r"(.)\1\1", ws[0]) and ws[0] not in INTERJECTIONS


# -- choosing a title -----------------------------------------------------------------------------

def score_title(title: str, fact_words: set[str], channel: str) -> float:
    """Higher = more likely to be a specific, scroll-stopping, truthful title. Range roughly -10..10."""
    t = title.strip()
    low = t.lower()
    score = 0.0
    n = len(t)
    if n < 15 or n > TITLE_TARGET:
        score -= 6                                  # truncated on phones, or too thin to mean anything
    elif 25 <= n <= 60:
        score += 2
    specific = fact_words & set(words(t))           # real nouns/verbs from transcript + chat
    score += min(4, 1.5 * len(specific))
    score -= 2.5 * sum(1 for g in GENERIC if re.search(rf"\b{re.escape(g)}\b", low))
    if channel.lower() in low or "gta" in low:
        score += 1
    emojis = len(EMOJI.findall(t))
    score += 0.5 if emojis == 1 else -2 * max(0, emojis - 1)
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.5:
        score -= 3                                  # shouting reads as spam
    if "#" in t:
        score -= 3
    if PROFANE.search(t):
        score -= 5                                  # limits reach (advertiser-friendly guidelines)
    if any(low == e.lower() for e in STYLE_EXAMPLES):
        score -= 8                                  # copied the example instead of describing the clip
    return round(score, 2)


def pick_title(candidates: list[str], ctx: dict) -> tuple[str | None, list[tuple[str, float]]]:
    said, _ = chat_digest(ctx.get("chat", []))
    facts = set(words(ctx.get("transcript", ""))) | {w for m in said for w in words(m)}
    channel = ctx["channel"].strip()
    scored, seen = [], set()
    for c in candidates:
        c = balance_quotes(unlabel(clean_text(str(c), TITLE_MAX)))
        if c and c.lower() not in seen:
            seen.add(c.lower())
            scored.append((c, score_title(c, facts, channel)))
    scored.sort(key=lambda x: -x[1])                # stable: ties keep the model's order
    return (scored[0][0] if scored else None), scored


# -- assembling ---------------------------------------------------------------------------------------

def topical_hashtags(raw: list[str], channel: str) -> list[str]:
    out = []
    for h in raw:
        h = hashtag(str(h))
        if 3 <= len(h) <= 26 and h.lower() not in ("#shorts", "#gta6", hashtag(channel).lower()):
            out.append(h)
    return out


def assemble(creative: dict, ctx: dict, base_tags: list[str], source: str) -> Metadata:
    channel, handle = ctx["channel"].strip(), (ctx.get("handle") or "").strip()   # YouTube titles can carry spaces
    candidates = list(creative.get("titles") or []) or [creative.get("title") or ""]
    best, scored = pick_title(candidates, ctx)
    title = clean_text(best or "", TITLE_TARGET) or clean_text(f"{channel} in GTA 6 😱", TITLE_TARGET)
    title = title[:1].upper() + title[1:]
    if "hook" in creative or "question" in creative:
        body = "\n\n".join(p for p in (clean_text(creative.get("hook") or "", 200),
                                       clean_text(creative.get("question") or "", 120)) if p)
    else:
        body = clean_text(creative.get("description") or "", 600)
    credit = (f"🎥 Clip from {channel}'s livestream{f' ({handle})' if handle else ''}. "
              f"Watch the full moment: {source_link(ctx['video_id'], ctx.get('offset_seconds'))}")
    # #GTA6 first: YouTube shows the first hashtags above the title
    lead = dict.fromkeys(["#GTA6", hashtag(channel), *topical_hashtags(creative.get("hashtags") or [], channel)])
    tags_line = " ".join([*list(lead)[:MAX_HASHTAGS - 1], "#Shorts"])
    description = "\n\n".join(p for p in (body, credit, tags_line) if p)
    tags = clean_tags(creative.get("tags") or [], base_tags, [channel.lower()])
    return Metadata(title, description, tags, source, scored)


def template_creative(ctx: dict) -> dict:
    """Used when the model is unavailable or returns garbage: plain but never wrong
    (a clip only exists because chat spiked, so 'chat lost it' is always true)."""
    return {"titles": [f"{ctx['channel']}'s chat lost it at this GTA 6 scene 😭"],
            "hook": f"Chat exploded during {ctx['channel']}'s GTA 6 stream. Watch why.",
            "question": "What would you have done?",
            "hashtags": [], "tags": []}


def clip_context(db: Database, clip_id: int) -> dict:
    row = db.one(
        """SELECT x.window_start_utc, x.window_start_pts, x.window_end_pts, x.chat_sample, x.review_note, s.video_id,
                  s.title stream_title, s.actual_start_at, c.title channel, c.handle, t.text transcript,
                  e.llm_result, k.ratio
             FROM clips x JOIN live_sessions s ON s.id = x.session_id
             JOIN channels c ON c.channel_id = s.channel_id
             LEFT JOIN transcripts t ON t.clip_id = x.id
             LEFT JOIN evaluations e ON e.clip_id = x.id
             LEFT JOIN spikes k ON k.id = x.spike_id
            WHERE x.id = ?""", (clip_id,))
    offset = None
    if row["actual_start_at"]:
        start = datetime.fromisoformat(row["actual_start_at"].replace("Z", "+00:00")).timestamp()
        offset = max(0, int(row["window_start_utc"] - start))
    llm = json.loads(row["llm_result"]) if row["llm_result"] else {}
    return {"channel": row["channel"].strip(), "handle": row["handle"], "video_id": row["video_id"],
            "stream_title": row["stream_title"] or "", "transcript": row["transcript"] or "",
            "chat": [m["m"] for m in json.loads(row["chat_sample"] or "[]")], "summary": llm.get("summary", ""),
            "offset_seconds": offset, "ratio": row["ratio"], "note": row["review_note"] or "",
            "duration": round(row["window_end_pts"] - row["window_start_pts"])}


def build_prompt(ctx: dict) -> str:
    said, reactions = chat_digest(ctx["chat"])
    return PROMPT.format(
        channel=ctx["channel"], duration=ctx.get("duration") or 40, stream_title=ctx["stream_title"][:150],
        transcript=(ctx["transcript"] or "(no speech)")[:800],
        said=" | ".join(said) or "(none captured)",
        reactions=", ".join(f'"{m}" ×{n}' for m, n in reactions) or "(none)",
        summary=ctx["summary"] or "(none)",
        note=(f'- What is on screen, from the channel owner (trust this most): "{ctx["note"][:300]}"\n'
              if ctx.get("note") else ""),
        examples="\n".join(f"  - {e}" for e in STYLE_EXAMPLES))


class MetadataWriter:
    def __init__(self, ollama_url: str, model: str, base_tags: list[str], http: httpx.Client | None = None,
                 temperature: float = 0.8):
        self.url = ollama_url.rstrip("/")
        self.model = model
        self.base_tags = base_tags
        self.temperature = temperature      # high enough that the 4 titles actually differ
        self.http = http or httpx.Client(timeout=300)

    def write(self, ctx: dict) -> Metadata:
        try:
            r = self.http.post(f"{self.url}/api/chat", json={
                "model": self.model, "stream": False, "format": SCHEMA,
                "options": {"temperature": self.temperature},
                "messages": [{"role": "user", "content": build_prompt(ctx)}]})
            r.raise_for_status()
            creative = json.loads(r.json()["message"]["content"])
            if not any(str(t).strip() for t in creative.get("titles") or []):
                raise ValueError("no titles")
            return assemble(creative, ctx, self.base_tags, "llm")
        except Exception:
            return assemble(template_creative(ctx), ctx, self.base_tags, "template")
