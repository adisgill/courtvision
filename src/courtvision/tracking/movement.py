"""How players move, from the frames: distance, speed, touches, dribbles, zones and spacing.

Per player-game (``data/tracking/movement.parquet``):

- **distance and speed.** Positions are smoothed with a centered moving average (the raw 25 Hz
  positions jitter by a few inches, which would add distance that nobody ran), then summed
  step by step within stretches where the same player is tracked without a break. Distance is
  kept for several smoothing windows (``SMOOTH``), split by the clock running or stopped and
  by offense or defense (the team that last held the ball is on offense). Speed is distance per
  second of running clock, as NBA.com computes it (distance over minutes played);
- **touches, possession time and dribbles.** A touch is a run of frames in one player's hands
  (``detect.holder``), with gaps of up to ``MERGE`` frames (a high dribble, a bobble) bridged; a
  dribble is the ball dropping below ``BOUNCE`` ft during a touch;
- **load:** top speed and distance run above ``FAST`` ft/s;
- **zones:** where a player spends his half-court offense (paint, midrange, three).

Per team-game (``data/tracking/spacing.parquet``): how spread out the five on offense are in
the half court, as the area of their convex hull, their mean pairwise distance and how many
stand in the corners.

``validate`` checks these against NBA.com's own numbers for the same games (``nbacom``),
choosing the smoothing window on half the games and reporting on the other half.
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from courtvision.config import get_paths
from courtvision.tracking import detect, sportvu
from courtvision.tracking.detect import FPS, HOOPS

log = logging.getLogger(__name__)

SMOOTH = (1, 5, 9, 15)  # moving-average windows (frames) kept; validate picks one
SPEED_WINDOW = 9  # for top speed and fast running
MPH = 3600 / 5280  # ft/s -> mph
FAST = 15.0  # ft/s (about 10 mph): running hard
MAX_SPEED = 32.0  # ft/s (about 22 mph, a sprinter's top speed): faster steps are glitches
MERGE = 15  # frames: a gap this short inside one player's touch is a dribble or a bobble
MIN_TOUCH = (3, 6, 10, 15)  # frames held: shorter holds are deflections and tips (validate
#                             picks the threshold; the rest of the touch stats use the first)
BOUNCE = 1.5  # ft: the ball this low during a touch is a dribble
GAP_MS = 100  # a jump in the timestamps longer than this breaks a stretch


def out_dir():
    return get_paths().data / "tracking"


# --------------------------------------------------------------------------- building blocks


def breaks(g: detect.Game) -> np.ndarray:
    """(n, 10) True where a player's stretch of continuous tracking starts (new player in the
    slot, new period, a gap in the timestamps, or a missing position)."""
    n = len(g.ts)
    valid = (g.ids > 0) & ~np.isnan(g.xy).any(axis=2)
    brk = np.ones((n, 10), bool)
    if n > 1:
        cut = (g.period[1:] != g.period[:-1]) | (np.diff(g.ts) > GAP_MS)
        brk[1:] = (g.ids[1:] != g.ids[:-1]) | cut[:, None] | (valid[1:] != valid[:-1])
    return brk | ~valid


def smooth(v: np.ndarray, brk: np.ndarray, w: int) -> np.ndarray:
    """Centered moving average of ``v`` (n, k) over ``w`` frames, never across a break (the
    window shrinks at a stretch's ends)."""
    if w <= 1:
        return v.astype(np.float64)
    h = w // 2
    n = v.shape[0]
    out = np.empty(v.shape, np.float64)
    idx = np.arange(n)
    for c in range(v.shape[1]):
        seg = np.cumsum(brk[:, c])
        first = np.flatnonzero(brk[:, c])  # start of each stretch
        start = first[seg - 1]
        end = np.append(first[1:], n)[seg - 1] - 1
        x = np.nan_to_num(v[:, c].astype(np.float64))
        cs = np.concatenate([[0.0], np.cumsum(x)])
        lo = np.maximum(idx - h, start)
        hi = np.minimum(idx + h, end)
        out[:, c] = (cs[hi + 1] - cs[lo]) / (hi - lo + 1)
    return out


def steps(g: detect.Game, w: int, brk: np.ndarray | None = None) -> np.ndarray:
    """(n, 10) feet moved since the previous frame, positions smoothed over ``w`` frames; 0 at a
    break."""
    brk = breaks(g) if brk is None else brk
    x = smooth(g.xy[:, :, 0], brk, w)
    y = smooth(g.xy[:, :, 1], brk, w)
    s = np.zeros_like(x)
    s[1:] = np.hypot(np.diff(x, axis=0), np.diff(y, axis=0))
    dt = np.zeros(len(s))
    dt[1:] = np.diff(g.ts) / 1000.0
    # nobody runs faster than ~22 mph: a faster step is the tracking jumping, not a player
    glitch = s > MAX_SPEED * np.maximum(dt, 1 / FPS)[:, None]
    return np.where(brk | glitch, 0.0, s)


def possession(g: detect.Game, hold: np.ndarray) -> np.ndarray:
    """Team on offense each frame (0 home, 1 away, -1 unknown): the team that last held the
    ball in this period."""
    team = np.where(hold >= 0, hold // 5, -1)
    out = np.full(len(team), -1, np.int8)
    last, period = -1, None
    for i, (t, p) in enumerate(zip(team, g.period, strict=True)):
        if p != period:
            last, period = -1, p
        if t >= 0:
            last = t
        out[i] = last
    return out


def attacking(g: detect.Game, hold: np.ndarray) -> dict[tuple[int, int], int]:
    """(period, team) -> the hoop that team attacks (0 left, 1 right): the half where it holds
    the ball most."""
    out = {}
    for p in np.unique(g.period):
        for t in (0, 1):
            m = (g.period == p) & (hold >= 0) & (hold // 5 == t)
            if m.sum() > FPS:
                out[(int(p), t)] = int(np.nanmedian(g.ball[m, 0]) > 47)
    for p in np.unique(g.period):  # one side seen only: the other team attacks the other hoop
        for t in (0, 1):
            if (int(p), t) not in out and (int(p), 1 - t) in out:
                out[(int(p), t)] = 1 - out[(int(p), 1 - t)]
    return out


def touches(g: detect.Game, hold: np.ndarray, run: np.ndarray) -> pd.DataFrame:
    """Every touch: who, first and last frame, frames in hand, dribbles, whether the clock ran
    at any point of it, and whether it started in the front court."""
    n = len(hold)
    change = np.flatnonzero(np.diff(hold) != 0) + 1
    starts = np.concatenate([[0], change])
    ends = np.append(change, n) - 1
    runs = [(int(hold[s]), int(s), int(e)) for s, e in zip(starts, ends, strict=True)
            if hold[s] >= 0]  # fmt: skip
    merged: list[list[int]] = []  # slot, start, end, frames held
    for k, s, e in runs:
        if merged and merged[-1][0] == k and s - merged[-1][2] - 1 <= MERGE \
                and g.period[s] == g.period[merged[-1][2]]:  # fmt: skip
            merged[-1][2] = e
            merged[-1][3] += e - s + 1
        else:
            merged.append([k, s, e, e - s + 1])
    z = g.ball[:, 2]
    low = np.nan_to_num(z, nan=9.0) < BOUNCE
    rows = []
    for k, s, e, held in merged:
        if held < MIN_TOUCH[0]:
            continue
        lo = low[s : e + 1]
        dribbles = int(np.sum(lo[1:] & ~lo[:-1]) + lo[0])
        rows.append({"slot": k, "person_id": int(g.ids[s, k]), "start": s, "end": e,
                     "held": held, "dribbles": dribbles,
                     "ran": bool(run[s : e + 1].any())})  # fmt: skip
    return pd.DataFrame(rows, columns=["slot", "person_id", "start", "end", "held", "dribbles",
                                       "ran"])  # fmt: skip


def _hull_area(p: np.ndarray) -> float:
    """Area of the convex hull of a few points (monotone chain + shoelace)."""
    pts = sorted(map(tuple, p))
    if len(pts) < 3:
        return 0.0

    def half(seq):
        h: list[tuple] = []
        for q in seq:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (q[1] - h[-2][1]) - \
                    (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0]) <= 0:  # fmt: skip
                h.pop()
            h.append(q)
        return h

    hull = np.array(half(pts)[:-1] + half(pts[::-1])[:-1])
    x, y = hull[:, 0], hull[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def zone(xy: np.ndarray, hoop: np.ndarray) -> np.ndarray:
    """0 paint, 1 midrange, 2 three, for points (m, 2) attacking ``hoop`` (m,)."""
    hx = HOOPS[hoop, 0]
    depth = np.abs(xy[:, 0] - np.where(hoop == 0, 0.0, 94.0))  # feet from the baseline
    lat = np.abs(xy[:, 1] - 25)
    dist = np.hypot(xy[:, 0] - hx, xy[:, 1] - 25)
    three = ((lat >= 22) & (depth <= 14)) | (dist >= 23.75)
    paint = (lat < 8) & (depth < 19)
    return np.where(three, 2, np.where(paint, 0, 1))


# --------------------------------------------------------------------------- one game


def game_movement(game_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(player-game movement, team-game spacing) for one game."""
    frames = sportvu.load_frames(game_id)
    g = detect.Game.from_frames(frames)
    teams = (int(frames.home_team_id.iloc[0]), int(frames.away_team_id.iloc[0]))
    n = len(g.ts)
    hold = detect.holder(g)
    run = detect.clock_running(g)
    poss = possession(g, hold)
    hoops = attacking(g, hold)
    brk = breaks(g)
    dt = np.zeros(n)
    dt[1:] = np.diff(g.ts) / 1000.0
    dt = np.where(dt * 1000 > GAP_MS, 0.0, dt)

    side = np.repeat(np.array([[0] * 5 + [1] * 5]), n, axis=0)
    on_off = poss[:, None] == side  # (n, 10): this player's team has the ball
    secs = np.where(brk, 0.0, dt[:, None])  # time counts like distance: within a stretch

    cols: dict[str, np.ndarray] = {
        "person_id": g.ids.ravel(), "slot": np.tile(np.arange(10), n),
        "t_run": (secs * run[:, None]).ravel(),
        "t_all": secs.ravel(),
        "t_off": (secs * (run[:, None] & on_off)).ravel(),
        "t_def": (secs * (run[:, None] & ~on_off & (poss[:, None] >= 0))).ravel(),
    }  # fmt: skip
    for w in SMOOTH:
        s = steps(g, w, brk)
        cols[f"d_run_{w}"] = (s * run[:, None]).ravel()
        cols[f"d_all_{w}"] = s.ravel()
        cols[f"d_off_{w}"] = (s * (run[:, None] & on_off)).ravel()
        cols[f"d_def_{w}"] = (s * (run[:, None] & ~on_off & (poss[:, None] >= 0))).ravel()
        if w == SPEED_WINDOW:
            # a few frames arrive milliseconds apart: speed over at least a frame's time
            fs = np.where(dt[:, None] > 0, s / np.maximum(dt, 1 / FPS)[:, None], 0.0)
            cols["d_fast"] = (s * run[:, None] * (fs > FAST)).ravel()
            # top speed held over a full second, so a single bad frame can't set it
            cols["top_speed"] = np.where(run[:, None], smooth(fs, brk, FPS), 0.0).ravel()

    # half-court offense: zones per player
    hc = np.zeros((n, 10), bool)
    zn = np.full((n, 10), -1, np.int8)
    for (p, t), hp in hoops.items():
        m = (g.period == p) & (poss == t) & run
        if not m.any():
            continue
        sl = slice(t * 5, t * 5 + 5)
        front = (g.xy[m, sl, 0] < 47) if hp == 0 else (g.xy[m, sl, 0] > 47)
        allfront = front.all(axis=1)
        mm = np.flatnonzero(m)[allfront]
        hc[mm, sl] = True
        pts = g.xy[mm, sl].reshape(-1, 2)
        zn[mm, sl] = zone(pts, np.full(len(pts), hp)).reshape(-1, 5)
    for z, name in enumerate(("paint", "mid", "three")):
        cols[f"t_{name}"] = (secs * (zn == z)).ravel()

    df = pd.DataFrame(cols)
    df = df[df.person_id > 0]
    agg = {c: "sum" for c in df.columns if c not in ("person_id", "slot", "top_speed")}
    agg["top_speed"] = "max"
    agg["slot"] = "first"
    out = df.groupby("person_id").agg(agg).reset_index()
    out["team_id"] = np.where(out.slot < 5, teams[0], teams[1])
    out["top_speed"] = out.top_speed * MPH  # ft/s -> mph (a 9-frame window tames spikes)

    t = touches(g, hold, run)
    t["front"] = [hoops.get((int(g.period[s]), int(k // 5))) is not None
                  and (g.ball[s, 0] < 47) == (hoops[(int(g.period[s]), int(k // 5))] == 0)
                  for s, k in zip(t.start, t.slot, strict=True)]  # fmt: skip
    t["span"] = (t.end - t.start + 1) / FPS
    ta = t.groupby("person_id").agg(
        touches=("held", "size"), touches_run=("ran", "sum"), held_s=("held", "sum"),
        span_s=("span", "sum"), dribbles=("dribbles", "sum"), front_touches=("front", "sum"),
    )  # fmt: skip
    ta["held_s"] = ta.held_s / FPS
    for k in MIN_TOUCH[1:]:
        ta[f"touches_run_{k}"] = t[t.ran & (t.held >= k)].groupby("person_id").size()
    run_t = t[t.ran]
    ta["dribbles_run"] = run_t.groupby("person_id").dribbles.sum()
    ta["span_run_s"] = run_t.groupby("person_id").span.sum()
    out = out.merge(ta.reset_index(), on="person_id", how="left").fillna(
        {c: 0 for c in ta.columns})  # fmt: skip
    out.insert(0, "game_id", game_id)

    # spacing: every 5th half-court frame (5 a second), the five on offense
    rows = []
    for (p, tm), _hp in hoops.items():
        sl = slice(tm * 5, tm * 5 + 5)
        m = np.flatnonzero((g.period == p) & (poss == tm) & run & hc[:, tm * 5])[::5]
        for i in m:
            pts = g.xy[i, sl].astype(np.float64)
            if np.isnan(pts).any():
                continue
            pd_ = np.hypot(*(pts[:, None, :] - pts[None, :, :]).transpose(2, 0, 1))
            hp = hoops[(int(p), tm)]
            corner = int(((np.abs(pts[:, 1] - 25) >= 20)
                          & (np.abs(pts[:, 0] - (0 if hp == 0 else 94)) <= 14)).sum())  # fmt: skip
            rows.append((tm, _hull_area(pts), pd_[np.triu_indices(5, 1)].mean(), corner))
    sp = pd.DataFrame(rows, columns=["side", "hull", "pairwise", "corners"])
    spacing = sp.groupby("side").agg(
        hull_area=("hull", "mean"),
        pairwise=("pairwise", "mean"),
        corners=("corners", "mean"),
        frames=("hull", "size"),
    )
    spacing = spacing.reset_index()
    spacing["team_id"] = [teams[s] for s in spacing.side]
    spacing.insert(0, "game_id", game_id)
    return out, spacing.drop(columns="side")


def game_safe(game_id: str) -> tuple[pd.DataFrame, pd.DataFrame] | dict:
    try:
        return game_movement(game_id)
    except Exception as e:  # one bad game shouldn't stop the rest
        return {"game_id": game_id, "error": f"{type(e).__name__}: {e}"}


def run(workers: int = 6) -> dict:
    """Movement for every converted game -> movement.parquet and spacing.parquet."""
    games = sportvu.game_ids()
    mov, spc, errors = [], [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for i, r in enumerate(pool.map(game_safe, games), 1):
            if isinstance(r, dict):
                errors.append(r)
            else:
                mov.append(r[0])
                spc.append(r[1])
            if i % 100 == 0:
                log.info("movement: %d/%d", i, len(games))
    pd.concat(mov, ignore_index=True).to_parquet(out_dir() / "movement.parquet", index=False)
    pd.concat(spc, ignore_index=True).to_parquet(out_dir() / "spacing.parquet", index=False)
    return {"games": len(mov), "errors": errors[:10]}


# --------------------------------------------------------------------------- the known answers


def _agree(ours: pd.Series, theirs: pd.Series) -> dict:
    ok = ours.notna() & theirs.notna() & np.isfinite(ours) & np.isfinite(theirs)
    a, b = ours[ok].to_numpy(float), theirs[ok].to_numpy(float)
    return {"n": int(ok.sum()), "r": round(float(np.corrcoef(a, b)[0, 1]), 3),
            "ours": round(float(a.mean()), 3), "nba": round(float(b.mean()), 3),
            "bias_pct": round(float((a.mean() - b.mean()) / b.mean() * 100), 1),
            "mae": round(float(np.abs(a - b).mean()), 3)}  # fmt: skip


def _official_minutes() -> pd.DataFrame:
    import duckdb

    con = duckdb.connect(str(get_paths().duckdb), read_only=True)
    try:
        return con.sql("select game_id, person_id, minutes from stg.player_games "
                       "where season = 2015").df()  # fmt: skip
    finally:
        con.close()


def validate() -> dict:
    """Our movement numbers vs NBA.com's for the same player-games.

    Settings (smoothing window, clock running or all frames, the shortest touch, possession
    time as frames in hand or the whole touch) are chosen on even game ids and reported on odd
    ones. Player-games with at least 10 minutes are compared. Rates use NBA.com's own minutes
    (distance over average speed, exact to the second); distance uses the official box score's
    minutes so it isn't checked against NBA.com's minutes twice."""
    from courtvision.tracking import nbacom

    nb = nbacom.load()
    m = pd.read_parquet(out_dir() / "movement.parquet")
    spd = nb["speed"][["game_id", "person_id", "DIST_FEET", "AVG_SPEED", "AVG_SPEED_OFF",
                       "AVG_SPEED_DEF", "DIST_MILES_OFF", "DIST_MILES_DEF"]]  # fmt: skip
    pos = nb["possessions"][["game_id", "person_id", "TOUCHES", "TIME_OF_POSS",
                             "AVG_SEC_PER_TOUCH", "AVG_DRIB_PER_TOUCH"]]  # fmt: skip
    df = m.merge(spd, on=["game_id", "person_id"]).merge(pos, on=["game_id", "person_id"],
                                                          how="left")  # fmt: skip
    df = df.merge(_official_minutes(), on=["game_id", "person_id"], how="left")
    df["nba_min"] = df.DIST_FEET / (df.AVG_SPEED * 88)  # 1 mph = 88 ft per minute
    df = df[(df.nba_min >= 10) & (df.t_run >= 300)].copy()
    fit = df.game_id.str[-1].astype(int) % 2 == 0
    out: dict = {"player_games": len(df), "games": int(df.game_id.nunique()),
                 "coverage": round(float((df.t_run / 60 / df.nba_min).median()), 3)}  # fmt: skip

    # speed: pick the variant on the fit half
    cand = {}
    for clock in ("run", "all"):
        for w in SMOOTH:
            mph = df[f"d_{clock}_{w}"] / df.t_run * MPH
            cand[(clock, w)] = float(np.abs(mph - df.AVG_SPEED)[fit].mean())
    clock, w = min(cand, key=cand.get)
    out["settings"] = {"clock": clock, "smooth": w}
    test = df[~fit].copy()
    test["mph"] = test[f"d_{clock}_{w}"] / test.t_run * MPH
    rows = {"avg_speed_mph": _agree(test.mph, test.AVG_SPEED)}
    if clock == "run":
        rows["speed_offense_mph"] = _agree(test[f"d_off_{w}"] / test.t_off * MPH,
                                           test.AVG_SPEED_OFF)  # fmt: skip
        rows["speed_defense_mph"] = _agree(test[f"d_def_{w}"] / test.t_def * MPH,
                                           test.AVG_SPEED_DEF)  # fmt: skip
    rows["distance_ft"] = _agree(test.mph / MPH * test.minutes * 60, test.DIST_FEET)

    # touches: pick the shortest touch on the fit half
    have = df.TOUCHES.notna()
    scale = df.nba_min * 60 / df.t_run
    tcols = {MIN_TOUCH[0]: "touches_run"} | {k: f"touches_run_{k}" for k in MIN_TOUCH[1:]}
    terr = {
        k: float(np.abs(df[c] * scale - df.TOUCHES)[fit & have].mean()) for k, c in tcols.items()
    }
    k = min(terr, key=terr.get)
    out["settings"]["min_touch_frames"] = k
    tcol = tcols[k]
    perr = {c: float(np.abs(df[c] * scale / 60 - df.TIME_OF_POSS)[fit & have].mean())
            for c in ("span_run_s", "held_s")}  # fmt: skip
    pcol = min(perr, key=perr.get)
    out["settings"]["possession_time"] = pcol
    t = df[~fit & have]
    s = scale[~fit & have]
    rows["touches"] = _agree(t[tcol] * s, t.TOUCHES)
    rows["time_of_possession_min"] = _agree(t[pcol] * s / 60, t.TIME_OF_POSS)
    busy = t[t.TOUCHES >= 10]
    rows["seconds_per_touch"] = _agree(busy[pcol] / busy[tcol], busy.AVG_SEC_PER_TOUCH)
    rows["dribbles_per_touch"] = _agree(busy.dribbles_run / busy[tcol], busy.AVG_DRIB_PER_TOUCH)

    # the same, summed over each player's games (players with 10+ games in the test half)
    g = test.groupby("person_id")
    tot = pd.DataFrame({"n": g.size(), "d": g[f"d_{clock}_{w}"].sum(), "t": g.t_run.sum(),
                        "nba_d": g.DIST_FEET.sum(), "nba_min": g.nba_min.sum()})  # fmt: skip
    tot = tot[tot.n >= 10]
    rows["avg_speed_mph_by_player"] = _agree(tot.d / tot.t * MPH, tot.nba_d / tot.nba_min / 88)
    out["checks"] = rows
    return out
