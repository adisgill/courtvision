"""The prediction ledger and scorecard (spec section 21), on a tiny in-memory database."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pandas as pd
import pytest

from courtvision.scorecard import log_predictions, play_rate, score


def _db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for s in ("stg", "metrics", "forecast"):
        con.execute(f"create schema {s}")
    games = pd.DataFrame({
        "game_id": ["g1", "g2", "g0"], "season": 2026,
        "game_date": pd.to_datetime(["2026-11-12", "2026-11-12", "2026-11-10"]),
        "home_score": [110, 100, 99], "away_score": [100, 104, 98],
    })  # fmt: skip
    pgc = pd.DataFrame({
        "game_id": ["g1", "g1", "g0"], "person_id": [1, 2, 1], "team_id": [10, 10, 10],
        "minutes": [30.0, 0.0, 30.0], "b_pts": [25, 0, 15], "b_oreb": 1, "b_dreb": 4,
        "b_ast": 3, "b_fg3m": 2, "b_stl": 1, "b_blk": 0, "b_tov": 2, "season": 2026,
        "season_type": "regular",
    })  # fmt: skip
    con.register("g", games)
    con.execute("create table stg.games as select * from g")
    con.register("p", pgc)
    con.execute("create table metrics.player_game_counts as select * from p")
    con.execute("create table forecast.margin_model as select 'm' as model, 13.9 as sigma")
    fg = pd.DataFrame({
        "game_id": ["g1", "g2"], "game_date": pd.to_datetime(["2026-11-12"] * 2),
        "tipoff": pd.to_datetime(["2026-11-12 19:00", "2026-11-12 21:00"]),
        "home_team_id": [10, 30], "away_team_id": [20, 40],
        "pred_margin": [6.0, 3.0], "home_win_prob": [0.66, 0.58],
    })  # fmt: skip
    lines = pd.DataFrame({"game_id": ["g1", "g1"], "team_id": [10, 10], "person_id": [1, 2],
                          "exp_minutes": [30.0, 12.0]})  # fmt: skip
    # (predicted, low, high) for player 1 and player 2
    # player 1's rebound range 1.6-4.6 covers 2 to 4; he gets 5, outside (rounding the range
    # outward to 2-5 would wrongly count it)
    for s, (one, two) in {"pts": ((20, 12, 28), (6, 2, 10)), "reb": ((4, 1.6, 4.6), (2, 0, 4)),
                          "ast": ((3, 1, 5), (1, 0, 2)), "fg3m": ((2, 0, 4), (0, 0, 1)),
                          "stocks": ((1, 0, 2), (0, 0, 1)),
                          "tov": ((2, 0, 4), (1, 0, 2))}.items():  # fmt: skip
        lines[s] = [one[0], two[0]]
        lines[f"{s}_lo"] = [one[1], two[1]]
        lines[f"{s}_hi"] = [one[2], two[2]]
    for name, df in (("games", fg), ("player_lines", lines)):
        con.register("x", df)
        con.execute(f"create table forecast.{name} as select * from x")
        con.unregister("x")
    return con


def test_ledger_appends_and_never_rewrites():
    con = _db()
    log_predictions(con, datetime(2026, 11, 12, 18, 0))
    con.execute("update forecast.games set pred_margin = 99")  # a later run changes its mind
    log_predictions(con, datetime(2026, 11, 12, 18, 30))
    led = con.execute("select predicted_at, pred_margin from forecast.ledger_games "
                      "where game_id = 'g1' order by 1").df()  # fmt: skip
    assert led.pred_margin.tolist() == [6.0, 99.0]  # both kept, as made


def test_scorecard_games_and_players():
    con = _db()
    log_predictions(con, datetime(2026, 11, 12, 18, 0))
    res = score(con, "2026-11-12", source="db")
    g = con.execute("select * from forecast.scorecard_games order by game_id").df()
    assert res["winners"] == "1/2"  # g1: home by 6 predicted, won by 10; g2: home lost
    assert g.actual_margin.tolist() == [10.0, -4.0]
    assert g.error.tolist() == [-4.0, 7.0]
    assert g.brier.tolist() == pytest.approx([(0.66 - 1) ** 2, 0.58**2])
    assert not g.after_tipoff.any()  # predicted at 6 pm, before both tip-offs
    p = con.execute("select * from forecast.scorecard_players").df()
    pts = p[p.stat == "pts"].set_index("person_id")
    assert bool(pts.loc[1, "played"]) and pts.loc[1, "actual"] == 25 and pts.loc[1, "inside"]
    assert not bool(pts.loc[2, "played"])  # expected, didn't play: not scored as a miss
    assert pts.loc[1, "season_avg"] == 15  # his average before the day
    reb = p[(p.stat == "reb") & (p.person_id == 1)].iloc[0]
    assert reb.actual == 5 and not reb.inside  # exact bounds, as calibrated
    assert res["inside_80"] == pytest.approx(p[p.played].inside.mean())


def test_ledger_takes_new_columns_without_rewriting():
    con = _db()
    log_predictions(con, datetime(2026, 11, 12, 18, 0))
    con.execute("alter table forecast.games add column range_sigma double default 3.0")
    log_predictions(con, datetime(2026, 11, 12, 18, 30))
    led = con.execute("select predicted_at, range_sigma, pred_margin from forecast.ledger_games "
                      "where game_id = 'g1' order by 1").df()  # fmt: skip
    assert pd.isna(led.range_sigma.iloc[0]) and led.range_sigma.iloc[1] == 3.0
    assert led.pred_margin.tolist() == [6.0, 6.0]  # the earlier row as it was


def test_scorecard_uses_each_predictions_own_range():
    con = _db()
    con.execute("alter table forecast.games add column range_sigma double default 3.0")
    log_predictions(con, datetime(2026, 11, 12, 18, 0))
    score(con, "2026-11-12", source="db")
    g = con.execute("select game_id, margin_inside_80, range_sigma from forecast.scorecard_games "
                    "order by game_id").df()  # fmt: skip
    # g1 missed by 4: inside +-1.28 x 13.9, outside +-1.28 x 3 (3.8)
    assert g.range_sigma.tolist() == [3.0, 3.0] and not g.margin_inside_80.any()
    score(con, "2026-11-12", source="db")  # rescoring replaces the day's rows
    assert con.execute("select count(*) from forecast.scorecard_games").fetchall()[0][0] == 2


def test_play_rate_starts_even_and_learns_from_the_scorecard():
    con = _db()
    assert play_rate(con) == {"rate": 0.5, "listed": 0, "played": 0}
    log_predictions(con, datetime(2026, 11, 12, 18, 0))
    av = pd.DataFrame({"game_id": ["g1", "g1"], "person_id": [1.0, 2.0],
                       "status": ["Questionable", "Questionable"],
                       "predicted_at": pd.Timestamp("2026-11-12 18:00")})  # fmt: skip
    con.register("a", av)
    con.execute("create table forecast.ledger_availability as select * from a")
    score(con, "2026-11-12", source="db")
    q = play_rate(con)  # player 1 played, player 2 didn't: 1 of 2, pulled toward even
    assert q["listed"] == 2 and q["played"] == 1 and q["rate"] == pytest.approx(11 / 22)
