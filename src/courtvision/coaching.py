"""Coaching analytics from play-by-play (spec section 15).

Built by ``courtvision coaching`` into schema ``coach`` (after ``ingest coaches``):

* ``coach.possessions``  every possession since 1996-97: offense, period, how it started
  (after a make, a miss, a steal, a dead-ball turnover, or the period start), how long
  it took, points, the score margin when it began, clutch flag.
* ``coach.timeouts``     every timeout: the play run out of it (the calling team's next
  offensive action, ``ATO``) and the opponent's scoring in the three possessions before
  and after it (does it stop a run?).
* ``coach.team_games``   per team-game: pace, after-timeout scoring, run-stopping, first
  half vs third quarter, clutch, how possessions start and how fast they go, rotation
  depth, starters' minutes in blowouts, timeouts and challenges.
* ``coach.profiles``     per coach-season and career (``season = 0``): each metric relative
  to league average, shrunk toward it by sample size, plus style (pace, 3-point rate).
* ``coach.tempo``        per game: whose usual pace the game was played at.
* ``coach.h2h``          coach vs coach: record, margin, tempo control, and (from the
  walk-forward) margin against the pre-game prediction.

Everything here is computable from play-by-play alone, and none of it is published by
the NBA: after-timeout efficiency, run-stopping, halftime adjustment, tempo control,
coach value against expectation.
"""

from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

CLUTCH_SECONDS, CLUTCH_MARGIN = 300, 5
RUN_WINDOW = 3  # opponent possessions before / after a timeout
EARLY_SECONDS = 8  # a possession used within 8 s: early offense
# shrinkage: evidence (plays, games) at which a coach's own rate and league average count
# equally
K_ATO, K_GAMES, K_CLUTCH = 250, 120, 600

EVENTS_SQL = """
create or replace table coach.events as
with e as (
    select p.game_id, p.season, p.seq, p.off_team_id, p.poss_id, e.period, e.elapsed_s,
           e.clock_s, e.etype, e.team_id, e.made, e.shot_value,
           e.steal_person_id is not null as steal,
           case when e.etype = 'fga' and e.made then e.shot_value
                when e.etype = 'ft' and e.made then 1 else 0 end as pts,
           g.home_team_id, g.away_team_id
    from metrics.pbp_events p
    join stg.events e using (game_id, seq)
    join stg.games g using (game_id)
    join metrics.game_pbp_qa q using (game_id)
    where g.season_type = 'regular' and q.pbp_ok
)
select *,
       coalesce(sum(case when team_id = home_team_id then pts end) over w, 0) as home_before,
       coalesce(sum(case when team_id = away_team_id then pts end) over w, 0) as away_before
from e
window w as (partition by game_id order by seq rows between unbounded preceding and 1 preceding);
"""

POSSESSIONS_SQL = f"""
create or replace table coach.possessions as
with a as (
    select game_id, any_value(season) as season, poss_id, any_value(off_team_id) as team_id,
           any_value(home_team_id) as home_team_id, any_value(away_team_id) as away_team_id,
           min(period) as period, min(seq) as first_seq, max(seq) as last_seq,
           min(elapsed_s) as first_t, max(elapsed_s) as last_t, arg_min(clock_s, seq) as clock,
           sum(case when team_id = off_team_id then pts else 0 end) as pts,
           arg_min(etype, seq) as first_etype, arg_max(etype, seq) as last_etype,
           arg_max(made, seq) as last_made, arg_max(steal, seq) as last_steal,
           arg_min(home_before, seq) as home_before, arg_min(away_before, seq) as away_before,
           min(case when etype in ('fga', 'tov') or (etype = 'ft' and team_id = off_team_id)
                    then elapsed_s end) as first_action_t
    from coach.events where poss_id is not null
    group by game_id, poss_id
), b as (
    select *, lag(last_etype) over w as prev_etype, lag(last_made) over w as prev_made,
           lag(last_steal) over w as prev_steal, lag(last_t) over w as prev_t,
           lag(period) over w as prev_period
    from a window w as (partition by game_id order by first_seq)
)
select game_id, season, poss_id, team_id,
       case when team_id = home_team_id then away_team_id else home_team_id end as opp_id,
       period, first_seq, last_seq, pts,
       case when prev_period is null or prev_period <> period then 'period start'
            when first_etype = 'reb' then 'after miss'
            when prev_etype = 'tov' and prev_steal then 'after steal'
            when prev_etype = 'tov' then 'after dead ball turnover'
            when prev_etype in ('fga', 'ft') and prev_made then 'after make'
            else 'other' end as start_type,
       case when prev_period = period then first_action_t - prev_t end as seconds_to_action,
       case when team_id = home_team_id then home_before - away_before
            else away_before - home_before end as margin,
       period >= 4 and clock <= {CLUTCH_SECONDS}
         and abs(home_before - away_before) <= {CLUTCH_MARGIN} as clutch
from b;
"""

TIMEOUTS_SQL = f"""
create or replace table coach.timeouts as
with t as (
    select game_id, season, seq, period, team_id from coach.events where etype = 'timeout'
      and team_id is not null
), act as (  -- offensive actions: a shot, a turnover, or one's own free throw
    select game_id, seq, period, team_id, poss_id,
           case when etype = 'fga' and shot_value = 3 then 'three'
                when etype = 'fga' then 'two' when etype = 'ft' then 'free throws'
                else 'turnover' end as action
    from coach.events
    where etype in ('fga', 'tov') or (etype = 'ft' and team_id = off_team_id)
), ato as (
    select t.game_id, t.seq, arg_min(a.poss_id, a.seq) as poss_id,
           arg_min(a.action, a.seq) as ato_action
    from t join act a on a.game_id = t.game_id and a.period = t.period
                     and a.team_id = t.team_id and a.seq > t.seq
    group by t.game_id, t.seq
), ato_pts as (
    select o.game_id, o.seq, coalesce(sum(e.pts) filter (where e.team_id = t.team_id
                                                         and e.seq > t.seq), 0) as pts
    from ato o join t using (game_id, seq)
    left join coach.events e on e.game_id = o.game_id and e.poss_id = o.poss_id
    group by o.game_id, o.seq
), opp as (  -- the opponent's possessions around the timeout
    select t.game_id, t.seq, p.pts, p.first_seq < t.seq as before,
           row_number() over (partition by t.game_id, t.seq, p.first_seq < t.seq
                              order by abs(p.first_seq - t.seq)) as k
    from t join coach.possessions p
      on p.game_id = t.game_id and p.period = t.period and p.team_id <> t.team_id
)
select t.*, o.poss_id is not null as has_ato, o.ato_action, coalesce(ap.pts, 0) as ato_pts,
       (select avg(pts) from opp x where x.game_id = t.game_id and x.seq = t.seq
          and x.before and x.k <= {RUN_WINDOW}) as opp_before,
       (select avg(pts) from opp x where x.game_id = t.game_id and x.seq = t.seq
          and not x.before and x.k <= {RUN_WINDOW}) as opp_after
from t left join ato o using (game_id, seq) left join ato_pts ap using (game_id, seq);
"""

TEAM_GAMES_SQL = f"""
create or replace table coach.team_games as
with p as (
    select game_id, season, team_id, count(*) as poss, sum(pts) as pts,
           count(*) filter (where period <= 2) as h1_poss, sum(pts) filter (where period <= 2)
               as h1_pts,
           count(*) filter (where period = 3) as q3_poss, sum(pts) filter (where period = 3)
               as q3_pts,
           count(*) filter (where clutch) as clutch_poss, sum(pts) filter (where clutch)
               as clutch_pts,
           count(*) filter (where start_type in ('after miss', 'after steal')) as live_poss,
           count(*) filter (where start_type in ('after miss', 'after steal')
                            and seconds_to_action <= {EARLY_SECONDS}) as transition_poss,
           sum(pts) filter (where start_type in ('after miss', 'after steal')
                            and seconds_to_action <= {EARLY_SECONDS}) as transition_pts,
           count(*) filter (where seconds_to_action <= {EARLY_SECONDS}) as early_poss,
           sum(pts) filter (where seconds_to_action <= {EARLY_SECONDS}) as early_pts,
           count(*) filter (where seconds_to_action > 18) as late_poss,
           sum(pts) filter (where seconds_to_action > 18) as late_pts
    from coach.possessions group by all
), t as (
    select game_id, team_id, count(*) as timeouts, count(*) filter (where has_ato) as ato_n,
           sum(ato_pts) filter (where has_ato) as ato_pts,
           count(*) filter (where ato_action = 'three') as ato_three,
           count(*) filter (where ato_action = 'free throws') as ato_ft,
           count(*) filter (where ato_action = 'turnover') as ato_tov,
           count(*) filter (where opp_before is not null and opp_after is not null) as run_n,
           sum(opp_before) filter (where opp_after is not null) as run_before,
           sum(opp_after) filter (where opp_before is not null) as run_after
    from coach.timeouts group by all
), ch as (
    select lpad(gameId::varchar, 10, '0') as game_id, teamId as team_id, count(*) as challenges
    from kaggle.play_by_play where actionType = 'timeout' and subType = 'challenge'
    group by all
), r as (
    select game_id, team_id, count(*) filter (where minutes >= 10) as rotation,
           sum(minutes) filter (where rk <= 5) as top5_min,
           sum(minutes) filter (where rk <= 8) / nullif(sum(minutes), 0) as top8_share
    from (select game_id, team_id, minutes,
                 row_number() over (partition by game_id, team_id order by minutes desc) rk
          from metrics.player_game_counts where season_type = 'regular') x
    group by all
)
select p.*, o.pts as opp_pts, o.h1_pts as opp_h1_pts, o.q3_pts as opp_q3_pts,
       o.clutch_pts as opp_clutch_pts, o.clutch_poss as opp_clutch_poss,
       coalesce(t.timeouts, 0) as timeouts, coalesce(t.ato_n, 0) as ato_n,
       coalesce(t.ato_pts, 0) as ato_pts, coalesce(t.ato_three, 0) as ato_three,
       coalesce(t.ato_ft, 0) as ato_ft, coalesce(t.ato_tov, 0) as ato_tov,
       coalesce(t.run_n, 0) as run_n,
       t.run_before, t.run_after, ch.challenges, r.rotation, r.top5_min, r.top8_share,
       b.b_fg3a, b.b_fga, abs(b.b_pts - b.o_b_pts) >= 20 as blowout,
       b.b_pts > b.o_b_pts as win
from p
join p o on o.game_id = p.game_id and o.team_id <> p.team_id
left join t on t.game_id = p.game_id and t.team_id = p.team_id
left join ch on ch.game_id = p.game_id and ch.team_id = p.team_id
left join r on r.game_id = p.game_id and r.team_id = p.team_id
left join metrics.team_game_counts b on b.game_id = p.game_id and b.team_id = p.team_id;
"""


def _shrink(own: pd.Series, n: pd.Series, k: float) -> pd.Series:
    """Own rate (relative to league) shrunk toward 0 with ``k`` units of prior."""
    return own * n / (n + k)


def profiles(con) -> pd.DataFrame:
    """Coach-season and career (season 0) profiles, relative to league and shrunk."""
    tg = con.execute(
        """select g.*, a.coach_id, a.coach_name, a.assumed
           from coach.team_games g join coach.assignments a using (game_id, team_id)"""
    ).df()
    lg = tg.groupby("season").agg(
        lg_ato=("ato_pts", "sum"), lg_ato_n=("ato_n", "sum"), lg_pts=("pts", "sum"),
        lg_poss=("poss", "sum"), lg_run_b=("run_before", "sum"), lg_run_a=("run_after", "sum"),
        lg_run_n=("run_n", "sum"), lg_fg3a=("b_fg3a", "sum"), lg_fga=("b_fga", "sum"),
        lg_tr=("transition_pts", "sum"), lg_tr_n=("transition_poss", "sum"),
        lg_live=("live_poss", "sum"), lg_rot=("rotation", "mean"),
        lg_timeouts=("timeouts", "mean"), lg_challenges=("challenges", "mean"),
        lg_ato_three=("ato_three", "sum"), lg_ato_ft=("ato_ft", "sum"),
        lg_ato_tov=("ato_tov", "sum"),
    )  # fmt: skip
    tg = tg.merge(lg, left_on="season", right_index=True)
    # per-game contributions relative to league, so sums over any span stay comparable
    tg["ato_rel"] = tg.ato_pts - tg.ato_n * tg.lg_ato / tg.lg_ato_n
    tg["run_rel"] = (tg.run_after.fillna(0) - tg.run_before.fillna(0)) - tg.run_n * (
        tg.lg_run_a - tg.lg_run_b) / tg.lg_run_n  # fmt: skip
    q3 = 100 * (tg.q3_pts - tg.opp_q3_pts) / tg.q3_poss.where(tg.q3_poss > 0)
    h1 = 100 * (tg.h1_pts - tg.opp_h1_pts) / tg.h1_poss.where(tg.h1_poss > 0)
    tg["half_adj"] = q3 - h1
    tg["clutch_net"] = tg.clutch_pts.fillna(0) - tg.opp_clutch_pts.fillna(0)
    tg["clutch_n"] = tg.clutch_poss.fillna(0) + tg.opp_clutch_poss.fillna(0)
    tg["tr_rel"] = (
        tg.transition_pts.fillna(0) - tg.transition_poss.fillna(0) * tg.lg_tr / tg.lg_tr_n
    )
    tg["net100"] = 100 * (tg.pts - tg.opp_pts) / tg.poss
    tg = tg.sort_values(["team_id", "season", "game_id"])
    tg["prev_win"] = tg.groupby(["team_id", "season"]).win.shift()

    def agg(g: pd.DataFrame) -> pd.Series:
        n = len(g)
        after_l, after_w = g[g.prev_win == False].net100, g[g.prev_win == True].net100  # noqa: E712
        ato_n = max(g.ato_n.sum(), 1)

        def ato_mix(col: str) -> float:  # share of plays out of timeouts, minus league share
            return g[col].sum() / ato_n - (g[f"lg_{col}"] / g.lg_ato_n).mean()

        return pd.Series({
            "ato_three_rel": ato_mix("ato_three"), "ato_ft_rel": ato_mix("ato_ft"),
            "ato_tov_rel": ato_mix("ato_tov"),
            "games": n, "wins": int(g.win.sum()), "team_ids": sorted(set(g.team_id)),
            "pace": g.poss.mean(), "fg3a_rate_rel": (g.b_fg3a.sum() / g.b_fga.sum())
            - (g.lg_fg3a / g.lg_fga).mean(),
            "ato_plays": int(g.ato_n.sum()),
            "ato_pp100_rel": 100 * g.ato_rel.sum() / max(g.ato_n.sum(), 1),
            "run_stop_pp100": -100 * g.run_rel.sum() / max(g.run_n.sum(), 1),
            "halftime_adj": g.half_adj.mean(),
            "clutch_poss": int(g.clutch_n.sum()),
            "clutch_net100": 100 * g.clutch_net.sum() / max(g.clutch_n.sum() / 2, 1),
            "transition_share": g.transition_poss.sum() / max(g.live_poss.sum(), 1),
            "transition_pp100_rel": 100 * g.tr_rel.sum() / max(g.transition_poss.sum(), 1),
            "rotation": g.rotation.mean() - g.lg_rot.mean(),
            "starters_min_blowout": g[g.blowout].top5_min.mean() / 5 if g.blowout.any()
            else np.nan,
            "timeouts_pg": g.timeouts.mean(), "challenges_pg": g.challenges.mean(),
            "bounce_back": (after_l.mean() - after_w.mean()) if len(after_l) >= 5
            and len(after_w) >= 5 else np.nan,
            "net100": g.net100.mean(), "assumed": bool(g.assumed.any()),
        })  # fmt: skip

    seasons = tg.groupby(["coach_id", "coach_name", "season"]).apply(agg, include_groups=False)
    career = tg.groupby(["coach_id", "coach_name"]).apply(agg, include_groups=False)
    career["season"] = 0
    prof = pd.concat([seasons.reset_index(), career.reset_index()], ignore_index=True)
    # shrink the noisy ones toward league average by sample size
    prof["ato_pp100_rel"] = _shrink(prof.ato_pp100_rel, prof.ato_plays, K_ATO)
    prof["run_stop_pp100"] = _shrink(prof.run_stop_pp100, prof.ato_plays, K_ATO)
    prof["clutch_net100"] = _shrink(prof.clutch_net100, prof.clutch_poss, K_CLUTCH)
    prof["halftime_adj"] = _shrink(prof.halftime_adj, prof.games, K_GAMES)
    prof["bounce_back"] = _shrink(prof.bounce_back, prof.games, K_GAMES)
    return prof


def tempo_control(tg: pd.DataFrame) -> pd.DataFrame:
    """Per team-game: the game's pace (both teams' possessions averaged), each team's usual
    pace that season (its other games), and ``control``: how much closer the game was to
    this team's usual pace than to the opponent's. The two sides of a game sum to zero.
    ``tg``: game_id, season, team_id, poss, coach_id."""
    tg = tg.copy()
    tg["game_pace"] = tg.groupby("game_id").poss.transform("mean")
    s = tg.groupby(["season", "team_id"]).game_pace.agg(["sum", "count"])
    tg = tg.join(s, on=["season", "team_id"])
    tg["usual"] = (tg["sum"] - tg.game_pace) / (tg["count"] - 1).where(tg["count"] > 1)
    o = tg[["game_id", "team_id", "usual", "coach_id"]].rename(
        columns={"team_id": "opp_id", "usual": "opp_usual", "coach_id": "opp_coach_id"}
    )
    t = tg.merge(o, on="game_id")
    t = t[t.team_id != t.opp_id]
    t["control"] = (t.game_pace - t.opp_usual).abs() - (t.game_pace - t.usual).abs()
    return t[["game_id", "season", "team_id", "opp_id", "coach_id", "opp_coach_id",
              "usual", "opp_usual", "game_pace", "control"]].reset_index(drop=True)  # fmt: skip


def tempo(con) -> pd.DataFrame:
    return tempo_control(con.execute(
        """select g.game_id, g.season, g.team_id, g.poss, a.coach_id
           from coach.team_games g left join coach.assignments a using (game_id, team_id)"""
    ).df())  # fmt: skip


def build(con: duckdb.DuckDBPyConnection) -> dict:
    con.execute("create schema if not exists coach")
    for name, sql in (("events", EVENTS_SQL), ("possessions", POSSESSIONS_SQL),
                      ("timeouts", TIMEOUTS_SQL), ("team_games", TEAM_GAMES_SQL)):  # fmt: skip
        log.info("coach.%s", name)
        con.execute(sql)
    prof = profiles(con)
    tem = tempo(con)
    t_coach = tem.groupby("coach_id").control.agg(["mean", "count"])
    prof = prof.merge(
        t_coach.rename(columns={"mean": "tempo_control", "count": "tempo_games"}),
        left_on="coach_id",
        right_index=True,
        how="left",
    )
    prof.loc[prof.season != 0, ["tempo_control", "tempo_games"]] = np.nan
    seas = tem.groupby(["coach_id", "season"]).control.mean().rename("tempo_control_s")
    prof = prof.merge(seas, left_on=["coach_id", "season"], right_index=True, how="left")
    prof["tempo_control"] = prof.tempo_control.fillna(prof.tempo_control_s)
    prof = prof.drop(columns="tempo_control_s")
    for name, df in (("profiles", prof), ("tempo", tem)):
        con.register("tmp_df", df)
        con.execute(f"create or replace table coach.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    con.execute("drop table coach.events")  # large and only needed to build the above
    return {
        "coach_seasons": int((prof.season != 0).sum()),
        "coaches": int((prof.season == 0).sum()),
    }


# ---- value against expectation and coach vs coach (after the walk-forward) -----------------


def _team_rows(con) -> pd.DataFrame:
    """Walk-forward games from each team's side: margin, roster expectation, prediction."""
    g = con.execute("select * from forecast.wf_games").df()
    best = (
        con.execute(
            "select any_value(model) from forecast.margin_model where model <> 'player intervals'"
        )
        .df()
        .iloc[0, 0]
    )
    g["pred"] = g[f"pred_{best}"]
    sides = []
    for side, sign in (("h", 1), ("a", -1)):
        sides.append(pd.DataFrame({
            "game_id": g.game_id, "season": g.season, "game_date": g.game_date,
            "team_id": g.home_team_id if side == "h" else g.away_team_id,
            "coach_id": g[f"coach_{side}"],
            "opp_coach_id": g["coach_a" if side == "h" else "coach_h"],
            "margin": sign * g.margin, "vs_roster": sign * (g.margin - g.roster),
            "vs_pred": sign * (g.margin - g.pred),
        }))  # fmt: skip
    return pd.concat(sides, ignore_index=True).dropna(subset=["coach_id"])


def reliability(rows: pd.DataFrame, col: str, min_games: int = 40) -> dict:
    """Is ``col`` a trait of the coach or noise? Split-half (odd vs even games of a
    coach-season, stepped up to a full season), season-to-season for the same coach, and
    the games of evidence at which the coach's own number and zero count equally."""
    r = rows.sort_values(["coach_id", "season", "game_date"]).copy()
    r["half"] = r.groupby(["coach_id", "season"]).cumcount() % 2
    cs = r.groupby(["coach_id", "season", "half"])[col].mean().unstack()
    n = r.groupby(["coach_id", "season"]).size()
    cs = cs[n.reindex(cs.index) >= min_games]
    rh = float(cs[0].corr(cs[1]))
    full = r.groupby(["coach_id", "season"])[col].agg(["mean", "size"])
    full = full[full["size"] >= min_games].reset_index()
    nxt = full.assign(season=full.season - 1).rename(columns={"mean": "next"})
    yy = full.merge(nxt[["coach_id", "season", "next"]], on=["coach_id", "season"])
    per_game_var = float(r[col].var())
    true_var = float(full["mean"].var() - per_game_var / full["size"].mean())
    return {"metric": col, "coach_seasons": len(cs), "split_half": rh,
            "season_reliability": 2 * rh / (1 + rh) if rh > -1 else np.nan,
            "year_to_year": float(yy["mean"].corr(yy["next"])), "year_pairs": len(yy),
            "k_games": per_game_var / true_var if true_var > 0 else np.inf}  # fmt: skip


TRAITS = ["pace", "fg3a_rate_rel", "ato_pp100_rel", "ato_three_rel", "ato_tov_rel",
          "run_stop_pp100", "halftime_adj", "clutch_net100", "transition_share",
          "transition_pp100_rel", "rotation", "starters_min_blowout", "timeouts_pg",
          "challenges_pg", "bounce_back", "tempo_control", "net100"]  # fmt: skip


def persistence(con, extra: pd.DataFrame | None = None, min_games: int = 40) -> pd.DataFrame:
    """Season-to-season correlation of each coach metric for the same coach: overall, and
    only where he changed teams (the trait test: does it follow the coach?). Pace and
    3-point rate are taken relative to the season's league average first."""
    p = con.execute(
        "select * from coach.profiles where season > 0 and games >= ?", [min_games]
    ).df()
    p["pace"] = p.pace - p.groupby("season").pace.transform("mean")
    p["team"] = p.team_ids.map(lambda t: tuple(sorted(t)))
    cols = [c for c in TRAITS if c in p.columns]
    if extra is not None:
        p = p.merge(extra[extra.games >= min_games].drop(columns="games"),
                    on=["coach_id", "season"], how="left")  # fmt: skip
        cols += [c for c in extra.columns if c not in ("coach_id", "season", "games")]
    nxt = p.assign(season=p.season - 1)
    pair = p.merge(nxt, on=["coach_id", "season"], suffixes=("", "_next"))
    moved = pair[pair.team != pair.team_next]
    rows = []
    for c in cols:
        a = pair[[c, f"{c}_next"]].dropna()
        m = moved[[c, f"{c}_next"]].dropna()
        rows.append({"metric": c, "same_coach_corr": float(a[c].corr(a[f"{c}_next"])),
                     "same_coach_pairs": len(a),
                     "new_team_corr": float(m[c].corr(m[f"{c}_next"])) if len(m) > 5
                     else np.nan, "new_team_pairs": len(m)})  # fmt: skip
    return pd.DataFrame(rows)


def value(con: duckdb.DuckDBPyConnection) -> dict:
    """``coach.value`` (per coach-season and career): margin against the roster's
    expectation and against the full pre-game prediction, shrunk by their measured
    reliability; ``coach.value_reliability``; ``coach.h2h`` coach vs coach."""
    rows = _team_rows(con)
    rel = pd.DataFrame([reliability(rows, c) for c in ("vs_roster", "vs_pred")])
    k = dict(zip(rel.metric, rel.k_games, strict=True))
    out = []
    for keys, season in ((["coach_id", "season"], None), (["coach_id"], 0)):
        a = rows.groupby(keys).agg(games=("margin", "size"), margin=("margin", "mean"),
                                   vs_roster=("vs_roster", "mean"),
                                   vs_pred=("vs_pred", "mean"))  # fmt: skip
        a = a.reset_index()
        if season is not None:
            a["season"] = season
        for c in ("vs_roster", "vs_pred"):  # shrink: games / (games + k)
            kc = k[c] if np.isfinite(k[c]) else 1e9
            a[f"{c}_shrunk"] = a[c] * a.games / (a.games + kc)
        out.append(a)
    val = pd.concat(out, ignore_index=True)
    names = con.execute("select coach_id, coach_name from coach.coaches").df()
    val = val.merge(names, on="coach_id", how="left")

    # coach vs coach: every season for record, margin and tempo; walk-forward seasons for
    # margin against the prediction
    t = con.execute(
        """select t.game_id, t.season, t.coach_id, t.opp_coach_id, t.control,
                  g.pts - g.opp_pts as margin, g.win::int as win
           from coach.tempo t join coach.team_games g using (game_id, team_id)
           where t.coach_id is not null and t.opp_coach_id is not null"""
    ).df()
    t = t.merge(rows[["game_id", "coach_id", "vs_pred"]], on=["game_id", "coach_id"], how="left")
    h2h = t.groupby(["coach_id", "opp_coach_id"]).agg(
        games=("win", "size"), wins=("win", "sum"), margin=("margin", "mean"),
        tempo_control=("control", "mean"), vs_pred=("vs_pred", "mean"),
        vs_pred_games=("vs_pred", "count"), first_season=("season", "min"),
        last_season=("season", "max"),
    ).reset_index()  # fmt: skip
    # does a pairing's history say anything about their next meeting?
    nx = t.dropna(subset=["vs_pred"]).sort_values(["coach_id", "opp_coach_id", "game_id"])
    nx["prior"] = nx.groupby(["coach_id", "opp_coach_id"]).vs_pred.transform(
        lambda v: v.shift().expanding().mean())  # fmt: skip
    nx = nx.dropna(subset=["prior"])
    rel = pd.concat([rel, pd.DataFrame([{
        "metric": "h2h_vs_pred", "year_to_year": float(nx.prior.corr(nx.vs_pred)),
        "year_pairs": len(nx)}])], ignore_index=True)  # fmt: skip
    # which profile metrics are the coach's (they repeat next season, and follow him to a
    # new team) and which are the roster's?
    vrows = val[val.season != 0][["coach_id", "season", "games", "vs_roster", "vs_pred"]]
    pers = persistence(con, vrows)
    rel = rel.merge(pers, on="metric", how="outer")
    for name, df in (("value", val), ("value_reliability", rel), ("h2h", h2h)):
        con.register("tmp_df", df)
        con.execute(f"create or replace table coach.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    return {"reliability": rel, "pairs": len(h2h)}
