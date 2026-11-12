"""SQL stage of the metric build: counts, league baselines, components, ratings.

Everything here implements docs/metrics_spec.md. The structure mirrors the spec:

* **Counts** (additive) are built once per level: team-game, team-season, player-game,
  player-season. Higher levels are sums of lower ones, so every value is a ratio of sums
  (spec section 3) and roll-ups are exact by construction.
* **Formulas** (:data:`MEASURES`) are written once, as SQL expressions over standard
  column names, and applied to every level's counts table. Column-name conventions:

  ========== ==================================================================
  ``a_*``    actor shot counts from play-by-play (the unit's own shots)
  plain      play-by-play counts of the unit's *team* offense: the team itself,
             or the team while the player is on the floor
  ``o_*``    the same for the opponent (team: the opponent; player: the
             opponent while the player is on the floor)
  ``b_*``    actor box-score counts (team: whole team incl. team turnovers)
  ``o_b_*``  opponent box-score counts (team levels only)
  ``bc_*``   position-weighted (``w_bc``) actor counts; ``fc_*`` likewise
  ``L.*``    league baselines for the season (:data:`LEAGUE_SQL`)
  ========== ==================================================================

* **Ratings**: values go to one long table (level, season, tier, measure, unit, value,
  exposure), are shrunk at player-season level, and are scaled 50 + 10 z against each
  (level, season, tier, measure) reference population.
"""

from __future__ import annotations

C_FT = 0.40  # share of free throws that end a trip (spec section 1)

RATED_TYPES_SQL = "'regular', 'play_in', 'playoffs', 'cup_final'"
SEASON_AGG_TYPES_SQL = "'regular', 'playoffs'"
FIRST_SEASON = 1954  # seasons before 1954-55 have incomplete game coverage

ZONES = {"RA": "ra", "PAINT": "pnt", "MID": "mid", "C3": "c3", "ATB3": "atb3"}
ZONE_VALUE = {"RA": 2, "PAINT": 2, "MID": 2, "C3": 3, "ATB3": 3}
PAINT_ZONES = ("RA", "PAINT")
PERIMETER_ZONES = ("MID", "C3", "ATB3")

# Play-by-play counts summed at every level.
PBP_COUNTS = [
    "fga", "fgm", "pts_fg", "fta", "ftm", "tov", "oreb", "dreb",
    "fga_r", "fgm_r", "pts_r", "xp_r",
    "fga_paint", "pts_paint", "xp_paint", "fga_perim", "pts_perim", "xp_perim",
    *[f"{k}_{z}" for z in ZONES.values() for k in ("fga", "fgm")],
]  # fmt: skip
# Position-weighted actor play-by-play counts (Backcourt / Frontcourt, Presence).
PBP_WEIGHTED = ["fga_r", "pts_r", "xp_r", "fga", "fta", "tov"]
# Box-score columns (player and team).
BOX_COUNTS = [
    "pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk",
    "tov", "pf", "plays",
]  # fmt: skip
BOX_WEIGHTED = ["pts", "ftm", "fga", "fta", "tov", "plays", "stl", "pf"]
OPP_BOX = ["pts", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "tov", "poss"]
ACTOR_SHOTS = ["fga_r", "fgm_r", "pts_r", "xp_r", *[f"{k}_{z}" for z in ZONES.values() for k in ("fga", "fgm")]]  # fmt: skip


def _sums(cols, src="", prefix="", weight=None) -> str:
    w = f"{weight} * " if weight else ""
    return ",\n    ".join(f"sum({w}{src}{c}) as {prefix}{c}" for c in cols)


def _cols(cols, src="", prefix="") -> str:
    return ",\n    ".join(f"{src}{c} as {prefix}{c}" for c in cols)


def _cols_if(cols, cond, src="", prefix="") -> str:
    return ",\n    ".join(f"case when {cond} then {src}{c} end as {prefix}{c}" for c in cols)


def _zone_list(zones) -> str:
    return ", ".join(f"'{z}'" for z in zones)


# ---- base tables (no tier logic) ---------------------------------------------------------


def base_sql() -> list[tuple[str, str]]:
    """Statements that build everything up to the league baselines."""
    zone_cases = "\n".join(
        f"    case when s.zone = '{z}' then 1 else 0 end as fga_{k},\n"
        f"    case when s.zone = '{z}' then s.fgm else 0 end as fgm_{k},"
        for z, k in ZONES.items()
    )
    rated = _zone_list(ZONES)
    value_case = " ".join(f"when '{z}' then {v}" for z, v in ZONE_VALUE.items())
    return [
        (
            "positions",
            """
create or replace table metrics.positions as
with f as (
    select person_id,
           coalesce(is_guard::int, 0) as g,
           coalesce(is_forward::int, 0) as f,
           coalesce(is_center::int, 0) as c
    from stg.players
)
select person_id,
       case when g + f + c > 0 then g / (g + f + c) end as w_bc,
       case when g + f + c > 0 then (f + c) / (g + f + c) end as w_fc
from f;
""",
        ),
        (
            "box_player_game",
            f"""
create or replace table metrics.box_player_game as
select
    pg.game_id, pg.season, pg.season_type, g.game_date, pg.person_id, pg.team_id,
    pg.opponent_team_id,
    coalesce(pg.minutes, 0) as minutes,
    coalesce(pg.points, 0) as b_pts, coalesce(pg.fgm, 0) as b_fgm, coalesce(pg.fga, 0) as b_fga,
    coalesce(pg.fg3m, 0) as b_fg3m, coalesce(pg.fg3a, 0) as b_fg3a,
    coalesce(pg.ftm, 0) as b_ftm, coalesce(pg.fta, 0) as b_fta,
    coalesce(pg.oreb, 0) as b_oreb, coalesce(pg.dreb, 0) as b_dreb,
    coalesce(pg.ast, 0) as b_ast, coalesce(pg.stl, 0) as b_stl, coalesce(pg.blk, 0) as b_blk,
    coalesce(pg.tov, 0) as b_tov, coalesce(pg.pf, 0) as b_pf,
    coalesce(pg.fga, 0) + {C_FT} * coalesce(pg.fta, 0) + coalesce(pg.tov, 0) as b_plays,
    pg.plus_minus as b_pm,
    pos.w_bc, pos.w_fc
from stg.player_games pg
join stg.games g using (game_id)
left join metrics.positions pos using (person_id)
where pg.season_type in ({RATED_TYPES_SQL})
  and pg.season >= {FIRST_SEASON}
  and pg.team_id is not null
  and (coalesce(pg.minutes, 0) > 0
       or coalesce(pg.fga, 0) + coalesce(pg.fta, 0) + coalesce(pg.tov, 0)
          + coalesce(pg.reb, 0) + coalesce(pg.pf, 0) > 0
       -- played under 30 seconds (box minutes round to 0) but is in a reconstructed stint
       or exists (select 1 from metrics.stints s
                  where s.game_id = pg.game_id and s.person_id = pg.person_id));
""",
        ),
        (
            "box_team_game",
            f"""
-- Team box built from player rows (more complete than team rows before ~1985; see the
-- data audit), plus team turnovers from the team row (official TOV = player + team).
create or replace table metrics.box_team_game as
with p as (
    select game_id, season, season_type, any_value(game_date) as game_date, team_id,
           any_value(opponent_team_id) as opponent_team_id,
           sum(minutes) as minutes,
           {_sums([c for c in BOX_COUNTS if c not in ("tov", "plays")], prefix="b_", src="b_")},
           sum(b_tov) as b_tov_players,
           {_sums(BOX_WEIGHTED, src="b_", prefix="bc_b_", weight="coalesce(w_bc, 0)")},
           {_sums(BOX_WEIGHTED, src="b_", prefix="fc_b_", weight="coalesce(w_fc, 0)")}
    from metrics.box_player_game
    group by game_id, season, season_type, team_id
)
select p.*,
       coalesce(t.team_tov, 0) as b_team_tov,
       p.b_tov_players + coalesce(t.team_tov, 0) as b_tov,
       p.b_fga + {C_FT} * p.b_fta + p.b_tov_players + coalesce(t.team_tov, 0) as b_plays,
       p.b_fga + {C_FT} * p.b_fta - p.b_oreb + p.b_tov_players + coalesce(t.team_tov, 0)
           as b_poss
from p
left join stg.team_games t using (game_id, team_id);
""",
        ),
        (
            "stat_availability",
            """
-- D9(a): a box-score input counts from the first season in which it is non-zero in at
-- least 95% of regular-season team-games, and stays at or above 95% afterwards.
create or replace table metrics.stat_availability as
with s as (
    select season, count(*) as team_games,
           avg((b_fga > 0)::int) as fga, avg((b_fta > 0)::int) as fta,
           avg((b_fg3a > 0)::int) as fg3a, avg((b_oreb > 0)::int) as oreb,
           avg((b_dreb > 0)::int) as dreb, avg((b_tov > 0)::int) as tov,
           avg((b_stl > 0)::int) as stl, avg((b_blk > 0)::int) as blk,
           avg((b_ast > 0)::int) as ast, avg((b_pf > 0)::int) as pf
    from metrics.box_team_game where season_type = 'regular'
    group by season
)
unpivot s on fga, fta, fg3a, oreb, dreb, tov, stl, blk, ast, pf into name stat value share;

create or replace table metrics.stat_first_season as
select stat,
       coalesce(max(season) filter (where share < 0.95) + 1, min(season)) as first_season
from metrics.stat_availability
group by stat;
""",
        ),
        (
            "event_facts",
            """
create or replace table metrics.event_facts as
select
    e.game_id, e.season, e.season_type, p.stint_id, e.team_id, e.person_id,
    (e.etype = 'fga')::int as fga,
    (e.etype = 'fga' and coalesce(e.made, false))::int as fgm,
    case when e.etype = 'fga' and e.made then e.shot_value else 0 end as pts_fg,
    case when e.etype = 'fga' then cv_zone(e.shot_value, e.x_legacy, e.y_legacy) end as zone,
    (e.etype = 'ft')::int as fta,
    (e.etype = 'ft' and coalesce(e.made, false))::int as ftm,
    (e.etype = 'tov')::int as tov,
    (e.etype = 'reb' and e.person_id is not null and p.reb_kind = 'off')::int as oreb,
    (e.etype = 'reb' and e.person_id is not null and p.reb_kind = 'def')::int as dreb,
    coalesce(pos.w_bc, 0) as w_bc,
    coalesce(pos.w_fc, 0) as w_fc
from metrics.event_inputs e
join metrics.pbp_events p on p.game_id = e.game_id and p.seq = e.seq
left join metrics.positions pos on pos.person_id = e.person_id
where e.etype in ('fga', 'ft', 'tov', 'reb') and e.team_id is not null;
""",
        ),
        (
            "zone_baselines",
            f"""
-- League zone make rates p_(s,z) and expected points per shot, regular season.
create or replace table metrics.zone_baselines as
select season, zone,
       case zone {value_case} end as shot_value,
       count(*) as fga,
       sum(fgm) as fgm,
       sum(fgm) / count(*) as fg_pct,
       count(*) / sum(count(*)) over (partition by season) as share,
       case zone {value_case} end * sum(fgm) / count(*) as xpts
from metrics.event_facts
where season_type = 'regular' and fga = 1 and zone in ({rated})
group by season, zone;
""",
        ),
        (
            "event_counts",
            f"""
create or replace table metrics.event_counts as
with s as (
    select f.*, f.fga = 1 and f.zone in ({rated}) as rated, zb.xpts
    from metrics.event_facts f
    left join metrics.zone_baselines zb on zb.season = f.season and zb.zone = f.zone
)
select
    s.game_id, s.season, s.season_type, s.stint_id, s.team_id, s.person_id, s.w_bc, s.w_fc,
    s.fga, s.fgm, s.pts_fg, s.fta, s.ftm, s.tov, s.oreb, s.dreb,
    s.rated::int as fga_r,
    case when s.rated then s.fgm else 0 end as fgm_r,
    case when s.rated then s.pts_fg else 0 end as pts_r,
    case when s.rated then s.xpts else 0 end as xp_r,
    (s.zone in ({_zone_list(PAINT_ZONES)}))::int as fga_paint,
    case when s.zone in ({_zone_list(PAINT_ZONES)}) then s.pts_fg else 0 end as pts_paint,
    case when s.zone in ({_zone_list(PAINT_ZONES)}) then s.xpts else 0 end as xp_paint,
    (s.zone in ({_zone_list(PERIMETER_ZONES)}))::int as fga_perim,
    case when s.zone in ({_zone_list(PERIMETER_ZONES)}) then s.pts_fg else 0 end as pts_perim,
    case when s.zone in ({_zone_list(PERIMETER_ZONES)}) then s.xpts else 0 end as xp_perim,
{zone_cases}
from s;
""",
        ),
        (
            "pbp_team_game",
            f"""
create or replace table metrics.pbp_team_game as
with c as (
    select game_id, team_id,
           {_sums(PBP_COUNTS)},
           {_sums(PBP_WEIGHTED, prefix="bc_", weight="w_bc")},
           {_sums(PBP_WEIGHTED, prefix="fc_", weight="w_fc")}
    from metrics.event_counts
    group by game_id, team_id
), p as (
    select game_id, team_id, count(*) as poss from metrics.possessions group by all
)
select c.*, coalesce(p.poss, 0) as poss
from c left join p using (game_id, team_id);
""",
        ),
        (
            "game_pbp_qa",
            """
-- A game's play-by-play is complete when both teams' play-by-play points and field-goal
-- attempts are within 3 of the box score (true for >99% of team-games every season; a few
-- 2019-20 games are truncated in the source). Tier A uses complete games only.
create or replace table metrics.game_pbp_qa as
select b.game_id,
       bool_and(abs(coalesce(t.pts_fg + t.ftm, 0) - b.b_pts) <= 3
                and abs(coalesce(t.fga, 0) - b.b_fga) <= 3) as pbp_ok,
       coalesce(bool_and(q.ok), false) as lineups_ok
from metrics.box_team_game b
join metrics.pbp_team_game t using (game_id, team_id)
left join metrics.lineup_qa q using (game_id)
group by b.game_id;
""",
        ),
        (
            "pbp_stint_team",
            f"""
create or replace table metrics.pbp_stint_team as
with c as (
    select game_id, stint_id, team_id,
           {_sums(PBP_COUNTS)},
           {_sums(PBP_WEIGHTED, prefix="bc_", weight="w_bc")},
           {_sums(PBP_WEIGHTED, prefix="fc_", weight="w_fc")}
    from metrics.event_counts
    group by game_id, stint_id, team_id
), p as (
    select game_id, stint_id, team_id, count(*) as poss from metrics.possessions group by all
), k as (
    select distinct game_id, stint_id, team_id from metrics.stints
)
select k.game_id, k.stint_id, k.team_id,
       {_cols(PBP_COUNTS, src="coalesce(c.", prefix="").replace(" as ", ", 0) as ")},
       {_cols(["bc_" + c for c in PBP_WEIGHTED] + ["fc_" + c for c in PBP_WEIGHTED], src="coalesce(c.").replace(" as ", ", 0) as ")},
       coalesce(p.poss, 0) as poss
from k
left join c using (game_id, stint_id, team_id)
left join p using (game_id, stint_id, team_id);
""",
        ),
        (
            "oncourt_player_game",
            f"""
-- On-court counts (D7 Defense, D3 rebounding, Presence): team offense (plain names) and
-- opponent offense (o_*) during the player's stints. Only games whose lineups pass QA.
create or replace table metrics.oncourt_player_game as
with st as (
    select s.game_id, s.stint_id, s.team_id, s.person_id, any_value(s.end_s - s.start_s) as dur
    from metrics.stints s
    join metrics.game_pbp_qa q on q.game_id = s.game_id and q.pbp_ok and q.lineups_ok
    group by all
)
select st.game_id, st.person_id, st.team_id,
       sum(st.dur) as sec_on,
       {_sums(PBP_COUNTS + ["poss"], src="o.")},
       {_sums(["bc_" + c for c in PBP_WEIGHTED] + ["fc_" + c for c in PBP_WEIGHTED], src="o.")},
       {_sums(PBP_COUNTS + ["poss"], src="d.", prefix="o_")}
from st
join metrics.pbp_stint_team o
  on o.game_id = st.game_id and o.stint_id = st.stint_id and o.team_id = st.team_id
join metrics.pbp_stint_team d
  on d.game_id = st.game_id and d.stint_id = st.stint_id and d.team_id <> st.team_id
group by st.game_id, st.person_id, st.team_id;
""",
        ),
        (
            "team_game_counts",
            f"""
create or replace table metrics.team_game_counts as
select
    b.game_id, b.season, b.season_type, b.game_date, b.team_id, b.opponent_team_id,
    q.pbp_ok, q.lineups_ok,
    {_cols_if(PBP_COUNTS + ["poss"], "q.pbp_ok", src="t.")},
    {_cols_if(["bc_" + c for c in PBP_WEIGHTED] + ["fc_" + c for c in PBP_WEIGHTED], "q.pbp_ok", src="t.")},
    {_cols_if(PBP_COUNTS + ["poss"], "q.pbp_ok", src="o.", prefix="o_")},
    {_cols_if(ACTOR_SHOTS, "q.pbp_ok", src="t.", prefix="a_")},
    {_cols(["minutes"] + ["b_" + c for c in BOX_COUNTS] + ["b_poss", "b_team_tov", "b_tov_players"], src="b.")},
    {_cols(["b_" + c for c in OPP_BOX], src="ob.", prefix="o_")},
    {_cols(["bc_b_" + c for c in BOX_WEIGHTED] + ["fc_b_" + c for c in BOX_WEIGHTED], src="b.")},
    1.0 as share,
    b.b_poss as bcfc_den_b
from metrics.box_team_game b
left join metrics.box_team_game ob
  on ob.game_id = b.game_id and ob.team_id = b.opponent_team_id
left join metrics.pbp_team_game t on t.game_id = b.game_id and t.team_id = b.team_id
left join metrics.pbp_team_game o on o.game_id = b.game_id and o.team_id = b.opponent_team_id
left join metrics.game_pbp_qa q on q.game_id = b.game_id;
""",
        ),
        (
            "player_game_counts",
            f"""
create or replace table metrics.player_game_counts as
with a as (
    select game_id, person_id, {_sums(ACTOR_SHOTS)}
    from metrics.event_counts where person_id is not null
    group by game_id, person_id
)
select
    b.game_id, b.season, b.season_type, b.game_date, b.person_id, b.team_id,
    b.opponent_team_id, b.minutes, b.w_bc, b.w_fc,
    oc.sec_on,
    {_cols_if(ACTOR_SHOTS, "q.pbp_ok", src="a.", prefix="a_")},
    {_cols(PBP_COUNTS + ["poss"], src="oc.")},
    {_cols(PBP_COUNTS + ["poss"], src="oc.o_", prefix="o_")},
    {_cols(["b_" + c for c in BOX_COUNTS] + ["b_pm"], src="b.")},
    {", ".join(f"case when q.pbp_ok then coalesce(b.w_bc, 0) * a.{c} end as bc_{c}" for c in ("fga_r", "pts_r", "xp_r"))},
    {", ".join(f"case when q.pbp_ok then coalesce(b.w_fc, 0) * a.{c} end as fc_{c}" for c in ("fga_r", "pts_r", "xp_r"))},
    {", ".join(f"coalesce(b.w_bc, 0) * b.b_{c} as bc_b_{c}" for c in BOX_WEIGHTED)},
    {", ".join(f"coalesce(b.w_fc, 0) * b.b_{c} as fc_b_{c}" for c in BOX_WEIGHTED)},
    0.2 as share,
    b.b_plays as bcfc_den_b
from metrics.box_player_game b
left join a using (game_id, person_id)
left join metrics.oncourt_player_game oc using (game_id, person_id)
left join metrics.game_pbp_qa q using (game_id);
""",
        ),
        (
            "league_baselines",
            f"""
-- League constants per season, regular season, all teams (spec sections 1 and 5).
create or replace table metrics.league_baselines as
select
    season,
    count(*) as team_games,
    -- play-by-play (tier A)
    sum(pts_r) / nullif(sum(fga_r), 0) as lg_pps,
    sum(tov) / nullif(sum(poss), 0) as lg_tovr,
    sum(pts_fg + ftm) / nullif(sum(poss), 0) as lg_ppp,
    sum(pts_fg + ftm) / nullif(sum(poss - tov), 0) as lg_ppnt,
    sum(fta) / nullif(sum(fga), 0) as lg_ftr,
    sum(ftm) / nullif(sum(fta), 0) as lg_ftpct,
    sum(dreb) / nullif(sum(dreb + oreb), 0) as lg_dreb_pct,
    sum(oreb) / nullif(sum(dreb + oreb), 0) as lg_oreb_pct,
    sum(tov) / nullif(sum(fga + {C_FT} * fta + tov), 0) as lg_tovp_pbp,
    (sum(pts_r) / nullif(sum(fga_r), 0) * sum(fga_perim) - sum(pts_perim))
        / nullif(sum(poss), 0) as lg_dz_perim,
    (sum(pts_r) / nullif(sum(fga_r), 0) * sum(fga_paint) - sum(pts_paint))
        / nullif(sum(poss), 0) as lg_dz_paint,
    sum(case when pbp_ok then bc_b_stl end) / nullif(sum(poss), 0) as lg_bc_stl,
    sum(case when pbp_ok then fc_b_stl end) / nullif(sum(poss), 0) as lg_fc_stl,
    sum(case when pbp_ok then bc_b_pf end) / nullif(sum(poss), 0) as lg_bc_pf,
    sum(case when pbp_ok then fc_b_pf end) / nullif(sum(poss), 0) as lg_fc_pf,
    -- box score (tiers B, B-, C and Ball control)
    sum(b_pts - b_ftm) / nullif(sum(b_fga), 0) as lg_pps_b,
    sum(b_fgm - b_fg3m) / nullif(sum(b_fga - b_fg3a), 0) as lg_p2_b,
    sum(b_fg3m) / nullif(sum(b_fg3a), 0) as lg_p3_b,
    sum(b_tov) / nullif(sum(b_poss), 0) as lg_tovr_b,
    sum(b_pts) / nullif(sum(b_poss), 0) as lg_ppp_b,
    sum(b_pts) / nullif(sum(b_poss - b_tov), 0) as lg_ppnt_b,
    sum(b_fta) / nullif(sum(b_fga), 0) as lg_ftr_b,
    sum(b_ftm) / nullif(sum(b_fta), 0) as lg_ftpct_b,
    sum(b_dreb) / nullif(sum(b_dreb + b_oreb), 0) as lg_dreb_pct_b,
    sum(b_oreb) / nullif(sum(b_dreb + b_oreb), 0) as lg_oreb_pct_b,
    sum(b_tov) / nullif(sum(b_plays), 0) as lg_tovp,
    sum(b_ftm) / nullif(sum(b_pf), 0) as lg_ftpts_per_pf,
    sum(b_ast) / nullif(sum(b_fgm), 0) as lg_ast_rate,
    sum(b_ast) / nullif(sum(b_tov_players), 0) as lg_ato,
    {C_FT} as c_ft
from metrics.team_game_counts
where season_type = 'regular'
group by season;
""",
        ),
    ]
