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

-- One row per play-by-play event, both schemas normalized into one vocabulary so that
-- possession counting and lineup reconstruction (courtvision.metrics.pbp) can treat every
-- season alike.
--   seq        chronological order within the game. Live-format games use orderNumber.
--              Legacy actionNumber is out of clock order for ~0.4% of events, so legacy
--              games sort by (period, clock descending, actionNumber).
--   etype      fga / ft / reb / tov / foul / sub / jump / viol / timeout / period_start /
--              period_end / other
--   team_id    repaired: teamId, else playerteamId (2026-27 has only this), else a team id
--              stored in personId (legacy team events), else the player's box-score team,
--              else (live team rebounds/turnovers with no team) the possession column.
--   person_id  players only (team ids and 0 removed).
create or replace view stg.events as
with raw as (
    select
        p.*,
        lower(trim(p.actionType)) as act,
        trim(p.subType)           as sub,
        bool_or(p.actionType in ('2pt', '3pt')) over (partition by p.game_id) as is_live,
        cv_clock_s(p.clock)       as clock_s,
        case when cv_is_team_id(p.personId) or p.personId in ('0', '') then null
             else try_cast(p.personId as bigint) end as player_id
    from stg.pbp p
), typed as (
    select
        r.*,
        case
            when r.act in ('made shot', 'missed shot', '2pt', '3pt') then 'fga'
            when r.act in ('free throw', 'freethrow') then 'ft'
            when r.act = 'rebound' then 'reb'
            when r.act = 'turnover' then 'tov'
            when r.act = 'foul' then 'foul'
            when r.act = 'substitution' then 'sub'
            when r.act in ('jump ball', 'jumpball') then 'jump'
            when r.act = 'violation' then 'viol'
            when r.act = 'timeout' then 'timeout'
            when r.act = 'period' and lower(r.sub) = 'start' then 'period_start'
            when r.act = 'period' and lower(r.sub) = 'end' then 'period_end'
            else 'other'
        end as etype
    from raw r
)
select
    t.game_id,
    t.season,
    t.season_type,
    case when t.is_live then 'live' else 'legacy' end as pbp_format,
    row_number() over (
        partition by t.game_id
        order by t.period,
                 case when t.is_live then 0 else -t.clock_s end,
                 case when t.is_live then coalesce(t.orderNumber, t.actionNumber)
                      else t.actionNumber end
    )                                                  as seq,
    t.actionNumber                                     as action_number,
    t.period,
    t.clock_s,
    cv_period_start_s(t.period) + cv_period_len_s(t.period) - t.clock_s as elapsed_s,
    t.etype,
    coalesce(
        case when cv_is_team_id(t.teamId) then try_cast(t.teamId as bigint) end,
        case when cv_is_team_id(t.playerteamId) then try_cast(t.playerteamId as bigint) end,
        case when cv_is_team_id(t.personId) then try_cast(t.personId as bigint) end,
        pg.team_id,
        case when t.is_live and t.etype in ('reb', 'tov') and cv_is_team_id(t.possession)
             then try_cast(t.possession as bigint) end
    )                                                  as team_id,
    t.player_id                                        as person_id,
    t.playerName                                       as player_name,
    -- shots
    case
        when t.etype <> 'fga' then null
        when t.act = '3pt' then 3
        when t.act = '2pt' then 2
        when t.shotValue in (2, 3) then t.shotValue
        when t.description ilike '%3PT%' then 3
        else 2
    end                                                as shot_value,
    case
        when t.etype = 'fga' and not t.is_live then t.act = 'made shot'
        when t.etype = 'fga' then t.shotResult ilike '%made'
        -- legacy FTs carry an empty shotResult; the description starts with MISS on a miss
        when t.etype = 'ft' and t.is_live then t.shotResult ilike '%made'
        when t.etype = 'ft' then not (t.description ilike 'MISS%')
    end                                                as made,
    case when t.etype = 'fga' then t.xLegacy end       as x_legacy,
    case when t.etype = 'fga' then t.yLegacy end       as y_legacy,
    -- free throws: technical FTs do not belong to a possession
    case when t.etype = 'ft'
         then t.sub ilike '%technical%' or coalesce(t.descriptor, '') ilike 'technical' end
                                                       as ft_technical,
    -- last free throw of an ordinary trip (n of n), after which the other team gets the
    -- ball; flagrant / clear-path / away-from-play / take trips keep the ball, so excluded
    case when t.etype = 'ft' then
        regexp_extract(t.sub, '(\\d) ?of ?(\\d)', 1) <> ''
        and regexp_extract(t.sub, '(\\d) ?of ?(\\d)', 1)
            = regexp_extract(t.sub, '(\\d) ?of ?(\\d)', 2)
        and not regexp_matches(lower(t.sub || ' ' || coalesce(t.descriptor, '')),
                               'technical|flagrant|clear|away|take')
    end                                                as ft_last,
    -- rebounds: the live schema labels them; legacy kind is derived in courtvision.metrics.pbp
    case when t.etype = 'reb' and lower(t.sub) in ('offensive', 'defensive')
         then left(lower(t.sub), 3) end                as reb_kind,
    -- fouls and other events that a bench player can be charged with
    t.etype = 'foul' and t.sub ilike '%technical%' or t.act = 'ejection' as bench_possible,
    -- substitutions: live has one row per player ('in' / 'out'); legacy has one row per
    -- swap, personId = player out, player in only by name in 'SUB: <in> FOR <out>'.
    -- 210 early 2019-20 games use a third form, one 'out' row per swap ("X replaced by Y")
    -- carrying the player in as subsInPersonId.
    case when t.etype = 'sub' and try_cast(nullif(t.subsInPersonId, '0') as bigint) is not null
              then 'swap'
         when t.etype = 'sub' and t.is_live then lower(t.sub)
         when t.etype = 'sub' then 'swap' end          as sub_dir,
    case when t.etype = 'sub' then try_cast(nullif(t.subsInPersonId, '0') as bigint) end
                                                       as sub_in_person_id,
    case when t.etype = 'sub' and not t.is_live
         then trim(regexp_extract(t.description, '^SUB: (.+) FOR (.+)$', 1)) end
                                                       as sub_in_name,
    case when t.etype = 'sub' and not t.is_live
         then trim(regexp_extract(t.description, '^SUB: (.+) FOR (.+)$', 2)) end
                                                       as sub_out_name,
    try_cast(nullif(t.assistPersonId, '0') as bigint)    as assist_person_id,
    try_cast(nullif(t.stealPersonId, '0') as bigint)     as steal_person_id,
    try_cast(nullif(t.blockPersonId, '0') as bigint)     as block_person_id,
    try_cast(nullif(t.foulDrawnPersonId, '0') as bigint) as foul_drawn_person_id,
    case when cv_is_team_id(t.possession) then try_cast(t.possession as bigint) end
                                                       as possession_team_id,
    t.description
from typed t
left join stg.player_games pg on pg.game_id = t.game_id and pg.person_id = t.player_id;
"""


def build(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(STAGING_SQL)
