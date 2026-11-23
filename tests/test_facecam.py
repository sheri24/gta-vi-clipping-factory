import numpy as np
import pytest

from app.captions.ass import CaptionStyle
from app.captions.worker import CaptionWorker
from app.clips.facecam import H, W, detect_facecam, face_expand, plausible, stable_face
from app.clips.pipeline import choose_layout


def synthetic_stream(box, n=30, seed=0):
    """Webcam overlay = static 'room' (with its own static internal lines, which must NOT be taken for
    borders) inside `box`; outside = 'gameplay' that changes completely every frame."""
    rng = np.random.default_rng(seed)
    x0, y0, x1, y1 = box
    room = np.full((y1 - y0, x1 - x0, 3), 70, np.uint8)
    room[:, (x1 - x0) // 3] = 200                       # a static vertical wall edge inside the room
    room[(y1 - y0) // 2, :] = 180                       # a static horizontal shelf inside the room
    frames = []
    for _ in range(n):
        f = np.zeros((H, W, 3), np.uint8)
        f[:] = rng.integers(0, 256, 3)                   # each frame: a different game scene colour
        bx, by = rng.integers(0, W - 120), rng.integers(0, H - 80)
        f[by:by + 80, bx:bx + 120] = rng.integers(0, 256, 3)   # a moving object in the game
        f[y0:y1, x0:x1] = room
        frames.append(f)
    return frames


def faces_for(box, game_face_every=4):
    """Fake detector: the streamer's face sits in the overlay; a game character's face moves around."""
    x0, y0, x1, y1 = box
    state = {"i": 0}

    def detect(frame):
        state["i"] += 1
        out = [(x0 + (x1 - x0) * 0.4, y0 + (y1 - y0) * 0.3, 24.0, 30.0)]
        if state["i"] % game_face_every == 0:
            out.append((300.0 + state["i"] * 7 % 200, 150.0 + state["i"] * 13 % 100, 40.0, 50.0))
        return out
    return detect


def as_px(rect):
    x, y, w, h = rect
    return round(x * W), round(y * H), round((x + w) * W), round((y + h) * H)


@pytest.mark.parametrize("box", [(0, 0, 164, 92),           # top-left corner, like Lacy
                                 (476, 0, 640, 92),         # top-right corner
                                 (420, 230, 600, 330)])     # floating bottom-right, all four borders
def test_finds_overlay_box_despite_static_room_edges(box):
    res = detect_facecam(synthetic_stream(box), faces_for(box))
    assert res is not None and res.method == "borders"
    got = as_px(res.rect)
    assert all(abs(g - b) <= 3 for g, b in zip(got, box)), (got, box)
    assert res.face_frames == 30 and res.confidence > 0.5


def test_no_stable_face_means_no_facecam():
    frames = synthetic_stream((0, 0, 164, 92))
    moving_only = iter(range(1000))

    def detect(frame):
        i = next(moving_only)
        return [(20.0 + i * 15 % 500, 20.0 + i * 11 % 300, 30.0, 30.0)]
    assert detect_facecam(frames, detect) is None


def test_stable_face_ignores_game_faces():
    per_frame = [[(60, 30, 24, 30)] + ([(300 + i * 9, 200, 40, 40)] if i % 2 else []) for i in range(20)]
    face, hits = stable_face(per_frame)
    assert hits == 20 and abs(face[0] - 60) < 1


def test_face_expand_is_camera_shaped_and_inside_frame():
    x0, y0, x1, y1 = face_expand((600, 340, 30, 30))
    assert 0 <= x0 < x1 <= W and 0 <= y0 < y1 <= H
    assert abs((x1 - x0) / (y1 - y0) - 16 / 9) < 0.05


def test_plausible_rejects_whole_screen_and_slivers():
    face = (60, 30, 24, 30)
    assert plausible((0, 0, 164, 92), face)
    assert not plausible((0, 0, W, H), face)          # whole screen
    assert not plausible((50, 20, 60, 120), face)     # thin sliver


def test_choose_layout_precedence():
    assert choose_layout("auto", None, "0,0,0.25,0.25") == ("split", "0,0,0.25,0.25")
    assert choose_layout("auto", None, None) == ("blur", None)
    assert choose_layout("auto", "0.7,0,0.3,0.3", "0,0,0.25,0.25") == ("split", "0.7,0,0.3,0.3")   # manual wins
    assert choose_layout("blur", None, "0,0,0.25,0.25") == ("blur", None)
    assert choose_layout("split", None, None) == ("blur", None)          # nothing to split with


def test_split_layout_moves_captions_to_the_seam():
    w = CaptionWorker(None, None, None, CaptionStyle(margin_v=470))
    assert w.style_for("blur").margin_v == 470
    assert w.style_for("split").margin_v == w.split_margin_v == 1180
