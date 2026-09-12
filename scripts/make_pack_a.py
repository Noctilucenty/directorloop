#!/usr/bin/env python
"""Generate demo pack A: a fictional, procedurally rendered phone-stand demonstration.

The footage is a clearly labeled graphical fixture (spec section 6 allows this when recorded
footage is unavailable). One 2.5D scene is rendered from three camera framings so the wide shot,
the close-up and the result shot are geometrically consistent:

  wide     the whole desk; the stand is small and the orange locking tab is tiny
  closeup  the tab and the slot fill the frame while the tab is pressed down into the slot
  result   the phone resting on the locked stand while a fingertip taps the screen

Rendering is deterministic (no randomness, no timestamps, single-threaded x264, bitexact muxing),
so asset content hashes are stable across runs on the same ffmpeg build.

Usage:
  .venv/bin/python scripts/make_pack_a.py                       # full pack into packs/pack_a_stand_demo
  .venv/bin/python scripts/make_pack_a.py --out /tmp/p --width 270 --height 480 --fps 10 --speech say
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from directorloop.domain import (  # noqa: E402
    AssetKind,
    AssetManifest,
    AssetOrigin,
    AssetRecord,
    Budget,
    Caption,
    Claim,
    ClaimKind,
    ConstraintKind,
    CreativeBrief,
    EditPlan,
    EvaluationSuite,
    EvidenceModality,
    GoalProfile,
    MusicTrack,
    NarrationTrack,
    OutputProfile,
    ProbeOption,
    ProbeQuestion,
    ProtectedConstraint,
    RepairAction,
    RequiredInformation,
    Segment,
    SourceReference,
    SourceTruth,
    StreamInfo,
    SuiteSplit,
    load_pack,
    sha256_file,
    utc_now_iso,
    write_checksums,
)
from directorloop.providers.speech import SpeechError, SpeechResult, default_speech_provider  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

NARRATION_TEXT = (
    "I made this phone stand out of a cereal box. Set it up, lock it, and it holds. "
    "Even when you tap around on the screen."
)

# ----------------------------------------------------------------------------- palette
WALL = (226, 218, 205)
WALL_SHADE = (214, 205, 190)
DESK = (168, 128, 88)
DESK_FAR = (150, 112, 76)
BASE_TOP = (205, 168, 118)
BASE_FRONT = (184, 146, 100)
BASE_SIDE = (160, 126, 86)
PANEL_FRONT = (196, 158, 108)
PANEL_EDGE = (150, 118, 80)
LOOP = (176, 138, 92)
LOOP_SHADE = (140, 108, 70)
SLOT = (58, 40, 26)
SLOT_INNER = (36, 24, 16)
TAB = (232, 118, 44)
TAB_SIDE = (196, 92, 30)
TAB_TOP = (244, 150, 84)
LIP_FRONT = (176, 140, 96)
LIP_TOP = (198, 160, 112)
PHONE = (34, 34, 40)
PHONE_SIDE = (22, 22, 26)
SCREEN = (52, 56, 66)
SCREEN_HIGHLIGHT = (86, 92, 106)
RIPPLE = (150, 160, 190)
FINGER = (222, 176, 150)
FINGER_SHADE = (200, 150, 124)
NAIL = (240, 214, 200)
MUG = (120, 140, 150)
MUG_DARK = (96, 112, 122)
NOTEBOOK = (90, 96, 120)
NOTEBOOK_EDGE = (230, 226, 216)
PLANT_POT = (150, 96, 70)
PLANT = (96, 130, 84)

# ----------------------------------------------------------------------------- projection
KX = 0.50  # screen x offset per unit of depth
KY = 0.35  # screen y offset (upward) per unit of depth
NOMINAL_HEIGHT = 1920  # camera scales are expressed for this frame height

Vec3 = tuple[float, float, float]
Vec2 = tuple[float, float]


def project(p: Vec3) -> Vec2:
    x, y, z = p
    return (x + KX * z, -y - KY * z)


@dataclass(frozen=True)
class Camera:
    scale: float  # pixels per world unit at the nominal output size
    cx: float  # projected-space center x
    cy: float  # projected-space center y


@dataclass
class Frame:
    image: Image.Image
    draw: ImageDraw.ImageDraw
    cam: Camera
    width: int
    height: int
    ss: int  # supersampling factor

    @property
    def k(self) -> float:
        """Pixels per world unit. Camera scale is defined at a 1920 px tall frame, so smaller
        renders are true downscales of the same composition."""
        return self.cam.scale * (self.height / NOMINAL_HEIGHT) * self.ss

    def px(self, p: Vec3) -> Vec2:
        sx, sy = project(p)
        return ((sx - self.cam.cx) * self.k + self.width * self.ss / 2, (sy - self.cam.cy) * self.k + self.height * self.ss / 2)

    def poly(self, pts: list[Vec3], color: tuple[int, int, int]) -> None:
        self.draw.polygon([self.px(p) for p in pts], fill=color)

    def poly2d(self, pts: list[Vec2], color: tuple[int, int, int]) -> None:
        self.draw.polygon(pts, fill=color)

    def rect(self, x0: float, y0: float, x1: float, y1: float, color: tuple[int, int, int]) -> None:
        """Axis-aligned rectangle in pixel space, tolerant of off-frame or inverted bounds."""
        W, H = self.width * self.ss, self.height * self.ss
        x0, x1 = sorted((max(0.0, min(W, x0)), max(0.0, min(W, x1))))
        y0, y1 = sorted((max(0.0, min(H, y0)), max(0.0, min(H, y1))))
        if x1 > x0 and y1 > y0:
            self.draw.rectangle([x0, y0, x1, y1], fill=color)

    def unit(self, n: float) -> float:
        return n * self.k


# ----------------------------------------------------------------------------- geometry (world units)
BASE_X0, BASE_X1, BASE_Z0, BASE_Z1, BASE_Y = 0.0, 80.0, 0.0, 60.0, 10.0
PANEL_Z, PANEL_T, PANEL_H, PANEL_LEAN = 48.0, 4.0, 100.0, 0.36
PANEL_X0, PANEL_X1 = 8.0, 72.0
TAB_X0, TAB_X1, TAB_Z, TAB_T, TAB_H = 32.0, 48.0, 44.0, 3.0, 22.0
TAB_TRAVEL = TAB_H - 2.0  # seated: 2 units of tab remain above the base (a clear band in the close-up, a faint sliver in the wide)
SLOT_X0, SLOT_X1, SLOT_Z0, SLOT_Z1 = 31.0, 49.0, 43.0, 47.5
LIP_X0, LIP_X1, LIP_Z0, LIP_Z1, LIP_H = 8.0, 72.0, 4.0, 8.0, 5.0
PHONE_X0, PHONE_X1, PHONE_H, PHONE_Z = 18.0, 62.0, 92.0, 9.0
DESK_DEPTH = 150.0


def ease_in_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 0.5 - 0.5 * math.cos(math.pi * t)


def ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def panel_point(x: float, h: float, z_off: float = 0.0) -> Vec3:
    """Point on the leaning back panel at height h above the base top (front face when z_off=0)."""
    return (x, BASE_Y + h, PANEL_Z + PANEL_LEAN * h + z_off)


def phone_point(x: float, h: float, z_off: float = 0.0) -> Vec3:
    return (x, BASE_Y + LIP_H + h, PHONE_Z + PANEL_LEAN * h + z_off)


# ----------------------------------------------------------------------------- drawing
def draw_background(f: Frame) -> None:
    W, H = f.width * f.ss, f.height * f.ss
    f.rect(0, 0, W, H, WALL)
    # wall shading band near the horizon
    horizon_y = f.px((0.0, 0.0, DESK_DEPTH))[1]
    f.rect(0, horizon_y - f.unit(30), W, horizon_y, WALL_SHADE)
    # desk: far band then main surface
    far_y = f.px((0.0, 0.0, DESK_DEPTH - 22))[1]
    f.rect(0, horizon_y, W, H, DESK_FAR)
    f.rect(0, far_y, W, H, DESK)


def draw_props(f: Frame) -> None:
    # notebook on the left, lying flat
    f.poly([(-70, 0, 20), (-14, 0, 20), (-14, 0, 72), (-70, 0, 72)], NOTEBOOK_EDGE)
    f.poly([(-70, 1.5, 20), (-14, 1.5, 20), (-14, 1.5, 72), (-70, 1.5, 72)], NOTEBOOK)
    # mug on the right: a cylinder approximated by a rectangle body plus ellipse top
    f.poly([(112, 0, 58), (134, 0, 58), (134, 30, 58), (112, 30, 58)], MUG)
    f.poly([(134, 0, 58), (140, 0, 66), (140, 30, 66), (134, 30, 58)], MUG_DARK)
    tx, ty = f.px((123, 30, 62))
    rx, ry = f.unit(14), f.unit(5)
    f.draw.ellipse([tx - rx, ty - ry, tx + rx, ty + ry], fill=MUG_DARK)
    # small plant far back
    f.poly([(150, 0, 110), (172, 0, 110), (172, 22, 110), (150, 22, 110)], PLANT_POT)
    px, py = f.px((161, 32, 110))
    f.draw.ellipse([px - f.unit(18), py - f.unit(14), px + f.unit(18), py + f.unit(14)], fill=PLANT)


def draw_base_top(f: Frame) -> None:
    f.poly(
        [(BASE_X0, BASE_Y, BASE_Z0), (BASE_X1, BASE_Y, BASE_Z0), (BASE_X1, BASE_Y, BASE_Z1), (BASE_X0, BASE_Y, BASE_Z1)],
        BASE_TOP,
    )


def draw_panel(f: Frame) -> None:
    # right side edge (thickness), then the front face
    f.poly(
        [panel_point(PANEL_X1, 0), panel_point(PANEL_X1, 0, PANEL_T), panel_point(PANEL_X1, PANEL_H, PANEL_T),
         panel_point(PANEL_X1, PANEL_H)],
        PANEL_EDGE,
    )
    f.poly(
        [panel_point(PANEL_X0, PANEL_H), panel_point(PANEL_X1, PANEL_H), panel_point(PANEL_X1, PANEL_H, PANEL_T),
         panel_point(PANEL_X0, PANEL_H, PANEL_T)],
        PANEL_EDGE,
    )
    f.poly(
        [panel_point(PANEL_X0, 0), panel_point(PANEL_X1, 0), panel_point(PANEL_X1, PANEL_H), panel_point(PANEL_X0, PANEL_H)],
        PANEL_FRONT,
    )


def draw_base_front_and_side(f: Frame) -> None:
    f.poly([(BASE_X1, 0, BASE_Z0), (BASE_X1, 0, BASE_Z1), (BASE_X1, BASE_Y, BASE_Z1), (BASE_X1, BASE_Y, BASE_Z0)], BASE_SIDE)
    f.poly([(BASE_X0, 0, BASE_Z0), (BASE_X1, 0, BASE_Z0), (BASE_X1, BASE_Y, BASE_Z0), (BASE_X0, BASE_Y, BASE_Z0)], BASE_FRONT)


def draw_slot(f: Frame) -> None:
    f.poly([(SLOT_X0, BASE_Y, SLOT_Z0), (SLOT_X1, BASE_Y, SLOT_Z0), (SLOT_X1, BASE_Y, SLOT_Z1), (SLOT_X0, BASE_Y, SLOT_Z1)], SLOT)
    f.poly([(SLOT_X0 + 0.6, BASE_Y, SLOT_Z0 + 1.6), (SLOT_X1 - 0.6, BASE_Y, SLOT_Z0 + 1.6),
            (SLOT_X1 - 0.6, BASE_Y, SLOT_Z1 - 0.4), (SLOT_X0 + 0.6, BASE_Y, SLOT_Z1 - 0.4)], SLOT_INNER)


def draw_tab(f: Frame, drop: float) -> None:
    """drop: 0 = fully up (unlocked), 1 = seated in the slot (locked)."""
    bottom = BASE_Y - TAB_TRAVEL * drop
    top = bottom + TAB_H
    vis_bottom = max(bottom, BASE_Y)  # the part inside the slot is hidden
    if top <= vis_bottom:
        return
    # right side face
    f.poly([(TAB_X1, vis_bottom, TAB_Z), (TAB_X1, vis_bottom, TAB_Z + TAB_T), (TAB_X1, top, TAB_Z + TAB_T), (TAB_X1, top, TAB_Z)],
           TAB_SIDE)
    # top face
    f.poly([(TAB_X0, top, TAB_Z), (TAB_X1, top, TAB_Z), (TAB_X1, top, TAB_Z + TAB_T), (TAB_X0, top, TAB_Z + TAB_T)], TAB_TOP)
    # front face
    f.poly([(TAB_X0, vis_bottom, TAB_Z), (TAB_X1, vis_bottom, TAB_Z), (TAB_X1, top, TAB_Z), (TAB_X0, top, TAB_Z)], TAB)


def draw_loops(f: Frame) -> None:
    """Two thin guide loops on the panel that the tab slides through."""
    for h0, h1 in ((8.0, 11.5), (19.0, 22.5)):
        f.poly([(TAB_X0 - 2.5, BASE_Y + h0, TAB_Z - 0.5), (TAB_X1 + 2.5, BASE_Y + h0, TAB_Z - 0.5),
                (TAB_X1 + 2.5, BASE_Y + h1, TAB_Z - 0.5), (TAB_X0 - 2.5, BASE_Y + h1, TAB_Z - 0.5)], LOOP)
        f.poly([(TAB_X0 - 2.5, BASE_Y + h0, TAB_Z - 0.5), (TAB_X1 + 2.5, BASE_Y + h0, TAB_Z - 0.5),
                (TAB_X1 + 2.5, BASE_Y + h0 + 0.8, TAB_Z - 0.5), (TAB_X0 - 2.5, BASE_Y + h0 + 0.8, TAB_Z - 0.5)], LOOP_SHADE)


def draw_lip(f: Frame) -> None:
    f.poly([(LIP_X0, BASE_Y, LIP_Z0), (LIP_X1, BASE_Y, LIP_Z0), (LIP_X1, BASE_Y + LIP_H, LIP_Z0), (LIP_X0, BASE_Y + LIP_H, LIP_Z0)],
           LIP_FRONT)
    f.poly([(LIP_X0, BASE_Y + LIP_H, LIP_Z0), (LIP_X1, BASE_Y + LIP_H, LIP_Z0), (LIP_X1, BASE_Y + LIP_H, LIP_Z1),
            (LIP_X0, BASE_Y + LIP_H, LIP_Z1)], LIP_TOP)


def draw_phone(f: Frame, dx: float = 0.0, dy: float = 0.0, ripple: float | None = None, settle: float = 0.0) -> None:
    """The dark phone slab leaning on the panel. dx/dy offset in world units (used for the slide-in)."""

    def pp(x: float, h: float, z_off: float = 0.0) -> Vec3:
        px_, py_, pz_ = phone_point(x, h, z_off)
        return (px_ + dx, py_ + dy + settle, pz_)

    f.poly([pp(PHONE_X1, 0), pp(PHONE_X1, 0, 3), pp(PHONE_X1, PHONE_H, 3), pp(PHONE_X1, PHONE_H)], PHONE_SIDE)
    f.poly([pp(PHONE_X0, 0), pp(PHONE_X1, 0), pp(PHONE_X1, PHONE_H), pp(PHONE_X0, PHONE_H)], PHONE)
    f.poly([pp(PHONE_X0 + 2, 3), pp(PHONE_X1 - 2, 3), pp(PHONE_X1 - 2, PHONE_H - 3), pp(PHONE_X0 + 2, PHONE_H - 3)], SCREEN)
    f.poly([pp(PHONE_X0 + 4, PHONE_H - 30), pp(PHONE_X0 + 14, PHONE_H - 30), pp(PHONE_X0 + 14, PHONE_H - 6),
            pp(PHONE_X0 + 4, PHONE_H - 6)], SCREEN_HIGHLIGHT)
    if ripple is not None:
        cx, cy = f.px(pp(40, 50))
        r = f.unit(4 + 10 * ripple)
        width = max(1, int(f.unit(1.2 * (1 - ripple)) + 1))
        f.draw.ellipse([cx - r, cy - r * 0.6, cx + r, cy + r * 0.6], outline=RIPPLE, width=width)


def draw_finger(f: Frame, tip: Vec2, direction: Vec2, width_units: float) -> None:
    """A simple finger in screen space: capsule from tip along direction, plus a wider hand blob."""
    w = f.unit(width_units)
    length = f.unit(width_units * 5.0)  # finger, then the hand widens and leaves the frame
    hand_len = f.unit(width_units * 24)
    dx, dy = direction
    n = math.hypot(dx, dy) or 1.0
    dx, dy = dx / n, dy / n
    nx, ny = -dy, dx
    tx, ty = tip
    ex, ey = tx + dx * length, ty + dy * length
    hx, hy = tx + dx * hand_len, ty + dy * hand_len
    # hand: widens from the knuckle outward, drawn first so the finger sits on top
    hand = [(ex + nx * w * 0.6, ey + ny * w * 0.6), (hx + nx * w * 2.4, hy + ny * w * 2.4),
            (hx - nx * w * 1.6, hy - ny * w * 1.6), (ex - nx * w * 0.6, ey - ny * w * 0.6)]
    f.poly2d(hand, FINGER)
    hand_s = [(ex + nx * w * 0.6, ey + ny * w * 0.6), (hx + nx * w * 2.4, hy + ny * w * 2.4),
              (hx + nx * w * 1.6, hy + ny * w * 1.6), (ex + nx * w * 0.3, ey + ny * w * 0.3)]
    f.poly2d(hand_s, FINGER_SHADE)
    quad = [(tx + nx * w / 2, ty + ny * w / 2), (ex + nx * w / 2, ey + ny * w / 2),
            (ex - nx * w / 2, ey - ny * w / 2), (tx - nx * w / 2, ty - ny * w / 2)]
    f.poly2d(quad, FINGER)
    # shaded underside
    quad_s = [(tx + nx * w / 2, ty + ny * w / 2), (ex + nx * w / 2, ey + ny * w / 2),
              (ex + nx * w * 0.25, ey + ny * w * 0.25), (tx + nx * w * 0.25, ty + ny * w * 0.25)]
    f.poly2d(quad_s, FINGER_SHADE)
    f.draw.ellipse([tx - w / 2, ty - w / 2, tx + w / 2, ty + w / 2], fill=FINGER)
    # nail on the top side of the tip
    nail_cx, nail_cy = tx + dx * w * 0.55 - nx * w * 0.22, ty + dy * w * 0.55 - ny * w * 0.22
    f.draw.ellipse([nail_cx - w * 0.22, nail_cy - w * 0.16, nail_cx + w * 0.22, nail_cy + w * 0.16], fill=NAIL)


def draw_hand(f: Frame, tip: Vec2, direction: Vec2, width_units: float) -> None:
    """A whole hand seen from the front: bent index finger on top, palm right behind it, thumb
    to the side, forearm leaving the frame. Used in the wide shot, where a hand pressing
    something at the base of the stand naturally hides the mechanism from a desk-level camera."""
    w = f.unit(width_units)
    dx, dy = direction
    n = math.hypot(dx, dy) or 1.0
    dx, dy = dx / n, dy / n
    nx, ny = -dy, dx
    tx, ty = tip
    palm_cx, palm_cy = tx + dx * w * 1.5, ty + dy * w * 1.5
    prx, pry = w * 3.2, w * 2.3
    # forearm first (under everything), widening toward the frame edge
    fx, fy = tx + dx * w * 30, ty + dy * w * 30
    forearm = [(palm_cx + nx * w * 2.4, palm_cy + ny * w * 2.4), (fx + nx * w * 3.2, fy + ny * w * 3.2),
               (fx - nx * w * 3.2, fy - ny * w * 3.2), (palm_cx - nx * w * 2.4, palm_cy - ny * w * 2.4)]
    f.poly2d(forearm, FINGER)
    forearm_s = [(palm_cx + nx * w * 2.4, palm_cy + ny * w * 2.4), (fx + nx * w * 3.2, fy + ny * w * 3.2),
                 (fx + nx * w * 1.8, fy + ny * w * 1.8), (palm_cx + nx * w * 1.2, palm_cy + ny * w * 1.2)]
    f.poly2d(forearm_s, FINGER_SHADE)
    # thumb to the left of the palm
    thx, thy = palm_cx - w * 3.4, palm_cy - w * 0.4
    f.draw.ellipse([thx - w * 1.2, thy - w * 0.75, thx + w * 1.2, thy + w * 0.75], fill=FINGER_SHADE)
    f.draw.ellipse([thx - w * 1.1, thy - w * 0.65, thx + w * 1.1, thy + w * 0.55], fill=FINGER)
    # palm with a shaded lower half
    f.draw.ellipse([palm_cx - prx, palm_cy - pry, palm_cx + prx, palm_cy + pry], fill=FINGER_SHADE)
    f.draw.ellipse([palm_cx - prx, palm_cy - pry, palm_cx + prx, palm_cy + pry * 0.55], fill=FINGER)
    # bent index finger poking out above the palm, tip at the contact point
    ex, ey = tx + dx * w * 1.2, ty + dy * w * 1.2
    quad = [(tx + nx * w / 2, ty + ny * w / 2), (ex + nx * w / 2, ey + ny * w / 2),
            (ex - nx * w / 2, ey - ny * w / 2), (tx - nx * w / 2, ty - ny * w / 2)]
    f.poly2d(quad, FINGER)
    f.draw.ellipse([tx - w / 2, ty - w / 2, tx + w / 2, ty + w / 2], fill=FINGER)
    nail_cx, nail_cy = tx - dx * w * 0.05, ty - dy * w * 0.05
    f.draw.ellipse([nail_cx - w * 0.24, nail_cy - w * 0.17, nail_cx + w * 0.24, nail_cy + w * 0.17], fill=NAIL)


# ----------------------------------------------------------------------------- shots
CAMERAS = {
    # stand roughly 11 percent of frame height; centered lower-middle of a desk scene
    "wide": Camera(scale=1.6, cx=project((40.0, 0.0, 30.0))[0] - 6, cy=-105.0),
    # the tab and slot fill the frame
    "closeup": Camera(scale=32.0, cx=project((40.0, 12.0, 46.0))[0], cy=project((40.0, 20.0, 46.0))[1]),
    # three-quarter view of the phone on the locked stand
    "result": Camera(scale=8.5, cx=project((40.0, 55.0, 30.0))[0], cy=project((40.0, 60.0, 30.0))[1]),
}
SHOT_SECONDS = {"wide": 6.0, "closeup": 3.0, "result": 4.0}


def tab_drop_for(shot: str, t: float) -> float:
    if shot == "wide":
        return ease_in_out((t - 2.0) / 1.5)
    if shot == "closeup":
        if t < 0.3:
            return 0.0
        if t < 2.0:
            return ease_in_out((t - 0.3) / 1.7)
        if t < 2.2:
            return 1.0 - 0.015 * math.sin(math.pi * (t - 2.0) / 0.2)  # tiny settle, tab stays visible
        return 1.0
    return 1.0


def finger_state(shot: str, t: float, f: Frame, drop: float) -> tuple[Vec2, Vec2, float] | None:
    """Return (tip px, direction px, width units) or None when the finger is out of frame."""
    tab_top_y = BASE_Y - TAB_TRAVEL * drop + TAB_H
    if shot == "wide":
        # hand comes from the camera side (bottom right), presses, then leaves the same way
        if t < 1.4 or t > 4.05:
            return None
        contact = f.px((40.0, tab_top_y, TAB_Z + 1.0))
        offscreen = (f.width * f.ss * 0.95, f.height * f.ss * 1.25)
        if t < 2.0:
            a = ease_out((t - 1.4) / 0.6)
        elif t <= 3.7:
            a = 1.0
        else:
            a = 1 - ease_in_out((t - 3.7) / 0.35)
        tip = (offscreen[0] + (contact[0] - offscreen[0]) * a, offscreen[1] + (contact[1] - offscreen[1]) * a)
        return tip, (0.24, 0.97), 12.0
    if shot == "closeup":
        # fingertip from the upper right, touching only the top edge of the tab near its right end
        w_units = 8.0
        edge = f.px((TAB_X1 - 4.5, tab_top_y, TAB_Z))
        contact = (edge[0], edge[1] - f.unit(w_units / 2 - 0.3))
        away = (contact[0] + f.unit(30), contact[1] - f.unit(34))
        if t < 0.3:
            a = ease_out(t / 0.3)
        elif t <= 2.2:
            a = 1.0
        elif t < 2.7:
            a = 1 - ease_in_out((t - 2.2) / 0.5)
        else:
            return None
        tip = (away[0] + (contact[0] - away[0]) * a, away[1] + (contact[1] - away[1]) * a)
        return tip, (0.72, -0.75), w_units
    if shot == "result":
        taps = [(1.5, 1.9), (2.1, 2.5)]
        contact = f.px(phone_point(40.0, 50.0, -0.5))
        rest = (contact[0] + f.unit(10), contact[1] - f.unit(16))
        if t < 1.1 or t > 2.9:
            return None
        a = 0.0
        for start, end in taps:
            if start <= t <= end:
                u = (t - start) / (end - start)
                a = 1 - abs(2 * u - 1)  # down then up
                a = ease_in_out(a)
        if t < 1.5:
            a = 0.0
        tip = (rest[0] + (contact[0] - rest[0]) * a, rest[1] + (contact[1] - rest[1]) * a)
        return tip, (0.55, -1.0), 10.0
    return None


def result_tap_ripple(t: float) -> float | None:
    for start in (1.68, 2.28):
        if start <= t <= start + 0.35:
            return (t - start) / 0.35
    return None


def result_settle(t: float) -> float:
    for start in (1.68, 2.28):
        if start <= t <= start + 0.25:
            return -0.25 * math.sin(math.pi * (t - start) / 0.25)
    return 0.0


def render_frame(shot: str, t: float, width: int, height: int, ss: int = 2) -> Image.Image:
    cam = CAMERAS[shot]
    img = Image.new("RGB", (width * ss, height * ss), WALL)
    f = Frame(image=img, draw=ImageDraw.Draw(img), cam=cam, width=width, height=height, ss=ss)
    draw_background(f)
    draw_props(f)
    draw_base_top(f)
    draw_panel(f)
    draw_base_front_and_side(f)
    draw_slot(f)
    drop = tab_drop_for(shot, t)
    draw_tab(f, drop)
    draw_loops(f)
    draw_lip(f)
    if shot == "wide" and t >= 3.8:
        a = ease_out((t - 3.8) / 1.2)
        draw_phone(f, dx=60 * (1 - a), dy=90 * (1 - a))
    if shot == "result":
        draw_phone(f, ripple=result_tap_ripple(t), settle=result_settle(t))
    fs = finger_state(shot, t, f, drop)
    if fs is not None:
        tip, direction, w = fs
        if shot == "wide":
            draw_hand(f, tip, direction, w)
        else:
            draw_finger(f, tip, direction, w)
    if ss > 1:
        img = img.resize((width, height), Image.LANCZOS)
    return img


def frames_for(shot: str, width: int, height: int, fps: int) -> Iterator[bytes]:
    n = int(round(SHOT_SECONDS[shot] * fps))
    for i in range(n):
        t = i / fps
        yield render_frame(shot, t, width, height).tobytes()


def encode_video(shot: str, out_path: Path, width: int, height: int, fps: int) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-threads", "1", "-movflags", "+faststart", "-fflags", "+bitexact", "-flags", "+bitexact",
        "-map_metadata", "-1", str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    for frame in frames_for(shot, width, height, fps):
        proc.stdin.write(frame)
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed for {shot}")


# ----------------------------------------------------------------------------- audio
def write_music(out_path: Path, seconds: float = 12.0, sr: int = 44100) -> None:
    """Quiet deterministic pad: two alternating major-seventh chords, slow attack, peak -20 dBFS."""
    t = np.arange(int(seconds * sr)) / sr
    chords = [[146.83, 220.0, 277.18, 329.63], [196.0, 246.94, 293.66, 369.99]]  # Dmaj7, Gmaj7
    mix = np.zeros_like(t)
    seg = 3.0
    for k in range(int(math.ceil(seconds / seg))):
        chord = chords[k % 2]
        start, end = k * seg, min((k + 1) * seg, seconds)
        mask = (t >= start) & (t < end)
        local = t[mask] - start
        env = np.minimum(local / 0.9, 1.0) * np.minimum((end - start - local) / 0.9, 1.0)
        env = np.clip(env, 0.0, 1.0)
        for i, freq in enumerate(chord):
            detune = 1.0 + 0.0008 * (i - 1.5)
            sine = np.sin(2 * np.pi * freq * detune * t[mask])
            tri = 2 / np.pi * np.arcsin(np.sin(2 * np.pi * freq * 0.5 * t[mask]))
            mix[mask] += env * (0.7 * sine + 0.3 * tri) / len(chord)
    fade = np.minimum(t / 1.5, 1.0) * np.minimum((seconds - t) / 1.5, 1.0)
    mix *= np.clip(fade, 0.0, 1.0)
    peak = np.max(np.abs(mix)) or 1.0
    mix = mix / peak * 0.1  # -20 dBFS
    pcm = (mix * 32767).astype("<i2")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def make_narration(out_path: Path, prefer: str) -> SpeechResult:
    provider = default_speech_provider(prefer=prefer)
    try:
        return provider.synthesize(NARRATION_TEXT, out_path)
    except SpeechError as exc:
        if prefer == "say" or provider.name == "macos_say":
            raise
        print(f"ElevenLabs failed ({exc}); falling back to macOS say", file=sys.stderr)
        return default_speech_provider(prefer="say").synthesize(NARRATION_TEXT, out_path)


NARRATION_SIDECAR = "narration.json"


def existing_narration(out: Path) -> tuple[Path, dict] | None:
    """Return (wav path, sidecar) when the pack already holds a narration for the exact script."""
    sidecar_path = out / NARRATION_SIDECAR
    if not sidecar_path.exists():
        return None
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if sidecar.get("text") != NARRATION_TEXT:
        return None
    wav = out / "assets" / str(sidecar.get("storage_key", ""))
    return (wav, sidecar) if wav.is_file() else None


def reuse_narration(src: Path, out_path: Path, sidecar: dict | None) -> SpeechResult:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out_path)
    meta = sidecar or {}
    return SpeechResult(
        provider=str(meta.get("provider", "reused_wav")),
        model=str(meta.get("model", "unknown")),
        duration_ms=ffprobe_stream(out_path).duration_ms or 0,
        sample_rate=44100,
        path=out_path,
    )


# ----------------------------------------------------------------------------- probing
def ffprobe_stream(path: Path) -> StreamInfo:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    info = StreamInfo(container=data.get("format", {}).get("format_name"), size_bytes=int(data["format"].get("size", 0)))
    dur = data.get("format", {}).get("duration")
    if dur:
        info.duration_ms = int(round(float(dur) * 1000))
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and info.width is None:
            info.width, info.height = int(s["width"]), int(s["height"])
            num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
            info.fps = float(num) / float(den) if float(den) else None
            info.video_codec = s.get("codec_name")
            info.nb_frames = int(s["nb_frames"]) if str(s.get("nb_frames", "")).isdigit() else None
            rot = 0
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rot = int(sd["rotation"])
            info.rotation = rot
        elif s.get("codec_type") == "audio":
            info.has_audio = True
            info.audio_codec = s.get("codec_name")
            info.audio_sample_rate = int(s.get("sample_rate", 0)) or None
            info.audio_channels = int(s.get("channels", 0)) or None
    return info


# ----------------------------------------------------------------------------- pack records
PROJECT_ID = "proj_pack_a"


def build_truth() -> SourceTruth:
    return SourceTruth(
        id="truth_pack_a",
        version=1,
        is_fictional=True,
        title="Cereal-box phone stand with a locking tab",
        summary=(
            "A fictional cardboard phone stand. An orange tab on the back panel slides down through two "
            "guide loops into a slot in the base, which locks the panel angle. Once locked, a phone rests "
            "against the panel and stays in place when the screen is tapped."
        ),
        claims=[
            Claim(id="c_object", text="The object being demonstrated is a phone stand.", kind=ClaimKind.ENTITY,
                  supporting_asset_ids=["asset_wide", "asset_result"]),
            Claim(id="c_material", text="The stand is made from a cereal box (cardboard).", kind=ClaimKind.FACT,
                  supporting_asset_ids=["asset_narration"]),
            Claim(id="c_moving_part", text="The small orange tab is the part that moves during locking.",
                  kind=ClaimKind.MECHANISM, supporting_asset_ids=["asset_closeup", "asset_wide"]),
            Claim(id="c_lock_action", text="Locking is done by pushing the orange tab down into a slot in the base.",
                  kind=ClaimKind.MECHANISM, supporting_asset_ids=["asset_closeup", "asset_wide"]),
            Claim(id="c_tab_location", text="After locking, the tab sits inside the slot in the base.",
                  kind=ClaimKind.OUTCOME, supporting_asset_ids=["asset_closeup"]),
            Claim(id="c_result_stable", text="After locking, the phone stays in place when the screen is tapped.",
                  kind=ClaimKind.OUTCOME, supporting_asset_ids=["asset_result", "asset_narration"]),
        ],
        entities=["phone stand", "orange locking tab", "slot in the base", "phone"],
        event_order=["c_object", "c_moving_part", "c_lock_action", "c_tab_location", "c_result_stable"],
        source_references=[SourceReference(label="authored fixture", note="Fictional product authored for this pack; no external source.")],
        approved_by="leon",
        approved_at="2026-09-12T00:00:00Z",
    )


def build_brief() -> CreativeBrief:
    return CreativeBrief(
        id="brief_pack_a",
        project_id=PROJECT_ID,
        profile=GoalProfile.PRODUCT_DEMO,
        objective="Make it immediately clear how the stand locks and that the phone stays put.",
        audience="people who make things at home",
        language="en",
        aspect_ratio="9:16",
        target_duration_ms_min=8000,
        target_duration_ms_max=12000,
        required_information=[
            RequiredInformation(id="r_object", text="What the object is", claim_ids=["c_object"]),
            RequiredInformation(id="r_lock", text="How the stand locks: the tab goes down into the slot",
                                claim_ids=["c_moving_part", "c_lock_action", "c_tab_location"]),
            RequiredInformation(id="r_stable", text="The phone stays put when tapped", claim_ids=["c_result_stable"]),
        ],
        style_notes="Plain maker-video feel. No title cards, no CTA.",
        protected_constraints=[
            ProtectedConstraint(id="pc_narration", kind=ConstraintKind.KEEP_NARRATION, reason="The creator's voiceover is final."),
            ProtectedConstraint(id="pc_music", kind=ConstraintKind.KEEP_MUSIC, reason="Keep the original music bed."),
            ProtectedConstraint(id="pc_product", kind=ConstraintKind.PRESERVE_PRODUCT_APPEARANCE,
                                reason="Do not change how the stand looks."),
            ProtectedConstraint(id="pc_facts", kind=ConstraintKind.NO_NEW_FACTS, reason="Do not invent features."),
            ProtectedConstraint(id="pc_duration", kind=ConstraintKind.MAX_DURATION_MS, value_ms=12000, reason="Short-form limit."),
        ],
        allowed_actions=[
            RepairAction.TRIM_OR_RETIME,
            RepairAction.REORDER_SEGMENTS,
            RepairAction.REPLACE_WITH_EXISTING_ASSET,
            RepairAction.CROP_EXISTING_SHOT,
            RepairAction.REVISE_CAPTIONS,
            RepairAction.GENERATE_MISSING_SHOT,
        ],
        generation_permitted=True,
        narration_change_permitted=False,
        budget=Budget(),
        review_status="approved",
        approved_by="leon",
    )


def build_suite() -> EvaluationSuite:
    ns = ProbeOption(id="not_shown", text="Not shown or cannot tell from the video")

    def q(qid: str, text: str, modality: EvidenceModality, options: list[tuple[str, str]], correct: str,
          claims: list[str], guard: bool = False) -> ProbeQuestion:
        return ProbeQuestion(
            id=qid, text=text, modality=modality,
            options=[ProbeOption(id=i, text=t) for i, t in options] + [ns],
            correct_option_id=correct, claim_ids=claims, regression_guard=guard,
        )

    return EvaluationSuite(
        id="suite_pack_a_dev",
        version=1,
        split=SuiteSplit.DEV,
        story_family="stand_demo",
        questions=[
            q("q_lock_action", "What physical action locks the stand?", EvidenceModality.VISUAL,
              [("o1", "The orange tab is slid sideways along the base into a groove"),
               ("o2", "The orange tab is pushed straight down into a slot in the base"),
               ("o3", "The orange tab is twisted a quarter turn until it locks"),
               ("o4", "The orange tab is folded over the top edge of the phone")],
              "o2", ["c_lock_action"]),
            q("q_tab_location", "After the stand is locked, where is the orange tab?", EvidenceModality.VISUAL,
              [("o1", "Inside a slot in the base"),
               ("o2", "Sticking up above the back panel"),
               ("o3", "Lying flat along the top surface of the base"),
               ("o4", "Clipped over the top edge of the phone")],
              "o1", ["c_tab_location"]),
            q("q_moving_part", "Which part of the stand moves while it is being locked?", EvidenceModality.VISUAL,
              [("o1", "The whole back panel swings"),
               ("o2", "The base slides forward"),
               ("o3", "A small orange tab"),
               ("o4", "The front lip flips up")],
              "o3", ["c_moving_part"]),
            q("q_material", "What is the stand made from?", EvidenceModality.EITHER,
              [("o1", "A cereal box (cardboard)"),
               ("o2", "Wood"),
               ("o3", "3D-printed plastic"),
               ("o4", "Bent metal")],
              "o1", ["c_material"], guard=True),
            q("q_object", "What kind of object is being demonstrated?", EvidenceModality.EITHER,
              [("o1", "A phone case"),
               ("o2", "A phone stand"),
               ("o3", "A wireless charger"),
               ("o4", "A small speaker")],
              "o2", ["c_object"], guard=True),
            q("q_result_stable", "What happens when the screen is tapped after the stand is locked?", EvidenceModality.EITHER,
              [("o1", "The phone stays in place"),
               ("o2", "The stand tips over"),
               ("o3", "The phone slides off"),
               ("o4", "The back panel folds flat")],
              "o1", ["c_result_stable"], guard=True),
        ],
    )


def build_baseline_plan(narration_ms: int, fps: int) -> EditPlan:
    """The creator's ordinary first cut: wide shot then result shot. Not sabotaged."""
    wide_out = 5500
    result_out = 4000
    # keep the whole voiceover inside the timeline; the wide clip is 6.0 s long
    if narration_ms > wide_out + result_out:
        wide_out = min(6000, narration_ms - result_out + 100)
    return EditPlan(
        output=OutputProfile(width=720, height=1280, fps_num=fps, fps_den=1),
        segments=[
            Segment(id="seg_wide", asset_id="asset_wide", source_in_ms=0, source_out_ms=wide_out, fit="cover", label="wide shot"),
            Segment(id="seg_result", asset_id="asset_result", source_in_ms=0, source_out_ms=result_out, fit="cover", label="result shot"),
        ],
        captions=[
            Caption(id="cap_01", text="Cereal-box phone stand", start_ms=300, end_ms=2500),
            Caption(id="cap_02", text="Locks in one move", start_ms=3000, end_ms=5000),
            Caption(id="cap_03", text="Holds when you tap", start_ms=6500, end_ms=9200),
        ],
        narration=NarrationTrack(asset_id="asset_narration", offset_ms=0),
        music=MusicTrack(asset_id="asset_music", gain_db=-16.0),
        change_rationale="Creator's first cut.",
    )


def asset_record(asset_id: str, kind: AssetKind, path: Path, label: str, claims: list[str]) -> AssetRecord:
    content_hash = sha256_file(path)
    stream = ffprobe_stream(path)
    return AssetRecord(
        id=asset_id,
        project_id=PROJECT_ID,
        kind=kind,
        origin=AssetOrigin.RENDERED_FIXTURE,
        content_hash=content_hash,
        storage_key=f"{content_hash}{path.suffix}",
        original_filename=path.name,
        label=label,
        duration_ms=stream.duration_ms,
        stream=stream,
        declared_claims=claims,
        rights_note="Procedurally rendered for this project; no third-party material",
        created_at=utc_now_iso(),
    )


def write_pack_md(
    root: Path, speech: SpeechResult, width: int, height: int, fps: int, durations: dict[str, int], narration_reused: bool
) -> None:
    reuse_note = (
        "Reused from the previous generation of this pack (hash unchanged, no new synthesis)."
        if narration_reused
        else "Synthesized during this generation."
    )
    text = f"""# Pack A: cereal-box phone stand (rendered fixture), fixture v2

Status: fictional graphical demonstration, procedurally rendered by `scripts/make_pack_a.py`.
There is no real product and no recorded footage in this pack. The spec (section 6) allows a
clearly labeled fictional graphical demo when recorded footage is unavailable; this is that case.

## Provenance and rights

- Every video asset is drawn with Pillow from a deterministic scene description and encoded with
  ffmpeg (libx264, single-threaded, bitexact muxing). No third-party images, fonts, or clips.
- Music is a synthesized pad (numpy), 12 s, peak -20 dBFS. No licensed audio.
- Narration: provider `{speech.provider}`, model/voice `{speech.model}`, {speech.duration_ms} ms. {reuse_note}
  Script (exact): "{NARRATION_TEXT}"
  The voiceover deliberately does not explain the mechanism verbally, which is how first drafts
  commonly go; the visuals have to carry that information.
- Rendered at {width}x{height}, {fps} fps. Durations: {json.dumps(durations)} (ms).

## What each shot establishes

- wide shot: the whole desk from a desk-level phone-camera angle. The stand is about 11 percent of
  frame height and the orange tab about 1 percent. A hand reaches in from the camera side and
  presses at the base of the stand (2.0-3.7 s); from this viewpoint the hand covers the tab and the
  slot for the whole press, and when it withdraws the tab is already seated inside the base, so the
  after-state shows nothing where the tab was. A viewer of this shot sees: a hand touches the base,
  the hand leaves, a phone is placed (3.8-5.0 s). Whether the tab was pushed down, slid, twisted or
  folded is genuinely not visible. This is the realistic case the loop is meant to catch, not a
  sabotaged edit: the mechanism was filmed, just from an angle that hides it.
- close-up: the tab, its two guide loops and the slot fill the frame; a fingertip arrives from the
  upper right and touches only the top edge of the tab, so the tab body, loops and slot stay
  visible beside it while the tab is pressed down and seats into the slot (0.3-2.2 s).
- result shot: the phone on the locked stand; a fingertip taps the screen twice at 1.5 s and 2.1 s
  and nothing moves.
- The baseline edit uses only the wide and result shots. The close-up exists in the asset pool but
  is not used, which is the ordinary first-cut situation the loop is meant to catch.

## Fixture history

- v1: the wide shot showed the fingertip beside the tab; a frame-based viewer (8 frames at 512 px)
  could still read "a finger pushes the small orange tab down into the base", so the wide-shot
  baseline passed the mechanism questions. The no-media control was clean, so the questions did
  not leak; the fixture was simply too legible.
- v2 (current): hand occludes the mechanism in the wide shot as described above; close-up angle
  changed so the mechanism stays visible beside the fingertip; suite options rebalanced so every
  option describes a plausible cardboard mechanism and only the pixels decide.

## Replacing the fixture with real footage

Record with a phone in portrait, steady, good light, no music playing:

1. wide: the whole desk with the stand small in frame; perform the lock, then place the phone.
2. close-up: fill the frame with the locking mechanism; perform the lock slowly.
3. result: three-quarter view of the phone on the locked stand; tap the screen twice.
4. Record the voiceover separately (or reuse this script) as a mono WAV.

Then copy the files into `assets/` named by their sha256 (`shasum -a 256 file`), update
`assets/manifest.json` (origin `recorded`, rights note, declared claims), keep `brief.json`,
`source_truth.json` and `suite.json` if the mechanism is the same, and run
`.venv/bin/python -c "from directorloop.domain import write_checksums; write_checksums('packs/pack_a_stand_demo')"`.

## Integrity

`checksums.json` lists the sha256 of every JSON file and media asset. `load_pack` verifies them.
Narration from ElevenLabs is not bit-reproducible between generations, so regenerating the pack
changes the narration hash and rewrites `checksums.json`; the video and music hashes are stable.
"""
    (root / "PACK.md").write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------------- main
def generate(
    out: Path,
    width: int,
    height: int,
    fps: int,
    speech_pref: str,
    quiet: bool = False,
    narration_wav: Path | None = None,
    resynthesize: bool = False,
) -> Path:
    out = Path(out)
    assets_dir = out / "assets"

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    # Keep an existing narration (or an explicit WAV) so its hash stays stable and no TTS credit is spent.
    reuse_src: Path | None = None
    reuse_meta: dict | None = None
    keep_dir = Path(tempfile.mkdtemp(prefix="packa_narration_"))
    if narration_wav is not None:
        reuse_src = keep_dir / "narration.wav"
        shutil.copyfile(narration_wav, reuse_src)
        found = existing_narration(out)
        reuse_meta = found[1] if found and found[0].read_bytes() == reuse_src.read_bytes() else None
    elif not resynthesize:
        found = existing_narration(out)
        if found:
            reuse_src = keep_dir / "narration.wav"
            shutil.copyfile(found[0], reuse_src)
            reuse_meta = found[1]

    if out.exists():
        shutil.rmtree(out)
    assets_dir.mkdir(parents=True)
    tmp = out / "_build"
    tmp.mkdir()

    durations: dict[str, int] = {}
    records: list[AssetRecord] = []
    for shot in ("wide", "closeup", "result"):
        path = tmp / f"{shot}.mp4"
        log(f"rendering {shot} ({SHOT_SECONDS[shot]} s at {width}x{height}, {fps} fps)")
        encode_video(shot, path, width, height, fps)
        label = {"wide": "wide shot", "closeup": "close-up shot", "result": "result shot"}[shot]
        claims = {
            "wide": ["c_object", "c_moving_part", "c_lock_action"],
            "closeup": ["c_moving_part", "c_lock_action", "c_tab_location"],
            "result": ["c_object", "c_result_stable"],
        }[shot]
        rec = asset_record(f"asset_{shot}", AssetKind.VIDEO, path, label, claims)
        durations[shot] = rec.duration_ms or 0
        records.append(rec)

    narr_path = tmp / "narration.wav"
    narration_reused = reuse_src is not None
    if reuse_src is not None:
        log("reusing existing narration WAV (no speech synthesis)")
        speech = reuse_narration(reuse_src, narr_path, reuse_meta)
    else:
        log(f"synthesizing narration ({speech_pref})")
        speech = make_narration(narr_path, speech_pref)
    shutil.rmtree(keep_dir, ignore_errors=True)
    rec = asset_record("asset_narration", AssetKind.AUDIO, narr_path, "narration", ["c_material", "c_object", "c_result_stable"])
    durations["narration"] = rec.duration_ms or 0
    narration_record = rec
    records.append(rec)

    log("writing music bed")
    music_path = tmp / "music.wav"
    write_music(music_path)
    rec = asset_record("asset_music", AssetKind.AUDIO, music_path, "music bed", [])
    durations["music"] = rec.duration_ms or 0
    records.append(rec)

    for rec in records:
        src = tmp / (rec.original_filename or "")
        shutil.move(str(src), str(assets_dir / rec.storage_key))
        rec.original_filename = None  # keep names out of the manifest; labels are neutral
    shutil.rmtree(tmp)
    (out / NARRATION_SIDECAR).write_text(
        json.dumps(
            {
                "text": NARRATION_TEXT,
                "provider": speech.provider,
                "model": speech.model,
                "duration_ms": durations["narration"],
                "asset_id": narration_record.id,
                "storage_key": narration_record.storage_key,
                "reused": narration_reused,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    manifest = AssetManifest(project_id=PROJECT_ID, assets=records,
                             rights_statement="All assets rendered or synthesized for this project. Fictional product.")
    (assets_dir / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json"), indent=2), encoding="utf-8")
    (out / "brief.json").write_text(json.dumps(build_brief().model_dump(mode="json"), indent=2), encoding="utf-8")
    (out / "source_truth.json").write_text(json.dumps(build_truth().model_dump(mode="json"), indent=2), encoding="utf-8")
    (out / "suite.json").write_text(json.dumps(build_suite().model_dump(mode="json"), indent=2), encoding="utf-8")
    plan = build_baseline_plan(durations["narration"], fps)
    (out / "baseline_plan.json").write_text(json.dumps(plan.model_dump(mode="json"), indent=2), encoding="utf-8")
    write_pack_md(out, speech, width, height, fps, durations, narration_reused)
    write_checksums(out)
    pack = load_pack(out, verify_checksums=True)
    log(f"pack ok: {len(pack.manifest.assets)} assets, baseline timeline {plan.timeline_duration_ms()} ms, "
        f"narration {durations['narration']} ms via {speech.provider}/{speech.model}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO_ROOT / "packs" / "pack_a_stand_demo"))
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--speech", choices=["auto", "elevenlabs", "say"], default="auto")
    ap.add_argument("--narration-wav", type=Path, default=None,
                    help="Reuse this WAV as the narration instead of synthesizing. Default: reuse the pack's "
                         "existing narration when its recorded script matches.")
    ap.add_argument("--resynthesize-narration", action="store_true",
                    help="Ignore any existing narration and synthesize a fresh one (spends TTS credit).")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    generate(
        Path(args.out), args.width, args.height, args.fps, args.speech, quiet=args.quiet,
        narration_wav=args.narration_wav, resynthesize=args.resynthesize_narration,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
