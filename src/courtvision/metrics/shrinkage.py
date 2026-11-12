"""Small-sample shrinkage constants (docs/metrics_spec.md section 4).

Player-season values are shrunk toward league average as ``v * n / (n + k)``, where
``k`` is the exposure at which the measure's split-half reliability is 0.5. It is
estimated, not chosen: split every qualified player-season into odd and even games
(by date), compute the measure on each half, correlate the halves (``r``) at mean half
exposure ``n_h``, and solve ``r = n_h / (n_h + k)`` for ``k = n_h (1 - r) / r``. That is
the Spearman-Brown formula from the spec, solved at the half-season exposure; both give
the same ``k``.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from courtvision.metrics import measures as M
from courtvision.metrics import rate

K_MAX = 1e12  # r <= 0: the measure carries no stable signal; shrink fully


def _half_tables(con: duckdb.DuckDBPyConnection, first: int, last: int) -> None:
    sums = ",\n    ".join(f"sum({c}) as {c}" for c in rate.PLAYER_SUM_COLS)
    pres = ",\n    ".join(
        f"sum(on_{c}) as on_{c}, sum(tm_{c}) - sum(on_{c}) as off_{c}" for c in rate.PRESENCE_COLS
    )
    con.execute(f"""
create or replace temp table half_player as
select season, 'regular' as season_type, person_id, 0 as team_id, half, count(*) as games,
    any_value(w_bc) as w_bc, any_value(w_fc) as w_fc,
    {sums},
    0.2 as share, sum(b_plays) as bcfc_den_b
from (
    select *, (row_number() over (partition by person_id, season
                                  order by game_date, game_id) - 1) % 2 as half
    from metrics.player_game_counts
    where season_type = 'regular' and season between {first} and {last}
)
group by season, person_id, half;

create or replace temp table half_presence as
select season, 'regular' as season_type, person_id, team_id, half, count(*) as games,
    {pres}
from (
    select *, (row_number() over (partition by person_id, team_id, season
                                  order by game_date, game_id) - 1) % 2 as half
    from metrics.presence_player_game
    where season_type = 'regular' and season between {first} and {last}
)
group by season, person_id, team_id, half;
""")
    for h in (0, 1):
        con.execute(
            f"create or replace temp table half_player_{h} as "
            f"select * from half_player where half = {h}"
        )
        con.execute(
            f"create or replace temp table half_presence_{h} as "
            f"select * from half_presence where half = {h}"
        )


def estimate(
    con: duckdb.DuckDBPyConnection, starts: dict[str, int], first: int = 2013, last: int = 2025
) -> pd.DataFrame:
    """Estimate k for every player-season measure; write ``metrics.shrinkage_constants``."""
    _half_tables(con, first, last)
    frames = []
    for h in (0, 1):
        q = rate.values_select(
            "half", f"half_player_{h}", ["team_id", "person_id"], "player", starts
        )
        p = rate.presence_values_select(f"half_presence_{h}")
        df = pd.concat(
            [
                con.sql(q).df(),
                con.sql(p).df().drop(columns=["on_poss", "off_poss"]),
            ]
        )
        df["half"] = h
        frames.append(df)
    keys = ["season", "person_id", "team_id", "tier", "measure", "family"]
    both = frames[0].merge(frames[1], on=keys, suffixes=("_0", "_1"))

    rows = []
    for (tier, measure, family), g in both.groupby(["tier", "measure", "family"]):
        season_min = M.QUALIFY.get(family, (0, 0))[0]
        g = g[(g.exposure_0 >= season_min / 2) & (g.exposure_1 >= season_min / 2)]
        if len(g) < 30:
            continue
        r = float(g.value_0.corr(g.value_1))
        n_h = float(((g.exposure_0 + g.exposure_1) / 2).mean())
        k = n_h * (1 - r) / r if r > 0 else K_MAX
        rows.append(
            {
                "tier": tier,
                "measure": measure,
                "family": family,
                "seasons": f"{first}-{last}",
                "n_pairs": len(g),
                "r_half": r,
                "n_half": n_h,
                "r_full": 2 * r / (1 + r) if r > -1 else None,
                "k": min(k, K_MAX),
            }
        )
    out = pd.DataFrame(rows)
    con.register("shrinkage_df", out)
    con.execute("create or replace table metrics.shrinkage_constants as select * from shrinkage_df")
    con.unregister("shrinkage_df")
    return out
