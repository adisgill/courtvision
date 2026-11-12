"""The metric build: ``courtvision metrics build``.

Stages, each reading only what earlier stages wrote:

1. ``pbp``: materialize ``stg.events`` and game rosters to Parquet (one pass over the
   play-by-play), then parse every season in parallel into possessions, rebound kinds and
   stints (:mod:`courtvision.metrics.pbp`).
2. ``sql``: league baselines, counts, components and ratings, as DuckDB tables in schema
   ``metrics`` (:mod:`courtvision.metrics.sql`), plus small-sample shrinkage constants
   (:mod:`courtvision.metrics.shrinkage`).
3. ``export``: every output table to ``data/parquet/metrics/<table>.parquet``.
"""

from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import duckdb

from courtvision.db import sql_glob, sql_path
from courtvision.metrics import pbp

log = logging.getLogger(__name__)

# Columns materialized for each event: everything the parser reads plus shot details.
_EVENT_INPUT_COLUMNS = (
    pbp.EVENT_SQL_COLUMNS + ", season, season_type, shot_value, x_legacy, y_legacy"
)


def _rated_types_sql() -> str:
    return ", ".join(f"'{t}'" for t in pbp.RATED_SEASON_TYPES)


def pbp_seasons(con: duckdb.DuckDBPyConnection) -> list[int]:
    """Seasons with play-by-play for rated games."""
    rows = con.execute(
        f"select distinct season from stg.pbp where season_type in ({_rated_types_sql()}) "
        "order by 1"
    ).fetchall()
    return [r[0] for r in rows]


def materialize_pbp_inputs(
    con: duckdb.DuckDBPyConnection, inputs_dir: Path, seasons: list[int]
) -> None:
    """Write ``stg.events`` and rosters for ``seasons`` to ``inputs_dir/<table>/season=N/``."""
    in_list = ", ".join(str(s) for s in seasons)
    queries = {
        "events": f"select {_EVENT_INPUT_COLUMNS} from stg.events "
        f"where season_type in ({_rated_types_sql()}) and season in ({in_list})",
        "roster": f"""select pg.game_id, pg.person_id, pg.team_id, pg.minutes,
                             pl.last_name, pl.first_name, pg.season
                      from stg.player_games pg left join stg.players pl using (person_id)
                      where pg.season_type in ({_rated_types_sql()}) and pg.season in ({in_list})
                        and pg.team_id is not null""",
    }
    for name, query in queries.items():
        for s in seasons:
            shutil.rmtree(inputs_dir / name / f"season={s}", ignore_errors=True)
        (inputs_dir / name).mkdir(parents=True, exist_ok=True)
        con.execute(
            f"copy ({query}) to {sql_path(inputs_dir / name)} "
            "(format parquet, partition_by (season), overwrite_or_ignore, compression zstd)"
        )


def parse_pbp(inputs_dir: Path, out_dir: Path, seasons: list[int], workers: int | None = None):
    """Parse seasons in parallel; returns {season: (counts, games, games_ok)}."""
    workers = workers or max(1, min(len(seasons), (os.cpu_count() or 2) // 2, 8))
    results = {}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(pbp.parse_season_files, inputs_dir, out_dir, s) for s in seasons]
        for fut in as_completed(futures):
            season, counts, games, ok = fut.result()
            log.info("pbp %s: %d games, %d pass lineup QA, %s", season, games, ok, counts)
            results[season] = (counts, games, ok)
    return dict(sorted(results.items()))


PBP_VIEWS = {
    "pbp_events": "events",
    "possessions": "possessions",
    "stints": "stints",
    "lineup_qa": "qa",
}


def register_pbp_views(con: duckdb.DuckDBPyConnection, inputs_dir: Path, out_dir: Path) -> None:
    con.execute("create schema if not exists metrics")
    con.execute(
        "create or replace view metrics.event_inputs as select * from read_parquet("
        f"{sql_glob(inputs_dir / 'events', '*/*.parquet')}, hive_partitioning = true)"
    )
    for view, folder in PBP_VIEWS.items():
        con.execute(
            f"create or replace view metrics.{view} as select * from read_parquet("
            f"{sql_glob(out_dir / folder, '*/*.parquet')}, hive_partitioning = true)"
        )


def run_pbp_stage(
    con: duckdb.DuckDBPyConnection,
    work_dir: Path,
    seasons: list[int] | None = None,
    workers: int | None = None,
) -> dict:
    seasons = seasons or pbp_seasons(con)
    inputs_dir, out_dir = work_dir / "inputs", work_dir / "pbp"
    log.info("materializing play-by-play inputs for %d seasons", len(seasons))
    materialize_pbp_inputs(con, inputs_dir, seasons)
    results = parse_pbp(inputs_dir, out_dir, seasons, workers)
    register_pbp_views(con, inputs_dir, out_dir)
    return results


# ---- SQL stage ------------------------------------------------------------------------------

OUTPUT_TABLES = (
    "league_baselines", "zone_baselines", "stat_first_season", "shrinkage_constants",
    "reference_stats", "team_game", "team_season", "player_game", "player_season",
    "presence_season", "presence_rolling", "lineup_qa_summary", "build_info",
    "team_game_counts", "player_game_counts", "ratings_long",
)  # fmt: skip


def _run(con: duckdb.DuckDBPyConnection, name: str, sql: str) -> None:
    import time

    t = time.time()
    con.execute(sql)
    log.info("  %-24s %5.1fs", name, time.time() - t)


def run_sql_stage(con: duckdb.DuckDBPyConnection, skip_base: bool = False) -> dict:
    from courtvision.metrics import measures, rate, shrinkage
    from courtvision.metrics import sql as msql

    log.info("counts and league baselines")
    if not skip_base:
        for name, stmt in msql.base_sql():
            _run(con, name, stmt)
    starts = measures.tier_starts(con)
    log.info("tier start seasons: %s", starts)
    for name, stmt in rate.season_counts_sql():
        _run(con, name, stmt)
    for name, stmt in rate.values_sql(starts):
        _run(con, name, stmt)
    for name, stmt in rate.presence_sql():
        _run(con, name, stmt)
    log.info("shrinkage constants (split-half)")
    shrinkage.estimate(con, starts)
    for name, stmt in rate.ratings_sql():
        _run(con, name, stmt)
    for name, stmt in rate.wide_sql():
        _run(con, name, stmt)
    run_outputs(con)
    return starts


def run_outputs(con: duckdb.DuckDBPyConnection) -> None:
    """Presence and QA summary tables built from the ratings."""
    from courtvision.metrics import rate

    _run(
        con,
        "presence_season",
        """
create or replace table metrics.presence_season as
with r as (
    select * from metrics.ratings_long where level = 'player_season' and family = 'presence'
)
select p.*, b.presence_net_box, c.games, c.sec_on / 60 as minutes_on
from (
    pivot r on measure
    using first(value) as value, first(value_adj) as value_adj, first(rating) as rating,
          first(low_sample) as low_sample
    group by season, season_type, person_id, team_id
) p
left join metrics.presence_box_season b using (season, season_type, person_id, team_id)
left join metrics.presence_season_counts c using (season, season_type, person_id, team_id);
""",
    )
    _run(con, "presence_rolling", rate.presence_rolling_sql())
    _run(
        con,
        "lineup_qa_summary",
        """
create or replace table metrics.lineup_qa_summary as
select q.season, count(*) as games, avg(q.ok::int) as lineups_ok,
       avg(g.pbp_ok::int) as pbp_complete
from metrics.lineup_qa q left join metrics.game_pbp_qa g using (game_id)
group by q.season order by q.season;
""",
    )


def write_build_info(con: duckdb.DuckDBPyConnection, kaggle_raw: Path, starts: dict) -> None:
    import json
    import subprocess
    from datetime import UTC, datetime

    from courtvision import __version__
    from courtvision.config import REPO_ROOT

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            ).stdout.strip()
        )  # fmt: skip
    except OSError:
        commit, dirty = None, None
    manifest = kaggle_raw / "_manifest.json"
    kaggle = json.loads(manifest.read_text()) if manifest.exists() else {}
    info = {
        "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "courtvision_version": __version__,
        "git_commit": commit,
        "git_dirty": dirty,
        "kaggle_downloaded_at_utc": kaggle.get("downloaded_at_utc"),
        "kaggle_version": kaggle.get("requested_version"),
        "spec": "docs/metrics_spec.md v1.0",
        "tier_starts": json.dumps(starts),
    }
    cols = ", ".join(f"? as {k}" for k in info)
    con.execute(f"create or replace table metrics.build_info as select {cols}", list(info.values()))


def export(con: duckdb.DuckDBPyConnection, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for t in OUTPUT_TABLES:
        con.execute(
            f"copy metrics.{t} to {sql_path(dest / (t + '.parquet'))} "
            "(format parquet, compression zstd)"
        )


def build_all(
    con: duckdb.DuckDBPyConnection,
    data_dir: Path,
    kaggle_raw: Path,
    seasons: list[int] | None = None,
    workers: int | None = None,
    skip_pbp: bool = False,
) -> dict:
    work = data_dir / "work"
    if not skip_pbp:
        run_pbp_stage(con, work, seasons, workers)
    else:
        register_pbp_views(con, work / "inputs", work / "pbp")
    starts = run_sql_stage(con)
    write_build_info(con, kaggle_raw, starts)
    export(con, data_dir / "parquet" / "metrics")
    return starts
