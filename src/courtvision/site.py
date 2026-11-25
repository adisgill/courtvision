"""``courtvision site``: the data the web app (``app/``: React + DuckDB-WASM) reads, and its deploy.

Exports every table of the demo database (``courtvision demo``: play-level and game-level rows
for recent seasons, season-level history whole) as
``app/public/data/<schema>.<table>.<hash8>.parquet`` (immutable, content-addressed: a new
version never overwrites an old one, so a page still showing the old version keeps working)
and ``manifest.json`` with the version, build time, data-through date, each table's file and
row count, and the pipeline's SQL macros (``courtvision.db.MACROS``), which the app installs
so its queries read like the pipeline's own.

``--publish`` builds the app and force-pushes it, with the data, as one orphan commit on the
``gh-pages`` branch (GitHub Pages serves it at adisgill.github.io/courtvision). One commit per
deploy keeps the DuckDB wasm and the data out of an ever-growing history; the previous
version's data files are carried over until its visitors refresh.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from courtvision.config import REPO_ROOT
from courtvision.db import MACROS

APP = REPO_ROOT / "app"
OUT = APP / "public" / "data"
SITE_URL = "https://adisgill.github.io/courtvision/"

ATTRIBUTION = {
    "kaggle": "Games, box scores, play-by-play and schedules: the 'Historical NBA Data and "
              "Player Box Scores' dataset on Kaggle (eoinamoore), updated nightly.",
    "nba": "Shot charts, league shot averages and the official injury report: NBA.com "
           "(stats.nba.com via nba_api; the NBA's injury report PDFs).",
    "bbref": "Player contracts and head coaches: Basketball-Reference.com.",
    "barttorvik": "College player seasons (2007-08 on) for the draft: Barttorvik.com (T-Rank).",
    "picks": "Traded draft picks and their protections: Hoops Rumors' traded first-round pick "
             "lists (which credit RealGM), entered by hand.",
}  # fmt: skip


# small lookups every page needs, derived from the demo tables (schema ``web``)
WEB = {
    "web.team_names": """
        with t as (select cv_season(gameId) as season, teamId as team_id,
                          any_value(teamCity) as city, any_value(teamName) as name
                   from kaggle.team_statistics where cv_is_team_id(teamId) group by all)
        select t.season, t.team_id, t.city || ' ' || t.name as team, t.name,
               -- the dataset pads its abbreviations and writes the Spurs' as SAN
               any_value(case trim(h.teamAbbrev) when 'SAN' then 'SAS'
                              else trim(h.teamAbbrev) end) as abbrev
        from t left join kaggle.team_histories h
          on h.teamId = t.team_id and t.season between h.seasonFounded and h.seasonActiveTill
             and h.league = 'NBA'
        group by all""",
    "web.player_names": """
        select person_id, trim(coalesce(first_name, '') || ' ' || last_name) as player,
               from_year, to_year
        from stg.players""",
}


def _league_hex(con) -> pd.DataFrame:
    """The league baseline per shot-map hexagon, per season and season type (the app bins a
    team's or player's shots the same way, ``app/src/ui/court.tsx``, and compares)."""
    from courtvision.app import court

    shots = con.execute("""select season, season_type, x_legacy::double x, y_legacy::double y,
                                  made::int made, shot_value from stg.shots
                           where x_legacy is not null""").df()  # fmt: skip
    parts = [court.league_hex(g).assign(season=s, season_type=t)
             for (s, t), g in shots.groupby(["season", "season_type"])]  # fmt: skip
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def build(demo_db: Path, publish_dir: Path = OUT) -> dict:
    """Write the Parquet files and manifest from ``demo_db`` into ``publish_dir``."""
    if publish_dir.exists():
        shutil.rmtree(publish_dir)
    publish_dir.mkdir(parents=True)
    files, rows = {}, {}
    con = duckdb.connect(str(demo_db), read_only=True)
    try:
        tables = con.execute("select schema_name, table_name from duckdb_tables() "
                             "order by 1, 2").fetchall()  # fmt: skip
        con.register("league_hex_df", _league_hex(con))
        sources = ({f"{s}.{t}": f'select * from "{s}"."{t}"' for s, t in tables} | WEB
                   | {"web.league_hex": "select * from league_hex_df"})  # fmt: skip
        for name, sql in sources.items():
            tmp = publish_dir / f"{name}.parquet"
            # sorted rows: unchanged data gives identical bytes, so an unchanged table keeps its
            # file name, and the leading columns (level, season, ...) group into row groups a
            # query can skip
            con.execute(f"copy (select * from ({sql}) order by all) to '{tmp.as_posix()}' "
                        "(format parquet, compression zstd, row_group_size 50000)")  # fmt: skip
            h = hashlib.sha256(tmp.read_bytes()).hexdigest()[:8]
            final = publish_dir / f"{name}.{h}.parquet"
            tmp.rename(final)
            files[name] = final.name
            rows[name] = con.execute(f"select count(*) from read_parquet('{final.as_posix()}')"
                                     ).fetchall()[0][0]  # fmt: skip
        info = con.execute("select data_through from demo.info").fetchall() \
            if "demo.info" in files else []  # fmt: skip
    finally:
        con.close()
    version = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    manifest = {
        "version": version,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_through": str(info[0][0]) if info and info[0][0] else None,
        "files": files,
        "rows": rows,
        "macros": MACROS,
        "attribution": ATTRIBUTION,
    }
    (publish_dir / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                               encoding="utf-8")  # fmt: skip
    size = sum(p.stat().st_size for p in publish_dir.iterdir())
    return {"version": version, "tables": len(files), "mb": round(size / 1e6, 1)}


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd or REPO_ROOT, check=True, capture_output=True,
                       text=True)  # fmt: skip
    return r.stdout.strip()


def publish(branch: str = "gh-pages") -> str:
    """Build the app and force-push it, with the data, as a single commit on ``branch``.
    Authored by the repo-local git identity; no other metadata is added."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("npm not found")
    subprocess.run([npm, "run", "build"], cwd=APP, check=True)
    dist = APP / "dist"
    manifest = json.loads((dist / "data" / "manifest.json").read_text(encoding="utf-8"))
    url = _git("config", "remote.origin.url")
    name, email = _git("config", "user.name"), _git("config", "user.email")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        site = Path(tmp) / "site"
        try:
            _git("clone", "--depth", "1", "--branch", branch, url, str(site))
            old = json.loads((site / "data" / "manifest.json").read_text(encoding="utf-8"))
            keep = {f: (site / "data" / f).read_bytes() for f in old["files"].values()
                    if (site / "data" / f).exists()}  # fmt: skip
        except (subprocess.CalledProcessError, FileNotFoundError, KeyError):
            keep = {}
            if site.exists():
                shutil.rmtree(site, ignore_errors=True)
            site.mkdir(parents=True)
            _git("init", cwd=site)
        for p in site.iterdir():
            if p.name != ".git":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(dist, site, dirs_exist_ok=True)
        for f, b in keep.items():
            if not (site / "data" / f).exists():
                (site / "data" / f).write_bytes(b)
        (site / ".nojekyll").touch()
        _git("checkout", "--orphan", "deploy", cwd=site)
        _git("add", "-A", cwd=site)
        _git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m",
             f"Deploy data version {manifest['version']}", cwd=site)  # fmt: skip
        _git("push", "--force", url, f"deploy:{branch}", cwd=site)
    return manifest["version"]
