"""Causal per-frame state: who has the ball, which team is on offense, which hoop it attacks.

Everything here looks only backward, so a frame's state is final the moment the frame
arrives. The training tables (batch, a whole game at once) and the streaming engine (one
frame at a time) call these same functions, and a test checks that they agree.

``detect.holder`` (used by T1-T3) looks a frame or two ahead (central differences, runs of
high touches); :func:`holder` is its causal twin: backward differences, and a high touch
counts once it has lasted ``HIGH_FRAMES`` frames.
"""

from __future__ import annotations

import numpy as np

from courtvision.tracking.detect import (
    CO_MOVING,
    FPS,
    HIGH_FRAMES,
    HIGH_REACH,
    HIGH_TOP,
    HOLD_TOP,
    REACH,
)

HISTORY = 13  # frames of history any per-frame computation needs (clock running: 12 back)
LOST = 12  # frames: the other team holding the ball this long ends the possession (T3)
HOOP_VOTES = 30 * FPS  # held frames this period (either team) before the votes set the hoops
VEL_BALL = 5  # frames back for the ball's velocity (0.2 s)


def holder(ball: np.ndarray, xy: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """Slot (0-9) holding or dribbling the ball, -1 if nobody, for frames (T, ...). Row t
    uses rows t-4..t only, so the last row of any window of 5+ frames is final."""
    d = np.hypot(xy[:, :, 0] - ball[:, None, 0], xy[:, :, 1] - ball[:, None, 1])
    d = np.where((ids > 0) & np.isfinite(d), d, np.inf)
    k = np.argmin(d, axis=1)
    rows = np.arange(len(k))
    dk = d[rows, k]
    z = np.nan_to_num(ball[:, 2], nan=99.0)
    low = (dk < REACH) & (z < HOLD_TOP)
    vb = np.zeros((len(k), 2))
    vp = np.zeros((len(k), 2))
    if len(k) > 2:
        vb[2:] = (ball[2:, :2] - ball[:-2, :2]) * (FPS / 2)
        vp[2:] = (xy[rows[2:], k[2:]] - xy[rows[:-2], k[2:]]) * (FPS / 2)
    rel = np.nan_to_num(np.hypot(*(vb - vp).T), nan=np.inf)
    high = (dk < HIGH_REACH) & (z < HIGH_TOP) & (rel < CO_MOVING) & ~low
    keep = high.copy()
    for b in range(1, HIGH_FRAMES):  # the same player high for HIGH_FRAMES frames in a row
        prev = np.zeros(len(k), bool)
        prev[b:] = high[:-b] & (k[:-b] == k[b:])
        keep &= prev
    return np.where(low | keep, k, -1)


def team_of(slot: int | np.ndarray):
    """0 home (slots 0-4), 1 away (5-9), -1 nobody."""
    return np.where(np.asarray(slot) >= 0, np.asarray(slot) // 5, -1)


class Possession:
    """The team on offense, 0 home or 1 away (-1 before anyone has had the ball this period).
    It changes only when the other team has held the ball ``LOST`` frames in a row, so a
    deflection or a hand on a rebound doesn't flip it; ``start`` is the frame the new team
    first got the ball."""

    def __init__(self) -> None:
        self.period = None
        self.offense = -1
        self.start = 0
        self.run_team = -1
        self.run = 0

    def update(self, i: int, period: int, team: int) -> tuple[int, int]:
        if period != self.period:
            self.period, self.offense, self.start = period, -1, i
            self.run_team, self.run = -1, 0
        if team >= 0 and team == self.run_team:
            self.run += 1
        else:
            self.run_team, self.run = team, 1 if team >= 0 else 0
        if self.run >= LOST and self.run_team != self.offense:
            self.offense, self.start = self.run_team, i - LOST + 1
        return self.offense, self.start


class Hoops:
    """Which hoop each team attacks (0 left, 1 right): the half where it holds the ball most
    this period (and the other team least), once the ball has been held ``HOOP_VOTES``
    frames; before that, last period's (teams change ends at halftime only); in the first
    period, the votes so far."""

    def __init__(self) -> None:
        self.period = None
        self.votes = np.zeros((2, 2), np.int64)  # [team, half]
        self.last: dict[int, int] = {}  # team -> hoop at the end of the previous period

    def _side(self, team: int) -> int | None:
        # evidence that ``team`` attacks the right hoop: it holding the ball on the right, or
        # the other team holding it on the left
        ev = self.votes[team] + self.votes[1 - team][::-1]
        if ev.sum() >= HOOP_VOTES:
            return int(ev[1] > ev[0])
        if team in self.last:
            return 1 - self.last[team] if self.period == 3 else self.last[team]
        if ev.sum():
            return int(ev[1] > ev[0])
        return None

    def update(self, period: int, team: int, ball_x: float) -> None:
        if period != self.period:
            if self.period is not None:
                for t in (0, 1):
                    s = self._side(t)
                    if s is not None:
                        self.last[t] = s
            self.period = period
            self.votes[:] = 0
        if team >= 0 and np.isfinite(ball_x):
            self.votes[team, int(ball_x > 47.0)] += 1

    def hoop(self, team: int) -> int:
        """The hoop ``team`` attacks; if nothing is known yet, the half the ball is in is
        as good a guess as any, so 0."""
        if team < 0:
            return 0
        s = self._side(team)
        return 0 if s is None else s


def clock_running(clock: np.ndarray, period: np.ndarray) -> np.ndarray:
    """(T,) whether the game clock moved over the last ``HISTORY - 1`` frames (row t uses
    rows t-12 and t)."""
    lag = HISTORY - 1
    run = np.zeros(len(clock), bool)
    if len(clock) > lag:
        run[lag:] = (clock[:-lag] - clock[lag:] > 0.2) & (period[:-lag] == period[lag:])
    return run


def ball_velocity(ball: np.ndarray) -> np.ndarray:
    """(T, 3) ball velocity in ft/s over the last ``VEL_BALL`` frames (0 for the first)."""
    v = np.zeros_like(ball, dtype=np.float64)
    if len(ball) > VEL_BALL:
        v[VEL_BALL:] = (ball[VEL_BALL:] - ball[:-VEL_BALL]) * (FPS / VEL_BALL)
    return np.nan_to_num(v)


def track(period: np.ndarray, ball: np.ndarray, xy: np.ndarray, ids: np.ndarray) -> dict:
    """The causal state for every frame of a game, by the same code the engine runs per
    frame: holder slot, offense (0/1/-1), the frame its possession began, the hoop the
    offense attacks, and the hoops carried into each period (to start the engine there)."""
    n = len(period)
    hold = holder(ball, xy, ids)
    team = team_of(hold)
    poss, hoops = Possession(), Hoops()
    offense = np.empty(n, np.int8)
    start = np.empty(n, np.int64)
    hoop = np.empty(n, np.int8)
    run_team = np.empty(n, np.int8)
    run = np.empty(n, np.int32)
    votes = np.empty((n, 2, 2), np.int32)
    carried: dict[int, dict[int, int]] = {}
    bx = ball[:, 0]
    for i in range(n):
        p, t = int(period[i]), int(team[i])
        o, s = poss.update(i, p, t)
        hoops.update(p, t, float(bx[i]))
        if p not in carried:
            carried[p] = dict(hoops.last)
        offense[i], start[i], hoop[i] = o, s, hoops.hoop(o)
        run_team[i], run[i], votes[i] = poss.run_team, poss.run, hoops.votes
    return {"holder": hold, "offense": offense, "start": start, "hoop": hoop,
            "carried": carried, "run_team": run_team, "run": run, "votes": votes}  # fmt: skip


def restore(st: dict, period: np.ndarray, i: int) -> tuple[Possession, Hoops]:
    """The trackers exactly as they stand after frame ``i`` (from :func:`track`'s record),
    so the engine can start mid-game without replaying everything before."""
    p = int(period[i])
    poss = Possession()
    poss.period, poss.offense, poss.start = p, int(st["offense"][i]), int(st["start"][i])
    poss.run_team, poss.run = int(st["run_team"][i]), int(st["run"][i])
    hoops = Hoops()
    hoops.period, hoops.votes = p, st["votes"][i].astype(np.int64).copy()
    hoops.last = dict(st["carried"][p])
    return poss, hoops
