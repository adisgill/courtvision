"""Per-possession impact: regularized adjusted plus-minus (RAPM) from rebuilt lineups.

Spec v1.1, section 5.7. Every stint (a stretch with the same ten players) gives two rows,
one per offense: points per 100 possessions relative to league average, weighted by
possessions. Each player has an offense and a defense coefficient, plus one home-court
term. Ridge regression shrinks every coefficient toward a prior:

* ``season-only``: prior 0 (league average); this season's possessions alone.
* ``impact`` (the headline): prior = the player's estimate after the previous season
  (or, for a player new to the league, the average newcomer). Seasons are fitted in order,
  so each season's estimate carries the evidence of every season before it.

The ridge strength ``lambda`` (in possessions) is chosen by 5-fold cross-validation over
games. Standard errors come from the ridge posterior. Everything is written in points per
100 possessions with positive = good for the player's team (defense is negated).
"""

from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

LAMBDAS = (500.0, 1000.0, 2000.0, 3500.0, 5000.0, 8000.0)
CV_SEASONS = (2004, 2009, 2014, 2019, 2024)
FOLDS = 5
QUALIFY_POSS = 2000  # offense + defense possessions for a rated full season
TAU = 1.0  # year-to-year drift in a player's true offense or defense (pts/100), for SEs


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
    """Normal equations of one season, kept per CV fold so folds can be left out."""

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
        if folds:  # only the cross-validation seasons keep per-fold matrices (memory)
            self.A_fold = [self._gram(self.fold == k) for k in range(FOLDS)]
            self.b_fold = [self._rhs(self.fold == k) for k in range(FOLDS)]
            self.A, self.b = sum(self.A_fold), sum(self.b_fold)
        else:
            everything = np.ones(len(df), dtype=bool)
            self.A, self.b = self._gram(everything), self._rhs(everything)
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

    def solve(self, lam: float, prior: np.ndarray, A=None, b=None) -> np.ndarray:
        A = self.A if A is None else A
        b = self.b if b is None else b
        pen = np.full(self.p, lam)
        pen[-1] = 1e-6  # home court is not shrunk
        return np.linalg.solve(A + np.diag(pen), b + pen * prior)

    def cv_error(self, lam: float, prior: np.ndarray) -> float:
        """Possession-weighted squared error on held-out games."""
        err = 0.0
        for k in range(FOLDS):
            beta = self.solve(lam, prior, self.A - self.A_fold[k], self.b - self.b_fold[k])
            mask = self.fold == k
            pred = (beta[self.idx[mask]] * self.val[mask]).sum(axis=1)
            err += float((self.w[mask] * (self.y[mask] - pred) ** 2).sum())
        return err / float(self.w.sum())

    def prior_from(self, last: dict[int, tuple[float, float]], newcomer: tuple[float, float]):
        prior = np.zeros(self.p)
        for i, pid in enumerate(self.players):
            o, d = last.get(int(pid), newcomer)
            prior[2 * i], prior[2 * i + 1] = o, d
        return prior

    def posterior_se(self, lam: float, beta: np.ndarray) -> tuple[np.ndarray, float]:
        """Ridge posterior standard errors (prior variance sigma2 / lam) and sigma2."""
        pred = (beta[self.idx] * self.val).sum(axis=1)
        dof = max(len(self.w) - self.p, 1)
        sigma2 = float((self.w * (self.y - pred) ** 2).sum()) / dof
        pen = np.full(self.p, lam)
        pen[-1] = 1e-6
        return np.sqrt(sigma2 * np.diag(np.linalg.inv(self.A + np.diag(pen)))), sigma2


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


def _sequential(seasons: dict[int, Season], lam: float, newcomer, cv_at=()) -> tuple:
    """Fit seasons in order with last season's estimates as the prior."""
    last: dict[int, tuple[float, float]] = {}
    fits, cv = {}, []
    for s in sorted(seasons):
        S = seasons[s]
        prior = S.prior_from(last, newcomer)
        if s in cv_at:
            cv.append(S.cv_error(lam, prior))
        beta = S.solve(lam, prior)
        fits[s] = (beta, prior)
        for i, pid in enumerate(S.players):
            last[int(pid)] = (beta[2 * i], beta[2 * i + 1])
    return fits, (float(np.mean(cv)) if cv else None)


def build(
    con: duckdb.DuckDBPyConnection,
    seasons: list[int] | None = None,
    lambdas: tuple[float, float] | None = None,
) -> pd.DataFrame:
    """Fit every season and write ``metrics.player_impact`` and ``metrics.impact_lambda``.

    ``lambdas`` = (season-only, prior-informed) skips the cross-validation and keeps the
    existing ``impact_cv`` / ``impact_lambda`` tables (for quick re-runs).
    """
    seasons = seasons or [
        r[0]
        for r in con.execute(
            "select distinct season from metrics.possessions order by 1"
        ).fetchall()
    ]
    data: dict[int, Season] = {}
    cv_at = [] if lambdas else [s for s in CV_SEASONS if s in seasons] or sorted(seasons)[-2:-1]
    for s in seasons:
        df = stint_rows(con, s)
        if len(df) > 1000:
            data[s] = Season(df, folds=s in cv_at)
    cv_at = [s for s in cv_at if s in data]
    log.info("impact: %d seasons of stints", len(data))
    # 1) season-only lambda
    if lambdas:
        only = {lambdas[0]: float("nan")}
    else:
        only = {lam: np.mean([data[s].cv_error(lam, np.zeros(data[s].p)) for s in cv_at])
                for lam in LAMBDAS}  # fmt: skip
    lam_only = min(only, key=only.get)
    # 2) average newcomer from season-only fits (first season in the data)
    seen: set[int] = set()
    new_o, new_d, new_w = [], [], []
    for s in sorted(data):
        S = data[s]
        beta = S.solve(lam_only, np.zeros(S.p))
        for i, pid in enumerate(S.players):
            if s > min(data) and int(pid) not in seen:
                new_o.append(beta[2 * i])
                new_d.append(beta[2 * i + 1])
                new_w.append(S.poss_off[i] + S.poss_def[i])
        seen.update(int(p) for p in S.players)
    newcomer = (float(np.average(new_o, weights=new_w)), float(np.average(new_d, weights=new_w)))
    # 3) prior-informed lambda
    if lambdas:
        seq = {lambdas[1]: float("nan")}
    else:
        seq = {lam: _sequential(data, lam, newcomer, cv_at)[1] for lam in LAMBDAS}
    lam_seq = min(seq, key=seq.get)
    log.info("impact: lambda season-only %s, prior-informed %s; newcomer prior %s",
             lam_only, lam_seq, newcomer)  # fmt: skip
    fits, _ = _sequential(data, lam_seq, newcomer)
    ses = carried_se(fits, data, lam_seq)

    rows = []
    for s, (beta, _prior) in fits.items():
        S = data[s]
        se = ses[s]
        only_beta = S.solve(lam_only, np.zeros(S.p))
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
                   sqrt(sum(poss * (impact - 0) ^ 2) / sum(poss)
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
    if lambdas:
        return out
    lam = pd.DataFrame(
        [{"mode": "season_only", "lambda": lam_only, "cv_mse": only[lam_only]},
         {"mode": "prior_informed", "lambda": lam_seq, "cv_mse": seq[lam_seq]}]
    )  # fmt: skip
    lam["newcomer_off"], lam["newcomer_def"] = newcomer[0], -newcomer[1]
    grid = pd.DataFrame(
        [{"mode": "season_only", "lambda": k, "cv_mse": v} for k, v in only.items()]
        + [{"mode": "prior_informed", "lambda": k, "cv_mse": v} for k, v in seq.items()]
    )
    con.register("lam_df", lam)
    con.register("grid_df", grid)
    con.execute("create or replace table metrics.impact_lambda as select * from lam_df")
    con.execute("create or replace table metrics.impact_cv as select * from grid_df")
    con.unregister("lam_df")
    con.unregister("grid_df")
    return out
