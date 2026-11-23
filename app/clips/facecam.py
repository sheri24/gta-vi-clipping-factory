"""Find the streamer's webcam overlay automatically, per stream.

1. Sample frames spread over the recording.
2. Detect faces (OpenCV YuNet). The streamer's face is the one that stays in the SAME place in most
   frames; game characters' faces move around (Lacy's finale: webcam face in 35/35 frames, best game
   face cluster 5/35).
3. Each side of the box is the line that best combines (a) edge persistence across frames (overlay
   borders are static) and (b) "still inside, changing outside": the webcam room barely changes over
   time while the game changes constantly (measured on Lacy: ~3 vs ~88 grey-level std). Room edges are
   static on BOTH sides, so they score ~0; no qualifying line on a side means the box runs to the frame edge.
4. If no clean box is found, fall back to a camera-shaped crop centred on the face (still shows the
   reaction, which is the point of the split layout).
"""

import logging
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.recording.media import NO_WINDOW

log = logging.getLogger(__name__)

W, H = 640, 360                     # analysis resolution
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_SIZE = 232589
# Border score = edge persistence along the span x (outside change - inside change)/(sum).
# Measured on Lacy's overlay: true borders 0.28-0.44; room edges and frame-edge sides <= 0.02.
BORDER_SCORE = 0.15
CONTRAST_BAND = 6                   # px each side used to compare how much the content changes
MIN_FACE_FRACTION = 0.5             # the webcam face must be present in >= 50% of frames


@dataclass(frozen=True)
class FacecamResult:
    rect: tuple[float, float, float, float]   # x, y, w, h as fractions of the frame
    confidence: float                          # 0-1
    method: str                                # borders | face-expand
    face_frames: int
    frames: int

    @property
    def rect_str(self) -> str:
        return ",".join(f"{v:.4f}" for v in self.rect)


# -- frames ---------------------------------------------------------------------------------------

def sample_frames(paths: list[Path], every_seconds: float = 10, max_frames: int = 40,
                  ffmpeg_bin: str = "ffmpeg") -> list[np.ndarray]:
    frames: list[np.ndarray] = []
    size = W * H * 3
    for p in paths:
        raw = subprocess.run([ffmpeg_bin, "-v", "error", "-i", str(p), "-vf", f"fps=1/{every_seconds},scale={W}:{H}",
                              "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, timeout=300,
                             creationflags=NO_WINDOW).stdout
        frames += [np.frombuffer(raw[i:i + size], np.uint8).reshape(H, W, 3) for i in range(0, len(raw) - size + 1, size)]
    if len(frames) > max_frames:   # spread evenly over everything we have
        idx = np.linspace(0, len(frames) - 1, max_frames).round().astype(int)
        frames = [frames[i] for i in idx]
    return frames


# -- faces -----------------------------------------------------------------------------------------

class YuNetFaces:
    """OpenCV's YuNet face detector (MIT). Model (~230 KB) downloads once from OpenCV's GitHub."""

    def __init__(self, models_dir: Path, score_threshold: float = 0.7):
        self.path = models_dir / YUNET_FILE
        self.score_threshold = score_threshold
        self._det = None

    def _load(self):
        if self._det is None:
            import cv2
            if not self.path.exists() or self.path.stat().st_size != YUNET_SIZE:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(YUNET_URL, self.path)
                if self.path.stat().st_size != YUNET_SIZE:
                    self.path.unlink()
                    raise RuntimeError("YuNet download incomplete")
            self._det = cv2.FaceDetectorYN.create(str(self.path), "", (W, H), score_threshold=self.score_threshold)
        return self._det

    def __call__(self, frame: np.ndarray) -> list[tuple[float, float, float, float]]:
        _, out = self._load().detect(frame)
        return [tuple(float(v) for v in row[:4]) for row in (out if out is not None else [])]


def stable_face(faces_per_frame: list[list[tuple]], min_fraction: float = MIN_FACE_FRACTION):
    """Median box of the face that sits in the same place (±5% of the frame) in most frames."""
    clusters: list[dict] = []
    for i, faces in enumerate(faces_per_frame):
        for (x, y, w, h) in faces:
            cx, cy = x + w / 2, y + h / 2
            for c in clusters:
                if abs(c["cx"] - cx) < 0.05 * W and abs(c["cy"] - cy) < 0.05 * H:
                    c["boxes"].append((x, y, w, h))
                    c["frames"].add(i)
                    break
            else:
                clusters.append({"cx": cx, "cy": cy, "boxes": [(x, y, w, h)], "frames": {i}})
    if not clusters or not faces_per_frame:
        return None, 0
    best = max(clusters, key=lambda c: len(c["frames"]))
    hits = len(best["frames"])
    if hits < min_fraction * len(faces_per_frame):
        return None, hits
    return tuple(np.median(np.array(best["boxes"]), axis=0)), hits


# -- borders ----------------------------------------------------------------------------------------

def persistent_edges(frames: list[np.ndarray]) -> np.ndarray:
    import cv2
    return np.mean([cv2.Canny(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), 60, 160) > 0 for f in frames], axis=0)


def _line_strength(edges: np.ndarray, axis: str, pos: int, lo: int, hi: int) -> float:
    """Mean edge persistence along [lo, hi) of a column (axis='x') or row (axis='y'), taking the best of
    ±1 px at each step: Canny puts the same border on neighbouring pixels depending on the content."""
    lo, hi = max(0, lo), min(edges.shape[0 if axis == "x" else 1], hi)
    if hi <= lo:
        return 0.0
    band = edges[lo:hi, max(0, pos - 1):pos + 2] if axis == "x" else edges[max(0, pos - 1):pos + 2, lo:hi].T
    return float(band.max(axis=1).mean())


def temporal_change(frames: list[np.ndarray]) -> np.ndarray:
    """Per-pixel std of grey level over time: ~0 for static overlays/webcam rooms, high for gameplay."""
    import cv2
    return np.array([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames], dtype=np.float32).std(axis=0)


def _contrast(change: np.ndarray, axis: str, pos: int, lo: int, hi: int, inside_before: bool) -> float:
    """(outside - inside) / (outside + inside) change, comparing a thin band either side of the line."""
    k = CONTRAST_BAND
    if axis == "x":
        before = change[lo:hi, max(0, pos - k):pos]
        after = change[lo:hi, pos + 1:pos + 1 + k]
    else:
        before = change[max(0, pos - k):pos, lo:hi]
        after = change[pos + 1:pos + 1 + k, lo:hi]
    if before.size == 0 or after.size == 0:
        return 0.0
    inside, outside = (before.mean(), after.mean()) if inside_before else (after.mean(), before.mean())
    return float((outside - inside) / (outside + inside + 1e-6))


def find_box(edges: np.ndarray, change: np.ndarray, face: tuple) -> tuple[tuple[int, int, int, int], float]:
    """Best-scoring border line on each side of the face; frame edge where none qualifies.
    Returns ((x0, y0, x1, y1) in analysis px, confidence 0-1)."""
    fx, fy, fw, fh = (int(round(v)) for v in face)
    rows = (max(0, fy - fh // 2), min(H, fy + fh + fh // 2))      # span a vertical border must cover
    cols = (max(0, fx - fw // 2), min(W, fx + fw + fw // 2))      # span a horizontal border must cover
    found = []

    def best(axis, positions, span, inside_before, frame_edge):
        top_score, top_pos = 0.0, frame_edge
        for p in positions:
            s = _line_strength(edges, axis, p, *span) * max(0.0, _contrast(change, axis, p, *span, inside_before))
            if s > top_score:
                top_score, top_pos = s, p
        if top_score < BORDER_SCORE:
            return frame_edge
        found.append(top_score)
        return top_pos

    # Search all the way to the frame edges: the change-contrast term, not distance, rejects wrong lines.
    x0 = best("x", range(fx - 2, 0, -1), rows, False, 0)
    x1 = best("x", range(fx + fw + 2, W - 1), rows, True, W)
    y0 = best("y", range(fy - 2, 0, -1), cols, False, 0)
    y1 = best("y", range(fy + fh + 2, H - 1), cols, True, H)
    confidence = min(1.0, float(np.mean(found)) / 0.3) if found else 0.0
    return (x0, y0, x1, y1), round(confidence, 3)


def plausible(box: tuple[int, int, int, int], face: tuple) -> bool:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return False
    area = (w * h) / (W * H)
    return (0.01 <= area <= 0.30            # a webcam overlay, not the whole screen
            and 0.8 <= w / h <= 2.6         # roughly camera-shaped
            and face[2] >= 0.08 * w)        # the face is a meaningful part of it


def face_expand(face: tuple) -> tuple[int, int, int, int]:
    """Camera-shaped (16:9) crop around the face, ~5 face-widths wide, clamped to the frame."""
    fx, fy, fw, fh = face
    w = min(W, 5 * fw)
    h = min(H, w * 9 / 16)
    cx, cy = fx + fw / 2, fy + fh / 2
    x0 = int(min(max(0, cx - w / 2), W - w))
    y0 = int(min(max(0, cy - h * 0.45), H - h))
    return x0, y0, int(x0 + w), int(y0 + h)


MIN_SEGMENTS = 3          # ~3 min of footage before the first attempt
MAX_ATTEMPTS = 4          # streamer may be away from the camera at first
RETRY_MINUTES = 5


def detect_for_sessions(db, faces, ffmpeg_bin: str = "ffmpeg") -> list[int]:
    """Run detection for recording sessions that don't have an answer yet. Returns session ids updated."""
    from datetime import datetime, timedelta, timezone

    from app.db import utcnow
    retry_before = (datetime.now(timezone.utc) - timedelta(minutes=RETRY_MINUTES)).isoformat(timespec="seconds")
    sessions = db.query(
        """SELECT s.id FROM live_sessions s JOIN channels c ON c.channel_id = s.channel_id
            WHERE c.record_enabled = 1 AND s.facecam_rect IS NULL AND s.facecam_attempts < ?
              AND s.storage_cleaned_at IS NULL          -- no footage left (or a past stream: see app/vod.py)
              AND (s.facecam_checked_at IS NULL OR s.facecam_checked_at < ?)""", (MAX_ATTEMPTS, retry_before))
    updated = []
    for s in sessions:
        paths = [Path(r["path"]) for r in db.query(
            """SELECT path FROM segments WHERE session_id = ? AND deleted_at IS NULL
                ORDER BY seq DESC LIMIT 10""", (s["id"],))]
        if len(paths) < MIN_SEGMENTS:
            continue
        try:
            result = detect_facecam(sample_frames(list(reversed(paths)), every_seconds=15, ffmpeg_bin=ffmpeg_bin), faces)
        except Exception as e:   # never blocks clipping; blur layout is the fallback
            log.exception("facecam detection failed")
            db.log_error("clips.facecam", str(e)[:200], str(s["id"]))
            result = None
        db.execute(
            """UPDATE live_sessions SET facecam_rect = ?, facecam_confidence = ?, facecam_method = ?,
                   facecam_checked_at = ?, facecam_attempts = facecam_attempts + 1 WHERE id = ?""",
            (result.rect_str if result else None, result.confidence if result else None,
             result.method if result else "none", utcnow(), s["id"]))
        log.info("facecam for session %d: %s", s["id"], result or "none found")
        updated.append(s["id"])
    return updated


def detect_facecam(frames: list[np.ndarray], faces) -> FacecamResult | None:
    """faces: callable(frame) -> list of (x, y, w, h) boxes in analysis pixels."""
    if len(frames) < 6:
        return None
    face, hits = stable_face([faces(f) for f in frames])
    if face is None:
        return None   # no consistent webcam face: this stream has no (visible) facecam
    box, conf = find_box(persistent_edges(frames), temporal_change(frames), face)
    method = "borders"
    if conf == 0.0 or not plausible(box, face):
        box, conf, method = face_expand(face), 0.4, "face-expand"
    x0, y0, x1, y1 = box
    return FacecamResult((x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H), round(conf, 3), method, hits, len(frames))
