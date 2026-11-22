"""The web app's export (`courtvision site`): content-addressed Parquet files and a manifest."""

from __future__ import annotations

import json

import duckdb

from courtvision import site
from courtvision.db import MACROS


def _demo(path) -> None:
    con = duckdb.connect(str(path))
    con.execute(MACROS)
    for s in ("kaggle", "stg", "demo"):
        con.execute(f"create schema {s}")
    con.execute("""create table kaggle.team_statistics as select * from (values
        (22600001, 1610612759, 'San Antonio', 'Spurs'), (22600001, 1610612738, 'Boston', 'Celtics'),
        (22500001, 1610612759, 'San Antonio', 'Spurs')) t(gameId, teamId, teamCity, teamName)""")
    con.execute("""create table kaggle.team_histories as select * from (values
        (1610612759, 'San Antonio', 'Spurs', 'SAN  ', 1976, 2100, 'NBA'),
        (1610612738, 'Boston', 'Celtics', 'BOS', 1946, 2100, 'NBA'))
        t(teamId, teamCity, teamName, teamAbbrev, seasonFounded, seasonActiveTill, league)""")
    con.execute("""create table stg.players as select * from (values
        (1, 'Victor', 'Wembanyama', 2023, 2026))
        t(person_id, first_name, last_name, from_year, to_year)""")
    con.execute("""create table stg.shots as select * from (values
        (2026, 'regular', 0, 10, true, 2), (2026, 'regular', 0, 260, false, 3),
        (2026, 'regular', 230, 40, true, 3))
        t(season, season_type, x_legacy, y_legacy, made, shot_value)""")
    con.execute("create table demo.info as select date '2026-11-20' as data_through")
    con.close()


def test_export_writes_hashed_files_and_a_manifest(tmp_path):
    db = tmp_path / "courtvision.duckdb"
    _demo(db)
    out = tmp_path / "data"
    res = site.build(db, out)
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert res["version"] == m["version"] and m["data_through"] == "2026-11-20"
    assert "cv_season" in m["macros"]  # the app installs the pipeline's macros
    for name in ("stg.shots", "web.team_names", "web.player_names", "web.league_hex"):
        f = out / m["files"][name]
        assert f.exists() and f.name.startswith(name + ".") and f.suffix == ".parquet"
    teams = (out / m["files"]["web.team_names"]).as_posix()
    names = duckdb.sql(f"select season, abbrev from read_parquet('{teams}') "
                       "where team_id = 1610612759 order by 1").fetchall()  # fmt: skip
    assert names == [(2025, "SAS"), (2026, "SAS")]  # padding trimmed, SAN written as SAS
    # unchanged data: identical bytes, so the same file names and version
    assert site.build(db, out)["version"] == res["version"]
