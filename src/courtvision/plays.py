"""What teams run: play types from play-by-play, 2015-16 on (spec section 17).

No tracking data. Every field-goal attempt is classified from what the play-by-play
records about the shot itself: its description, shot type and descriptor (the live
format's descriptors and the legacy text share one vocabulary: driving, pullup, step
back, cutting, alley oop, putback, floating, turnaround, hook) and how its possession
started (transition: after a miss or steal, first action within 8 seconds; from
``coach.possessions``). Assists are recorded only on made shots, so they are never used
to classify (that would leak the result). Precedence, first match wins:

    Putback > Transition > Lob > Cut > Drive > Floater > Post-up > Step-back > Pull-up
    > Other rim (layups and dunks with no move recorded: rolls, dump-offs, running)
    > Spot-up three (threes with no move recorded: mostly catch-and-shoot)
    > Two-point jumper (no move recorded)

Efficiency on a play is noisy at team level, so each play's shrinkage comes from its
measured split-half reliability (``k = n_half * (1 - r) / r`` shots), not a guess.
Tagging grew more detailed over the years (more shots carry a move word in 2020 than
in 2015), so compare teams within a season: every number is relative to that season's
league.

The detailed vocabulary starts in 2015-16 (the "cutting" tag appears then), so play types
start there. ``courtvision plays`` writes schema ``plays``:

* ``shots``        every regular-season field-goal attempt with play, zone, assist
* ``league``       per season and play: share of shots, FG%, points per shot
* ``team``         per team, season and side (offense / defense): share, points per 100
                   shots, both against league; efficiency shrunk toward league average
* ``player``       per player and season: his play mix and efficiency
* ``assists``      passer -> shooter connections by play
* ``turnovers``    per team, season and side: turnovers by kind per 100 possessions
* ``reliability``  how stable each play's share and efficiency are (split-half), and the
                   shrinkage that follows from it
* ``matchup_test`` does a defense's allowance add to an offense's own numbers in
                   predicting a game (each season's second half from its first)?
"""

from __future__ import annotations

import logging

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

FIRST_SEASON = 2015
TRANSITION_SECONDS = 8
K_DEFAULT = 300.0  # shots of prior when reliability can't be measured
K_RANGE = (30.0, 5000.0)
PLAYS = ["Transition", "Putback", "Lob", "Cut", "Drive", "Floater", "Post-up", "Step-back",
         "Pull-up", "Other rim", "Spot-up three", "Two-point jumper"]  # fmt: skip
DESCRIBE = {
    "Transition": "shots in possessions after a miss or steal, within 8 seconds",
    "Putback": "tip-ins and putbacks after an offensive rebound",
    "Lob": "alley-oops",
    "Cut": "cutting layups and dunks",
    "Drive": "driving layups, dunks, finger rolls and hooks",
    "Floater": "floaters",
    "Post-up": "hooks, turnarounds and fadeaways (post and mid-post)",
    "Step-back": "step-back jumpers",
    "Pull-up": "pull-up jumpers off the dribble",
    "Other rim": "layups and dunks with no move recorded (rolls, dump-offs, running)",
    "Spot-up three": "threes with no move recorded (mostly catch-and-shoot)",
    "Two-point jumper": "two-point jumpers with no move recorded",
}
TOV_KINDS = ["Bad pass", "Lost ball", "Offensive foul", "Out of bounds", "Travel",
             "Shot clock", "Other"]  # fmt: skip

SHOTS_SQL = f"""
create or replace table plays.shots as
with s as (
    select e.game_id, e.season, e.seq, e.team_id, e.person_id,
           nullif(e.assist_person_id, 0) as assist_person_id, e.shot_value, e.made,
           e.x_legacy as x, e.y_legacy as y,
           lower(concat_ws(' ', e.description, sh.sub_type, sh.descriptor)) as d,
           case when e.team_id = g.home_team_id then g.away_team_id
                else g.home_team_id end as opp_id,
           p.poss_id
    from stg.events e
    join stg.games g using (game_id)
    left join metrics.pbp_events p using (game_id, seq)
    left join stg.shots sh using (game_id, action_number)
    where e.etype = 'fga' and e.season >= {FIRST_SEASON} and e.season_type = 'regular'
      and e.team_id is not null
), k as (
    select s.*, coalesce(c.start_type in ('after miss', 'after steal')
                         and c.seconds_to_action <= {TRANSITION_SECONDS}, false) as transition
    from s left join coach.possessions c on c.game_id = s.game_id and c.poss_id = s.poss_id
)
select game_id, season, seq, team_id, opp_id, person_id, assist_person_id, shot_value,
       made, case when made then shot_value else 0 end as pts, x, y,
       cv_zone(shot_value, x, y) as zone, transition, assist_person_id is not null as assisted,
       case
         when regexp_matches(d, 'putback|\\btip\\b|tip-in|\\btip ?(layup|dunk|shot)')
              then 'Putback'
         when transition then 'Transition'
         when regexp_matches(d, 'alley[ -]?oop') then 'Lob'
         when d like '%cutting%' then 'Cut'
         when d like '%driving%' and d not like '%floating%' then 'Drive'
         when d like '%floating%' then 'Floater'
         when regexp_matches(d, '\\bhook|turnaround|fadeaway')
              and not regexp_matches(d, 'step[ -]?back') then 'Post-up'
         when regexp_matches(d, 'step[ -]?back') then 'Step-back'
         when regexp_matches(d, 'pull[ -]?up') then 'Pull-up'
         when regexp_matches(d, 'layup|dunk|finger ?roll') then 'Other rim'
         when shot_value = 3 then 'Spot-up three'
         else 'Two-point jumper'
       end as play
from k;
"""

TOV_SQL = f"""
create or replace table plays.turnover_events as
select e.game_id, e.season, e.team_id,
       case when e.team_id = g.home_team_id then g.away_team_id else g.home_team_id end
           as opp_id,
       case when d like '%bad pass%' then 'Bad pass'
            when d like '%lost ball%' then 'Lost ball'
            when regexp_matches(d, 'offensive (foul|charge)|charge') then 'Offensive foul'
            when regexp_matches(d, 'out[ -]of[ -]bounds|step out') then 'Out of bounds'
            when d like '%travel%' then 'Travel'
            when d like '%shot clock%' then 'Shot clock'
            else 'Other' end as kind
from (select *, lower(coalesce(description, '')) as d from stg.events) e
join stg.games g using (game_id)
where e.etype = 'tov' and e.season >= {FIRST_SEASON} and e.season_type = 'regular'
  and e.team_id is not null;
"""


def _rel(df: pd.DataFrame, k) -> pd.DataFrame:
    """Points per 100 shots against league on the same play, raw and shrunk; ``k`` is a
    number of shots or a mapping play -> shots."""
    kk = df.play.map(k).fillna(K_DEFAULT) if isinstance(k, dict | pd.Series) else k
    df["pp100_rel"] = 100 * (df.pts - df.shots * df.lg_pps) / df.shots
    df["pp100_rel_shrunk"] = 100 * (df.pts - df.shots * df.lg_pps) / (df.shots + kk)
    return df


def team_profile(shots: pd.DataFrame, league: pd.DataFrame, side: str = "offense",
                 k=K_DEFAULT) -> pd.DataFrame:  # fmt: skip
    """Per team and play from ``shots`` (needs season, team_id, opp_id, play, pts, made,
    assisted and an optional ``w`` weight): shots, share, share vs league, points per
    100 shots vs league (raw, shrunk with ``k`` shots: a number or play -> shots).
    ``side = 'defense'`` profiles what each team allows. ``league``: per season and
    play, ``lg_share`` and ``lg_pps``."""
    s = shots[["season", "team_id", "opp_id", "play", "pts", "made", "assisted"]].copy()
    s["w"] = shots["w"].to_numpy() if "w" in shots else 1.0
    s["team"] = s.team_id if side == "offense" else s.opp_id
    s = s.merge(league[["season", "play", "lg_pps", "lg_share"]], on=["season", "play"])
    for c in ("pts", "made", "assisted"):
        s[c] = s[c] * s.w
    s["exp"] = s.w * s.lg_pps
    s["lgs"] = s.w * s.lg_share
    g = s.groupby(["team", "play"])[["w", "pts", "made", "assisted", "exp", "lgs"]].sum()
    g = g.rename(columns={"w": "shots"}).reset_index()
    g["lg_share"] = g.lgs / g.shots
    g["lg_pps"] = g.exp / g.shots
    g["share"] = g.shots / g.groupby("team").shots.transform("sum")
    g["share_rel"] = g.share - g.lg_share
    g["pps"] = g.pts / g.shots
    g["fg"] = g.made / g.shots
    g["assisted_share"] = g.assisted / g.made.where(g.made > 0)  # of made shots
    g = _rel(g, k).drop(columns=["exp", "lgs"]).rename(columns={"team": "team_id"})
    g["side"] = side
    return g


def _halves(shots: pd.DataFrame, key: str) -> pd.Series:
    """0/1 per shot: odd or even game of its team-season (``key``: team_id or opp_id)."""
    g = shots[["season", key, "game_id"]].drop_duplicates().sort_values("game_id")
    g["par"] = g.groupby(["season", key]).cumcount() % 2
    return shots.merge(g, on=["season", key, "game_id"], how="left").par.to_numpy()


def split_half(shots: pd.DataFrame, league: pd.DataFrame) -> pd.DataFrame:
    """Per play and side: correlation between odd and even games of a team-season, of
    the play's share of shots and of its points per 100 shots vs league; ``n_half``
    the average shots per half; ``k`` = n_half (1 - r) / r, the shots of evidence at
    which a team's own efficiency and league average count equally."""
    rows = []
    for side in ("offense", "defense"):
        par = _halves(shots, "team_id" if side == "offense" else "opp_id")
        halves = []
        for p in (0, 1):
            x = shots[par == p]
            for season, xs in x.groupby("season"):
                t = team_profile(xs, league[league.season == season], side)
                halves.append(t.assign(season=season, par=p))
        h = pd.concat(halves).pivot_table(index=["season", "team_id", "play"], columns="par",
                                          values=["share", "pp100_rel", "shots"])  # fmt: skip
        for play, g in h.groupby(level="play"):
            r_eff = float(g["pp100_rel"][0].corr(g["pp100_rel"][1]))
            n_half = float(g["shots"].mean().mean())
            k = n_half * (1 - r_eff) / r_eff if r_eff > 0.02 else K_RANGE[1]
            rows.append({"play": play, "side": side,
                         "share_r": float(g["share"][0].corr(g["share"][1])),
                         "efficiency_r": r_eff, "n_half": n_half,
                         "k": float(np.clip(k, *K_RANGE)), "team_seasons": len(g)})  # fmt: skip
    return pd.DataFrame(rows)


def matchup_test(shots: pd.DataFrame, league: pd.DataFrame, k_off: dict,
                 k_def: dict) -> pd.DataFrame:  # fmt: skip
    """Each season's second-half team-games predicted from the first half: a team's
    points per 100 shots on a play vs league as 0 (league), as the offense's shrunk
    first-half number, and as offense + the opponent defense's shrunk allowance. Mean
    squared error weighted by shots; gains are reductions from the league baseline."""
    order = shots[["season", "game_id"]].drop_duplicates().sort_values("game_id")
    order["late"] = order.groupby("season").cumcount() >= order.groupby(
        "season").game_id.transform("size") / 2  # fmt: skip
    s = shots.merge(order[["game_id", "late"]], on="game_id")
    first, second = s[~s.late], s[s.late]
    prof = {}
    for side, k in (("offense", k_off), ("defense", k_def)):
        prof[side] = pd.concat(
            [team_profile(x, league[league.season == se], side, k).assign(season=se)
             for se, x in first.groupby("season")]
        ).set_index(["season", "team_id", "play"]).pp100_rel_shrunk  # fmt: skip
    g = second.groupby(["season", "game_id", "team_id", "opp_id", "play"]).agg(
        shots=("pts", "size"), pts=("pts", "sum")).reset_index()  # fmt: skip
    g = g.merge(league[["season", "play", "lg_pps"]], on=["season", "play"])
    g["actual"] = 100 * (g.pts / g.shots - g.lg_pps)
    idx_o = pd.MultiIndex.from_frame(g[["season", "team_id", "play"]])
    idx_d = pd.MultiIndex.from_frame(g[["season", "opp_id", "play"]])
    g["off"] = prof["offense"].reindex(idx_o).fillna(0).to_numpy()
    g["dfn"] = prof["defense"].reindex(idx_d).fillna(0).to_numpy()
    rows = []
    for play, x in [("all plays", g), *g.groupby("play")]:

        def mse(pred, x=x):
            return float(np.average((x.actual - pred) ** 2, weights=x.shots))

        base = mse(0.0)
        both = mse(x.off + x.dfn)
        rows.append({"play": play, "team_games": len(x), "mse_league": base,
                     "gain_offense_pct": 100 * (base - mse(x.off)) / base,
                     "gain_offense_defense_pct": 100 * (base - both) / base})  # fmt: skip
    return pd.DataFrame(rows)


def build(con: duckdb.DuckDBPyConnection) -> dict:
    con.execute("create schema if not exists plays")
    log.info("plays.shots")
    con.execute(SHOTS_SQL)
    con.execute(TOV_SQL)
    lg = con.execute("""
        select season, play, count(*) as shots, avg(made::int) as fg, avg(pts) as pps,
               sum(assisted::int) / nullif(sum(made::int), 0) as assisted_share
        from plays.shots group by all""").df()
    lg["share"] = lg.shots / lg.groupby("season").shots.transform("sum")
    league = lg.rename(columns={"share": "lg_share", "pps": "lg_pps"})
    shots = con.execute("""select season, game_id, team_id, opp_id, person_id, play, pts,
                                  made::int as made, assisted::int as assisted
                           from plays.shots""").df()
    log.info("plays: reliability")
    rel = split_half(shots, league)
    k = {side: dict(zip(r.play, r.k, strict=True)) for side, r in rel.groupby("side")}
    teams = []
    for season, s in shots.groupby("season"):
        lgs = league[league.season == season]
        for side in ("offense", "defense"):
            teams.append(team_profile(s, lgs, side, k[side]).assign(season=season))
    team = pd.concat(teams, ignore_index=True)

    # players: their mix and efficiency (shrunk with the team constants: conservative)
    p = shots.merge(league[["season", "play", "lg_pps"]], on=["season", "play"])
    pl = p.groupby(["season", "person_id", "play"]).agg(
        shots=("pts", "size"), pts=("pts", "sum"), made=("made", "sum"),
        assisted=("assisted", "sum"), lg_pps=("lg_pps", "mean"),
    ).reset_index()  # fmt: skip
    main_team = p.groupby(["season", "person_id"]).team_id.agg(lambda v: v.mode().iloc[0])
    pl = pl.merge(main_team.rename("team_id").reset_index(), on=["season", "person_id"])
    pl["share"] = pl.shots / pl.groupby(["season", "person_id"]).shots.transform("sum")
    pl["pps"] = pl.pts / pl.shots
    pl = _rel(pl, k["offense"])

    assists = con.execute("""
        select season, team_id, assist_person_id as passer_id, person_id as shooter_id, play,
               count(*) as assists, sum(pts) as pts
        from plays.shots where made and assist_person_id is not null group by all""").df()
    tov = con.execute("""
        with t as (select season, team_id, opp_id, kind, count(*) n from plays.turnover_events
                   group by all),
        poss as (select season, team_id, sum(poss) poss, sum(o_poss) o_poss
                 from metrics.team_game_counts where season_type = 'regular' and season >= ?
                 group by all)
        select t.season, t.team_id, 'offense' as side, t.kind, sum(t.n) as n,
               100 * sum(t.n) / any_value(p.poss) as per100
        from t join poss p using (season, team_id) group by all
        union all
        select t.season, t.opp_id, 'defense', t.kind, sum(t.n),
               100 * sum(t.n) / any_value(p.o_poss)
        from t join poss p on p.season = t.season and p.team_id = t.opp_id group by all""",
                      [FIRST_SEASON]).df()  # fmt: skip
    tov["lg_per100"] = tov.groupby(["season", "side", "kind"]).per100.transform("mean")
    tov["per100_rel"] = tov.per100 - tov.lg_per100
    log.info("plays: matchup test")
    mt = matchup_test(shots, league, k["offense"], k["defense"])
    for name, df in (("league", lg), ("team", team), ("player", pl), ("assists", assists),
                     ("turnovers", tov), ("reliability", rel), ("matchup_test", mt)):  # fmt: skip
        con.register("tmp_df", df)
        con.execute(f"create or replace table plays.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    return {"shots": len(shots), "seasons": int(shots.season.nunique()),
            "team_rows": len(team), "players": int(pl.person_id.nunique())}  # fmt: skip
