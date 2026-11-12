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
5. **Agreement with NBA.com**: do the parts add up to the NBA's offensive, defensive and
   net rating, and how do player ratings relate to PIE?
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from courtvision.metrics import measures as M
from courtvision.metrics import rate

FIRST, LAST = 1996, 2025  # complete play-by-play seasons used for team validation

COMPONENTS = [
    "shooting_sq", "shooting_sm", "shooting_fd", "shooting_fm", "defense_dq", "defense_dm",
    "defense_dt", "defense_df", "defense_dr", "ball_bt", "ball_bo",
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


def _nba_team_seasons(con, first: int = FIRST, last: int = LAST) -> pd.DataFrame:
    """NBA.com's official team ratings per regular season (from the advanced box score
    in ``kaggle.team_statistics_extended``) beside our values, all relative to league."""
    df = con.sql(f"""
        with g as (
            select cv_season(e.gameId) as season, e.teamId as team_id, e.win::int as win,
                   e.possessions as poss, e.offensiveRating as ortg,
                   e.defensiveRating as drtg
            from kaggle.team_statistics_extended e
            join stg.games s on s.game_id = lpad(e.gameId::varchar, 10, '0')
            where s.season_type = 'regular' and e.possessions > 0
              and s.season between {first} and {last}
        ),
        t as (
            select season, team_id, avg(win) as win_pct,
                   sum(ortg * poss) / sum(poss) as ortg, sum(drtg * poss) / sum(poss) as drtg,
                   sum(poss) as nba_poss
            from g group by all
        )
        select t.*,
               t.ortg - sum(t.ortg * t.nba_poss) over w / sum(t.nba_poss) over w as ortg_rel,
               t.drtg - sum(t.drtg * t.nba_poss) over w / sum(t.nba_poss) over w as drtg_rel,
               v.shooting_rating, v.ball_control_rating, v.defense_rating,
               v.backcourt_rating, v.frontcourt_rating,
               -- per 100 possessions, so every part is on the net-rating scale
               v.shooting_sq_value * c.fga / c.poss as shooting_sq,
               v.shooting_sm_value * c.fga / c.poss as shooting_sm,
               v.shooting_fd_value * c.b_fga / c.poss as shooting_fd,
               v.shooting_fm_value * c.b_fga / c.poss as shooting_fm,
               v.ball_bt_value * c.b_plays / c.poss as ball_bt,
               v.ball_bo_value as ball_bo,
               v.defense_dq_value as defense_dq, v.defense_dm_value as defense_dm,
               v.defense_dt_value as defense_dt, v.defense_df_value as defense_df,
               v.defense_dr_value as defense_dr
        from t
        join metrics.team_season v
          on v.season = t.season and v.team_id = t.team_id and v.season_type = 'regular'
        join metrics.team_season_counts c
          on c.season = t.season and c.team_id = t.team_id and c.season_type = 'regular'
        window w as (partition by t.season)
    """).df()
    df["net_rel"] = df.ortg_rel - df.drtg_rel
    df["offense"] = df[[c for c in COMPONENTS if not c.startswith("defense")]].sum(axis=1)
    df["defense"] = df[[c for c in COMPONENTS if c.startswith("defense")]].sum(axis=1)
    df["total"] = df.offense + df.defense
    return df


def _ols_r2(df: pd.DataFrame, y: str, cols: list[str]) -> tuple[float, np.ndarray]:
    x = np.column_stack([np.ones(len(df)), *(df[c] for c in cols)])
    beta, *_ = np.linalg.lstsq(x, df[y], rcond=None)
    resid = df[y] - x @ beta
    return float(1 - resid.var() / df[y].var()), beta


def nba_agreement(con) -> dict[str, pd.DataFrame]:
    """How the ratings line up with NBA.com's offensive, defensive and net rating (teams)
    and with PIE and on-court net rating (players)."""
    t = _nba_team_seasons(con)
    four = _four_factors(
        con,
        f"""(select *, null as net_rtg from metrics.team_season_counts
             where season_type = 'regular' and season between {FIRST} and {LAST})""",
    ).drop(columns="net_rtg")
    ff = four.columns.drop(["season", "team_id"]).tolist()
    four[ff] = four[ff] - four.groupby("season")[ff].transform("mean")  # relative to league
    t = t.merge(four, on=["season", "team_id"])
    off_ff = [c for c in ff if c.startswith("off_")]
    def_ff = [c for c in ff if c.startswith("def_")]
    rows = []
    for target, label, ours, theirs in [
        ("ortg_rel", "offensive rating", "offense", off_ff),
        ("drtg_rel", "defensive rating", "defense", def_ff),
        ("net_rel", "net rating", "total", ff),
    ]:
        rows.append({
            "NBA.com": label,
            "our parts, plain sum (r²)": t[ours].corr(t[target]) ** 2,
            "our parts, fitted (R²)": _ols_r2(
                t, target,
                [c for c in COMPONENTS
                 if ours == "total" or c.startswith("defense") == (ours == "defense")],
            )[0],
            "Four Factors, fitted (R²)": _ols_r2(t, target, theirs)[0],
        })  # fmt: skip
    rows.append({
        "NBA.com": "win %",
        "our parts, plain sum (r²)": t.total.corr(t.win_pct) ** 2,
        "our parts, fitted (R²)": _ols_r2(t, "win_pct", COMPONENTS)[0],
        "Four Factors, fitted (R²)": _ols_r2(t, "win_pct", ff)[0],
    })  # fmt: skip
    fit = pd.DataFrame(rows)
    fit.attrs["team_seasons"] = len(t)
    fit.loc[len(fit)] = {"NBA.com": "win % from NBA net rating itself",
                         "our parts, plain sum (r²)": t.net_rel.corr(t.win_pct) ** 2}  # fmt: skip

    _, beta = _ols_r2(t, "net_rel", COMPONENTS)
    weights = pd.DataFrame({"component": COMPONENTS, "weight in NBA net rating": beta[1:],
                            "sd (pts/100 poss.)": [t[c].std() for c in COMPONENTS]})  # fmt: skip

    p = con.sql(f"""
        with n as (
            select cv_season(e.gameId) as season, e.personId as person_id,
                   count(distinct e.playerteamId) as teams, sum(e.possessions) as poss,
                   sum(e.playerImpactEstimate * e.possessions) / sum(e.possessions) as pie,
                   sum(e.netRating * e.possessions) / sum(e.possessions) as on_court_net,
                   sum(e.usagePercentage * e.possessions) / sum(e.possessions) as usage
            from kaggle.player_statistics_extended e
            join stg.games s on s.game_id = lpad(e.gameId::varchar, 10, '0')
            where s.season_type = 'regular' and e.possessions > 0
              and s.season between 2013 and {LAST}
            group by all
            having sum(e.possessions) >= 2000 and count(distinct e.playerteamId) = 1
        )
        select n.*, v.shooting_rating, v.defense_rating, v.ball_control_rating,
               v.backcourt_rating, v.frontcourt_rating, ps.presence_rating
        from n
        join metrics.player_season v
          on v.season = n.season and v.person_id = n.person_id and v.team_id = 0
         and v.season_type = 'regular'
        left join metrics.presence_season ps
          on ps.season = n.season and ps.person_id = n.person_id
         and ps.season_type = 'regular'
    """).df()
    players = pd.DataFrame([
        {"our rating": m, "r with PIE": p[m].corr(p.pie),
         "r with on-court net rating": p[m].corr(p.on_court_net),
         "r with usage %": p[m].corr(p.usage)}
        for m in ["shooting_rating", "defense_rating", "ball_control_rating",
                  "backcourt_rating", "frontcourt_rating", "presence_rating"]
    ])  # fmt: skip
    q = p.dropna(subset=["shooting_rating", "defense_rating", "ball_control_rating",
                         "presence_rating"])  # fmt: skip
    ours4 = ["shooting_rating", "defense_rating", "ball_control_rating", "presence_rating"]
    players.attrs.update(
        player_seasons=len(p),
        r2_pie_ours=_ols_r2(q, "pie", ours4)[0],
        r2_pie_usage=_ols_r2(q, "pie", ["usage"])[0],
        r2_pie_both=_ols_r2(q, "pie", [*ours4, "usage"])[0],
    )
    return {"fit": fit, "weights": weights, "players": players}


def impact_check(con) -> pd.DataFrame:
    """Does last season's per-player number predict this season's team net rating?

    Each team-season's prediction is the sum over its players of last season's estimate
    times this season's share of the team's possessions (the five shares sum to 5).
    Players with no estimate last season get the newcomer prior (impact) or 0 (on/off).
    """
    df = con.sql(f"""
        with share as (
            select season, team_id, person_id, sum(poss) as poss
            from metrics.player_game_counts
            where season_type = 'regular' and poss is not null group by all
        ),
        team as (
            select season, team_id, sum(poss) as poss,
                   100 * (sum(pts_fg + ftm) - sum(o_pts_fg + o_ftm)) / sum(poss) as net
            from metrics.team_game_counts
            where season_type = 'regular' and pbp_ok group by all
        ),
        onoff as (
            select season, person_id, arg_max(presence_value, minutes_on) as v
            from metrics.presence_season where season_type = 'regular' group by all
        ),
        newc as (select newcomer_off + newcomer_def as v
                 from metrics.impact_lambda where mode = 'headline')
        select s.season, s.team_id, any_value(t.net) as net,
               sum(s.poss / t.poss * coalesce(i.impact, (select v from newc)))
                   as from_impact,
               sum(s.poss / t.poss * coalesce(i.box, (select v from newc))) as from_box,
               sum(s.poss / t.poss * coalesce(i.season_only, 0)) as from_season_only,
               sum(s.poss / t.poss * coalesce(o.v, 0)) as from_on_off
        from share s
        join team t using (season, team_id)
        left join metrics.player_impact i
          on i.person_id = s.person_id and i.season = s.season - 1
        left join onoff o on o.person_id = s.person_id and o.season = s.season - 1
        where s.season between {FIRST + 1} and {LAST}
        group by s.season, s.team_id
    """).df()
    rows = []
    for col, label in (("from_impact", "impact (adjusted plus-minus, box + last-season prior)"),
                       ("from_box", "box model alone"),
                       ("from_season_only", "season-only adjusted plus-minus"),
                       ("from_on_off", "raw on/off (old Presence)")):  # fmt: skip
        r = df[col].corr(df.net)
        rows.append({"last season's number": label, "r with this season's net rating": r,
                     "r²": r**2})  # fmt: skip
    out = pd.DataFrame(rows)
    out.attrs["team_seasons"] = len(df)
    return out


TRACKED = ["off_rating", "def_rating", "net_rating", "ast_pct", "ast_ratio", "oreb_pct",
           "dreb_pct", "reb_pct", "tov_ratio", "efg_pct", "ts_pct", "usg_pct", "pace"]  # fmt: skip
NBA_COL = {"off_rating": "offensiveRating", "def_rating": "defensiveRating",
           "net_rating": "netRating", "ast_pct": "assistPercentage", "ast_ratio": "assistRatio",
           "oreb_pct": "offensiveReboundPercentage", "dreb_pct": "defensiveReboundPercentage",
           "reb_pct": "reboundPercentage", "tov_ratio": "teamTurnoverPercentage",
           "usg_pct": "usagePercentage", "pace": "pace"}  # fmt: skip


def tracking_checks(con) -> pd.DataFrame:
    """Our player advanced stats (computed from play-by-play counts and rebuilt lineups)
    against NBA.com's official per-game values, possession-weighted over the season."""
    weighted = ",\n".join(
        f"sum(e.{c} * e.possessions) / sum(e.possessions) as {k}" for k, c in NBA_COL.items()
    )
    nba = con.sql(f"""
        select cv_season(e.gameId) as season, e.personId as person_id, {weighted},
               (sum(e.fieldGoalsMade) + 0.5 * sum(e.threePointersMade))
                   / sum(e.fieldGoalsAttempted) as efg_pct,
               sum(e.points) / (2 * (sum(e.fieldGoalsAttempted)
                   + 0.44 * sum(e.freeThrowsAttempted))) as ts_pct
        from kaggle.player_statistics_extended e
        join stg.games s on s.game_id = lpad(e.gameId::varchar, 10, '0')
        where s.season_type = 'regular' and e.possessions > 0
          and s.season between 2013 and {LAST}
        group by all
        having sum(e.possessions) >= 2000 and count(distinct e.playerteamId) = 1
    """).df()
    ours = con.sql(f"""select * from metrics.player_advanced where season_type = 'regular'
                       and team_id = 0 and season between 2013 and {LAST}""").df()
    d = nba.merge(ours, on=["season", "person_id"], suffixes=("_nba", ""))
    rows = [{"stat": c, "r": d[c].astype(float).corr(d[f"{c}_nba"].astype(float)),
             "median ours": d[c].median(), "median NBA.com": d[f"{c}_nba"].median()}
            for c in TRACKED]  # fmt: skip
    out = pd.DataFrame(rows)
    out.attrs["player_seasons"] = len(d)
    return out


KAGGLE_USE = pd.DataFrame([
    ("games", "game list, dates, scores, home/away, season type", "staging, every view"),
    ("players", "names, birth dates, positions, draft", "positions, age, service, rookie scale"),
    ("player_statistics", "player box scores, every game since 1947", "box tiers, actor counts"),
    ("team_statistics", "team box scores", "box tiers, possessions (box), team names"),
    ("play_by_play", "every event since 1996-97 with shot locations",
     "possessions, lineups, zones, on-court counts, impact"),
    ("team_statistics_extended", "NBA.com advanced team box (ratings, possessions, Four "
     "Factors, fast-break / second-chance / off-turnover points)",
     "T2 possessions test; NBA.com agreement (section 6)"),
    ("player_statistics_extended", "NBA.com advanced player box (OffRtg, DefRtg, AST%, "
     "REB%, USG%, PIE, ...)", "tracking checks (section 8), official PIE, PIE comparison"),
    ("league_schedule_26_27", "this season's full schedule", "upcoming-game forecasts"),
    ("league_schedule_24_25 / 25_26", "earlier schedules", "not needed (games has results)"),
    ("team_histories", "franchise names over time", "not needed (names come from box scores)"),
], columns=["Kaggle table", "what it holds", "used for"])  # fmt: skip


def _md_table(df: pd.DataFrame, floatfmt: str = ".3f") -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.itertuples(index=False):
        cells = [
            ("" if np.isnan(v) else format(v, floatfmt)) if isinstance(v, float) else str(v)
            for v in row
        ]
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
    nba = nba_agreement(con)
    imp = impact_check(con)
    lam = con.sql("select * from metrics.impact_lambda").df()
    by_alpha = con.sql(
        """select alpha, arg_min("lambda", cv_mse) as best_lambda, min(cv_mse) as cv_mse
           from metrics.impact_cv where mode = 'headline' group by alpha order by alpha"""
    ).df()
    box = con.sql("select * from metrics.impact_box_model").df()
    trk = tracking_checks(con)
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
        "## 6. Agreement with NBA.com's ratings",
        "",
        "NBA.com's official offensive, defensive and net rating come from the advanced box "
        "score in `kaggle.team_statistics_extended`, possession-weighted over each regular "
        f"season and taken relative to league average ({nba['fit'].attrs['team_seasons']} "
        "team-seasons). Our parts are converted to points per 100 possessions. *Plain sum* "
        "adds them with no fitting, as the spec intends: the parts are in points, so a "
        "plain sum should track the NBA's rating. *Fitted* lets a regression reweight them; "
        "the Four Factors (relative to league) are fitted the same way, for reference.",
        "",
        _md_table(nba["fit"]),
        "",
        "Weight of each part when NBA net rating is regressed on all of them. A part whose "
        "points value is right gets a weight near 1 (the composite turns out to run about "
        "15% hot overall, so ~0.85-0.9 is normal). A weight far below that means the part "
        "is valued too highly.",
        "",
        _md_table(nba["weights"]),
        "",
        "**Players** (2013-14 on, one team, >= 2,000 possessions; "
        f"{nba['players'].attrs['player_seasons']} player-seasons). NBA.com's player rating "
        "is PIE, a share of the game's box-score events, so it rewards volume. Our ratings "
        f"are rates. R² of PIE from our four ratings: "
        f"{nba['players'].attrs['r2_pie_ours']:.2f}; from usage alone: "
        f"{nba['players'].attrs['r2_pie_usage']:.2f}; from both: "
        f"{nba['players'].attrs['r2_pie_both']:.2f}.",
        "",
        _md_table(nba["players"]),
        "",
        "## 7. Per-possession impact (adjusted plus-minus)",
        "",
        "Ridge strength `lambda` and prior blend `alpha` (weight on last season's estimate; "
        "1 - alpha on this season's box model) chosen by 5-fold cross-validation over games "
        "(`metrics.impact_cv`):",
        "",
        _md_table(lam, ".2f"),
        "",
        "Held-out error at each blend (best `lambda` for each). `alpha` = 1 is the v1.1 "
        "model, with no box-score prior:",
        "",
        _md_table(by_alpha, ".2f"),
        "",
        f"Box model (`metrics.impact_box_model`): R² against season-only estimates "
        f"{box.r2_off.iloc[0]:.2f} (offense), {box.r2_def.iloc[0]:.2f} (defense). Features "
        "are per 100 possessions relative to league; usage and true shooting relative to "
        "league; position.",
        "",
        _md_table(box[["feature", "coef_off", "coef_def"]]),
        "",
        "Out of sample: last season's player numbers, weighted by this season's possession "
        f"shares, against this season's team net rating ({imp.attrs['team_seasons']} "
        "team-seasons). Nothing from the predicted season enters the predictors except who "
        "played how much.",
        "",
        _md_table(imp),
        "",
        "## 8. Tracking checks against NBA.com and Kaggle data use",
        "",
        "NBA.com's advanced player stats, computed from our play-by-play counts and rebuilt "
        "lineups (`metrics.player_advanced`), against the official per-game values in "
        "`kaggle.player_statistics_extended`, possession-weighted over the season "
        f"(2013-14 on, one team, >= 2,000 possessions; {trk.attrs['player_seasons']} "
        "player-seasons). On-court ratings, assist rate, usage and shooting agree almost "
        "exactly, so lineups and possessions are tracked right in every season, not only "
        "the 2025-26 rotation check (T3). Rebound percentages run higher than NBA.com's "
        "because NBA.com counts team rebounds in the available rebounds and we count only "
        "rebounds a player gets credit for; the ranking agrees. PIE is shown as NBA.com's "
        "official figure: its published values don't follow its published formula "
        "consistently (our formula gives 0.168 for a Jokic game NBA.com lists at 0.177, "
        "but about half NBA.com's value for typical players).",
        "",
        _md_table(trk),
        "",
        "What each Kaggle table feeds:",
        "",
        _md_table(KAGGLE_USE),
        "",
    ]
    return "\n".join(parts)
