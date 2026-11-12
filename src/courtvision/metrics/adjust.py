"""Opponent-adjusted team ratings (spec v1.1, section 5.8).

For each season, measure and tier, every regular-season team-game value is modelled as

    value(team, game) = mu + a[team] + b[opponent] + h * (+1 home / -1 away)

by least squares with a tiny ridge (to pin the free constant). ``b[opponent]`` is what
that opponent does to everyone's value on this measure: a strong defense pulls offenses'
Shooting down, a weak one pushes it up. A team's **schedule effect** is the mean of
``b[opponent] + h * site`` over its own games, and its adjusted season value is its raw
season value minus that effect: what it would have posted against an average schedule.
The adjusted value is rated on the same team-season reference (mu, sigma) as the raw one.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd


def schedule_effects(g: pd.DataFrame) -> pd.Series:
    """Mean opponent + site effect per team for one (season, measure, tier) group.

    ``g`` has columns team_id, opp_id, home (bool), value (one row per team-game).
    """
    teams = np.sort(pd.unique(np.concatenate([g.team_id.values, g.opp_id.values])))
    n = len(teams)
    pos = pd.Series(np.arange(n), index=teams)
    ti, oi = pos.loc[g.team_id].to_numpy(), pos.loc[g.opp_id].to_numpy()
    site = np.where(g.home.to_numpy(), 1.0, -1.0)
    m = len(g)
    x = np.zeros((m, 2 * n + 2))
    x[np.arange(m), ti] = 1.0
    x[np.arange(m), n + oi] = 1.0
    x[:, 2 * n] = site
    x[:, 2 * n + 1] = 1.0  # intercept
    y = g.value.to_numpy(float)
    pen = np.full(2 * n + 2, 1e-3)
    pen[-2:] = 0.0
    beta = np.linalg.solve(x.T @ x + np.diag(pen), x.T @ y)
    b = beta[n : 2 * n]
    # centre the opponent effects on the games actually played
    b = b - np.bincount(oi, minlength=n).dot(b) / m
    effect = b[oi] + beta[2 * n] * site
    return pd.Series(effect).groupby(g.team_id.to_numpy()).mean()


def build(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    games = con.execute(
        """
        select r.season, r.tier, r.measure, r.team_id, r.value,
               case when r.team_id = g.home_team_id then g.away_team_id
                    else g.home_team_id end as opp_id,
               r.team_id = g.home_team_id as home
        from metrics.ratings_long r
        join stg.games g using (game_id)
        where r.level = 'team_game' and r.season_type = 'regular' and r.value is not null
        """
    ).df()
    out = []
    for (season, tier, measure), g in games.groupby(["season", "tier", "measure"], sort=False):
        if g.team_id.nunique() < 4:
            continue
        eff = schedule_effects(g)
        out.append(pd.DataFrame({"season": season, "tier": tier, "measure": measure,
                                 "team_id": eff.index.astype("int64"),
                                 "schedule": eff.to_numpy()}))  # fmt: skip
    sched = pd.concat(out, ignore_index=True)
    con.register("sched_df", sched)
    con.execute(
        """
        create or replace table metrics.team_season_adj as
        select r.season, r.team_id, r.tier, r.measure, r.value,
               s.schedule, r.value - s.schedule as value_adj,
               r.rating,
               case when ref.sigma > 0 then greatest(0, least(100,
                    50 + 10 * (r.value - s.schedule - ref.mu) / ref.sigma)) end as rating_adj
        from metrics.ratings_long r
        join sched_df s using (season, team_id, tier, measure)
        left join metrics.reference_stats ref
          on ref.level = 'team_season' and ref.season = r.season and ref.tier = r.tier
         and ref.measure = r.measure
        where r.level = 'team_season' and r.season_type = 'regular'
        """
    )
    con.unregister("sched_df")
    return sched
