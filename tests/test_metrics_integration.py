"""Acceptance tests T4-T6 (docs/metrics_spec.md section 8) against the built database.

Skipped unless ``courtvision metrics build`` has been run. T6 recomputes three 2025-26
games by hand from nba_api's official box scores and shot zones, a source independent of
the Kaggle play-by-play the pipeline uses.
"""

from __future__ import annotations

import math

import pytest

from courtvision.config import get_paths

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def con():
    import duckdb

    path = get_paths().duckdb
    if not path.exists():
        pytest.skip("no database; run `courtvision metrics build`")
    c = duckdb.connect(str(path), read_only=True)
    try:
        c.execute("select 1 from metrics.team_game limit 1")
    except duckdb.Error:
        pytest.skip("metrics not built; run `courtvision metrics build`")
    yield c
    c.close()


def one(con, sql, params=None):
    return con.execute(sql, params or []).fetchall()[0]


# ---- T4: roll-up identities -----------------------------------------------------------------


def test_t4_team_season_counts_are_sums_of_games(con):
    diff = one(
        con,
        """select max(abs(s.a_fga_r - g.a_fga_r)) + max(abs(s.o_poss - g.o_poss))
                  + max(abs(s.b_plays - g.b_plays))
           from metrics.team_season_counts s
           join (select season, season_type, team_id, sum(a_fga_r) a_fga_r, sum(o_poss) o_poss,
                        sum(b_plays) b_plays
                 from metrics.team_game_counts group by all) g
             using (season, season_type, team_id)""",
    )[0]
    assert diff < 1e-9


def test_t4_team_shots_are_sums_of_player_shots(con):
    # Actor-credited roll-up: a team's rated shots, points and expected points are its
    # players' sums (complete-play-by-play games).
    diff = one(
        con,
        """select max(abs(t.a_fga_r - p.a_fga_r)), max(abs(t.a_xp_r - p.a_xp_r)),
                  max(abs(t.a_pts_r - p.a_pts_r))
           from metrics.team_game_counts t
           join (select game_id, team_id, sum(a_fga_r) a_fga_r, sum(a_xp_r) a_xp_r,
                        sum(a_pts_r) a_pts_r
                 from metrics.player_game_counts group by all) p using (game_id, team_id)
           where t.pbp_ok and t.season = 2025""",
    )
    assert max(diff) < 1e-6


def test_t4_on_court_counts_are_five_times_team(con):
    diff = one(
        con,
        """select max(abs(p.o_poss - 5 * t.o_poss)), max(abs(p.o_xp_r - 5 * t.o_xp_r)),
                  max(abs(p.dreb - 5 * t.dreb))
           from metrics.team_game_counts t
           join (select game_id, team_id, sum(o_poss) o_poss, sum(o_xp_r) o_xp_r,
                        sum(dreb) dreb
                 from metrics.player_game_counts where o_poss is not null group by all) p
             using (game_id, team_id)
           where t.pbp_ok and t.lineups_ok""",
    )
    assert max(diff) < 1e-6


def test_t4_team_defense_is_exposure_weighted_mean_of_players(con):
    rows = con.execute(
        """with p as (
               select game_id, team_id, sum(value * exposure) / sum(exposure) as wmean
               from metrics.ratings_long
               where level = 'player_game' and measure = 'defense' and tier = 'A'
               group by all)
           select t.value, p.wmean
           from metrics.ratings_long t join p using (game_id, team_id)
           join metrics.team_game_counts c using (game_id, team_id)
           where t.level = 'team_game' and t.measure = 'defense' and t.tier = 'A'
             and c.lineups_ok and c.season = 2025
             -- a player on the floor for opponent shots but for no completed opponent
             -- possession has no defined rate (0 exposure); the identity is on counts
             -- (previous test), so such team-games are left out here (1 in ~2,600)
             and not exists (
                 select 1 from metrics.player_game_counts pc
                 where pc.game_id = t.game_id and pc.team_id = t.team_id
                   and pc.o_poss = 0 and pc.o_fga + pc.o_fta + pc.o_tov > 0)"""
    ).fetchall()
    assert len(rows) > 2500
    assert max(abs(a - b) for a, b in rows) < 1e-9


def test_t4_backcourt_players_sum_to_team(con):
    # Player Backcourt values are shares of the team's: value x exposure is 100 x the
    # points numerator at both levels, and the players' numerators sum to the team's.
    rows = con.execute(
        """with p as (
               select game_id, team_id, sum(value * exposure) as num
               from metrics.ratings_long
               where level = 'player_game' and measure = 'backcourt_sh' and tier = 'A'
               group by all)
           select t.value * t.exposure, p.num
           from metrics.ratings_long t join p using (game_id, team_id)
           join metrics.team_game_counts c using (game_id, team_id)
           where t.level = 'team_game' and t.measure = 'backcourt_sh' and t.tier = 'A'
             and c.lineups_ok and c.season = 2025
           limit 2000"""
    ).fetchall()
    assert rows
    assert max(abs(a - b) for a, b in rows) < 1e-6


# ---- T5: scale -----------------------------------------------------------------------------


def test_t5_team_season_ratings_center_on_50(con):
    rows = con.execute(
        """select season, tier, measure, avg(rating) m
           from metrics.ratings_long
           where level = 'team_season' and season_type = 'regular' and rating is not null
           group by all having abs(avg(rating) - 50) > 0.5"""
    ).fetchall()
    assert rows == []


# ---- T6: hand-checked games ----------------------------------------------------------------

ZONE = {
    "Restricted Area": "RA",
    "In The Paint (Non-RA)": "PAINT",
    "Mid-Range": "MID",
    "Left Corner 3": "C3",
    "Right Corner 3": "C3",
    "Above the Break 3": "ATB3",
}
VALUE = {"RA": 2, "PAINT": 2, "MID": 2, "C3": 3, "ATB3": 3}
GAMES = ["0022500001", "0022500600", "0022501200"]


@pytest.fixture(scope="module")
def league_zones(con):
    """League zone make rates for 2025-26 from the official shot charts."""
    rows = con.execute(
        """select SHOT_ZONE_BASIC, count(*), sum(SHOT_MADE_FLAG)
           from nba_api.shots where season = 2025 and season_type = 'regular'
           group by 1"""
    ).fetchall()
    fga, fgm = {}, {}
    for zone_basic, n, made in rows:
        z = ZONE.get(zone_basic)
        if z:
            fga[z] = fga.get(z, 0) + n
            fgm[z] = fgm.get(z, 0) + made
    p = {z: fgm[z] / fga[z] for z in fga}
    lg_pps = sum(VALUE[z] * fgm[z] for z in fga) / sum(fga.values())
    return p, lg_pps


@pytest.mark.parametrize("game_id", GAMES)
def test_t6_shooting_by_hand(con, league_zones, game_id):
    p, lg_pps = league_zones
    shots = con.execute(
        "select team_id, SHOT_ZONE_BASIC, SHOT_MADE_FLAG from nba_api.shots where game_id = ?",
        [game_id],
    ).fetchall()
    assert shots, "game not in the nba_api pull"
    for team in {t for t, _, _ in shots}:
        rated = [(ZONE[z], m) for t, z, m in shots if t == team and z in ZONE]
        n = len(rated)
        xp = sum(VALUE[z] * p[z] for z, _ in rated)
        pts = sum(VALUE[z] * m for z, m in rated)
        sq = 100 * (xp / n - lg_pps)
        sm = 100 * (pts - xp) / n
        got = dict(
            con.execute(
                """select measure, value from metrics.ratings_long
                   where level = 'team_game' and tier = 'A' and game_id = ? and team_id = ?
                     and measure in ('shooting', 'shooting_sq', 'shooting_sm')""",
                [game_id, team],
            ).fetchall()
        )
        # Kaggle play-by-play zones vs official zones differ on <0.01% of shots, and
        # league rates are computed from slightly different shot sets: allow 0.3 pts.
        assert math.isclose(got["shooting_sq"], sq, abs_tol=0.3), (team, got, sq)
        assert math.isclose(got["shooting_sm"], sm, abs_tol=0.3), (team, got, sm)
        assert math.isclose(got["shooting"], sq + sm, abs_tol=0.3)


@pytest.mark.parametrize("game_id", GAMES)
def test_t6_ball_control_turnovers_by_hand(con, game_id):
    lg_tovp, lg_ppnt_b = one(
        con, "select lg_tovp, lg_ppnt_b from metrics.league_baselines where season = 2025"
    )
    rows = con.execute(
        "select team_id, FGA, FTA, TOV from nba_api.game_log where game_id = ?", [game_id]
    ).fetchall()
    assert len(rows) == 2
    for team, fga, fta, tov in rows:
        plays = fga + 0.4 * fta + tov
        bt = 100 * (lg_tovp - tov / plays) * lg_ppnt_b
        got = one(
            con,
            """select value from metrics.ratings_long where level = 'team_game'
               and tier = 'A' and measure = 'ball_bt' and game_id = ? and team_id = ?""",
            [game_id, team],
        )[0]
        assert math.isclose(got, bt, abs_tol=1e-6), (team, got, bt)


@pytest.mark.parametrize("game_id", GAMES)
def test_t6_defense_turnovers_and_fouls_by_hand(con, game_id):
    # Opponent turnovers and free-throw rate against official box scores and the
    # official possession count (Kaggle team_statistics_extended).
    lg = one(
        con,
        """select lg_tovr, lg_ppnt, lg_ftr, lg_ftpct from metrics.league_baselines
           where season = 2025""",
    )
    lg_tovr, lg_ppnt, lg_ftr, lg_ftpct = lg
    rows = con.execute(
        """select g.team_id, o.FGA, o.FTA, o.TOV, x.possessions
           from nba_api.game_log g
           join nba_api.game_log o on o.game_id = g.game_id and o.team_id <> g.team_id
           join kaggle.team_statistics_extended x
             on lpad(x.gameId::varchar, 10, '0') = g.game_id and x.teamId = o.team_id
           where g.game_id = ?""",
        [game_id],
    ).fetchall()
    for team, ofga, ofta, otov, oposs in rows:
        dt = 100 * (otov / oposs - lg_tovr) * lg_ppnt
        df = 100 * (lg_ftr * ofga - ofta) / oposs * lg_ftpct
        got = dict(
            con.execute(
                """select measure, value from metrics.ratings_long where level = 'team_game'
                   and tier = 'A' and game_id = ? and team_id = ?
                   and measure in ('defense_dt', 'defense_df')""",
                [game_id, team],
            ).fetchall()
        )
        # counted vs official possessions differ by ~0.2 on average; turnovers in the
        # play-by-play can differ from the box by one (team turnovers)
        assert math.isclose(got["defense_dt"], dt, abs_tol=2.0), (team, got, dt)
        assert math.isclose(got["defense_df"], df, abs_tol=1.0), (team, got, df)
