import duckdb
import pytest

from courtvision.db import MACROS
from courtvision.ingest.kaggle_load import load, table_name


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Games.csv", "games"),
        ("PlayByPlay.parquet", "play_by_play"),
        ("PlayerStatisticsExtended.csv", "player_statistics_extended"),
        ("LeagueSchedule24_25.csv", "league_schedule_24_25"),
        ("TeamHistories.csv", "team_histories"),
    ],
)
def test_table_name(filename, expected):
    assert table_name(filename) == expected


def test_load_csv_roundtrip(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "Games.csv").write_text("gameId,homeScore\n22500001,110\n42500151,99\n")
    con = duckdb.connect()
    con.execute(MACROS)
    assert load(con, raw, tmp_path / "pq") == ["kaggle.games"]
    rows = con.execute(
        "select cv_game_id(gameId), cv_season(gameId), cv_season_type(gameId) "
        "from kaggle.games order by 1"
    ).fetchall()
    assert rows == [("0022500001", 2025, "regular"), ("0042500151", 2025, "playoffs")]


def test_season_macro_handles_both_centuries():
    con = duckdb.connect()
    con.execute(MACROS)
    got = con.execute(
        "select cv_season(24600001), cv_season('0029900001'), cv_season(20000001), "
        "cv_season_label(1999), cv_season_label(2025)"
    ).fetchall()[0]
    assert got == (1946, 1999, 2000, "1999-00", "2025-26")
