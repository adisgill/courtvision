"""The demo database (`courtvision demo`) and the app's download of it, on tiny files."""

from __future__ import annotations

import gzip
import os
from pathlib import Path

import duckdb
import pytest

from courtvision import demo
from courtvision.app import data as D


def _source(path: Path) -> None:
    con = duckdb.connect(str(path))
    for s in ("metrics", "stg", "coach"):
        con.execute(f"create schema {s}")
    con.execute("""create table metrics.ratings_long as select * from (values
        ('player_season', 1990, 'shooting', 'B', 1, 55.0),   -- old season, best tier: kept
        ('player_season', 1990, 'shooting', 'C', 1, 52.0),   -- old season, other tier: out
        ('player_game', 1990, 'shooting', 'B', 1, 60.0),     -- old game: out
        ('player_game', 2026, 'shooting', 'A', 1, 61.0),     -- recent game, any tier: kept
        ('player_game', 2026, 'shooting', 'B', 1, 58.0)
        ) t(level, season, measure, tier, person_id, rating)""")
    con.execute("""create table metrics.best_tier as select * from (values
        ('player_season', 1990, 'shooting', 'B')) t(level, season, measure, tier)""")
    con.execute(
        "create table metrics.team_season as select * from (values (1990), (2025), "
        "(2026)) t(season)"
    )
    con.execute("create table metrics.values_long as select 1 as x")  # pipeline only: left out
    con.execute("create table coach.current as select 1 as team_id")  # small: copied whole
    con.execute(
        "create view stg.games as select * from (values ('g1', 1990, date "
        "'1991-01-01', 100), ('g2', 2026, date '2026-11-12', 101)) "
        "t(game_id, season, game_date, home_score)"
    )
    con.close()


def test_build_keeps_recent_detail_and_season_history(tmp_path, monkeypatch):
    src, dest = tmp_path / "full.duckdb", tmp_path / "demo" / "courtvision.duckdb"
    _source(src)
    monkeypatch.setattr(demo, "BIG", 0)  # treat every table as big enough to filter
    res = demo.build(src, dest, since=2025)
    con = duckdb.connect(str(dest), read_only=True)
    r = con.execute("select level, season, tier from metrics.ratings_long order by all").fetchall()
    assert r == [("player_game", 2026, "A"), ("player_game", 2026, "B"),
                 ("player_season", 1990, "B")]  # fmt: skip
    assert [x[0] for x in con.execute("select season from metrics.team_season order by 1")
            .fetchall()] == [2025, 2026]  # fmt: skip
    assert con.execute("select count(*) from stg.games").fetchall()[0][0] == 2  # whole
    tables = {x[0] for x in con.execute("select schema_name || '.' || table_name "
                                        "from duckdb_tables()").fetchall()}  # fmt: skip
    assert "metrics.values_long" not in tables and "coach.current" in tables
    info = con.execute("select since, first_season, data_through from demo.info").fetchall()
    assert info[0][:2] == (2025, 1990) and str(info[0][2]) == "2026-11-12"
    assert res["tables"] == len(tables) - 1  # every table but demo.info


def test_app_downloads_the_demo_once_and_again_when_republished(tmp_path, monkeypatch):
    payload = tmp_path / "pub" / "courtvision.duckdb.gz"
    payload.parent.mkdir()
    payload.write_bytes(gzip.compress(b"first"))
    monkeypatch.setenv("COURTVISION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COURTVISION_DEMO_URL", payload.as_uri())
    p = D.db_path()
    assert p == tmp_path / "data" / "demo" / "courtvision.duckdb" and p.read_bytes() == b"first"
    p.write_bytes(b"local")  # unchanged upstream: the local copy is kept
    assert D.db_path().read_bytes() == b"local"
    # what the running app checks every 10 minutes: the published copy vs the one it has
    assert D.published_version() == D.loaded_version() is not None
    payload.write_bytes(gzip.compress(b"second"))
    os.utime(payload, (2_000_000_000, 2_000_000_000))  # a newer publication
    assert D.published_version() not in (None, D.loaded_version())
    assert D.db_path().read_bytes() == b"second"
    assert D.published_version() == D.loaded_version()


def test_app_prefers_the_full_database(tmp_path, monkeypatch):
    monkeypatch.setenv("COURTVISION_DATA_DIR", str(tmp_path))
    (tmp_path / "courtvision.duckdb").write_bytes(b"")
    monkeypatch.setenv("COURTVISION_DEMO_URL", "file:///nonexistent.gz")
    assert D.db_path() == tmp_path / "courtvision.duckdb"
    assert D.published_version() is None  # nothing to check for


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    monkeypatch.setattr("courtvision.config.load_env", lambda: None)
