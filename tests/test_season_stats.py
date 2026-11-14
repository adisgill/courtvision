"""Season-by-season stats for the Team and Player pages, on a tiny in-memory database."""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from courtvision.app import season_stats as S


def _box(**kw) -> dict:
    row = {"b_pts": 0, "b_oreb": 0, "b_dreb": 0, "b_ast": 0, "b_stl": 0, "b_blk": 0,
           "b_tov": 0, "b_fgm": 0, "b_fga": 0, "b_fg3m": 0, "b_fg3a": 0, "b_ftm": 0,
           "b_fta": 0}  # fmt: skip
    return row | kw


@pytest.fixture
def con():
    con = duckdb.connect()
    for s in ("metrics", "stg"):
        con.execute(f"create schema {s}")
    players = pd.DataFrame([
        # star: season 1980 (no steals recorded that year) and 2026 (10 games in)
        {"season": 1980, "person_id": 1, "games": 80, "minutes": 3000.0}
        | _box(b_pts=2400, b_stl=0, b_fgm=900, b_fga=1800, b_fta=500, b_ftm=400),
        {"season": 2026, "person_id": 1, "games": 10, "minutes": 350.0}
        | _box(b_pts=300, b_stl=20, b_fgm=110, b_fga=200, b_fta=80, b_ftm=60),
        # 2026 regular (8 of the 10 games) scoring less; a 2-game player who doesn't qualify
        {"season": 2026, "person_id": 2, "games": 8, "minutes": 200.0}
        | _box(b_pts=80, b_stl=4, b_fgm=30, b_fga=80, b_fta=20, b_ftm=15),
        {"season": 2026, "person_id": 3, "games": 2, "minutes": 20.0}
        | _box(b_pts=90, b_stl=9, b_fgm=40, b_fga=50),
        {"season": 1980, "person_id": 2, "games": 70, "minutes": 2000.0}
        | _box(b_pts=1000, b_fgm=400, b_fga=900, b_fta=200, b_ftm=150),
    ]).assign(season_type="regular", team_id=0)  # fmt: skip
    con.register("p", players)
    con.execute("create table metrics.player_season_counts as select * from p")
    avail = pd.DataFrame([(s, st, 0.0 if (s, st) == (1980, "stl") else 1.0)
                          for s in (1980, 2026) for st in ("fga", "fta", "fg3a", "oreb", "dreb",
                                                            "ast", "stl", "blk", "tov")],
                         columns=["season", "stat", "share"])  # fmt: skip
    con.register("a", avail)
    con.execute("create table metrics.stat_availability as select * from a")
    teams = pd.DataFrame([
        {"season": 2026, "team_id": 10, "games": 2, "b_poss": 200.0, "o_b_poss": 200.0,
         "o_b_pts": 210} | _box(b_pts=230),
        {"season": 2026, "team_id": 20, "games": 2, "b_poss": 200.0, "o_b_poss": 200.0,
         "o_b_pts": 230} | _box(b_pts=210),
    ]).assign(season_type="regular")  # fmt: skip
    con.register("t", teams)
    con.execute("create table metrics.team_season_counts as select * from t")
    con.execute("""create table stg.games as select * from (values
        (2026, 'regular', 10, 20, 120, 100), (2026, 'regular', 20, 10, 110, 110 - 1)
        ) g(season, season_type, home_team_id, away_team_id, home_score, away_score)""")
    return con


def test_player_seasons_per_game_and_league_comparison(con):
    me, lg = S.player_seasons(con, 1, "regular")
    assert list(me.season) == [2026, 1980]  # newest first
    pg = S._scale(me, "Per game")
    assert pg.pts.tolist() == [30.0, 30.0] and pg.stl.iloc[0] == 2.0
    assert pd.isna(me.stl.iloc[1])  # steals not recorded in 1980: blank, not zero
    assert me.fg_pct.iloc[0] == pytest.approx(55.0)  # percent, not a fraction
    # 2026: players with a quarter of the most games (10) qualify; the 2-game player doesn't
    assert set(lg[lg.season == 2026].person_id) == {1, 2}
    pct = S._percentile(S._scale(lg, "Per game"), pg, ["pts"])
    assert pct.pts.tolist() == [0.5, 0.5]  # beats the other qualified player, not himself


def test_team_seasons_record_and_ratings(con):
    t = S.team_seasons(con, "regular").set_index("team_id")
    assert (t.loc[10, "wins"], t.loc[10, "losses"]) == (1, 1)
    assert t.loc[10, "pts"] == 115.0 and t.loc[10, "opp"] == 105.0
    assert t.loc[10, "ortg"] == pytest.approx(115.0) and t.loc[10, "net"] == pytest.approx(10.0)
    assert t.loc[10, "pace"] == pytest.approx(100.0)
