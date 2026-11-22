"""Events found in the tracking itself: who has the ball, and every shot's flight.

Positions are in feet on a 94 x 50 court with the hoops at (5.25, 25) and (88.75, 25); the
rim is 10 ft up. Everything here works on plain arrays, so the same code runs on a replayed
game and, frame by frame, on a live one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from courtvision.tracking.sportvu import SLOTS

HOOPS = np.array([[5.25, 25.0], [88.75, 25.0]])
REACH = 3.0  # ft: the ball this close to a player (and low enough) is in his hands
HOLD_TOP = 9.0  # ft: above this the ball is in the air
FPS = 25


@dataclass
class Game:
    """One game's frames as arrays (n frames)."""

    period: np.ndarray  # (n,)
    clock: np.ndarray  # (n,) game clock, seconds left in the quarter
    shot_clock: np.ndarray  # (n,)
    ball: np.ndarray  # (n, 3) x, y, z
    xy: np.ndarray  # (n, 10, 2) players, slots h0-h4 then a0-a4
    ids: np.ndarray  # (n, 10) person ids (0 = empty slot)
    ts: np.ndarray  # (n,) ms

    @classmethod
    def from_frames(cls, f: pd.DataFrame) -> Game:
        xy = np.stack([f[[f"{s}_x", f"{s}_y"]].to_numpy(np.float32) for s in SLOTS], axis=1)
        ids = np.stack([f[f"{s}_id"].to_numpy(np.int64) for s in SLOTS], axis=1)
        return cls(period=f.period.to_numpy(), clock=f.game_clock.to_numpy(np.float64),
                   shot_clock=f.shot_clock.to_numpy(np.float64),
                   ball=f[["ball_x", "ball_y", "ball_z"]].to_numpy(np.float64), xy=xy,
                   ids=ids, ts=f.ts.to_numpy())  # fmt: skip


def ball_distances(g: Game) -> np.ndarray:
    """(n, 10) horizontal distance from the ball to each player."""
    d = np.hypot(g.xy[:, :, 0] - g.ball[:, None, 0], g.xy[:, :, 1] - g.ball[:, None, 1])
    return np.where(g.ids > 0, d, np.inf)


# a ball held up high (dunks, layups, jump shots at their peak): arm's length from the torso
# the tracking follows, moving with the player
HIGH_REACH, HIGH_TOP, HIGH_FRAMES = 3.0, 11.5, 3
TIP_REACH = 2.0  # a one-frame touch counts as a tip only this close
CO_MOVING = 6.0  # ft/s: a held ball moves with the player (a ball in flight does 15-35)


def _velocity(a: np.ndarray) -> np.ndarray:
    """Per-frame velocity (ft/s) along the first axis, by central differences."""
    v = np.zeros_like(a, dtype=np.float64)
    v[1:-1] = (a[2:] - a[:-2]) * (FPS / 2)
    v[0], v[-1] = v[1], v[-2]
    return v


def holder(g: Game, d: np.ndarray | None = None) -> np.ndarray:
    """Slot (0-9) of the player holding or dribbling the ball each frame, -1 if nobody.

    Held: within ``REACH`` and below ``HOLD_TOP``; or, up high (to ``HIGH_TOP``, as on dunks,
    tips and jump shots at their peak), within ``HIGH_REACH`` of the same player, moving with
    him (relative speed under ``CO_MOVING``), for at least ``HIGH_FRAMES`` frames in a row,
    so a ball merely flying over someone doesn't count."""
    d = ball_distances(g) if d is None else d
    k = np.argmin(d, axis=1)
    rows = np.arange(len(k))
    dk = d[rows, k]
    z = g.ball[:, 2]
    low = (dk < REACH) & (z < HOLD_TOP)
    rel = np.hypot(*(_velocity(g.ball[:, :2]) - _velocity(g.xy)[rows, k]).T)
    high = (dk < HIGH_REACH) & (z < HIGH_TOP) & (rel < CO_MOVING) & ~low
    # keep high frames only in runs of the same player at least HIGH_FRAMES long
    keep = np.zeros(len(k), bool)
    i, n = 0, len(k)
    while i < n:
        if not high[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and high[j + 1] and k[j + 1] == k[i]:
            j += 1
        if j - i + 1 >= HIGH_FRAMES:
            keep[i : j + 1] = True
        i = j + 1
    return np.where(low | keep, k, -1)


def possessor(
    hold: np.ndarray, j: int, back: int = 12, team_back: int = 2 * FPS, d: np.ndarray | None = None
) -> int:
    """The player with the ball at ``j``: first the team that had it most over the
    ``team_back`` frames up to ``j`` (slots 0-4 home, 5-9 away), then the last player of
    that team to hold it for 2+ frames in a row within ``back`` frames (so on a quick
    catch-and-shoot it's the shooter, not the passer), else the teammate nearest the ball. A
    defender contesting at the rim is never the shooter. -1 if nobody had it."""
    w = hold[max(j - team_back, 0) : j + 1]
    w = w[w >= 0]
    if not len(w):
        return -1
    team = int(np.bincount(w // 5, minlength=2).argmax())
    recent = hold[max(j - back, 0) : j + 1]
    mine = (recent >= 0) & (recent // 5 == team)
    for i in range(len(recent) - 1, 0, -1):
        if mine[i] and mine[i - 1] and recent[i] == recent[i - 1]:
            return int(recent[i])
    if mine.any():
        return int(np.bincount(recent[mine]).argmax())
    if d is not None:
        return team * 5 + int(np.argmin(d[j, team * 5 : team * 5 + 5]))
    return int(np.bincount(w[w // 5 == team]).argmax())


def clock_running(g: Game) -> np.ndarray:
    """Whether the game clock is running at each frame (it moved over the last half second)."""
    run = np.zeros(len(g.clock), bool)
    lag = FPS // 2
    run[lag:] = (g.clock[:-lag] - g.clock[lag:] > 0.2) & (g.period[:-lag] == g.period[lag:])
    run[:lag] = run[lag] if len(run) > lag else False
    return run


def hoop_distance(xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance to the nearer hoop and which hoop (0 left, 1 right), for points (..., 2)."""
    d = np.hypot(xy[..., None, 0] - HOOPS[:, 0], xy[..., None, 1] - HOOPS[:, 1])
    return d.min(axis=-1), d.argmin(axis=-1)


def refine_release(d: np.ndarray, j: int, k: int) -> int:
    """The ball stays within reach for the first frames of its flight: step back from ``j``
    while it was steadily moving away from player ``k`` to the frame it actually left him."""
    while j > 0 and d[j, k] > 1.0 and d[j - 1, k] < d[j, k] - 0.2:
        j -= 1
    return j


def releases_by(g: Game, hold: np.ndarray, person_id: int, lo: int, hi: int) -> list[int]:
    """Frames in [lo, hi) where ``person_id`` lets go of the ball and it then rises at least
    a foot within 0.4 s and gets closer to a hoop: his possible shot releases."""
    out = []
    z = g.ball[:, 2]
    for j in range(max(lo, 0), min(hi, len(hold) - 10)):
        k = hold[j]
        if k < 0 or g.ids[j, k] != person_id or hold[j + 1] == k:
            continue
        e = j + 10
        if g.period[e] != g.period[j]:
            continue
        d0, hoop = hoop_distance(g.ball[j, :2])
        d1 = float(np.hypot(*(g.ball[e, :2] - HOOPS[int(hoop)])))
        if np.nanmax(z[j : e + 1]) - z[j] >= 1.0 and d1 < d0:
            out.append(j)
    return out


def shots(g: Game) -> pd.DataFrame:
    """Every shot found in the ball's flight, with its release and shooter.

    Two kinds of candidate:

    - **rim**: the ball within 2 ft (horizontally) of a hoop at 7.5-11.5 ft, walked back to
      the last frame, within 4 s, in which a player had it (the release). A new arrival needs
      half a second away from the rim and a touch in between (a tip or putback), or a full
      second away;
    - **launch**: shots that never reach the rim (blocked shots, air balls): the ball leaves a
      player and within 0.4 s rises 1.5 ft and gets 1.5 ft closer to a hoop he's within 35 ft
      of. Launches within half a second of a rim candidate's release are the same shot.

    The shooter is whoever had the ball most over the half second up to the release
    (``possessor``). Shots with the clock stopped and the shooter at the free-throw line are
    marked as free throws. Passes can look like launches; the play-by-play match sorts them.
    """
    d = ball_distances(g)
    hold = holder(g, d)
    run = clock_running(g)
    n = len(hold)
    dh, hoop_of_ball = hoop_distance(g.ball[:, :2])
    z = g.ball[:, 2]
    at_rim = (dh < 2.0) & (z > 7.5) & (z < 11.5)

    # a touch: any player's hand on the ball for a frame (tips are one or two frames)
    nearest = np.argmin(d, axis=1)
    touch = np.where((d[np.arange(n), nearest] < TIP_REACH) & (z < 12.5), nearest, -1)

    cands: list[tuple[int, int, str, int]] = []  # release, end, kind, tipper (-1: none)
    last = -10 * FPS
    for i in np.flatnonzero(at_rim):
        gap = i - last
        between = slice(last + 1, i) if last >= 0 else slice(0, 0)
        touched = bool((hold[between] >= 0).any()) or bool((touch[between] >= 0).any())
        new = gap > FPS or (gap > FPS // 3 and touched)
        prev, last = last, i
        if not new:
            continue
        if prev >= 0 and i - prev <= 3 * FPS and (touch[prev + 1:i] >= 0).any() \
                and not (hold[prev + 1:i] >= 0).any():  # fmt: skip
            # back at the rim after a touch but no catch: a tip; its shooter is the tipper
            j = prev + 1 + int(np.flatnonzero(touch[prev + 1 : i] >= 0)[-1])
            cands.append((j, i, "tip", int(touch[j])))
            continue
        j = i - 1
        while j > max(i - 4 * FPS, 0) and hold[j] < 0:
            j -= 1
        if hold[j] >= 0 and g.period[j] == g.period[i]:
            cands.append((j, i, "rim", -1))

    rim_releases = np.array([c[0] for c in cands])
    for j in np.flatnonzero((hold[:-1] >= 0) & (hold[1:] < 0)):
        e = min(j + 10, n - 1)
        side = hold[j] // 5  # 0 home, 1 away: a teammate catching it at once makes it a pass
        after = hold[j + 1 : j + 6]
        if e <= j or g.period[e] != g.period[j] or ((after >= 0) & (after // 5 == side)).any():
            continue
        d0, hoop = hoop_distance(g.ball[j, :2])
        d1 = np.hypot(*(g.ball[e, :2] - HOOPS[int(hoop)]))
        rise = np.nanmax(z[j : e + 1]) - z[j]
        if d0 < 35 and rise >= 1.5 and d0 - d1 >= 1.5:
            if len(rim_releases) and np.min(np.abs(rim_releases - j)) <= FPS // 2:
                continue
            cands.append((int(j), int(e), "launch", -1))

    rows = []
    for j, i, kind, tipper in sorted(cands):
        k = tipper if kind == "tip" else possessor(hold, j, d=d)
        if k < 0:
            continue
        if kind != "tip":
            j = refine_release(d, j, k)
        sxy = g.xy[j, k]
        hoop = int(hoop_of_ball[i]) if kind != "launch" else int(hoop_distance(sxy)[1])
        dist = float(np.hypot(*(sxy - HOOPS[hoop])))
        ft = (not run[j]) and 13.0 <= dist <= 16.5 and abs(sxy[1] - 25) < 3.5
        rows.append({
            "period": int(g.period[j]), "release": j, "end": i, "kind": kind,
            "clock": float(g.clock[j]), "shooter_slot": k, "shooter_id": int(g.ids[j, k]),
            "shooter_x": float(sxy[0]), "shooter_y": float(sxy[1]), "hoop": hoop,
            "distance": dist, "peak_z": float(np.nanmax(z[j:i + 1])),
            "flight_s": (i - j) / FPS, "clock_running": bool(run[j]), "free_throw": bool(ft),
        })  # fmt: skip
    return pd.DataFrame(rows)
