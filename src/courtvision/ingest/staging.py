"""Conformed staging views (schema ``stg``) over the raw source views.

Staging does three things and nothing else:

1. Conform keys: ``game_id`` is always the 10-character NBA string id;
   ``season`` (start year, e.g. 2025 for 2025-26) and ``season_type`` are
   derived from it; person/team ids are BIGINT.
2. Rename to snake_case where we select columns explicitly.
3. Repair documented source defects (see docs/data_audit.md), with a flag
   column wherever a value is imputed rather than dropped:
   * ``player_statistics.playerteamId`` is NULL for 99.9% of 2021-22 and
     50.2% of 2000-01 regular-season rows (and <1% of a dozen other
     seasons). Recovered from the ``home`` flag + ``games`` home/away ids,
     which agree 100% wherever ``playerteamId`` is present; flagged by
     ``team_id_repaired``. Only preseason/All-Star rows stay unrepaired.
   * ``team_statistics`` has placeholder rows with ``teamId = 0`` (stale
     schedule rows for postponed games); dropped.
   * ``play_by_play`` mixes two schemas. Legacy (1996-97 to part of 2019-20)
     marks shots ``actionType`` 'Made Shot'/'Missed Shot' (sometimes with
     trailing spaces); live (2019-20 on) uses '2pt'/'3pt'. ``isFieldGoal`` is
     NULL for 323 live-format 2019-20 games, so shots are identified by
     ``actionType``, never by ``isFieldGoal``. ``teamId`` is NULL for all of
     2026-27; ``stg.shots`` recovers it from the shooter's box-score row
     (flagged ``team_id_repaired``).

No metric logic lives here.
"""

from __future__ import annotations

import duckdb

STAGING_SQL = """
create schema if not exists stg;

create or replace view stg.games as
select
    cv_game_id(gameId)            as game_id,
    cv_season(gameId)             as season,
    cv_season_type(gameId)        as season_type,
    cast(gameDateTimeEst as date) as game_date,
    gameDateTimeEst               as game_datetime_est,
    hometeamId                    as home_team_id,
    awayteamId                    as away_team_id,
    homeScore                     as home_score,
    awayScore                     as away_score,
    winner                        as winner_team_id,
    gameType                      as game_type_label,
    gameSubtype                   as game_subtype,
    gameLabel                     as game_label,
    gameSubLabel                  as game_sub_label,
    attendance,
    arenaName                     as arena_name
from kaggle.games;

create or replace view stg.players as
select
    personId       as person_id,
    firstName      as first_name,
    lastName       as last_name,
    birthDate      as birth_date,
    heightInches   as height_in,
    bodyWeightLbs  as weight_lb,
    guard = 1      as is_guard,
    forward = 1    as is_forward,
    center = 1     as is_center,
    draftYear      as draft_year,
    draftRound     as draft_round,
    draftNumber    as draft_number,
    fromYear       as from_year,
    toYear         as to_year,
    country,
    school
from kaggle.players;

create or replace view stg.player_games as
select
    cv_game_id(p.gameId)     as game_id,
    cv_season(p.gameId)      as season,
    cv_season_type(p.gameId) as season_type,
    p.personId               as person_id,
    coalesce(p.playerteamId,
             case when p.home = 1 then g.hometeamId else g.awayteamId end) as team_id,
    coalesce(p.opponentteamId,
             case when p.home = 1 then g.awayteamId else g.hometeamId end) as opponent_team_id,
    p.playerteamId is null   as team_id_repaired,
    p.home = 1               as is_home,
    p.win = 1                as is_win,
    try_cast(p.numMinutes as double) as minutes,
    p.startingPosition       as starting_position,
    p.comment,
    p.points,
    p.fieldGoalsMade         as fgm,
    p.fieldGoalsAttempted    as fga,
    p.threePointersMade      as fg3m,
    p.threePointersAttempted as fg3a,
    p.freeThrowsMade         as ftm,
    p.freeThrowsAttempted    as fta,
    p.reboundsOffensive      as oreb,
    p.reboundsDefensive      as dreb,
    p.reboundsTotal          as reb,
    p.assists                as ast,
    p.steals                 as stl,
    p.blocks                 as blk,
    p.turnovers              as tov,
    p.foulsPersonal          as pf,
    p.plusMinusPoints        as plus_minus
from kaggle.player_statistics p
left join kaggle.games g on g.gameId = p.gameId;

create or replace view stg.team_games as
select
    cv_game_id(gameId)       as game_id,
    cv_season(gameId)        as season,
    cv_season_type(gameId)   as season_type,
    teamId                   as team_id,
    opponentTeamId           as opponent_team_id,
    home = 1                 as is_home,
    win = 1                  as is_win,
    teamScore                as points,
    opponentScore            as opponent_points,
    fieldGoalsMade           as fgm,
    fieldGoalsAttempted      as fga,
    threePointersMade        as fg3m,
    threePointersAttempted   as fg3a,
    freeThrowsMade           as ftm,
    freeThrowsAttempted      as fta,
    reboundsOffensive        as oreb,
    reboundsDefensive        as dreb,
    reboundsTotal            as reb,
    reboundsTeam             as team_reb,
    assists                  as ast,
    steals                   as stl,
    blocks                   as blk,
    turnovers                as tov,  -- player turnovers only; official TOV = tov + team_tov
    turnoversTeam            as team_tov,
    turnoversTeam is null    as team_tov_missing,  -- all of 2021-22, half of 2000-01
    foulsPersonal            as pf,
    numMinutes               as minutes
from kaggle.team_statistics
where teamId <> 0;

create or replace view stg.pbp as
select
    cv_game_id(gameId)                as game_id,
    cv_season(gameId)                 as season,
    cv_season_type(gameId)            as season_type,
    try_cast(personId as bigint)      as person_id,
    try_cast(teamId as bigint)        as team_id,
    * exclude (gameId)
from kaggle.play_by_play;

-- One row per field-goal attempt, both PBP schemas unified.
-- x_legacy / y_legacy: tenths of a foot, rim at (0, 0), +y toward half court.
create or replace view stg.shots as
with s as (
    select
        p.*,
        trim(p.actionType) as action_type,
        case when p.actionType in ('2pt', '3pt') then 'live' else 'legacy' end as pbp_format
    from stg.pbp p
    where trim(p.actionType) in ('Made Shot', 'Missed Shot') or p.actionType in ('2pt', '3pt')
)
select
    s.game_id,
    s.season,
    s.season_type,
    s.actionNumber                                   as action_number,
    s.period,
    s.clock,
    s.pbp_format,
    coalesce(s.team_id, pg.team_id)                  as team_id,
    s.team_id is null                                as team_id_repaired,
    s.person_id,
    case
        when s.action_type = '3pt' then 3
        when s.action_type = '2pt' then 2
        when s.shotValue in (2, 3) then s.shotValue
        when s.description ilike '%3PT%' then 3
        else 2
    end                                              as shot_value,
    case
        when s.pbp_format = 'legacy' then s.action_type = 'Made Shot'
        else s.shotResult ilike '%made'
    end                                              as made,
    s.xLegacy                                        as x_legacy,
    s.yLegacy                                        as y_legacy,
    s.shotDistance                                   as shot_distance,
    trim(s.subType)                                  as sub_type,
    s.descriptor,
    s.area,
    s.areaDetail                                     as area_detail,
    try_cast(nullif(s.assistPersonId, '0') as bigint) as assist_person_id,
    try_cast(nullif(s.blockPersonId, '0') as bigint)  as block_person_id,
    s.description
from s
left join stg.player_games pg on pg.game_id = s.game_id and pg.person_id = s.person_id;
"""


def build(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(STAGING_SQL)
