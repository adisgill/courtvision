"""The daily run's scheduling rules, and the forecast's never-after-tip-off guard."""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pandas as pd
import pytest

from courtvision import daily
from courtvision.forecast import upcoming


@pytest.fixture
def con():
    con = duckdb.connect()
    for s in ("kaggle", "stg", "forecast"):
        con.execute(f"create schema {s}")
    con.execute("""create table kaggle.league_schedule_26_27 as select * from (values
        (1, timestamp '2026-11-13 19:00', 10, 20, 'Regular'),
        (2, timestamp '2026-11-13 22:30', 30, 40, 'Regular'),
        (3, timestamp '2026-11-15 19:30', 20, 10, 'Regular'),
        (4, timestamp '2026-11-16 19:00', 40, 30, 'Regular'),
        (5, timestamp '2026-11-14 12:00', 50, 60, 'Preseason')
        ) t(gameId, gameDateTimeEst, homeTeamId, awayTeamId, gameLabel)""")
    con.execute("create table stg.games as select 2026 season, date '2026-11-12' game_date, "
                "100 home_score")  # fmt: skip
    return con


def test_tips_and_plan(con):
    day = date(2026, 11, 13)
    t = daily.tips(con, day)
    assert t == [datetime(2026, 11, 13, 19, 0), datetime(2026, 11, 13, 22, 30)]
    assert daily.tips(con, date(2026, 11, 14)) == []  # preseason doesn't count
    p = daily.plan(day, t)
    assert p["forecast_at"] == datetime(2026, 11, 13, 18, 20)  # 40 minutes before the first
    assert p["build_by"] == datetime(2026, 11, 13, 20, 50)  # still time for the 10:30 game
    off = daily.plan(date(2026, 11, 14), [])
    assert off == {"forecast_at": None, "build_by": datetime(2026, 11, 14, 17, 15)}


def test_can_build_never_delays_the_first_forecast(con):
    p = daily.plan(date(2026, 11, 13), daily.tips(con, date(2026, 11, 13)))
    at = lambda h, m: datetime(2026, 11, 13, h, m)  # noqa: E731
    assert daily.can_build(at(13, 0), p, forecast_done=False)  # done by 14:00, long before
    assert not daily.can_build(at(17, 30), p, forecast_done=False)  # would run past 18:20
    assert daily.can_build(at(18, 30), p, forecast_done=True)  # after it: the later games
    assert not daily.can_build(at(21, 0), p, forecast_done=True)  # too late for 10:30
    off = daily.plan(date(2026, 11, 14), [])
    assert daily.can_build(datetime(2026, 11, 14, 17, 0), off, forecast_done=True)
    assert not daily.can_build(datetime(2026, 11, 14, 17, 20), off, forecast_done=True)


def test_kaggle_newer():
    t = datetime(2026, 11, 13, 16, 33)
    assert daily.kaggle_newer(t, datetime(2026, 11, 13, 6, 0))
    assert not daily.kaggle_newer(t, datetime(2026, 11, 13, 17, 0))
    assert daily.kaggle_newer(t, None) and not daily.kaggle_newer(None, None)


def test_unscored_days(con):
    ledger = pd.DataFrame({"game_id": ["a", "b", "c", "d"],
                           "game_date": pd.to_datetime(["2026-11-10", "2026-11-12",
                                                        "2026-11-12", "2026-11-13"])})  # fmt: skip
    con.register("lg", ledger)
    con.execute("create table forecast.ledger_games as select * from lg")
    today = date(2026, 11, 14)
    assert daily.unscored_days(con, today) == [date(2026, 11, 10), date(2026, 11, 12),
                                               date(2026, 11, 13)]  # fmt: skip
    con.execute(
        "create table forecast.scorecard_games as select * from (values "
        "(date '2026-11-10', 'a'), (date '2026-11-12', 'b')) t(day, game_id)"
    )
    # Nov 12 has one of two games scored (the other wasn't final): still to do
    assert daily.unscored_days(con, today) == [date(2026, 11, 12), date(2026, 11, 13)]


def test_forecast_skips_games_in_progress(con):
    before = upcoming(con, 2026, today=date(2026, 11, 13), now=datetime(2026, 11, 13, 18, 0))
    assert list(before.game_id) == ["0000000001", "0000000002"]
    # at 8 PM the 7 PM game is under way: its teams get their next game instead
    during = upcoming(con, 2026, today=date(2026, 11, 13), now=datetime(2026, 11, 13, 20, 0))
    assert list(during.game_id) == ["0000000002", "0000000003"]
