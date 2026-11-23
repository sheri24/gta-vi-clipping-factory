"""Find footage by meaning without downloading video.

Every YouTube video has storyboards: the little preview images shown when you hover over the timeline (a 12 h
stream has a 320x180 one every 10 s; a Short about one a second). We download those sprite sheets (tiny, from
i.ytimg.com), embed each thumbnail with CLIP (OpenAI, MIT, runs on the CPU), and cache the embeddings per video.
A text query ("a plane flying over the city") then scores every moment of every indexed video in milliseconds.
"""

import io
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
import torch
from PIL import Image

from narrator.common import CACHE, MODELS, video_info

_model = None
_preprocess = None


def clip_model():
    global _model, _preprocess
    if _model is None:
        import clip   # openai-clip; weights from OpenAI's CDN (reachable where Hugging Face isn't)
        _model, _preprocess = clip.load("ViT-B/32", device="cpu", download_root=str(MODELS))
        _model.eval()
    return _model, _preprocess


@torch.no_grad()
def embed_text(texts: list[str]) -> np.ndarray:
    import clip
    model, _ = clip_model()
    t = model.encode_text(clip.tokenize(texts, truncate=True)).float()
    return (t / t.norm(dim=-1, keepdim=True)).numpy()


@torch.no_grad()
def embed_images(images: list[Image.Image], batch: int = 64) -> np.ndarray:
    model, pre = clip_model()
    out = []
    for i in range(0, len(images), batch):
        x = torch.stack([pre(im.convert("RGB")) for im in images[i:i + batch]])
        e = model.encode_image(x).float()
        out.append((e / e.norm(dim=-1, keepdim=True)).numpy())
    return np.concatenate(out) if out else np.zeros((0, 512), np.float32)


def pick_storyboard(info: dict) -> dict | None:
    """Short videos: the biggest thumbnails. Long ones (a 12 h stream has 477 sheets at 320x180): the smallest
    set of sheets whose thumbnails are still >= 150 px wide (160x90, 25 per sheet: 3x fewer downloads). CLIP only
    needs the gist here; the exact shot is chosen later on the downloaded HD footage."""
    boards = [b for b in info.get("storyboards") or [] if b.get("fragments")]
    if not boards:
        return None
    if (info.get("duration") or 0) > 3600:
        usable = [b for b in boards if (b.get("width") or 0) >= 150]
        if usable:
            return min(usable, key=lambda b: len(b["fragments"]))
    return max(boards, key=lambda b: (b.get("width") or 0) * (b.get("height") or 0))


def thumbnails(video_id: str, http: httpx.Client, max_sheets: int | None = None) -> list[tuple[float, Image.Image]]:
    """(seconds into the video, thumbnail) for the whole video, from its storyboard sprite sheets."""
    info = video_info(video_id)
    sb = pick_storyboard(info)
    if not sb:
        return []
    cols, rows = sb["columns"], sb["rows"]
    w, h = sb["width"], sb["height"]
    step = 1 / sb["fps"] if sb.get("fps") else None
    out, t0 = [], 0.0
    for i, frag in enumerate(sb["fragments"][:max_sheets]):
        r = http.get(frag["url"])
        r.raise_for_status()
        sheet = Image.open(io.BytesIO(r.content))
        per_sheet = cols * rows
        sstep = step or (frag["duration"] / per_sheet)
        for k in range(per_sheet):
            t = t0 + k * sstep
            if t >= t0 + frag["duration"] - 1e-6:
                break
            x, y = (k % cols) * w, (k // cols) * h
            if x + w <= sheet.width and y + h <= sheet.height:
                out.append((t, sheet.crop((x, y, x + w, y + h))))
        t0 += frag["duration"]
        if i % 20 == 19:
            time.sleep(0.5)        # gentle on YouTube's thumbnail server
    return out


@dataclass
class Index:
    video_id: str
    times: np.ndarray       # seconds
    emb: np.ndarray         # (n, 512) unit vectors


def load_index(video_id: str, http: httpx.Client | None = None) -> Index | None:
    path = CACHE / "index" / f"{video_id}.npz"
    if path.exists():
        z = np.load(path)
        return Index(video_id, z["times"], z["emb"])
    own = http is None
    http = http or httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    try:
        thumbs = thumbnails(video_id, http)
    finally:
        if own:
            http.close()
    if not thumbs:
        return None
    emb = embed_images([im for _, im in thumbs])
    times = np.array([t for t, _ in thumbs], np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, times=times, emb=emb.astype(np.float16))
    return Index(video_id, times, emb)


def search(index: Index, query_emb: np.ndarray, top: int = 3, min_gap: float = 30.0,
           negatives: np.ndarray | None = None) -> list[tuple[float, float]]:
    """Best (time, score) moments for one query embedding, at least min_gap seconds apart. Moments that look more
    like any of the `negatives` (older GTA games, title cards, comparison screens) than like the query are dropped."""
    emb = index.emb.astype(np.float32)
    scores = emb @ query_emb.astype(np.float32)
    if negatives is not None and len(negatives):
        worst = (emb @ negatives.astype(np.float32).T).max(axis=1)
        scores = np.where(worst >= scores, -1.0, scores)
    picked: list[tuple[float, float]] = []
    for i in np.argsort(-scores):
        if scores[i] <= -1.0:
            break
        t = float(index.times[i])
        if all(abs(t - p) >= min_gap for p, _ in picked):
            picked.append((t, float(scores[i])))
        if len(picked) >= top:
            break
    return picked
