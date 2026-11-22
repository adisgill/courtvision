"""Shot quality: the chance a shot goes in, from what the tracking shows at its release.

**Features** (``features``; one row per field goal matched to the tracking, measured at the
release frame, never after it):

- where: distance and angle to the hoop, three or two (from the play-by-play);
- the defense: the closest defender's distance, whether he's between the shooter and the
  hoop, how fast he was closing over the last 0.4 s, and how many defenders are within 6 ft;
- the shooter: speed toward the hoop and across, how long he'd had the ball, and how many
  times he dribbled (``movement.touches``), so catch-and-shoot vs off the dribble;
- the moment: shot clock, end-of-quarter heaves, seconds since his team got the ball
  (transition), a putback (the ball was at the rim in the last 3 s), and the offense's
  spacing (convex-hull area of the five).

Nothing that happens after the release (the ball's flight, a block) is used.

**Model** (``fit``): logistic regression in numpy (natural cubic splines for the curved terms,
a small ridge penalty, Newton steps), per the spec's decision D5. Trained on games before
January 1, 2016 and judged on the January games it never saw, against a location-only model
with the same machinery, so the gain is what the tracking adds. A shooter-skill term (his
shrunk make rate over expected, from earlier games only) comes last.

**Shot-making** per player is points minus expected points, with a split-half reliability test.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ProcessPoolExecutor
from datetime import date

import numpy as np
import pandas as pd

from courtvision.config import get_paths
from courtvision.tracking import detect, movement, sportvu
from courtvision.tracking.detect import FPS, HOOPS

log = logging.getLogger(__name__)

CUTOFF = date(2016, 1, 1)  # train before, test from
DEFENDER_BUCKETS = (2.0, 4.0, 6.0)  # NBA.com's closest-defender ranges: 0-2, 2-4, 4-6, 6+ ft


def shots_dir():
    return get_paths().data / "tracking" / "shots"


def features_path():
    return get_paths().data / "tracking" / "shot_features.parquet"


# --------------------------------------------------------------------------- features


def _run_start(team: np.ndarray, period: np.ndarray) -> np.ndarray:
    """For each frame, the first frame of the current possession run (same team, same period)."""
    n = len(team)
    new = np.ones(n, bool)
    new[1:] = (team[1:] != team[:-1]) | (period[1:] != period[:-1])
    idx = np.where(new, np.arange(n), 0)
    return np.maximum.accumulate(idx)


def game_features(game_id: str) -> pd.DataFrame:
    """One row per matched field goal in ``game_id``, measured at its release."""
    shots = pd.read_parquet(shots_dir() / f"{game_id}.parquet")
    if shots.empty:
        return pd.DataFrame()
    g = detect.Game.from_frames(sportvu.load_frames(game_id))
    n = len(g.ts)
    hold = detect.holder(g)
    run = detect.clock_running(g)
    poss = movement.possession(g, hold)
    start = _run_start(poss, g.period)
    tch = movement.touches(g, hold, run)
    z = np.nan_to_num(g.ball[:, 2], nan=9.0)
    low = z < movement.BOUNCE
    dh, _ = detect.hoop_distance(g.ball[:, :2])
    rim = (dh < 2.0) & (z > 7.5) & (z < 11.5)

    rows = []
    for s in shots.itertuples(index=False):
        j = int(s.release)
        if j < 10 or j >= n:
            continue
        # the logged shooter, if he's on the floor at the release; else the tracked one
        on = np.flatnonzero(g.ids[j] == s.person_id)
        k = int(on[0]) if len(on) else int(s.shooter_slot)
        team = k // 5
        hx, hy = HOOPS[int(s.hoop)]
        p = g.xy[j, k].astype(np.float64)
        to_hoop = np.array([hx - p[0], hy - p[1]])
        dist = float(np.hypot(*to_hoop))
        u = to_hoop / max(dist, 1e-6)
        angle = float(np.degrees(np.arctan2(abs(p[1] - hy), abs(p[0] - hx))))

        dsl = slice(5, 10) if team == 0 else slice(0, 5)
        dxy = g.xy[j, dsl].astype(np.float64)
        dd = np.hypot(*(dxy - p).T)
        dd = np.where(np.isnan(dd), np.inf, dd)
        c = int(np.argmin(dd))
        closest = float(dd[c])
        rel = dxy[c] - p
        front = float(rel @ u / max(np.hypot(*rel), 1e-6)) if np.isfinite(closest) else 0.0
        prev = g.xy[j - 10, dsl.start + c].astype(np.float64) - g.xy[j - 10, k]
        closing = float((np.hypot(*prev) - closest) / 0.4) if np.isfinite(closest) else 0.0

        v = np.nan_to_num((g.xy[j, k] - g.xy[j - 5, k]).astype(np.float64) / 0.2)  # ft/s
        closing = closing if np.isfinite(closing) else 0.0  # a position missing 0.4 s earlier
        toward = float(v @ u)
        across = float(abs(v[0] * u[1] - v[1] * u[0]))

        mine = tch[(tch.slot == k) & (tch.start <= j) & (tch.end >= j - 5)]
        if len(mine):
            t0 = int(mine.start.iloc[-1])
            touch_s = (j - t0) / FPS
            lo = low[t0 : j + 1]
            dribbles = int(np.sum(lo[1:] & ~lo[:-1]) + lo[0])
        else:
            touch_s, dribbles = 0.0, 0

        off = g.xy[j, team * 5 : team * 5 + 5].astype(np.float64)
        hull = movement._hull_area(off) if not np.isnan(off).any() else np.nan
        sc = float(g.shot_clock[j])
        rows.append({
            "game_id": game_id, "action_number": int(s.actionNumber),
            "person_id": int(s.person_id), "team_id": int(s.team_id), "period": int(s.period),
            "method": s.method, "shooter_agrees": int(g.ids[j, k]) == int(s.shooter_id),
            "made": s.shotResult == "Made", "three": "3PT" in str(s.description),
            "distance": dist, "angle": angle, "x": float(p[0]), "y": float(p[1]),
            "closest_def": min(closest, 40.0), "def_front": front,
            "closing": float(np.clip(closing, -30, 30)),
            "defs_6ft": int((dd < 6).sum()),
            "speed_toward": float(np.clip(toward, -30, 30)), "speed_across": min(across, 30.0),
            "touch_s": touch_s, "dribbles": dribbles,
            "shot_clock": sc, "game_clock": float(g.clock[j]),
            "poss_s": (j - int(start[j])) / FPS,
            "putback": bool(rim[max(j - 3 * FPS, 0) : j].any()),
            "hull": hull,
        })  # fmt: skip
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    jump = df.distance > 10  # NBA.com's catch-and-shoot and pull-up are jump shots past 10 ft
    df["catch_shoot"] = jump & (df.touch_s <= 2) & (df.dribbles == 0)
    df["pull_up"] = jump & (df.dribbles >= 1)
    df["defender"] = pd.cut(df.closest_def, [-1, *DEFENDER_BUCKETS, 99],
                            labels=["0-2", "2-4", "4-6", "6+"]).astype(str)  # fmt: skip
    return df


def features_safe(game_id: str) -> pd.DataFrame | dict:
    try:
        return game_features(game_id)
    except Exception as e:  # one bad game shouldn't stop the rest
        return {"game_id": game_id, "error": f"{type(e).__name__}: {e}"}


def build_features(workers: int = 6) -> dict:
    """Features for every game's matched shots -> shot_features.parquet."""
    from courtvision.tracking import nbacom

    games = sportvu.game_ids()
    parts, errors = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(features_safe, games):
            (errors.append(r) if isinstance(r, dict) else parts.append(r))
    df = pd.concat(parts, ignore_index=True)
    dates = nbacom.games().set_index("game_id").date
    df["date"] = df.game_id.map(dates)
    df.to_parquet(features_path(), index=False)
    return {"shots": len(df), "games": int(df.game_id.nunique()), "errors": errors[:10]}


# --------------------------------------------------------------------------- the model


def ns_basis(x: np.ndarray, knots) -> np.ndarray:
    """Natural cubic spline basis (linear beyond the end knots): x plus len(knots) - 2 terms."""
    k = np.asarray(knots, float)
    x = np.clip(np.asarray(x, float), k[0], k[-1] + 10 * (k[-1] - k[0]))

    def d(i):
        return (np.maximum(x - k[i], 0) ** 3 - np.maximum(x - k[-1], 0) ** 3) / (k[-1] - k[i])

    cols = [x] + [d(i) - d(len(k) - 2) for i in range(len(k) - 2)]
    return np.column_stack(cols)


def design(df: pd.DataFrame, kind: str) -> np.ndarray:
    """Model inputs. ``kind``: "location" (where only) or "full" (plus what the tracking adds;
    "skill" also adds the shooter-skill column)."""
    dist = df.distance.to_numpy(float)
    three = df.three.to_numpy(float)
    ang = df.angle.to_numpy(float)
    cols = [ns_basis(dist, [0.5, 2, 4, 7, 12, 18, 23, 26, 32]), three[:, None],
            (three * dist)[:, None], ang[:, None], (ang * (dist < 8))[:, None],
            (ang * three)[:, None]]  # fmt: skip
    if kind in ("full", "skill"):
        df = df.fillna({"closing": 0.0, "speed_toward": 0.0, "speed_across": 0.0})
        close = (dist < 8).astype(float)[:, None]
        cd = ns_basis(df.closest_def.clip(0, 25), [0.5, 2, 3.5, 5, 7, 12])
        sc = df.shot_clock.to_numpy(float)
        sc_missing = np.isnan(sc)
        sc = np.where(sc_missing, 12.0, sc)
        cols += [
            cd * close, cd * (1 - close),
            df.def_front.to_numpy(float)[:, None],
            (df.def_front * (df.closest_def < 4)).to_numpy(float)[:, None],
            df.closing.clip(-10, 25).to_numpy(float)[:, None],
            df.defs_6ft.clip(0, 4).to_numpy(float)[:, None],
            df.speed_toward.clip(-15, 25).to_numpy(float)[:, None],
            df.speed_across.clip(0, 20).to_numpy(float)[:, None],
            np.log1p(df.touch_s.clip(0, 24)).to_numpy(float)[:, None],
            np.log1p(df.dribbles.clip(0, 20)).to_numpy(float)[:, None],
            df.catch_shoot.to_numpy(float)[:, None],
            ns_basis(sc, [1, 4, 8, 14, 20, 24]), sc_missing.astype(float)[:, None],
            ((df.game_clock < 3) & (df.distance > 28)).to_numpy(float)[:, None],
            np.log1p(df.poss_s.clip(0, 60)).to_numpy(float)[:, None],
            df.putback.to_numpy(float)[:, None],
            (df.hull.fillna(df.hull.median()) / 100).clip(0, 12).to_numpy(float)[:, None],
        ]  # fmt: skip
    if kind == "skill":
        cols.append(df.skill.to_numpy(float)[:, None])
    return np.column_stack(cols)


class Logistic:
    """Ridge-penalized logistic regression by Newton's method, on standardized inputs."""

    def __init__(self, lam: float = 1.0):
        self.lam = lam

    def fit(self, X: np.ndarray, y: np.ndarray, iters: int = 50) -> Logistic:
        self.mu, self.sd = X.mean(0), X.std(0)
        self.sd[self.sd == 0] = 1.0
        Z = self._z(X)
        w = np.zeros(Z.shape[1])
        R = self.lam * np.eye(Z.shape[1])
        R[0, 0] = 0.0
        for _ in range(iters):
            p = 1 / (1 + np.exp(-(Z @ w)))
            grad = Z.T @ (y - p) - R @ w
            H = (Z * (p * (1 - p))[:, None]).T @ Z + R
            step = np.linalg.solve(H, grad)
            w += step
            if np.abs(step).max() < 1e-8:
                break
        self.w = w
        return self

    def _z(self, X: np.ndarray) -> np.ndarray:
        return np.column_stack([np.ones(len(X)), (X - self.mu) / self.sd])

    def predict(self, X: np.ndarray) -> np.ndarray:
        return 1 / (1 + np.exp(-(self._z(X) @ self.w)))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def auc(y: np.ndarray, p: np.ndarray) -> float:
    r = pd.Series(p).rank().to_numpy()
    pos = y == 1
    return float((r[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * (~pos).sum()))


def calibration(y: np.ndarray, p: np.ndarray, bins: int = 10) -> dict:
    """Deciles of predicted chance vs made rate; the expected calibration error; and the
    recalibration slope (1 = right spread) and intercept (0 = right level)."""
    q = pd.qcut(p, bins, labels=False, duplicates="drop")
    t = pd.DataFrame({"q": q, "p": p, "y": y}).groupby("q").agg(pred=("p", "mean"),
                                                                 made=("y", "mean"),
                                                                 n=("y", "size"))  # fmt: skip
    ece = float((t.n * (t.pred - t.made).abs()).sum() / t.n.sum())
    lp = np.log(np.clip(p, 1e-9, 1 - 1e-9) / np.clip(1 - p, 1e-9, 1))
    m = Logistic(lam=0.0).fit(lp[:, None], y)
    slope = m.w[1] / m.sd[0]
    inter = m.w[0] - slope * m.mu[0]
    return {"ece": round(ece, 4), "slope": round(float(slope), 3),
            "intercept": round(float(inter), 3),
            "deciles": t.round(3).reset_index(drop=True).to_dict("list")}  # fmt: skip


def _scores(y, p) -> dict:
    return {"log_loss": round(log_loss(y, p), 4), "brier": round(float(np.mean((p - y) ** 2)), 4),
            "auc": round(auc(y, p), 4)}  # fmt: skip


def _skill(train: pd.DataFrame, rows: pd.DataFrame, k: float, own_game_out: bool) -> np.ndarray:
    """Each shooter's make rate over expected, shrunk toward 0 by ``k`` shots, from ``train``
    (leaving out the row's own game when ``own_game_out``)."""
    resid = train.made.astype(float) - train.p_base
    tot = resid.groupby(train.person_id).sum()
    cnt = resid.groupby(train.person_id).size()
    s = rows.person_id.map(tot).fillna(0).to_numpy()
    c = rows.person_id.map(cnt).fillna(0).to_numpy()
    if own_game_out:
        key = train.person_id.astype(str) + "|" + train.game_id
        gt = resid.groupby(key).sum()
        gc = resid.groupby(key).size()
        rk = rows.person_id.astype(str) + "|" + rows.game_id
        s = s - rk.map(gt).fillna(0).to_numpy()
        c = c - rk.map(gc).fillna(0).to_numpy()
    return s / (c + k)


def fit(df: pd.DataFrame | None = None, lam: float = 1.0, skill_k: float = 150.0) -> dict:
    """Train on games before ``CUTOFF``, judge on the rest: base rate, location only, full
    tracking, full plus shooter skill. Writes per-shot expected values for every shot (each
    from the model that never saw its game) and the report."""
    df = pd.read_parquet(features_path()) if df is None else df.copy()
    df = df[df.date.notna()].reset_index(drop=True)
    y = df.made.to_numpy(float)
    tr = (pd.to_datetime(df.date) < pd.Timestamp(CUTOFF)).to_numpy()
    te = ~tr

    report: dict = {"shots": len(df), "train_shots": int(tr.sum()), "test_shots": int(te.sum()),
                    "cutoff": CUTOFF.isoformat(), "models": {}}  # fmt: skip
    report["models"]["base_rate"] = _scores(y[te], np.full(te.sum(), y[tr].mean()))
    preds = {}
    for kind in ("location", "full"):
        X = design(df, kind)
        m = Logistic(lam).fit(X[tr], y[tr])
        preds[kind] = m.predict(X)
        report["models"][kind] = _scores(y[te], preds[kind][te]) | {"inputs": X.shape[1]}

    # shooter skill from the full model's residuals: training rows leave their own game out,
    # test rows use the training games only (all earlier in the season)
    df["p_base"] = preds["full"]
    skill = np.zeros(len(df))
    skill[tr] = _skill(df[tr], df[tr], skill_k, own_game_out=True)
    skill[te] = _skill(df[tr], df[te], skill_k, own_game_out=False)
    df["skill"] = skill
    X = design(df, "skill")
    m = Logistic(lam).fit(X[tr], y[tr])
    preds["skill"] = m.predict(X)
    report["models"]["full_plus_skill"] = _scores(y[te], preds["skill"][te]) | {
        "inputs": X.shape[1]
    }

    loc, full = report["models"]["location"]["log_loss"], report["models"]["full"]["log_loss"]
    base = report["models"]["base_rate"]["log_loss"]
    report["gain"] = {
        "location_vs_base_pct": round((base - loc) / base * 100, 2),
        "tracking_vs_location_pct": round((loc - full) / loc * 100, 2),
    }  # fmt: skip
    report["calibration"] = {k: calibration(y[te], preds[k][te]) for k in ("location", "full")}
    subsets = {"rim (<4 ft)": df.distance < 4, "short (4-14 ft)": df.distance.between(4, 14),
               "long two (14+ ft)": (df.distance > 14) & ~df.three, "three": df.three}  # fmt: skip
    report["by_shot"] = {
        name: {"n": int((te & m_).sum()),
               "location": round(log_loss(y[te & m_], preds["location"][te & m_]), 4),
               "full": round(log_loss(y[te & m_], preds["full"][te & m_]), 4),
               "made": round(float(y[te & m_].mean()), 3),
               "xfg_full": round(float(preds["full"][te & m_].mean()), 3)}
        for name, m_ in ((k, v.to_numpy()) for k, v in subsets.items())}  # fmt: skip

    # out-of-sample expected values for every shot: test shots from the model above, training
    # shots from the same model fit on the other games (two folds by game id parity)
    xfg = preds["full"].copy()
    X = design(df, "full")
    for half in (0, 1):
        held = (df.game_id.str[-1].astype(int) % 2 == half).to_numpy() & tr
        fitset = tr & ~held
        xfg[held] = Logistic(lam).fit(X[fitset], y[fitset]).predict(X[held])
    xloc = preds["location"].copy()
    XL = design(df, "location")
    for half in (0, 1):
        held = (df.game_id.str[-1].astype(int) % 2 == half).to_numpy() & tr
        fitset = tr & ~held
        xloc[held] = Logistic(lam).fit(XL[fitset], y[fitset]).predict(XL[held])
    value = np.where(df.three, 3, 2)
    out = df.drop(columns=["p_base", "skill"]).assign(
        xfg=xfg, xfg_location=xloc, xpts=xfg * value, pts=y * value
    )
    out.to_parquet(get_paths().data / "tracking" / "shot_quality.parquet", index=False)
    report["shot_making"] = shot_making(out)
    path = get_paths().data / "tracking" / "shot_quality_report.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    return report


def shot_making(sq: pd.DataFrame, min_shots: int = 150) -> dict:
    """Split-half reliability (odd vs even games) of points over expected per shot, against
    raw points per shot, for players with ``min_shots`` matched shots."""
    sq = sq.assign(half=sq.game_id.str[-1].astype(int) % 2, over=sq.pts - sq.xpts)
    n = sq.groupby("person_id").size()
    keep = n[n >= min_shots].index
    s = sq[sq.person_id.isin(keep)]
    by = s.groupby(["person_id", "half"]).agg(over=("over", "mean"), pps=("pts", "mean"),
                                              xpts=("xpts", "mean")).unstack()  # fmt: skip
    by = by.dropna()

    def r(col):
        return round(float(np.corrcoef(by[(col, 0)], by[(col, 1)])[0, 1]), 3)

    return {"players": len(by), "min_shots": min_shots,
            "split_half_r": {"points_over_expected": r("over"), "points_per_shot": r("pps"),
                             "expected_points_per_shot": r("xpts")}}  # fmt: skip


# --------------------------------------------------------------------------- the known answers


def validate(df: pd.DataFrame | None = None) -> dict:
    """Closest-defender ranges, catch-and-shoot and pull-up counts vs NBA.com's, per player-game.

    Our counts cover matched shots only (about 9 in 10), so per player-game we compare shares:
    of a player's shots in a game, what part had a defender within 2 ft, and so on."""
    from courtvision.tracking import nbacom

    df = pd.read_parquet(features_path()) if df is None else df
    nb = nbacom.load()
    out: dict = {}

    d = nb["defender"].groupby(["game_id", "person_id", "defender"]).FGA.sum().unstack(fill_value=0)
    ours = df.groupby(["game_id", "person_id", "defender"]).size().unstack(fill_value=0)
    both = d.join(ours, lsuffix="_nba", rsuffix="_ours", how="inner")
    cats = ["0-2", "2-4", "4-6", "6+"]
    nba_tot = both[[f"{c}_nba" for c in cats]].sum(axis=1)
    our_tot = both[[f"{c}_ours" for c in cats]].sum(axis=1)
    busy = (nba_tot >= 8) & (our_tot >= 6)
    out["defender_share_overall"] = {
        c: {"nba": round(float(both[f"{c}_nba"].sum() / nba_tot.sum()), 3),
            "ours": round(float(both[f"{c}_ours"].sum() / our_tot.sum()), 3)}
        for c in cats}  # fmt: skip
    a = np.concatenate([(both[f"{c}_ours"] / our_tot)[busy] for c in cats])
    b = np.concatenate([(both[f"{c}_nba"] / nba_tot)[busy] for c in cats])
    out["defender_share_player_game"] = {"cells": len(a),
                                         "r": round(float(np.corrcoef(a, b)[0, 1]), 3)}  # fmt: skip
    out["matched_share_of_nba_fga"] = round(float(our_tot.sum() / nba_tot.sum()), 3)

    for name, col, flag in (("catch_shoot", "CATCH_SHOOT_FGA", "catch_shoot"),
                            ("pull_up", "PULL_UP_FGA", "pull_up")):  # fmt: skip
        t = nb[name][["game_id", "person_id", col]]
        o = df.groupby(["game_id", "person_id"]).agg(n=("made", "size"), k=(flag, "sum"))
        j = t.merge(o.reset_index(), on=["game_id", "person_id"])
        j = j.merge(both.assign(nba_fga=nba_tot)[["nba_fga"]].reset_index(),
                    on=["game_id", "person_id"])  # fmt: skip
        j = j[(j.nba_fga >= 8) & (j.n >= 6)]
        ours_share, nba_share = j.k / j.n, j[col] / j.nba_fga
        out[name] = {"player_games": len(j),
                     "share_nba": round(float(j[col].sum() / j.nba_fga.sum()), 3),
                     "share_ours": round(float(j.k.sum() / j.n.sum()), 3),
                     "r_share": round(float(np.corrcoef(ours_share, nba_share)[0, 1]), 3),
                     }  # fmt: skip
    return out
