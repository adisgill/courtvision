"""Validation of the built ratings (``courtvision validate``; notebooks/validation.ipynb).

Four questions, each answered from the built ``metrics`` schema:

1. **Stabilization**: how much exposure does each player measure need before it is
   more signal than noise? (split-half reliability, the shrinkage constant ``k``)
2. **Persistence**: do team ratings in the first half of a season predict the same
   ratings in the second half?
3. **Signal beyond Four Factors / net rating**: do first-half dimension components
   predict second-half net rating better than first-half net rating or the Four Factors?
4. **Tier agreement (T7)**: how closely do box-score tiers track the play-by-play tier
   where both exist?
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from courtvision.metrics import measures as M
from courtvision.metrics import rate

FIRST, LAST = 1996, 2025  # complete play-by-play seasons used for team validation

COMPONENTS = [
    "shooting_sq", "shooting_sm", "defense_dq", "defense_dm", "defense_dt", "defense_df",
    "defense_dr", "ball_bt", "ball_bo",
]  # fmt: skip


def stabilization(con) -> pd.DataFrame:
    """Split-half reliability per player-season measure and the exposure it takes to
    reach reliability 0.5 (``k``) and 0.7."""
    df = con.sql(
        "select tier, measure, family, n_pairs, r_half, n_half, k "
        "from metrics.shrinkage_constants order by family, measure, tier"
    ).df()
    df["n_for_r70"] = df.k * 0.7 / 0.3
    return df


def _team_halves(con, first: int = FIRST, last: int = LAST) -> pd.DataFrame:
    """Team values over the first and second halves of each regular season."""
    sums = ",\n    ".join(f"sum({c}) as {c}" for c in rate.TEAM_SUM_COLS)
    starts = M.tier_starts(con)
    frames = []
    for half in (0, 1):
        table = f"""(
    select season, 'regular' as season_type, team_id, {sums},
           1.0 as share, sum(b_poss) as bcfc_den_b,
           100 * (sum(pts_fg + ftm) - sum(o_pts_fg + o_ftm)) / nullif(sum(poss), 0) as net_rtg
    from (
        select *, (row_number() over (partition by season, team_id
                                      order by game_date, game_id) - 1)
                  >= count(*) over (partition by season, team_id) / 2 as half
        from metrics.team_game_counts
        where season_type = 'regular' and pbp_ok and season between {first} and {last}
    )
    where half = {bool(half)}
    group by season, team_id
)"""
        ms = [m for m in M.MEASURES if m.levels in ("both", "team") and m.tier == "A"]
        vals = pd.concat(
            [con.sql(rate.measure_select(m, "half", table, ["team_id"], starts)).df() for m in ms]
        )
        wide = vals.pivot_table(
            index=["season", "team_id"], columns="measure", values="value"
        ).reset_index()
        extra = _four_factors(con, table)
        wide = wide.merge(extra, on=["season", "team_id"])
        wide["half"] = half
        frames.append(wide)
    return pd.concat(frames)


def _four_factors(con, table: str) -> pd.DataFrame:
    # 3PM per team comes from the box score (b_fg3m); play-by-play has made threes too,
    # but the box score is the textbook Four Factors source.
    return con.sql(f"""
        select season, team_id, net_rtg,
               (b_fgm + 0.5 * b_fg3m) / nullif(b_fga, 0) as off_efg,
               b_tov / nullif(b_fga + 0.44 * b_fta + b_tov, 0) as off_tov,
               b_oreb / nullif(b_oreb + o_b_dreb, 0) as off_orb,
               b_ftm / nullif(b_fga, 0) as off_ftr,
               (o_b_fgm + 0.5 * o_b_fg3m) / nullif(o_b_fga, 0) as def_efg,
               o_b_tov / nullif(o_b_fga + 0.44 * o_b_fta + o_b_tov, 0) as def_tov,
               b_dreb / nullif(b_dreb + o_b_oreb, 0) as def_drb,
               o_b_ftm / nullif(o_b_fga, 0) as def_ftr
        from {table}
    """).df()


def persistence(halves: pd.DataFrame) -> pd.DataFrame:
    """First-half vs second-half correlation of each team measure (pooled seasons)."""
    a = halves[halves.half == 0].set_index(["season", "team_id"])
    b = halves[halves.half == 1].set_index(["season", "team_id"])
    cols = [c for c in a.columns if c not in ("half",) and c in b.columns]
    rows = []
    for c in cols:
        x, y = a[c].align(b[c], join="inner")
        ok = x.notna() & y.notna()
        if ok.sum() > 30:
            rows.append({"measure": c, "r_first_second": float(np.corrcoef(x[ok], y[ok])[0, 1]),
                         "team_seasons": int(ok.sum())})  # fmt: skip
    return pd.DataFrame(rows).sort_values("r_first_second", ascending=False)


def _cv_r2(x: np.ndarray, y: np.ndarray, groups: np.ndarray) -> tuple[float, float]:
    """Leave-one-season-out OLS: out-of-sample R^2 and RMSE."""
    pred = np.empty_like(y)
    for g in np.unique(groups):
        tr, te = groups != g, groups == g
        xt = np.column_stack([np.ones(tr.sum()), x[tr]])
        beta, *_ = np.linalg.lstsq(xt, y[tr], rcond=None)
        pred[te] = np.column_stack([np.ones(te.sum()), x[te]]) @ beta
    resid = y - pred
    return 1 - (resid**2).sum() / ((y - y.mean()) ** 2).sum(), float(np.sqrt((resid**2).mean()))


FOUR_FACTORS = ["off_efg", "off_tov", "off_orb", "off_ftr", "def_efg", "def_tov", "def_drb",
                "def_ftr"]  # fmt: skip


def predictive(halves: pd.DataFrame) -> pd.DataFrame:
    """Predict second-half net rating from first-half information (out of sample)."""
    a = halves[halves.half == 0].set_index(["season", "team_id"])
    b = halves[halves.half == 1].set_index(["season", "team_id"])
    df = a.join(b[["net_rtg"]].rename(columns={"net_rtg": "y"}), how="inner").dropna(
        subset=["y", "net_rtg", *COMPONENTS, *FOUR_FACTORS]
    )
    y = df.y.to_numpy()
    groups = df.index.get_level_values("season").to_numpy()
    models = {
        "first-half net rating": ["net_rtg"],
        "Four Factors (8)": FOUR_FACTORS,
        "dimension components (9)": COMPONENTS,
        "components, quality only (no shot making)": [
            c for c in COMPONENTS if c not in ("shooting_sm", "defense_dm")
        ],
        "net rating + components": ["net_rtg", *COMPONENTS],
        "Four Factors + components": [*FOUR_FACTORS, *COMPONENTS],
    }
    rows = []
    for name, cols in models.items():
        r2, rmse = _cv_r2(df[cols].to_numpy(), y, groups)
        rows.append({"model": name, "features": len(cols), "cv_r2": r2, "cv_rmse": rmse})
    out = pd.DataFrame(rows)
    out.attrs["team_seasons"] = len(df)
    return out


def tier_agreement(con) -> pd.DataFrame:
    """T7: correlation of tier A with tier B / B- / C values on the same units."""
    return con.sql("""
        with a as (select * from metrics.ratings_long where tier = 'A'),
             o as (select * from metrics.ratings_long where tier <> 'A')
        select a.level, a.measure, o.tier as other_tier, count(*) as units,
               corr(a.value, o.value) as r_value
        from a join o
          on a.level = o.level and a.measure = o.measure and a.season = o.season
         and a.season_type = o.season_type
         and a.game_id is not distinct from o.game_id
         and a.team_id = o.team_id and a.person_id is not distinct from o.person_id
        where a.level in ('team_season', 'player_season') and a.season_type = 'regular'
          and not a.low_sample
        group by all
        order by a.level, a.measure, o.tier
    """).df()


def scale_summary(con) -> pd.DataFrame:
    """T5 and the spread of ratings by level."""
    return con.sql("""
        select level, tier, measure, count(*) as units, avg(rating) as mean_rating,
               stddev_pop(rating) as sd_rating,
               avg((rating in (0, 100))::int) as share_clipped
        from metrics.ratings_long
        where season_type = 'regular' and not low_sample and rating is not null
          and measure in ('shooting', 'defense', 'ball_control', 'backcourt', 'frontcourt',
                          'presence')
        group by all order by level, measure, tier
    """).df()


def _md_table(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.itertuples(index=False):
        cells = [format(v, floatfmt) if isinstance(v, float) else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def report(con) -> str:
    """The full validation report as Markdown."""
    stab = stabilization(con)
    halves = _team_halves(con)
    pers = persistence(halves)
    pred = predictive(halves)
    tiers = tier_agreement(con)
    scale = scale_summary(con)
    head = [c for c in pers.measure if c in (*M.HEADLINE, *COMPONENTS, "net_rtg", *FOUR_FACTORS)]
    parts = [
        "# Validation",
        "",
        "Generated by `courtvision validate` from the built `metrics` schema. "
        "Do not edit by hand; `notebooks/validation.ipynb` draws the same numbers as charts.",
        "",
        "## 1. Stabilization (player-season, split-half)",
        "",
        "Each qualified player-season (2013-14 to 2025-26) is split into odd and even "
        "games. `r_half` is the correlation between halves at mean half exposure `n_half`; "
        "`k` is the exposure at which reliability reaches 0.5 (the shrinkage constant) and "
        "`n_for_r70` the exposure for 0.7. Units: FGA (Shooting), on-court opponent "
        "possessions (Defense), plays (Ball control), on-court possessions (Backcourt / "
        "Frontcourt), min(on, off) possessions (Presence).",
        "",
        _md_table(
            stab[["family", "measure", "tier", "n_pairs", "r_half", "n_half", "k", "n_for_r70"]],
            ".2f",
        ),  # fmt: skip
        "",
        "## 2. Persistence (team, first half vs second half of the season)",
        "",
        f"Regular seasons {FIRST}-{(FIRST + 1) % 100:02d} to {LAST}-{(LAST + 1) % 100:02d}, "
        "play-by-play-complete games only. Pooled Pearson correlation.",
        "",
        _md_table(pers[pers.measure.isin(head)]),
        "",
        "## 3. Signal beyond Four Factors and net rating",
        "",
        "Target: second-half net rating (points per 100 possessions). Features: first-half "
        f"values. Leave-one-season-out OLS over {pred.attrs['team_seasons']} team-seasons.",
        "",
        _md_table(pred),
        "",
        "## 4. Tier agreement (acceptance test T7)",
        "",
        "Correlation between the play-by-play tier (A) and the box-score tiers on the same "
        "team-seasons and qualified player-seasons, 1996-97 onward.",
        "",
        _md_table(tiers),
        "",
        "## 5. Scale (acceptance test T5 and spread)",
        "",
        "Regular-season ratings of qualified units for the headline dimensions.",
        "",
        _md_table(scale),
        "",
    ]
    return "\n".join(parts)
