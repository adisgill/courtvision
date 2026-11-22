"""Expected possession value (EPV): the points the team with the ball will still add to the
scoreboard this possession, given the floor at this frame (after Cervone et al. 2016).

**Target.** For a frame on a possession (``state.Possession``: the other team holding the
ball 12 frames ends it), the play-by-play points the offense scores from here to the end of
the possession that aren't on the scoreboard yet. "On the scoreboard" follows a simulated
score feed (:func:`score_feed`): a made field goal goes up half a second after the ball
reaches the rim (the release frame comes from the T1 alignment); a free throw, or a basket
the tracking didn't match, at the end of the possession it belongs to or half a second of
game clock after its play-by-play time, whichever is first. So every point is counted
exactly once, either on the scoreboard or in the EPV of the possession it comes from.

**Inputs** (:func:`raw_features`, all causal): shot clock, how long the team has had the
ball, who holds it (an attacker, a defender, nobody), the holder's distance to the hoop and
nearest defender, the value of a shot from his spot (the T2 location model), defenders
between him and the hoop (transition), the best open teammate, spacing, defenders in the
paint, the ball's height and speed toward the hoop when nobody holds it (a shot or pass in
the air), and dead-ball states (free throws, inbounds).

**Model.** Least squares on a spline basis (:func:`basis`), numpy only. Trained on games
before 2016-01-01 (out-of-fold within them, two folds by game id), tested on the rest.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from courtvision.tracking import shotquality as sq
from courtvision.tracking import state
from courtvision.tracking.detect import FPS

log = logging.getLogger(__name__)

HOOP = np.array([5.25, 25.0])  # every frame is mirrored so the offense attacks this hoop
FT_SPOT = np.array([19.0, 25.0])  # the free-throw line, 15 ft from the backboard
EVERY = FPS  # one training row a second
LAG_PBP = 3.0  # s: the play-by-play logs a basket up to ~3 s after it (T3)
RIM_KNOWN = 12  # frames after the ball reaches the rim that a make is on the scoreboard
RIDGE = 1.0

RAW = ["sc", "sc_missing", "age", "held_off", "held_def", "dh", "three_h", "cd_h", "lv_h",
       "ahead", "bd", "bz", "vt", "vz", "best_other", "open_count", "spacing", "paint_def",
       "off_three", "ft", "inbound", "running", "missing", "gc"]  # fmt: skip
CLOCK_ONLY = ["sc", "sc_missing", "age", "running", "gc"]  # what a score-and-clock feed knows


def out_dir() -> Path:
    from courtvision.config import get_paths

    d = get_paths().data / "tracking" / "live"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- shot value


def location_table() -> dict:
    """P(make) on a grid of distance (0-60 ft by 0.5) x angle (0-90 degrees by 5) x three,
    from the T2 location-only shot model refitted on the training games' shots."""
    from courtvision.tracking import nbacom

    df = pd.read_parquet(sq.features_path())
    if "date" not in df:
        df = df.merge(nbacom.games()[["game_id", "date"]], on="game_id")
    train = df[pd.to_datetime(df.date).dt.date < sq.CUTOFF]
    m = sq.Logistic(lam=1.0).fit(sq.design(train, "location"), train.made.to_numpy(float))
    dist = np.arange(0, 60.01, 0.5)
    ang = np.arange(0, 90.01, 5.0)
    d, a = np.meshgrid(dist, ang, indexing="ij")
    p = np.empty((2, len(dist), len(ang)))
    for t in (0, 1):
        grid = pd.DataFrame({"distance": d.ravel(), "angle": a.ravel(),
                             "three": np.full(d.size, bool(t))})  # fmt: skip
        z = m._z(sq.design(grid, "location")) @ m.w
        p[t] = (1 / (1 + np.exp(-z))).reshape(d.shape)
    return {"p": p.tolist(), "dist_step": 0.5, "angle_step": 5.0}


def shot_value(table: dict, dist: np.ndarray, angle: np.ndarray, three: np.ndarray) -> np.ndarray:
    """Expected points of a shot from these spots (any shape), by table lookup."""
    p = np.asarray(table["p"]) if not isinstance(table["p"], np.ndarray) else table["p"]
    di = np.clip(np.rint(np.nan_to_num(dist, nan=60.0) / table["dist_step"]), 0, p.shape[1] - 1)
    ai = np.clip(np.rint(np.nan_to_num(angle) / table["angle_step"]), 0, p.shape[2] - 1)
    t = three.astype(np.int64)
    return p[t, di.astype(np.int64), ai.astype(np.int64)] * (2 + t)


# --------------------------------------------------------------------------- features


def raw_features(
    ball, xy, ids, hold, offense, hoop, shot_clock, game_clock, running, age_s, vball, table: dict
) -> np.ndarray:
    """(T, len(RAW)) inputs for frames (T, ...): one row per frame, from that frame plus the
    causal history already folded into ``running``, ``age_s`` and ``vball``. The engine calls
    it with T = 1, the training tables with every sampled frame; the code is the same."""
    T = len(ball)
    o = np.where(offense >= 0, offense, 0).astype(np.int64)
    flip = (hoop == 1)[:, None]
    px = np.where(flip, 94.0 - xy[:, :, 0], xy[:, :, 0]).astype(np.float64)
    py = xy[:, :, 1].astype(np.float64)
    valid = (ids > 0) & np.isfinite(px) & np.isfinite(py)
    oi = o[:, None] * 5 + np.arange(5)
    di = (1 - o)[:, None] * 5 + np.arange(5)
    ox, oy, ov = (np.take_along_axis(a, oi, 1) for a in (px, py, valid))
    dx, dy, dv = (np.take_along_axis(a, di, 1) for a in (px, py, valid))

    dist_o = np.hypot(ox - HOOP[0], oy - HOOP[1])
    ang_o = np.degrees(np.arctan2(np.abs(oy - HOOP[1]), np.abs(ox - HOOP[0])))
    three_o = (dist_o > 23.75) | ((np.abs(oy - HOOP[1]) > 22.0) & (ox < 14.0))
    dd = np.hypot(ox[:, :, None] - dx[:, None, :], oy[:, :, None] - dy[:, None, :])
    dd = np.where(ov[:, :, None] & dv[:, None, :], dd, np.inf)
    od = np.minimum(dd.min(axis=2), 15.0)  # nearest defender to each attacker
    lv_o = np.where(ov, shot_value(table, dist_o, ang_o, three_o & ov), 0.0)
    dist_d = np.where(dv, np.hypot(dx - HOOP[0], dy - HOOP[1]), np.inf)

    held_off = (hold >= 0) & (hold // 5 == offense) & (offense >= 0)
    held_def = (hold >= 0) & (hold // 5 != offense) & (offense >= 0)
    jh = np.clip(np.where(held_off, hold - o * 5, 0), 0, 4)[:, None]
    hx, hy = np.take_along_axis(ox, jh, 1)[:, 0], np.take_along_axis(oy, jh, 1)[:, 0]
    bx = np.where(flip[:, 0], 94.0 - ball[:, 0], ball[:, 0]).astype(np.float64)
    by, bz = ball[:, 1].astype(np.float64), np.nan_to_num(ball[:, 2].astype(np.float64), nan=5.0)
    bd = np.nan_to_num(np.hypot(bx - HOOP[0], by - HOOP[1]), nan=40.0)
    dh = np.where(held_off, np.take_along_axis(dist_o, jh, 1)[:, 0], bd)
    dh = np.nan_to_num(dh, nan=40.0)
    three_h = held_off & np.take_along_axis(three_o, jh, 1)[:, 0]
    cd_h = np.where(held_off, np.take_along_axis(od, jh, 1)[:, 0], 6.0)
    lv_h = np.where(held_off, np.take_along_axis(lv_o, jh, 1)[:, 0], 0.0)
    ahead = (dist_d < (dh - 1.0)[:, None]).sum(axis=1)
    other = lv_o * np.minimum(od, 10.0) / 10.0
    other = np.where(held_off[:, None] & (np.arange(5) == jh), -np.inf, other)
    other = np.where(ov, other, -np.inf)
    best_other = np.max(other, axis=1)
    best_other = np.where(np.isfinite(best_other), best_other, 0.0)
    open_count = ((od > 8.0) & ov).sum(axis=1)
    pair = np.hypot(ox[:, :, None] - ox[:, None, :], oy[:, :, None] - oy[:, None, :])
    iu = np.triu_indices(5, 1)
    pv = (ov[:, :, None] & ov[:, None, :])[:, iu[0], iu[1]]
    pd_ = np.where(pv, pair[:, iu[0], iu[1]], 0.0)
    spacing = np.where(pv.sum(1) > 0, pd_.sum(1) / np.maximum(pv.sum(1), 1), 20.0)
    paint_def = (dv & (dx < 19.0) & (np.abs(dy - HOOP[1]) < 8.0)).sum(axis=1)
    off_three = (three_o & ov).sum(axis=1)

    vx = np.where(flip[:, 0], -vball[:, 0], vball[:, 0])
    to = np.stack([HOOP[0] - bx, HOOP[1] - by], axis=1)
    vt = (vx * to[:, 0] + vball[:, 1] * to[:, 1]) / np.maximum(np.hypot(*to.T), 1e-6)
    vt = np.nan_to_num(vt)
    still = ~running
    ft = held_off & still & (np.hypot(hx - FT_SPOT[0], hy - FT_SPOT[1]) < 3.0)
    oob = (hx < 0) | (hx > 94) | (hy < 0) | (hy > 50)
    inbound = held_off & still & oob
    sc = np.asarray(shot_clock, np.float64)
    gc = np.asarray(game_clock, np.float64)
    sc_missing = ~np.isfinite(sc)
    sc_eff = np.clip(np.where(sc_missing, gc, np.minimum(np.nan_to_num(sc), gc)), 0, 24)
    missing = (~valid).sum(axis=1)
    cols = [sc_eff, sc_missing, np.clip(age_s, 0, 30), held_off, held_def, np.clip(dh, 0, 60),
            three_h, cd_h, lv_h, ahead, np.clip(bd, 0, 60), np.clip(bz, 0, 20),
            np.clip(vt, -40, 40), np.clip(np.nan_to_num(vball[:, 2]), -40, 40), best_other,
            open_count, np.clip(spacing, 0, 50), paint_def, off_three, ft, inbound, running,
            missing, np.clip(gc, 0, 720)]  # fmt: skip
    out = np.column_stack([np.asarray(c, np.float64) for c in cols])
    assert out.shape == (T, len(RAW))
    return out


def basis(R: np.ndarray, kind: str = "film") -> np.ndarray:
    """Model inputs from raw features: intercept plus spline terms and interactions.
    ``kind="clock"`` uses only the shot clock, possession age, clock running and game
    clock, the possession information a score-and-clock feed has."""
    c = {name: R[:, i] for i, name in enumerate(RAW)}
    sc_b = sq.ns_basis(c["sc"], [1, 4, 8, 14, 20, 24])
    age_b = sq.ns_basis(c["age"], [1, 3, 6, 12, 24])
    end = (c["gc"] < 24).astype(float)
    cols = [
        np.ones(len(R)),
        sc_b,
        c["sc_missing"],
        age_b,
        c["running"],
        end,
        end * np.clip(c["gc"], 0, 24) / 24,
    ]
    if kind == "film":
        h, d = c["held_off"], c["held_def"]
        loose = 1 - h - d
        cols += [
            h, d,
            h[:, None] * sq.ns_basis(c["dh"], [2, 6, 12, 20, 26, 35]),
            h * c["three_h"],
            h[:, None] * sq.ns_basis(c["cd_h"], [1, 3, 5, 8, 12]),
            h * c["lv_h"], h * c["lv_h"] * np.minimum(c["cd_h"], 10) / 10,
            h * c["ahead"], h * (c["ahead"] <= 2), h * (c["ahead"] <= 1),
            loose[:, None] * sq.ns_basis(c["bd"], [2, 6, 15, 30]),
            loose * c["bz"] / 10, loose * ((c["bz"] > 8) & (c["bd"] < 25)),
            loose * ((c["bz"] > 8) & (c["bd"] < 25)) * np.clip(c["vt"], -30, 30) / 30,
            loose * np.clip(c["vt"], -30, 30) / 30, loose * np.clip(c["vz"], -30, 30) / 30,
            c["best_other"], c["open_count"], c["spacing"] / 10, c["paint_def"],
            c["off_three"], c["ft"], c["inbound"], c["missing"] > 0,
            c["ft"] * (c["sc_missing"] + (c["gc"] < 24)),
            (1 - c["running"]) * h,
        ]  # fmt: skip
    return np.column_stack([np.asarray(x, np.float64).reshape(len(R), -1) for x in cols])


# --------------------------------------------------------------------------- score feed


def _points(pbp: pd.DataFrame) -> np.ndarray:
    made = pbp.actionType.eq("Made Shot")
    ft = pbp.actionType.eq("Free Throw") & ~pbp.description.str.startswith("MISS")
    three = pbp.description.str.contains("3PT")
    return np.where(made, np.where(three, 3, 2), np.where(ft, 1, 0))


def segments(offense: np.ndarray, period: np.ndarray) -> pd.DataFrame:
    """Runs of one team on offense within a period: ``a``, ``b`` (first and last frame),
    ``team``; frames with no offense yet are left out."""
    n = len(offense)
    new = np.ones(n, bool)
    new[1:] = (offense[1:] != offense[:-1]) | (period[1:] != period[:-1])
    a = np.flatnonzero(new)
    b = np.append(a[1:] - 1, n - 1)
    s = pd.DataFrame({"a": a, "b": b, "team": offense[a].astype(int), "period": period[a]})
    return s[s.team >= 0].reset_index(drop=True)


def score_feed(
    pbp: pd.DataFrame,
    shots: pd.DataFrame,
    home_id: int,
    period: np.ndarray,
    clock: np.ndarray,
    offense: np.ndarray,
) -> dict:
    """Every scoring play with when it reaches the scoreboard (``known``, a frame index;
    ``n`` = never within the frames) and the possession segment it belongs to (-1 none),
    plus per frame: ``margin`` (home minus away, on the scoreboard) and ``pending`` (points
    the offense's current possession will still add)."""
    n = len(period)
    seg = segments(offense, period)
    seg_of = np.full(n, -1, np.int64)
    for k, r in enumerate(seg.itertuples(index=False)):
        seg_of[r.a : r.b + 1] = k
    ev = pbp.assign(pts=_points(pbp))
    ev = ev[ev.pts > 0].copy()
    ev["side"] = np.where(ev.team_id == home_id, 0, 1)
    rel = shots.set_index("actionNumber")
    bounds = {int(p): (int(np.argmax(period == p)), int(n - np.argmax(period[::-1] == p)))
              for p in np.unique(period)}  # fmt: skip
    known, owner = [], []
    for e in ev.itertuples(index=False):
        p, t, side = int(e.period), float(e.t), int(e.side)
        if p not in bounds:  # a period with no frames: on the board from the next one on
            later = np.flatnonzero(period > p)
            known.append(int(later[0]) if len(later) else n)
            owner.append(-1)
            continue
        p0, p1 = bounds[p]
        c = clock[p0:p1]
        past = np.flatnonzero(c < t - 0.5)
        by_clock = p0 + int(past[0]) if len(past) else p1
        if e.actionType == "Made Shot" and e.actionNumber in rel.index:
            s = rel.loc[e.actionNumber]
            s = s.iloc[0] if isinstance(s, pd.DataFrame) else s
            r = int(s.release)
            rim = int(s.end) if np.isfinite(s.end) else r + FPS
            k = int(seg_of[r]) if 0 <= r < n else -1
            if k >= 0 and seg.team[k] != side:
                k = -1
            kn = min(max(rim + RIM_KNOWN, r + 1), n)
            if k >= 0:
                kn = min(kn, int(seg.b[k]) + 1)
            known.append(kn)
            owner.append(k)
            continue
        cand = seg[(seg.period == p) & (seg.team == side)
                   & (clock[seg.b] - LAG_PBP <= t) & (t <= clock[seg.a] + 0.5)]  # fmt: skip
        if len(cand):
            k = int(cand.index[-1])
            later = np.flatnonzero(c[int(seg.a[k]) - p0 :] < t - 0.5)
            kc = int(seg.a[k]) + int(later[0]) if len(later) else p1
            known.append(min(int(seg.b[k]) + 1, kc))
            owner.append(k)
        else:
            known.append(by_clock)
            owner.append(-1)
    ev["known"] = np.asarray(known, np.int64)
    ev["segment"] = np.asarray(owner, np.int64)
    signed = np.where(ev.side == 0, ev.pts, -ev.pts)
    delta = np.zeros(n + 1)
    np.add.at(delta, ev.known.to_numpy(), signed)
    margin = np.cumsum(delta)[:n]
    diff = np.zeros(n + 1)
    mine = ev[ev.segment >= 0]
    np.add.at(diff, seg.a.to_numpy()[mine.segment.to_numpy()], mine.pts.to_numpy())
    np.add.at(diff, mine.known.to_numpy(), -mine.pts.to_numpy())
    pending = np.cumsum(diff)[:n]
    return {"events": ev, "margin": margin, "pending": pending, "segments": seg}


# --------------------------------------------------------------------------- training rows


def seconds_left(period: np.ndarray, clock: np.ndarray) -> np.ndarray:
    """Seconds left in regulation, or in the overtime being played."""
    return np.where(period <= 4, (4 - period) * 720.0 + clock, clock)


def game_inputs(game_id: str) -> dict:
    """Everything the engine and the training rows need for one game."""
    from courtvision.config import get_paths
    from courtvision.tracking import detect, sportvu

    data = get_paths().data / "tracking"
    frames = sportvu.load_frames(game_id)
    g = detect.Game.from_frames(frames)
    home_id, away_id = int(frames.home_team_id.iloc[0]), int(frames.away_team_id.iloc[0])
    pbp = pd.read_parquet(data / "pbp_2015.parquet", filters=[("game_id", "==", game_id)])
    shots = pd.read_parquet(data / "shots" / f"{game_id}.parquet")
    st = state.track(g.period, g.ball, g.xy, g.ids)
    feed = score_feed(pbp, shots, home_id, g.period, g.clock, st["offense"])
    return {"game_id": game_id, "g": g, "home_id": home_id, "away_id": away_id, "st": st,
            "feed": feed}  # fmt: skip


def frame_features(gi: dict, idx: np.ndarray, table: dict) -> np.ndarray:
    """Raw features at frames ``idx`` of a game (batch)."""
    g, st = gi["g"], gi["st"]
    running = state.clock_running(g.clock, g.period)
    vball = state.ball_velocity(g.ball)
    age = (idx - st["start"][idx]) / FPS
    return raw_features(g.ball[idx], g.xy[idx], g.ids[idx], st["holder"][idx],
                        st["offense"][idx], st["hoop"][idx], g.shot_clock[idx], g.clock[idx],
                        running[idx], age, vball[idx], table)  # fmt: skip


def game_rows(game_id: str, table: dict) -> pd.DataFrame:
    gi = game_inputs(game_id)
    g, st, feed = gi["g"], gi["st"], gi["feed"]
    idx = np.arange(0, len(g.ts), EVERY)
    R = frame_features(gi, idx, table)
    df = pd.DataFrame(R.astype(np.float32), columns=RAW)
    ids = g.ids[idx]
    for j, s in enumerate(state_slots()):
        df[s] = ids[:, j]
    df.insert(0, "game_id", game_id)
    df.insert(1, "frame", idx)
    df.insert(2, "period", g.period[idx].astype(np.int8))
    df.insert(3, "s_left", seconds_left(g.period[idx], g.clock[idx]).astype(np.float32))
    df.insert(4, "offense", st["offense"][idx])
    df.insert(5, "margin", feed["margin"][idx].astype(np.int16))
    df.insert(6, "pending", feed["pending"][idx].astype(np.int8))
    df["home_id"], df["away_id"] = gi["home_id"], gi["away_id"]
    return df


def state_slots() -> list[str]:
    from courtvision.tracking.sportvu import SLOTS

    return [f"{s}_id" for s in SLOTS]


def _rows_safe(args: tuple[str, dict]) -> pd.DataFrame | dict:
    gid, table = args
    try:
        return game_rows(gid, table)
    except Exception as e:  # one bad game shouldn't stop the rest
        return {"error": f"{gid}: {type(e).__name__}: {e}"}


def rows_path() -> Path:
    return out_dir() / "rows.parquet"


def model_path() -> Path:
    return out_dir() / "epv_model.json"


def build(workers: int = 6) -> dict:
    """One row a second for every game -> ``data/tracking/live/rows.parquet``."""
    from concurrent.futures import ProcessPoolExecutor

    from courtvision.tracking import sportvu

    table = location_table()
    (out_dir() / "location_table.json").write_text(json.dumps(table))
    gids = sportvu.game_ids()
    parts, errors = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(_rows_safe, [(g, table) for g in gids], chunksize=4):
            (errors.append(r["error"]) if isinstance(r, dict) else parts.append(r))
    df = pd.concat(parts, ignore_index=True)
    df.to_parquet(rows_path(), index=False)
    return {"games": len(parts), "rows": len(df), "errors": errors[:10]}


# --------------------------------------------------------------------------- the model


def _solve(XtX: np.ndarray, Xty: np.ndarray) -> np.ndarray:
    pen = RIDGE * np.eye(len(XtX))
    pen[0, 0] = 0.0
    return np.linalg.solve(XtX + pen, Xty)


def _split(df: pd.DataFrame) -> np.ndarray:
    """True for training games (before the T2 cutoff)."""
    from courtvision.tracking import nbacom

    dates = nbacom.games().set_index("game_id").date
    d = pd.to_datetime(df.game_id.map(dates)).dt.date
    return (d < sq.CUTOFF).to_numpy()


def fit(df: pd.DataFrame | None = None) -> dict:
    """Fit EPV (and the clock-only baseline) on the training games, out-of-fold within them,
    and score both on the test games -> ``epv_model.json``, ``epv`` columns in the rows and
    ``epv_report.json``."""
    df = pd.read_parquet(rows_path()) if df is None else df
    on = (df.offense >= 0).to_numpy()
    train = _split(df)
    fold = df.game_id.str[-1].astype(int).to_numpy() % 2
    R = df[RAW].to_numpy(np.float64)
    y = df.pending.to_numpy(np.float64)
    out = {}
    for kind in ("film", "clock"):
        X = basis(R, kind)
        pred = np.zeros(len(df))
        fits = {}
        for f in (0, 1, None):
            m = on & train & ((fold != f) if f is not None else True)
            fits[f] = _solve(X[m].T @ X[m], X[m].T @ y[m])
        for f in (0, 1):
            m = train & (fold == f)
            pred[m] = X[m] @ fits[1 - f]  # out of fold
        pred[~train] = X[~train] @ fits[None]
        pred = np.maximum(pred, 0.0)  # a linear fit can dip below zero; points can't
        pred[~on] = 0.0
        df[f"epv_{kind}" if kind == "clock" else "epv"] = pred.astype(np.float32)
        out[kind] = fits[None]
    vbar = float(df.epv[on & train].mean())
    table = json.loads((out_dir() / "location_table.json").read_text())
    model = {"raw": RAW, "w": out["film"].tolist(), "w_clock": out["clock"].tolist(),
             "vbar": vbar, "table": table, "every": EVERY}  # fmt: skip
    model_path().write_text(json.dumps(model))
    df.to_parquet(rows_path(), index=False)
    rep = evaluate(df, train, on)
    (out_dir() / "epv_report.json").write_text(json.dumps(rep, indent=2))
    return rep


def _r2(y, p) -> float:
    return float(1 - np.mean((y - p) ** 2) / np.var(y))


def evaluate(df: pd.DataFrame, train: np.ndarray, on: np.ndarray) -> dict:
    """Out-of-sample: how much of the variation in the points still to come the floor state
    explains, vs the clock alone and vs a constant; calibration by decile; a few states."""
    t = df[~train & on]
    y = t.pending.to_numpy(float)
    cal = pd.DataFrame({"epv": t.epv, "y": y})
    cal["bin"] = pd.qcut(cal.epv, 10, labels=False, duplicates="drop")
    cal = cal.groupby("bin").agg(predicted=("epv", "mean"), actual=("y", "mean"),
                                 rows=("y", "size")).round(3)  # fmt: skip
    starts = t[t.age < 1.0]
    states = {
        "transition (holder with 0-1 defenders ahead)": t[(t.held_off == 1) & (t.ahead <= 1)],
        "half court, shot clock 4 s or less": t[(t.held_off == 1) & (t.ahead >= 4) & (t.sc <= 4)],
        "holder open (nearest defender 6+ ft) inside 8 ft": t[(t.held_off == 1) & (t.cd_h >= 6)
                                                               & (t.dh < 8)],
        "free-throw setup": t[t.ft == 1],
        "defense has the ball (possession about to change)": t[t.held_def == 1],
    }  # fmt: skip
    return {
        "test_rows": len(t), "test_games": int(t.game_id.nunique()),
        "mean_pending": round(float(y.mean()), 3),
        "r2_film": round(_r2(y, t.epv.to_numpy()), 4),
        "r2_clock_only": round(_r2(y, t.epv_clock.to_numpy()), 4),
        "rmse_film": round(float(np.sqrt(np.mean((y - t.epv) ** 2))), 4),
        "rmse_clock_only": round(float(np.sqrt(np.mean((y - t.epv_clock) ** 2))), 4),
        "rmse_constant": round(float(np.std(y)), 4),
        "epv_at_possession_start": round(float(starts.epv.mean()), 3),
        "points_after_possession_start": round(float(starts.pending.mean()), 3),
        "calibration": cal.reset_index().to_dict("records"),
        "states": {k: {"rows": len(v), "epv": round(float(v.epv.mean()), 3),
                       "actual": round(float(v.pending.mean()), 3)}
                   for k, v in states.items()},
    }  # fmt: skip


class Model:
    """The fitted EPV model, for the engine: ``value(raw)``."""

    def __init__(self, path: Path | None = None) -> None:
        m = json.loads((path or model_path()).read_text())
        self.w = np.asarray(m["w"])
        self.w_clock = np.asarray(m["w_clock"])
        self.vbar = float(m["vbar"])
        self.table = {**m["table"], "p": np.asarray(m["table"]["p"])}

    def value(self, R: np.ndarray) -> np.ndarray:
        return np.maximum(basis(R, "film") @ self.w, 0.0)

    def value_clock(self, R: np.ndarray) -> np.ndarray:
        return np.maximum(basis(R, "clock") @ self.w_clock, 0.0)
