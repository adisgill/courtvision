"""Conformed staging views (schema ``stg``) over the raw source views.

Staging does three things and nothing else:

1. Conform keys: ``game_id`` is always the 10-character NBA string id;
   ``season`` (start year, e.g. 2025 for 2025-26) and ``season_type`` are
   derived from it; person/team ids are BIGINT.
2. Rename to snake_case where we select columns explicitly.
3. Repair documented source defects, always with a flag column saying the
   value was repaired (see docs/data_audit.md):
   * ``player_statistics.playerteamId`` is NULL for ~50% of 2000-01 and ~100%
     of 2021-22 rows. Recovered from the ``home`` flag + ``games`` home/away
     ids, which agree 100% wherever ``playerteamId`` is present.

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
    turnovers                as tov,
    turnoversTeam            as team_tov,
    foulsPersonal            as pf,
    numMinutes               as minutes
from kaggle.team_statistics;

create or replace view stg.pbp as
select
    cv_game_id(gameId)                as game_id,
    cv_season(gameId)                 as season,
    cv_season_type(gameId)            as season_type,
    try_cast(personId as bigint)      as person_id,
    try_cast(teamId as bigint)        as team_id,
    * exclude (gameId)
from kaggle.play_by_play;
"""


def build(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(STAGING_SQL)
