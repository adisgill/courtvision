"""Per-possession impact: regularized adjusted plus-minus (RAPM) with a box-score prior.

Spec section 11.4 (v1.1) and 12.1 (v1.2). Every stint (a stretch with the same ten
players) gives two rows, one per offense: points per 100 possessions relative to league
average, weighted by possessions. Each player has an offense and a defense coefficient,
plus one home-court term. Ridge regression shrinks every coefficient toward a prior:

* ``season_only``: prior 0 (league average); this season's possessions alone.
* ``impact`` (the headline): seasons are fitted in order, and each player's prior blends
  two things: his estimate after the previous season, and what his box score this season
  says (a linear **box model** fitted to season-only estimates; ``box_off``/``box_def``).
  The box part counts in proportion to its reliability, ``r = poss / (poss + K_BOX)``,
  so a season a few games old leans on last season. A player new to the league starts
  from the average newcomer, moved toward his box score by the same ``r``.

The ridge strength ``lambda`` (possessions) and the blend ``alpha`` (weight on last
season vs the box model) are chosen by 5-fold cross-validation over games. Everything is
in points per 100 possessions with positive = good for the player's team (defense is
negated). Each season's normal equations are eigendecomposed once, so the search over
``lambda`` and ``alpha`` costs only matrix-vector products.
"""

from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

LAMBDAS = (1000.0, 2000.0, 3500.0, 5000.0, 8000.0, 12000.0)
ALPHAS = (0.0, 0.25, 0.5, 0.75, 1.0)  # weight on last season's estimate in the prior
CV_SEASONS = (2004, 2009, 2014, 2019, 2024)
FOLDS = 5
QUALIFY_POSS = 2000  # offense + defense possessions for a rated full season
TAU = 1.0  # year-to-year drift in a player's true offense or defense (pts/100), for SEs
K_BOX = 1500  # possessions at which this season's box score is half-trusted in the prior
BOX_FEATURES = [
    "pts", "fga", "fg3a", "fta", "ast", "tov", "oreb", "dreb", "stl", "blk", "pf",
    "ts_rel", "usage", "ts_x_usage", "w_fc",
]  # fmt: skip


def stint_rows(con: duckdb.DuckDBPyConnection, season: int) -> pd.DataFrame:
    """One row per (game, stint, offense): points, possessions, the ten players, home."""
    return con.execute(
        """
        with g as (
            select game_id, home_team_id from stg.games
            where season = ? and season_type = 'regular'
        ),
        five as (
            select s.game_id, s.stint_id, s.team_id, list(s.person_id order by s.person_id) p
            from metrics.stints s
            join g using (game_id)
            join metrics.game_pbp_qa q on q.game_id = s.game_id and q.pbp_ok and q.lineups_ok
            group by all
            having count(*) = 5
        )
        select t.game_id, t.pts_fg + t.ftm as pts, t.poss, o.p as off5, d.p as def5,
               t.team_id = g.home_team_id as home
        from metrics.pbp_stint_team t
        join g using (game_id)
        join five o on o.game_id = t.game_id and o.stint_id = t.stint_id
                   and o.team_id = t.team_id
        join five d on d.game_id = t.game_id and d.stint_id = t.stint_id
                   and d.team_id <> t.team_id
        where t.poss > 0
        """,
        [season],
    ).df()


class Season:
    """Normal equations of one season (and of each CV fold), eigendecomposed for speed."""

    def __init__(self, df: pd.DataFrame, folds: bool = False):
        ids = pd.unique(np.concatenate([np.concatenate(df.off5.values),
                                        np.concatenate(df.def5.values)]))  # fmt: skip
        self.players = np.sort(ids)
        n = len(self.players)
        self.p = 2 * n + 1
        pos = pd.Series(np.arange(n), index=self.players)
        off = pos.loc[np.concatenate(df.off5.values)].to_numpy().reshape(-1, 5) * 2
        dfn = pos.loc[np.concatenate(df.def5.values)].to_numpy().reshape(-1, 5) * 2 + 1
        m = len(df)
        self.idx = np.concatenate([off, dfn, np.full((m, 1), self.p - 1)], axis=1)
        self.val = np.concatenate([np.ones((m, 10)), np.where(df.home, 1.0, -1.0)[:, None]],
                                  axis=1)  # fmt: skip
        self.w = df.poss.to_numpy(float)
        rate = 100 * df.pts.to_numpy(float) / self.w
        self.mean = np.average(rate, weights=self.w)
        self.y = rate - self.mean
        codes = pd.factorize(df.game_id)[0]
        self.fold = (codes * 7919) % FOLDS
        self.A = self._gram(np.ones(m, dtype=bool))
        self.b = self._rhs(np.ones(m, dtype=bool))
        self.eig = np.linalg.eigh(self.A)
        self.fold_eig, self.fold_b = [], []
        if folds:  # only the cross-validation seasons need the leave-one-fold-out systems
            for k in range(FOLDS):
                mask = self.fold == k
                self.fold_eig.append(np.linalg.eigh(self.A - self._gram(mask)))
                self.fold_b.append(self.b - self._rhs(mask))
        poss = np.bincount(self.idx[:, :10].ravel(), weights=np.repeat(self.w, 10),
                           minlength=self.p)  # fmt: skip
        self.poss_off, self.poss_def = poss[0:-1:2], poss[1:-1:2]

    def _gram(self, mask: np.ndarray) -> np.ndarray:
        idx, val, w = self.idx[mask], self.val[mask], self.w[mask]
        cells = (idx[:, :, None] * self.p + idx[:, None, :]).ravel()
        vals = (val[:, :, None] * val[:, None, :] * w[:, None, None]).ravel()
        return np.bincount(cells, weights=vals, minlength=self.p * self.p).reshape(self.p, -1)

    def _rhs(self, mask: np.ndarray) -> np.ndarray:
        idx, val = self.idx[mask], self.val[mask]
        wy = (self.w * self.y)[mask]
        return np.bincount(idx.ravel(), weights=(val * wy[:, None]).ravel(), minlength=self.p)

    @staticmethod
    def _solve_eig(eig, b: np.ndarray, lam: float, prior: np.ndarray) -> np.ndarray:
        vals, vecs = eig
        return vecs @ ((vecs.T @ (b + lam * prior)) / (vals + lam))

    def solve(self, lam: float, prior: np.ndarray) -> np.ndarray:
        """Ridge toward ``prior``: (A + lam I) beta = b + lam prior. (The home-court term is
        penalized too; with ~250k possessions behind it the shrinkage is under 2%.)"""
        return self._solve_eig(self.eig, self.b, lam, prior)

    def cv_error(self, lam: float, prior: np.ndarray) -> float:
        """Possession-weighted squared error on held-out games."""
        err = 0.0
        for k in range(FOLDS):
            beta = self._solve_eig(self.fold_eig[k], self.fold_b[k], lam, prior)
            mask = self.fold == k
            pred = (beta[self.idx[mask]] * self.val[mask]).sum(axis=1)
            err += float((self.w[mask] * (self.y[mask] - pred) ** 2).sum())
        return err / float(self.w.sum())

    def prior(self, last: dict, box: dict, newcomer: tuple[float, float], alpha: float):
        """Prior in coefficient space (defense = points allowed, so box defense negated)."""
        out = np.zeros(self.p)
        for i, pid in enumerate(self.players):
            pid = int(pid)
            bo, bd, r = box.get(pid, (0.0, 0.0, 0.0))
            bd = -bd
            if pid in last:
                lo, ld = last[pid]
                out[2 * i] = lo + (1 - alpha) * r * (bo - lo)
                out[2 * i + 1] = ld + (1 - alpha) * r * (bd - ld)
            else:
                no, nd = newcomer
                out[2 * i] = no + r * (bo - no)
                out[2 * i + 1] = nd + r * (bd - nd)
        return out

    def posterior_se(self, lam: float, beta: np.ndarray) -> tuple[np.ndarray, float]:
        """Ridge posterior standard errors (prior variance sigma2 / lam) and sigma2."""
        pred = (beta[self.idx] * self.val).sum(axis=1)
        dof = max(len(self.w) - self.p, 1)
        sigma2 = float((self.w * (self.y - pred) ** 2).sum()) / dof
        vals, vecs = self.eig
        diag = (vecs**2 / (vals + lam)).sum(axis=1)
        return np.sqrt(sigma2 * diag), sigma2


def carried_se(fits, data, lam: float, tau: float = TAU) -> dict[int, np.ndarray]:
    """Per-player standard errors that remember earlier seasons.

    The ridge posterior assumes the same prior width for everyone, so a ten-year veteran
    would look as uncertain as a rookie. Here each season's data adds its own precision
    (the posterior precision minus the prior's, which already accounts for sharing the
    floor with teammates), and last season's precision is carried forward after adding
    ``tau**2`` of year-to-year drift. A player new to the league starts at the prior.
    """
    prec_last: dict[tuple[int, int], float] = {}
    out = {}
    for s, (beta, _prior) in fits.items():
        S = data[s]
        se, sigma2 = S.posterior_se(lam, beta)
        base = lam / sigma2
        info = np.clip(1 / se[:-1] ** 2 - base, 0, None)
        prec = np.empty(len(info))
        for j in range(len(info)):
            key = (int(S.players[j // 2]), j % 2)
            prior = 1 / (1 / prec_last[key] + tau**2) if key in prec_last else base
            prec[j] = prior + info[j]
            prec_last[key] = prec[j]
        out[s] = 1 / np.sqrt(prec)
    return out


def _sequential(data: dict[int, Season], lam: float, alpha: float, box: dict,
                newcomer, cv_at=()) -> tuple:  # fmt: skip
    """Fit seasons in order; each prior blends last season's estimate and the box model."""
    last: dict[int, tuple[float, float]] = {}
    fits, cv = {}, []
    for s in sorted(data):
        S = data[s]
        prior = S.prior(last, box.get(s, {}), newcomer, alpha)
        if s in cv_at:
            cv.append(S.cv_error(lam, prior))
        beta = S.solve(lam, prior)
        fits[s] = (beta, prior)
        for i, pid in enumerate(S.players):
            last[int(pid)] = (beta[2 * i], beta[2 * i + 1])
    return fits, (float(np.mean(cv)) if cv else None)


# ---- box model --------------------------------------------------------------------------


def box_features(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Per player-season box-score rates relative to league, from games with lineups."""
    df = con.execute(
        """
        with g as (
            select * from metrics.player_game_counts
            where season_type = 'regular' and sec_on is not null and season >= 1996
        )
        select g.season, g.person_id, sum(g.poss) as p_off, sum(g.o_poss) as p_def,
               sum(b_pts) pts, sum(b_fga) fga, sum(b_fg3a) fg3a, sum(b_fta) fta,
               sum(b_ast) ast, sum(b_tov) tov, sum(b_oreb) oreb, sum(b_dreb) dreb,
               sum(b_stl) stl, sum(b_blk) blk, sum(b_pf) pf,
               sum(b_fga + 0.44 * b_fta + b_tov) / nullif(sum(fga + 0.44 * fta + tov), 0)
                   as usage,
               any_value(pos.w_fc) as w_fc
        from g left join metrics.positions pos using (person_id)
        group by g.season, g.person_id
        having sum(g.poss) > 0 and sum(g.o_poss) > 0
        """
    ).df()
    off = ["pts", "fga", "fg3a", "fta", "ast", "tov", "oreb"]
    dfn = ["dreb", "stl", "blk", "pf"]
    tsa = 2 * (df.fga + 0.44 * df.fta)
    df["ts"] = df.pts / tsa.where(tsa > 0)
    for c in off:
        df[c] = 100 * df[c] / df.p_off
    for c in dfn:
        df[c] = 100 * df[c] / df.p_def
    df["w_fc"] = df.w_fc.fillna(0.5)
    df["poss"] = df.p_off + df.p_def

    def league(col: pd.Series) -> pd.Series:
        """Possession-weighted season mean of ``col`` (rows where it is known)."""
        w = df.poss.where(col.notna(), 0)
        mean = (col.fillna(0) * w).groupby(df.season).sum() / w.groupby(df.season).sum()
        return df.season.map(mean)

    # relative to the season's league average
    for c in [*off, *dfn, "usage"]:
        df[c] = df[c] - league(df[c])
    df["ts_rel"] = (df.ts - league(df.ts)).fillna(0)
    df["usage"] = df.usage.fillna(0)
    df["ts_x_usage"] = df.ts_rel * df.usage
    return df[["season", "person_id", "poss", *BOX_FEATURES]]


def fit_box(feat: pd.DataFrame, targets: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Weighted least squares of season-only offense and defense on the box features.

    Returns (predictions per player-season, coefficient table)."""
    d = feat.merge(targets, on=["season", "person_id"])
    d = d[d.poss >= 500]
    x = np.column_stack([np.ones(len(d)), d[BOX_FEATURES].to_numpy(float)])
    w = np.sqrt(d.poss.clip(upper=10000).to_numpy(float))
    coef, r2 = {}, {}
    for side in ("off", "def"):
        y = d[f"season_only_{side}"].to_numpy(float)
        beta, *_ = np.linalg.lstsq(x * w[:, None], y * w, rcond=None)
        resid = y - x @ beta
        r2[side] = 1 - np.average(resid**2, weights=w**2) / np.average(
            (y - np.average(y, weights=w**2)) ** 2, weights=w**2
        )
        coef[side] = beta
    xf = np.column_stack([np.ones(len(feat)), feat[BOX_FEATURES].to_numpy(float)])
    pred = feat[["season", "person_id", "poss"]].copy()
    pred["box_off"] = xf @ coef["off"]
    pred["box_def"] = xf @ coef["def"]
    table = pd.DataFrame({"feature": ["intercept", *BOX_FEATURES],
                          "coef_off": coef["off"], "coef_def": coef["def"]})  # fmt: skip
    table.attrs.update(r2_off=r2["off"], r2_def=r2["def"], n=len(d))
    return pred, table


def build(
    con: duckdb.DuckDBPyConnection,
    seasons: list[int] | None = None,
    params: tuple[float, float, float] | None = None,
) -> pd.DataFrame:
    """Fit every season and write ``metrics.player_impact`` and the model tables.

    ``params`` = (season-only lambda, lambda, alpha) skips the cross-validation and keeps
    the existing ``impact_cv`` / ``impact_lambda`` tables (for quick re-runs).
    """
    seasons = seasons or [
        r[0]
        for r in con.execute(
            "select distinct season from metrics.possessions order by 1"
        ).fetchall()
    ]
    cv_at = [] if params else [s for s in CV_SEASONS if s in seasons] or sorted(seasons)[-2:-1]
    data: dict[int, Season] = {}
    for s in seasons:
        df = stint_rows(con, s)
        if len(df) > 1000:
            data[s] = Season(df, folds=s in cv_at)
    cv_at = [s for s in cv_at if s in data]
    log.info("impact: %d seasons of stints", len(data))

    # 1) season-only fits (lambda by CV) -> targets for the box model, newcomer average
    if params:
        only = {params[0]: float("nan")}
    else:
        only = {lam: np.mean([data[s].cv_error(lam, np.zeros(data[s].p)) for s in cv_at])
                for lam in LAMBDAS}  # fmt: skip
    lam_only = min(only, key=only.get)
    season_only, seen = {}, set()
    new_o, new_d, new_w, targets = [], [], [], []
    for s in sorted(data):
        S = data[s]
        beta = S.solve(lam_only, np.zeros(S.p))
        season_only[s] = beta
        targets.append(pd.DataFrame({"season": s, "person_id": S.players.astype("int64"),
                                     "season_only_off": beta[0:-1:2],
                                     "season_only_def": -beta[1:-1:2]}))  # fmt: skip
        for i, pid in enumerate(S.players):
            if s > min(data) and int(pid) not in seen:
                new_o.append(beta[2 * i])
                new_d.append(beta[2 * i + 1])
                new_w.append(S.poss_off[i] + S.poss_def[i])
        seen.update(int(p) for p in S.players)
    newcomer = (float(np.average(new_o, weights=new_w)), float(np.average(new_d, weights=new_w)))

    # 2) box model and the prior it gives every player-season
    feat = box_features(con)
    box_pred, box_table = fit_box(feat, pd.concat(targets, ignore_index=True))
    box_pred["r"] = box_pred.poss / (box_pred.poss + K_BOX)
    box = {
        s: {int(r.person_id): (r.box_off, r.box_def, r.r) for r in g.itertuples()}
        for s, g in box_pred.groupby("season")
    }
    log.info("impact: box model R2 offense %.3f, defense %.3f",
             box_table.attrs["r2_off"], box_table.attrs["r2_def"])  # fmt: skip

    # 3) lambda and alpha for the headline estimate
    if params:
        lam_seq, alpha = params[1], params[2]
        grid = {(lam_seq, alpha): float("nan")}
    else:
        grid = {(lam, a): _sequential(data, lam, a, box, newcomer, cv_at)[1]
                for lam in LAMBDAS for a in ALPHAS}  # fmt: skip
        lam_seq, alpha = min(grid, key=grid.get)
    log.info("impact: lambda season-only %s; headline lambda %s, alpha %s", lam_only,
             lam_seq, alpha)  # fmt: skip
    fits, _ = _sequential(data, lam_seq, alpha, box, newcomer)
    ses = carried_se(fits, data, lam_seq)

    rows = []
    for s, (beta, _prior) in fits.items():
        S = data[s]
        se = ses[s]
        only_beta = season_only[s]
        n = len(S.players)
        o, d = beta[0:-1:2], -beta[1:-1:2]
        rows.append(pd.DataFrame({
            "season": s,
            "person_id": S.players.astype("int64"),
            "poss_off": S.poss_off, "poss_def": S.poss_def,
            "impact_off": o, "impact_def": d, "impact": o + d,
            "impact_se": np.sqrt(se[0::2] ** 2 + se[1::2] ** 2),
            "season_only_off": only_beta[0:-1:2], "season_only_def": -only_beta[1:-1:2],
            "home_court": np.full(n, beta[-1] * 2),
            "league_ppp100": np.full(n, S.mean),
        }))  # fmt: skip
    out = pd.concat(rows, ignore_index=True)
    out = out.merge(box_pred[["season", "person_id", "box_off", "box_def"]],
                    on=["season", "person_id"], how="left")  # fmt: skip
    out["box"] = out.box_off + out.box_def
    out["season_only"] = out.season_only_off + out.season_only_def
    out["poss"] = out.poss_off + out.poss_def
    # points added over league average this season (offense + defense)
    out["points_added"] = (out.impact_off * out.poss_off + out.impact_def * out.poss_def) / 100

    con.register("impact_df", out)
    con.execute(
        f"""
        create or replace table metrics.player_impact as
        with f as (
            select i.*, lb.season_frac,
                   i.poss >= {QUALIFY_POSS} * lb.season_frac as qualified
            from impact_df i join metrics.league_baselines lb using (season)
        ),
        ref as (
            select season,
                   sum(impact * poss) / sum(poss) as mu,
                   sqrt(sum(poss * impact ^ 2) / sum(poss)
                        - (sum(impact * poss) / sum(poss)) ^ 2) as sigma
            from f where qualified group by season
        )
        select f.* exclude (season_frac),
               case when ref.sigma > 0
                    then greatest(0, least(100, 50 + 10 * (f.impact - ref.mu) / ref.sigma))
               end as impact_rating
        from f left join ref using (season)
        """
    )
    con.unregister("impact_df")
    box_table["r2_off"] = box_table.attrs["r2_off"]
    box_table["r2_def"] = box_table.attrs["r2_def"]
    con.register("box_df", box_table)
    con.execute("create or replace table metrics.impact_box_model as select * from box_df")
    con.unregister("box_df")
    if params:
        return out
    lam = pd.DataFrame(
        [{"mode": "season_only", "lambda": lam_only, "alpha": None, "cv_mse": only[lam_only]},
         {"mode": "headline", "lambda": lam_seq, "alpha": alpha,
          "cv_mse": grid[(lam_seq, alpha)]}]
    )  # fmt: skip
    lam["newcomer_off"], lam["newcomer_def"] = newcomer[0], -newcomer[1]
    cv = pd.DataFrame(
        [{"mode": "season_only", "lambda": k, "alpha": None, "cv_mse": v}
         for k, v in only.items()]
        + [{"mode": "headline", "lambda": k[0], "alpha": k[1], "cv_mse": v}
           for k, v in grid.items()]
    )  # fmt: skip
    con.register("lam_df", lam)
    con.register("cv_df", cv)
    con.execute("create or replace table metrics.impact_lambda as select * from lam_df")
    con.execute("create or replace table metrics.impact_cv as select * from cv_df")
    con.unregister("lam_df")
    con.unregister("cv_df")
    return out
