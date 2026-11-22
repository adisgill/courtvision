"""Defense from the frames: who guards whom, man or zone, ball screens and their coverages, help.

**Matchups** (in the spirit of Franks et al. 2015). A defender guarding attacker ``a`` stands
near a point between his man, the ball and the hoop::

    ideal(a) = W_MAN * a + W_BALL * ball + W_HOOP * hoop

Each frame, how far a defender is from each attacker's ideal point is evidence for who he's
guarding (counted once per ``DECORRELATE`` frames, how long that evidence takes to change); a
hidden Markov model (Viterbi, with a prior of ``SWITCH`` changes per frame) smooths it over
time. The weights are fit to this data by alternating assignment and least squares
(``fit_weights``). Matchups are computed for half-court defense only: the attacking team holds
or last held the ball, the ball is in its front court and all ten players are tracked.

**Possessions** (``possessions``): the runs of one team on offense in the half court, each
labelled man or zone by which story explains the defenders better (``man_or_zone``), with its
switch count.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from courtvision.tracking import detect, movement
from courtvision.tracking.detect import FPS, HOOPS

# fit_weights on 60 games (docs/tracking_spec.md §10); Franks et al. found 0.62, 0.11, 0.27
W_MAN, W_BALL, W_HOOP = 0.612, 0.115, 0.273
SIGMA = 3.5  # ft: spread of a defender around his ideal point (per coordinate, measured)
DECORRELATE = 45  # frames (~2 s) for that error to decorrelate: evidence counts once per
#                   this many frames, so 25 near-identical frames aren't 25 observations
SWITCH = 0.001  # prior chance per frame of a change of man (~1.5 a minute per defender)


# --------------------------------------------------------------------------- the floor state


class Floor:
    """One game's frames plus who has the ball, which way each team attacks, and the
    half-court frames, computed once and shared by everything below."""

    def __init__(self, g: detect.Game):
        self.g = g
        self.n = len(g.ts)
        self.d = detect.ball_distances(g)
        self.hold = detect.holder(g, self.d)
        self.run = detect.clock_running(g)
        self.poss = movement.possession(g, self.hold)
        self.hoops = movement.attacking(g, self.hold)
        hoop = np.full(self.n, -1)
        for (p, t), h in self.hoops.items():
            hoop[(g.period == p) & (self.poss == t)] = h
        self.hoop = hoop  # the hoop the offense attacks, per frame (-1 unknown)
        valid = (g.ids > 0).all(axis=1) & ~np.isnan(g.xy).any(axis=(1, 2))
        valid &= ~np.isnan(g.ball[:, :2]).any(axis=1)
        bx = g.ball[:, 0]
        front = np.where(hoop == 0, bx < 47, bx > 47)
        self.half = valid & (self.poss >= 0) & (hoop >= 0) & front & self.run
        self.start = _run_start(self.poss, g.period, self.half)

    def offense(self, i: int) -> slice:
        t = int(self.poss[i])
        return slice(t * 5, t * 5 + 5)

    def defense(self, i: int) -> slice:
        t = int(self.poss[i])
        return slice((1 - t) * 5, (1 - t) * 5 + 5)


def _run_start(poss: np.ndarray, period: np.ndarray, half: np.ndarray) -> np.ndarray:
    """First frame of the current half-court run (same team, same period, no break)."""
    n = len(poss)
    new = np.ones(n, bool)
    new[1:] = (poss[1:] != poss[:-1]) | (period[1:] != period[:-1]) | (half[1:] != half[:-1])
    idx = np.where(new, np.arange(n), 0)
    return np.maximum.accumulate(idx)


# --------------------------------------------------------------------------- matchups


def ideal_points(att: np.ndarray, ball: np.ndarray, hoop: np.ndarray,
                 w: tuple[float, float, float] | None = None) -> np.ndarray:  # fmt: skip
    """(m, 5, 2) where a defender guarding each attacker would stand."""
    wm, wb, wh = w or (W_MAN, W_BALL, W_HOOP)
    return wm * att + wb * ball[:, None, :] + wh * hoop[:, None, :]


def _viterbi(emit: np.ndarray, trans: np.ndarray) -> np.ndarray:
    """(L, 5) best attacker sequence for each of five defenders; ``emit`` is (L, def, att)."""
    L = len(emit)
    back = np.zeros((L, 5, 5), np.int8)
    delta = emit[0].copy()
    for i in range(1, L):
        cand = delta[:, :, None] + trans[None]  # (def, from, to)
        back[i] = cand.argmax(axis=1)
        delta = cand.max(axis=1) + emit[i]
    path = np.zeros((L, 5), np.int8)
    path[-1] = delta.argmax(axis=1)
    for i in range(L - 1, 0, -1):
        path[i - 1] = back[i, np.arange(5), path[i]]
    return path


def matchups(f: Floor, w: tuple[float, float, float] | None = None,
             switch: float | None = None) -> np.ndarray:  # fmt: skip
    """(n, 5) for each half-court frame and each defender (in slot order of the defending
    team), the attacker he guards (0-4, slot order of the offense); -1 elsewhere."""
    g = f.g
    out = np.full((f.n, 5), -1, np.int8)
    idx = np.flatnonzero(f.half)
    if not len(idx):
        return out
    team = f.poss[idx].astype(int)
    switch = SWITCH if switch is None else switch
    oslots = team[:, None] * 5 + np.arange(5)
    dslots = (1 - team)[:, None] * 5 + np.arange(5)
    att = g.xy[idx[:, None], oslots].astype(np.float64)  # (m, 5, 2)
    dfn = g.xy[idx[:, None], dslots].astype(np.float64)
    hoop = HOOPS[f.hoop[idx]]
    ideal = ideal_points(att, g.ball[idx, :2], hoop, w)
    cost = ((dfn[:, :, None, :] - ideal[:, None, :, :]) ** 2).sum(-1)  # (m, def, att)
    emit = -cost / (2 * SIGMA**2 * DECORRELATE)
    trans = np.full((5, 5), np.log(switch / 4))
    np.fill_diagonal(trans, np.log(1 - switch))

    # Viterbi for all five defenders at once, separately in each unbroken half-court run
    starts = f.start[idx]
    cut = np.flatnonzero((np.diff(starts) != 0) | (np.diff(idx) != 1)) + 1
    path = np.zeros((len(idx), 5), np.int8)
    for lo, hi in zip(np.r_[0, cut], np.r_[cut, len(idx)], strict=True):
        path[lo:hi] = _viterbi(emit[lo:hi], trans)
    out[idx] = path
    return out


def fit_weights(floors: list[Floor], rounds: int = 3) -> tuple[float, float, float]:
    """Alternate matchups and least squares for (man, ball, hoop) weights summing to 1."""
    w = (W_MAN, W_BALL, W_HOOP)
    for _ in range(rounds):
        xs, ys = [], []
        for f in floors:
            a = matchups(f, w)
            idx = np.flatnonzero(f.half)[::5]
            team = f.poss[idx].astype(int)
            oslots = team[:, None] * 5 + np.arange(5)
            dslots = (1 - team)[:, None] * 5 + np.arange(5)
            att = f.g.xy[idx[:, None], oslots].astype(np.float64)
            dfn = f.g.xy[idx[:, None], dslots].astype(np.float64)
            hoop = HOOPS[f.hoop[idx]][:, None, :]
            ball = f.g.ball[idx, None, :2]
            mine = np.take_along_axis(att, a[idx][:, :, None].astype(int), axis=1)
            # d - h = wm (a - h) + wb (b - h)
            xs.append(np.stack([(mine - hoop).reshape(-1), (np.broadcast_to(ball, mine.shape)
                                                             - hoop).reshape(-1)], 1))  # fmt: skip
            ys.append((dfn - hoop).reshape(-1))
        X, y = np.concatenate(xs), np.concatenate(ys)
        wm, wb = np.linalg.lstsq(X, y, rcond=None)[0]
        w = (float(wm), float(wb), float(1 - wm - wb))
    return w


# --------------------------------------------------------------------------- possessions

MIN_POSSESSION = 4 * FPS  # frames: shorter half-court runs say little about the scheme
MIN_MOVEMENT = 4.0  # ft: attackers who barely move can't tell man from zone


def possessions(f: Floor, assign: np.ndarray) -> pd.DataFrame:
    """Every half-court run of ``MIN_POSSESSION`` frames or more: who's on offense, how long,
    how many times the defenders changed men, and the man and zone fits (``man_or_zone``)."""
    g = f.g
    rows = []
    idx = np.flatnonzero(f.half)
    if not len(idx):
        return pd.DataFrame()
    starts = f.start[idx]
    cut = np.flatnonzero((np.diff(starts) != 0) | (np.diff(idx) != 1)) + 1
    for lo, hi in zip(np.r_[0, cut], np.r_[cut, len(idx)], strict=True):
        fr = idx[lo:hi]
        if len(fr) < MIN_POSSESSION:
            continue
        i0 = int(fr[0])
        off, dfn = f.offense(i0), f.defense(i0)
        a = assign[fr].astype(int)
        changes = int(((a[1:] != a[:-1]) & (np.arange(1, len(fr))[:, None] > 0)).sum())
        fit = man_or_zone(g.xy[fr, off].astype(np.float64), g.xy[fr, dfn].astype(np.float64),
                          g.ball[fr, :2].astype(np.float64), HOOPS[f.hoop[fr]], a)  # fmt: skip
        rows.append({"start": i0, "end": int(fr[-1]), "period": int(g.period[i0]),
                     "clock": float(g.clock[i0]), "offense": int(f.poss[i0]),
                     "seconds": len(fr) / FPS, "changes": changes} | fit)  # fmt: skip
    return pd.DataFrame(rows)


def man_or_zone(att: np.ndarray, dfn: np.ndarray, ball: np.ndarray, hoop: np.ndarray,
                assign: np.ndarray) -> dict:  # fmt: skip
    """Two stories for the defenders over one possession, and how well each fits (mean
    squared miss, sq ft):

    - **man**: each defender stands at his man's ideal point (the matchups above);
    - **zone**: each defender holds a spot of his own and the whole defense shades toward the
      ball by a common factor (fit by least squares).

    ``zone_fit`` > 1 means the zone story explains them better. It favors zone (each defender
    gets a spot fit to him), so a possession is called zone only if, also, defenders don't go
    with cutters (``follows``). ``movement`` is how much the attackers moved (their mean
    distance from their own average spot): when they stand still, or nobody cuts, both
    stories fit and the possession can't tell."""
    ideal = ideal_points(att, ball, hoop)
    mine = np.take_along_axis(ideal, assign[:, :, None], axis=1)
    man = float(((dfn - mine) ** 2).sum(-1).mean())
    home = dfn.mean(axis=0)  # (5, 2)
    db = ball - ball.mean(axis=0)  # (m, 2)
    y = (dfn - home).reshape(len(dfn), -1)  # (m, 10)
    x = np.tile(db, (1, 5))  # (m, 10): the same ball shift for every defender
    beta = float((x * y).sum() / max((x * x).sum(), 1e-9))
    zone = float(((y - beta * x) ** 2).reshape(len(dfn), 5, 2).sum(-1).mean())
    movement = float(np.linalg.norm(att - att.mean(axis=0), axis=-1).mean())
    cuts = follows(att, dfn, ideal)
    follow = float(np.median(cuts)) if len(cuts) else np.nan
    zone_fit = man / max(zone, 1e-9)
    readable = movement >= MIN_MOVEMENT and len(cuts) >= MIN_CUTS
    return {"man_miss": man, "zone_miss": zone, "zone_fit": zone_fit, "shade": beta,
            "movement": movement, "cuts": len(cuts), "follow": follow, "readable": readable,
            "zone": bool(readable and follow < ZONE_FOLLOW and zone_fit > ZONE_FIT)}  # fmt: skip


CUT_WINDOW, CUT_MIN = 2 * FPS, 10.0  # an attacker moving 10+ ft in 2 s is cutting
MIN_CUTS = 3
ZONE_FOLLOW, ZONE_FIT = 0.3, 1.2  # set before looking at labels; see the spec


def follows(att: np.ndarray, dfn: np.ndarray, ideal: np.ndarray) -> list[float]:
    """For every cut (an attacker moving ``CUT_MIN`` ft within ``CUT_WINDOW`` frames, checked
    each second), how far the defender who was on him at the start went with him: his
    movement along the cutter's, as a share of the cut (about 0.6-1 in man, near 0 in zone,
    where he stays home). The defender "on him" is the one nearest his ideal point."""
    out = []
    for s0 in range(0, len(att) - CUT_WINDOW, FPS):
        e = s0 + CUT_WINDOW
        disp = att[e] - att[s0]
        dist = np.linalg.norm(disp, axis=1)
        cost = ((dfn[s0][:, None] - ideal[s0][None]) ** 2).sum(-1)  # (def, att)
        for a in np.flatnonzero(dist >= CUT_MIN):
            d = int(cost[:, a].argmin())
            moved = dfn[e, d] - dfn[s0, d]
            out.append(float(np.clip(moved @ disp[a] / dist[a] ** 2, -0.5, 1.5)))
    return out


# --------------------------------------------------------------------------- ball screens

SCREEN_REACH = 5.0  # ft: a teammate this close to the ball handler may be screening
SCREEN_DEFENDER = 8.0  # ft: ... if the handler's defender is this close to him
SCREEN_DEEP = 15.0  # ft from the hoop: handlers nearer than this are posting or driving
# coverage rule settings (see _screen_row), as written before any labels. A revision tuned
# on the dev half of the labels (1.0 s, relative, 8 ft) raised dev accuracy 0.75 -> 0.80 but
# lowered the held-out test half 0.81 -> 0.76, so it was dropped (spec §10.3)
BLITZ_HOLD = 0.5  # s: both defenders on the handler at least this long
SWITCH_RELATIVE = False  # exchange judged by who's nearer whom (True) or within 6 ft (False)
DROP_DEEP = None  # ft: a screener's man this far back at the screen is in drop, even if the
#                   men end up exchanged later (None: no such limit)
GAP = 3 * FPS  # frames: one screen per handler and screener within this long
COVERAGES = ("switch", "blitz", "ice", "show", "drop")


def _guarding(assign_row: np.ndarray, att: int, dfn_xy: np.ndarray, att_xy: np.ndarray) -> int:
    """The defender (0-4) assigned to attacker ``att`` (the nearest if several), -1 if none."""
    ds = np.flatnonzero(assign_row == att)
    if not len(ds):
        return -1
    return int(ds[np.argmin(np.linalg.norm(dfn_xy[ds] - att_xy, axis=1))])


def screens(f: Floor, assign: np.ndarray, reach: float = SCREEN_REACH,
            defender: float = SCREEN_DEFENDER,
            deep: float = SCREEN_DEEP) -> pd.DataFrame:  # fmt: skip
    """On-ball screens: a teammate of the ball handler comes within ``SCREEN_REACH`` of him
    (the frame of closest approach is the screen), with the handler's defender within
    ``SCREEN_DEFENDER`` of the screener, the handler beyond ``SCREEN_DEEP`` and the screener
    not the one who just had the ball (that's a handoff). Each comes with its coverage
    (``_screen_row``) and what the screener did (roll, pop or stay)."""
    g = f.g
    rows = []
    hold = f.hold
    held = np.flatnonzero(f.half & (hold >= 0) & (hold // 5 == f.poss))
    last: dict[tuple[int, int], int] = {}
    hx = HOOPS[np.maximum(f.hoop, 0)]
    for i in held:
        h = int(hold[i])
        team = h // 5
        mates = [k for k in range(team * 5, team * 5 + 5) if k != h]
        dist = np.linalg.norm(g.xy[i, mates] - g.xy[i, h], axis=1)
        j = int(np.argmin(dist))
        if dist[j] > reach:
            continue
        s = mates[j]
        # only the frame of closest approach
        if i + 1 < f.n and np.linalg.norm(g.xy[i + 1, s] - g.xy[i + 1, h]) < dist[j]:
            continue
        if i > 0 and np.linalg.norm(g.xy[i - 1, s] - g.xy[i - 1, h]) < dist[j]:
            continue
        key = (int(g.ids[i, h]), int(g.ids[i, s]))
        if i - last.get(key, -GAP - 1) <= GAP:
            continue
        if np.linalg.norm(g.xy[i, h] - hx[i]) < deep:
            continue
        if (hold[max(i - FPS, 0) : i] == s).any():  # the screener just had it: a handoff
            continue
        row = _screen_row(f, assign, int(i), h, s, defender)
        if row is None:
            continue
        row["reach"] = float(dist[j])
        last[key] = int(i)
        rows.append(row)
    return pd.DataFrame(rows)


def _screen_row(f: Floor, assign: np.ndarray, i: int, h: int, s: int,
                defender: float = SCREEN_DEFENDER) -> dict | None:  # fmt: skip
    """Geometry of one screen and its coverage, or None if the defense can't be read.

    Coverage, first rule that holds:

    - **switch**: from 1 s after the screen, most of the time, the screener's man is on the
      handler (nearer him than the handler's own man, within 6 ft) and the handler's man is
      within 6 ft of the screener (``SWITCH_RELATIVE``, ``DROP_DEEP``: variants, off);
    - **blitz**: both defenders within 5 ft of the handler for ``BLITZ_HOLD`` s or more in
      the 1.5 s after the screen, the screener's man at or above the screen;
    - **ice**: half a second before, the handler's man sits on the screen side of him, and
      the handler goes away from the screen instead of using it;
    - **show**: the screener's man comes up to the level of the screen (within 2 ft of the
      screener's distance from the hoop) and within 7 ft of the handler, then goes back;
    - **drop**: otherwise; the screener's man stays back toward the hoop.

    Route (the handler's man, unless switch or ice): **under** if, when closest to the
    screener, he's more than a foot nearer the hoop than the screener, else **over**."""
    g = f.g
    team = h // 5
    off0, def0 = team * 5, (1 - team) * 5
    b = max(i - FPS, 0)  # one second before: who was guarding whom
    if assign[b, 0] < 0:
        return None
    dxy_b = g.xy[b, def0 : def0 + 5]
    dh = _guarding(assign[b], h - off0, dxy_b, g.xy[b, h])
    ds = _guarding(assign[b], s - off0, dxy_b, g.xy[b, s])
    if dh < 0 or ds < 0 or dh == ds:
        return None
    DH, DS = def0 + dh, def0 + ds
    dh_s = float(np.linalg.norm(g.xy[i, DH] - g.xy[i, s]))
    if dh_s > defender:
        return None
    hoop = HOOPS[f.hoop[i]]
    lo, hi = max(i - FPS // 2, 0), min(i + int(2.5 * FPS), f.n - 1)
    if g.period[lo] != g.period[hi]:
        return None
    w = np.arange(lo, hi + 1)

    def at(k, fr):
        return g.xy[fr, k].astype(np.float64)

    def hoopd(k, fr):
        return np.linalg.norm(at(k, fr) - hoop, axis=-1)

    s_hoop = float(hoopd(s, i))
    dh_h = np.linalg.norm(at(DH, w) - at(h, w), axis=1)
    ds_h = np.linalg.norm(at(DS, w) - at(h, w), axis=1)
    early = w <= i + int(1.5 * FPS)
    two_on_ball = int(((dh_h < 5) & (ds_h < 5) & early).sum())
    ds_high = hoopd(DS, w) >= s_hoop - 1.0
    ds_up = bool(((ds_h < 7) & (hoopd(DS, w) >= s_hoop - 2.0) & (w <= i + FPS)).any())
    late = w >= i + FPS
    dh_s_w = np.linalg.norm(at(DH, w) - at(s, w), axis=1)
    if SWITCH_RELATIVE:
        ds_s_w = np.linalg.norm(at(DS, w) - at(s, w), axis=1)
        swapped = (ds_h < dh_h) & (dh_s_w < ds_s_w) & (ds_h < 8)
    else:
        swapped = (ds_h < dh_h) & (ds_h < 6) & (dh_s_w < 6)
    ds_back = float(s_hoop - hoopd(DS, i))  # + : the screener's man is between him and hoop
    switched = bool(late.any() and swapped[late].mean() > 0.6)
    if DROP_DEEP is not None and ds_back >= DROP_DEEP:
        switched = False
    blitz = two_on_ball >= int(BLITZ_HOLD * FPS) and bool(ds_high[early].any())

    p = max(i - FPS // 2, 0)
    u = at(s, p) - at(h, p)
    u = u / max(np.linalg.norm(u), 1e-6)
    dh_side = float((at(DH, p) - at(h, p)) @ u)
    h_move = float((at(h, min(i + int(1.5 * FPS), f.n - 1)) - at(h, p)) @ u)
    ice = dh_side > 1.5 and h_move < 0

    if switched:
        coverage = "switch"
    elif blitz:
        coverage = "blitz"
    elif ice:
        coverage = "ice"
    elif ds_up:
        coverage = "show"
    else:
        coverage = "drop"
    k = int(np.argmin(np.linalg.norm(at(DH, w) - at(s, w), axis=1)))
    route = "under" if float(hoopd(DH, w[k])) < float(hoopd(s, w[k])) - 1.0 else "over"
    if coverage in ("switch", "ice"):
        route = "-"
    s_after = float(hoopd(s, min(i + 2 * FPS, f.n - 1)))
    action = "roll" if s_after < s_hoop - 6 else ("pop" if s_after > s_hoop + 3 else "stay")
    return {
        "frame": i, "period": int(g.period[i]), "clock": float(g.clock[i]),
        "shot_clock": float(g.shot_clock[i]), "offense": int(team),
        "handler": int(g.ids[i, h]), "screener": int(g.ids[i, s]),
        "handler_def": int(g.ids[i, DH]), "screener_def": int(g.ids[i, DS]),
        "x": float(g.xy[i, h, 0]), "y": float(g.xy[i, h, 1]),
        "handler_hoop": float(hoopd(h, i)), "screener_hoop": s_hoop, "handler_def_screener": dh_s,
        "side": bool(abs(float(g.xy[i, h, 1]) - 25) > 10),
        "two_on_ball_s": two_on_ball / FPS, "screener_def_back": ds_back,
        "screener_def_up": ds_up, "switched": switched, "ice_side": dh_side,
        "coverage": coverage, "route": route, "screener_action": action,
    }  # fmt: skip


# --------------------------------------------------------------------------- help on drives

DRIVE_FROM, DRIVE_TO = 16.0, 10.0  # ft from the hoop: a drive starts beyond, ends inside
HELP_NEAR, HELP_LEFT = 6.0, 4.0  # a helper ends this close to the driver, this far off his man
COVERED = 6.0  # ft: a man with a defender this close is covered again


def drives(f: Floor, assign: np.ndarray) -> pd.DataFrame:
    """Drives (the ball handler, with the ball, gets from beyond ``DRIVE_FROM`` to inside
    ``DRIVE_TO`` ft of the hoop within 2 s), whether a second defender helped (left his man
    by ``HELP_LEFT`` ft to end within ``HELP_NEAR`` ft of the driver), and the rotation when
    the driver kicks it out to the man the helper left (within 3 s): how open he was at the
    catch (nearest defender, ft) and how long until a defender closed out to ``COVERED`` ft
    (capped at 3 s)."""
    g = f.g
    hold = f.hold
    rows = []
    hx = HOOPS[np.maximum(f.hoop, 0)]
    i, n = 0, f.n
    while i < n:
        h = int(hold[i])
        if not f.half[i] or h < 0 or h // 5 != f.poss[i] or \
                np.linalg.norm(g.xy[i, h] - hx[i]) > DRIVE_TO:  # fmt: skip
            i += 1
            continue
        back = np.arange(max(i - 2 * FPS, 0), i)
        same = (hold[back] == h) & (g.period[back] == g.period[i])
        far = np.linalg.norm(g.xy[back, h] - hx[back], axis=1) > DRIVE_FROM
        if not (same & far).any():
            i += 1
            continue
        s0 = int(back[np.flatnonzero(same & far)[-1]])
        team = h // 5
        off0, def0 = team * 5, (1 - team) * 5
        if assign[s0, 0] < 0:
            i += FPS
            continue
        a0 = assign[s0]
        dh = _guarding(a0, h - off0, g.xy[s0, def0 : def0 + 5], g.xy[s0, h])
        helper, helped_man = -1, -1
        for d in range(5):
            man = off0 + int(a0[d])
            if d == dh or man == h:
                continue
            left = np.linalg.norm(g.xy[i, def0 + d] - g.xy[i, man]) - \
                np.linalg.norm(g.xy[s0, def0 + d] - g.xy[s0, man])  # fmt: skip
            if np.linalg.norm(g.xy[i, def0 + d] - g.xy[i, h]) < HELP_NEAR and left > HELP_LEFT:
                helper, helped_man = def0 + d, man
                break
        kick, open_ft, closeout = False, np.nan, np.nan
        if helper >= 0:
            # the kick-out: the ball reaches the man the helper left within 3 s
            fut = np.arange(i, min(i + 3 * FPS, n))
            got = np.flatnonzero(hold[fut] == helped_man)
            if len(got):
                kick = True
                c = int(fut[got[0]])
                after = np.arange(c, min(c + 3 * FPS, n))
                gap = g.xy[after, def0 : def0 + 5] - g.xy[after, helped_man][:, None]
                dd = np.linalg.norm(gap, axis=-1).min(axis=1)
                open_ft = float(dd[0])
                hit = np.flatnonzero(dd < COVERED)
                closeout = hit[0] / FPS if len(hit) else 3.0
        rows.append({"frame": i, "start": s0, "period": int(g.period[i]),
                     "clock": float(g.clock[i]), "offense": int(team),
                     "driver": int(g.ids[i, h]),
                     "driver_def": int(g.ids[i, def0 + dh]) if dh >= 0 else 0,
                     "helped": helper >= 0,
                     "helper": int(g.ids[i, helper]) if helper >= 0 else 0,
                     "kick_out": kick, "open_ft": open_ft,
                     "closeout_s": closeout})  # fmt: skip
        i += 2 * FPS  # one drive at a time
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- every game

LOOSE = {"reach": 8.0, "defender": 12.0, "deep": 12.0}  # the pool the labels sample from


def game_defense(game_id: str) -> dict[str, pd.DataFrame]:
    """Screens (strict, plus the looser pool for labeling), drives and possessions for one
    game, each with the game id and the team ids on offense and defense."""
    from courtvision.tracking import sportvu

    frames = sportvu.load_frames(game_id)
    teams = (int(frames.home_team_id.iloc[0]), int(frames.away_team_id.iloc[0]))
    f = Floor(detect.Game.from_frames(frames))
    a = matchups(f)
    out = {"screens": screens(f, a), "pool": screens(f, a, **LOOSE), "drives": drives(f, a),
           "possessions": possessions(f, a)}  # fmt: skip
    for df in out.values():
        if df.empty:
            continue
        df.insert(0, "game_id", game_id)
        df["offense_team"] = [teams[o] for o in df.offense]
        df["defense_team"] = [teams[1 - o] for o in df.offense]
    if not out["pool"].empty:
        strict = out["screens"].frame.to_numpy() if not out["screens"].empty else np.array([])
        out["pool"]["detected"] = [bool(len(strict) and np.abs(strict - fr).min() <= 12)
                                   for fr in out["pool"].frame]  # fmt: skip
    return out


def defense_safe(game_id: str) -> dict:
    try:
        return game_defense(game_id)
    except Exception as e:  # one bad game shouldn't stop the rest
        return {"error": f"{game_id}: {type(e).__name__}: {e}"}


def out_dir():
    from courtvision.config import get_paths

    return get_paths().data / "tracking" / "defense"


def run(workers: int = 6) -> dict:
    """Every game -> data/tracking/defense/{screens,pool,drives,possessions}.parquet."""
    from concurrent.futures import ProcessPoolExecutor

    from courtvision.tracking import sportvu

    games = sportvu.game_ids()
    parts: dict[str, list[pd.DataFrame]] = {"screens": [], "pool": [], "drives": [],
                                            "possessions": []}  # fmt: skip
    errors = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(defense_safe, games):
            if "error" in r:
                errors.append(r["error"])
                continue
            for k, df in r.items():
                if not df.empty:
                    parts[k].append(df)
    out_dir().mkdir(parents=True, exist_ok=True)
    counts = {}
    for k, v in parts.items():
        df = pd.concat(v, ignore_index=True)
        df.to_parquet(out_dir() / f"{k}.parquet", index=False)
        counts[k] = len(df)
    return counts | {"errors": errors[:10]}


# --------------------------------------------------------------------------- outcomes

LAG_PBP = 3.0  # s: the play-by-play logs a basket up to ~3 s after the ball left the hand
LOST = 12  # frames: the other team holding the ball this long ends the possession (a hand on
#           it for a frame or two, a deflection, a contest, doesn't)


def _points(pbp: pd.DataFrame) -> pd.Series:
    made = pbp.actionType.eq("Made Shot")
    ft = pbp.actionType.eq("Free Throw") & ~pbp.description.str.startswith("MISS")
    three = pbp.description.str.contains("3PT")
    return pd.Series(np.where(made, np.where(three, 3, 2), np.where(ft, 1, 0)), index=pbp.index)


def game_outcomes(game_id: str, events: pd.DataFrame) -> pd.DataFrame:
    """For each event (a screen or a drive: ``frame``, ``offense``, ``offense_team``), what
    the offense got before it lost the ball (the other team holding it ``LOST`` frames, the
    period ending, or 24 s): points (made baskets and free throws in the play-by-play between
    the event and the end of the possession), whether it shot, and the expected points of
    its shots (``shot_quality.parquet``)."""
    from courtvision.config import get_paths
    from courtvision.tracking import sportvu

    data = get_paths().data / "tracking"
    frames = sportvu.load_frames(game_id)
    g = detect.Game.from_frames(frames)
    hold = detect.holder(g)
    team = np.where(hold >= 0, hold // 5, -1)
    pbp = pd.read_parquet(data / "pbp_2015.parquet", filters=[("game_id", "==", game_id)])
    pbp = pbp.assign(pts=_points(pbp))
    pbp = pbp[pbp.pts > 0]
    shots = pd.read_parquet(data / "shots" / f"{game_id}.parquet",
                            columns=["actionNumber", "release", "team_id"])  # fmt: skip
    sq = pd.read_parquet(data / "shot_quality.parquet",
                         columns=["game_id", "action_number", "xpts"],
                         filters=[("game_id", "==", game_id)])  # fmt: skip
    shots = shots.merge(sq, left_on="actionNumber", right_on="action_number", how="left")
    n = len(g.ts)
    rows = []
    for r in events.itertuples(index=False):
        i = int(r.frame)
        side = int(r.offense)
        e, run = i, 0
        cap = min(i + 24 * FPS, n - 1)
        while e < cap and g.period[e + 1] == g.period[i]:
            run = run + 1 if team[e + 1] == 1 - side else 0
            if run >= LOST:
                e -= LOST - 1
                break
            e += 1
        p, c0, c1 = int(g.period[i]), float(g.clock[i]), float(g.clock[e])
        got = pbp[(pbp.period == p) & (pbp.team_id == r.offense_team)
                  & (pbp.t <= c0 + 0.5) & (pbp.t >= c1 - LAG_PBP)]  # fmt: skip
        mine = shots[(shots.team_id == r.offense_team) & (shots.release >= i)
                     & (shots.release <= e)]  # fmt: skip
        rows.append({"points": int(got.pts.sum()), "shot": len(mine) > 0,
                     "xpts": float(mine.xpts.sum()) if len(mine) else 0.0,
                     "poss_left_s": (e - i) / FPS})  # fmt: skip
    return pd.concat([events.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def _outcomes_one(args: tuple[str, pd.DataFrame, pd.DataFrame]) -> tuple:
    gid, sc, dr = args
    try:
        return game_outcomes(gid, sc), game_outcomes(gid, dr)
    except Exception as e:  # one bad game shouldn't stop the rest
        return {"error": f"{gid}: {type(e).__name__}: {e}"}


def outcomes(workers: int = 6) -> dict:
    """Points and expected points after every screen and drive, then coverage results by
    team -> screen_outcomes.parquet, drive_outcomes.parquet, coverage_by_team.parquet."""
    from concurrent.futures import ProcessPoolExecutor

    d = out_dir()
    sc = pd.read_parquet(d / "screens.parquet")
    dr = pd.read_parquet(d / "drives.parquet")
    jobs = [(gid, sc[sc.game_id == gid], dr[dr.game_id == gid]) for gid in sc.game_id.unique()]
    so, do, errors = [], [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(_outcomes_one, jobs):
            if isinstance(r, dict):
                errors.append(r["error"])
            else:
                so.append(r[0])
                do.append(r[1])
    s = pd.concat(so, ignore_index=True)
    dv = pd.concat(do, ignore_index=True)
    s.to_parquet(d / "screen_outcomes.parquet", index=False)
    dv.to_parquet(d / "drive_outcomes.parquet", index=False)
    by_team = coverage_by_team(s)
    by_team.to_parquet(d / "coverage_by_team.parquet", index=False)
    league = s.groupby("coverage").agg(screens=("points", "size"), points=("points", "mean"),
                                       xpts=("xpts", "mean"), shot=("shot", "mean"))  # fmt: skip
    league["share"] = league.screens / league.screens.sum()
    helped = dv.groupby(["helped", "kick_out"]).agg(
        drives=("points", "size"), points=("points", "mean"), xpts=("xpts", "mean")
    )
    return {"screens": len(s), "drives": len(dv), "errors": errors[:10],
            "league": league.round(3).reset_index().to_dict("records"),
            "drives_by_help": helped.round(3).reset_index().to_dict("records"),
            "kick_out_open_ft_median": round(float(dv.open_ft.median()), 2),
            "closeout_s_median": round(float(dv.closeout_s.median()), 2)}  # fmt: skip


def coverage_by_team(s: pd.DataFrame) -> pd.DataFrame:
    """Each defense's coverage mix and what it allowed per screen, by coverage."""
    t = s.groupby(["defense_team", "coverage"]).agg(
        screens=("points", "size"), points=("points", "mean"), xpts=("xpts", "mean")
    )
    t["share"] = t.screens / t.groupby(level=0).screens.transform("sum")
    return t.reset_index()
