"""Phase T3 on synthetic frames: matchups, man or zone, ball screens and their coverage,
help on drives, and the labeling strips."""

from __future__ import annotations

import struct
import zlib

import numpy as np
import pytest

from courtvision.tracking import defense, detect, labels
from courtvision.tracking.detect import HOOPS

HOOP = HOOPS[1]  # the offense (home, slots 0-4) attacks the right hoop


def _floor(xy, ball, hold=None):
    """A Floor over synthetic frames: home on offense the whole time, attacking hoop 1."""
    n = len(xy)
    g = detect.Game(period=np.ones(n, int), clock=600.0 - 0.04 * np.arange(n),
                    shot_clock=np.full(n, 20.0), ball=ball, xy=xy.astype(np.float32),
                    ids=np.tile(np.arange(1, 11), (n, 1)), ts=np.arange(n) * 40)  # fmt: skip
    f = defense.Floor.__new__(defense.Floor)
    f.g, f.n = g, n
    f.d = detect.ball_distances(g)
    f.hold = np.full(n, 0) if hold is None else hold
    f.run = np.ones(n, bool)
    f.poss = np.zeros(n, np.int8)
    f.hoops = {(1, 0): 1, (1, 1): 0}
    f.hoop = np.ones(n, int)
    f.half = np.ones(n, bool)
    f.start = np.zeros(n, int)
    return f


def _ideal(att, ball):
    return defense.W_MAN * att + defense.W_BALL * ball + defense.W_HOOP * HOOP


def _man_frames(n=200, cross=False):
    """Five attackers spread around the arc; each defender at his man's ideal point (defender
    k guards attacker (k + 2) % 5). With ``cross``, attacker 0 cuts across the floor."""
    att = np.zeros((n, 5, 2))
    base = np.array([[70, 5], [66, 15], [64, 25], [66, 35], [70, 45]], float)
    att[:] = base
    if cross:
        att[:, 0, 1] = np.linspace(5, 45, n)
    ball = np.tile([64.0, 25.0], (n, 1))
    xy = np.zeros((n, 10, 2))
    xy[:, :5] = att
    for k in range(5):
        xy[:, 5 + k] = _ideal(att[:, (k + 2) % 5], ball)
    return xy, np.column_stack([ball, np.full(n, 4.0)])


def test_matchups_follow_the_ideal_points():
    xy, ball = _man_frames()
    xy[:, 5:] += np.random.default_rng(0).normal(0, 1.0, xy[:, 5:].shape)
    a = defense.matchups(_floor(xy, ball))
    assert (a == (np.arange(5) + 2) % 5).mean() > 0.98


def test_fit_weights_recovers_the_mix():
    xy, ball = _man_frames(n=400, cross=True)
    xy[:, 1:5, 1] += np.sin(np.arange(400) / 30)[:, None] * 3  # everyone moves a little
    for k in range(5):
        xy[:, 5 + k] = _ideal(xy[:, (k + 2) % 5], ball[:, :2])
    w = defense.fit_weights([_floor(xy, ball)], rounds=2)
    assert np.allclose(w, (defense.W_MAN, defense.W_BALL, defense.W_HOOP), atol=0.02)


def test_man_follows_a_cutter_and_zone_does_not():
    xy, ball = _man_frames(n=200, cross=True)
    man = defense.man_or_zone(xy[:, :5], xy[:, 5:], ball[:, :2], np.tile(HOOP, (200, 1)),
                              np.tile((np.arange(5) + 2) % 5, (200, 1)))  # fmt: skip
    zone_xy = xy.copy()
    zone_xy[:, 5:] = zone_xy[0, 5:]  # defenders hold their spots while the cutter crosses
    guard = np.tile((np.arange(5) + 2) % 5, (200, 1))
    zone = defense.man_or_zone(zone_xy[:, :5], zone_xy[:, 5:], ball[:, :2],
                               np.tile(HOOP, (200, 1)), guard)  # fmt: skip
    assert man["follow"] > 0.5 and zone["follow"] < 0.1
    assert zone["zone_fit"] > man["zone_fit"]


def _screen(kind: str):
    """A ball screen at the top: handler (slot 0) dribbles right past the screener (slot 1)
    at frame 60. His man is slot 5, the screener's man slot 6; the others stand far away."""
    n = 160
    t = np.arange(n)
    xy = np.zeros((n, 10, 2))
    xy[:, 2:5] = [[80, 3], [80, 47], [86, 10]]
    xy[:, 7:10] = [[82, 5], [82, 45], [86, 14]]
    hy = np.clip(18 + (t - 20) * 0.3, 18, 40)  # the handler slides right, past y 25 at ~43
    xy[:, 0] = np.column_stack([np.full(n, 66.0), hy])
    xy[:, 1] = [66.5, 25.0]  # the screener, set at the top
    xy[:, 5] = xy[:, 0] + [-2.5, 0]  # the handler's man, on his top side
    xy[:, 6] = [70.0, 25.0]  # screener's man, a bit below him (toward the hoop)
    if kind == "switch":
        after = t >= 70
        xy[after, 6] = xy[after, 0] + [1.5, 0]  # the screener's man takes the handler
        xy[after, 5] = [65.0, 25.0]  # the handler's man takes the screener
    elif kind == "drop":
        xy[:, 6] = [76.0, 25.0]  # stays back near the paint
    elif kind == "blitz":
        both = (t >= 60) & (t <= 100)
        xy[both, 6] = xy[both, 0] + [1.5, 1.0]
        xy[both, 5] = xy[both, 0] + [-1.5, -1.0]
    ball = np.column_stack([xy[:, 0] + [0.5, 0], np.full(n, 3.0)])
    hold = np.zeros(n, int)
    return xy, ball, hold


@pytest.mark.parametrize("kind", ["switch", "drop", "blitz"])
def test_screen_coverage(kind):
    xy, ball, hold = _screen(kind)
    f = _floor(xy, ball, hold)
    # defender slot 5 guards the handler (attacker 0), slot 6 the screener, and so on
    assign = np.tile(np.arange(5, dtype=np.int8), (f.n, 1))
    s = defense.screens(f, assign)
    assert len(s) == 1
    assert s.coverage.iloc[0] == kind


def test_drive_with_help_and_rotation():
    n = 100
    xy = np.zeros((n, 10, 2))
    xy[:, 1:5] = [[70, 5], [70, 45], [85, 3], [85, 47]]
    xy[:, 6:10] = [[72, 7], [72, 43], [86, 6], [86, 44]]
    xy[:, 0, 0] = np.linspace(66, 84, n)  # the handler drives at the hoop
    xy[:, 0, 1] = 25
    xy[:, 5] = xy[:, 0] + [-2, 0]  # beaten, trailing
    k = np.clip((np.arange(n) - 30) / 35, 0, 1)[:, None]  # frames 30-65: the corner man's
    xy[:, 9] = (1 - k) * np.array([86, 44]) + k * np.array([80, 27])  # defender helps
    xy[78:, 4] = [83, 44]  # the open corner man steps in for the kick-out ...
    xy[90:, 7] = [83, 41]  # ... and his old teammate's defender rotates over
    ball = np.column_stack([xy[:, 0] + [0.5, 0], np.full(n, 3.0)])
    hold = np.zeros(n, int)
    hold[78:] = 4  # the kick-out to the corner
    ball[78:, :2] = xy[78:, 4]
    f = _floor(xy, ball, hold)
    assign = np.tile(np.array([0, 1, 2, 3, 4], np.int8), (n, 1))
    d = defense.drives(f, assign)
    assert len(d) == 1 and bool(d.helped.iloc[0]) and d.helper.iloc[0] == 10
    assert bool(d.kick_out.iloc[0]) and d.open_ft.iloc[0] > 6
    assert abs(d.closeout_s.iloc[0] - 12 / 25) < 0.05


def test_strip_is_a_valid_png():
    c = labels.Canvas(40, 30)
    c.disc(10, 10, 5, (200, 0, 0))
    c.text("23", 25, 15, (0, 0, 0))
    png = c.png()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (40, 30)
    idat = png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert len(raw) == h * (1 + 3 * w)
    assert c.a[10, 10].tolist() == [200, 0, 0]


def test_drawing_off_the_canvas_paints_nothing():
    c = labels.Canvas(40, 30)
    before = c.a.copy()
    c.square(20, -50, 5, (0, 0, 255))  # far above the panel: a negative slice end once
    c.text("12", 20, -60, (0, 0, 0))  # painted a bar across the whole canvas
    c.square(-50, 10, 5, (0, 0, 255))
    assert np.array_equal(c.a, before)
