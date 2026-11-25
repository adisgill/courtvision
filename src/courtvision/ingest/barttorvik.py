"""College player seasons from Barttorvik (T-Rank), user-approved 2026-11-24.

``https://barttorvik.com/getadvstats.php?year=<Y>&csv=1`` returns every Division I
player-season of the season ending in ``Y`` (2008 on) as one CSV without a header: box
totals, tempo-free rates, Barttorvik's BPM family, height, class, birth date, recruiting
rank and, for players since drafted, their NBA pick. Fetched on request only
(``courtvision ingest ncaa --fetch``), one request per season, ``DELAY`` seconds apart
(robots.txt: Crawl-delay 10; the endpoint is allowed for ordinary clients). Completed
seasons are downloaded once; the season in progress again at most once a day. Cached
gzipped under ``data/raw/barttorvik/`` (gitignored); ``build`` only reads the cache.

Columns were named by checking known players (``COLUMNS``); ``unk_*`` are ones not
identified and not used.
"""

from __future__ import annotations

import gzip
import io
import logging
import time
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from courtvision.ingest.bbref_contracts import USER_AGENT

log = logging.getLogger(__name__)

URL = "https://barttorvik.com/getadvstats.php"
FIRST_YEAR = 2008
DELAY = 11.0  # seconds between requests (robots.txt: Crawl-delay 10)

# position in the CSV -> name (checked against known players; see the module docstring)
COLUMNS = [
    "player",
    "team",
    "conf",
    "gp",
    "min_pct",
    "ortg",
    "usg",
    "efg",
    "ts",
    "orb_pct",
    "drb_pct",
    "ast_pct",
    "tov_pct",
    "ftm",
    "fta",
    "ft_pct",
    "fg2m",
    "fg2a",
    "fg2_pct",
    "fg3m",
    "fg3a",
    "fg3_pct",
    "blk_pct",
    "stl_pct",
    "ftr",
    "class",
    "height",
    "jersey",
    "porpag",
    "adjoe",
    "pfr",
    "year",
    "pid",
    "hometown",
    "rec_rank",
    "ast_tov",
    "rim_m",
    "rim_a",
    "mid_m",
    "mid_a",
    "rim_pct",
    "mid_pct",
    "dunk_m",
    "dunk_a",
    "dunk_pct",
    "pick",
    "drtg",
    "adrtg",
    "dporpag",
    "stops",
    "bpm",
    "obpm",
    "dbpm",
    "gbpm",
    "mpg",
    "ogbpm",
    "dgbpm",
    "oreb",
    "dreb",
    "treb",
    "ast",
    "stl",
    "blk",
    "pts",
    "role",
    "fg3_per100",
    "birth_date",
]


def _path(root: Path, year: int, current: int) -> Path:
    if year < current:
        return root / f"players_{year}.csv.gz"
    return root / "current" / f"players_{year}_{date.today().isoformat()}.csv.gz"


def fetch(root: Path, current: int, first: int = FIRST_YEAR, force: bool = False) -> list[Path]:
    """Download every season ``first``..``current`` not cached yet (the season in progress,
    ``current``, once a day). Returns the files written."""
    import requests

    wrote = []
    for year in range(first, current + 1):
        path = _path(root, year, current)
        if path.exists() and not force:
            continue
        if wrote:
            time.sleep(DELAY)
        r = requests.get(URL, params={"year": year, "csv": 1},
                         headers={"User-Agent": USER_AGENT}, timeout=60)  # fmt: skip
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("text/csv") or len(r.content) < 10_000:
            raise RuntimeError(f"barttorvik {year}: not a CSV ({r.headers.get('content-type')}, "
                               f"{len(r.content)} bytes)")  # fmt: skip
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(r.content))
        log.info("fetched barttorvik %s (%d rows)", year, r.text.count("\n"))
        wrote.append(path)
    return wrote


def latest_files(root: Path, current: int) -> dict[int, Path]:
    """Cached file per season: completed seasons, plus the newest download of ``current``."""
    files = {int(p.name.split("_")[1].split(".")[0]): p
             for p in root.glob("players_*.csv.gz")}  # fmt: skip
    newest = sorted((root / "current").glob(f"players_{current}_*.csv.gz"))
    if newest:
        files[current] = newest[-1]
    return dict(sorted(files.items()))


def parse(raw: bytes) -> pd.DataFrame:
    """One season's CSV -> named columns (extra trailing columns kept as ``unk_<i>``)."""
    df = pd.read_csv(io.BytesIO(raw), header=None, low_memory=False)
    names = COLUMNS + [f"unk_{i}" for i in range(len(COLUMNS), df.shape[1])]
    df.columns = names[: df.shape[1]]
    return df


def _inches(h: pd.Series) -> pd.Series:
    """'6-8' -> 80 (other forms -> missing)."""
    parts = h.astype("string").str.extract(r"^\s*(\d)-(\d{1,2})\s*$").astype("Float64")
    return parts[0] * 12 + parts[1]


def load(con: duckdb.DuckDBPyConnection, root: Path, current: int) -> dict:
    """Every cached season -> ``ncaa.player_seasons`` (one row per player, team, season).
    ``season`` is the NBA-style start year (2025 = 2025-26), ``as_of`` the download date
    of the season in progress."""
    files = latest_files(root, current)
    if not files:
        raise FileNotFoundError(f"no Barttorvik files under {root}: run with --fetch")
    frames = []
    for year, path in files.items():
        df = parse(gzip.decompress(path.read_bytes()))
        df = df[[c for c in df.columns if not c.startswith("unk_")]]
        df["season"] = year - 1
        df["as_of"] = (date.fromisoformat(path.name.split("_")[2][:10])
                       if path.parent.name == "current" else None)  # fmt: skip
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["height_in"] = _inches(df["height"])
    df["birth_date"] = pd.to_datetime(df["birth_date"], errors="coerce").dt.date
    df["pick"] = pd.to_numeric(df["pick"], errors="coerce").astype("Int64")
    df["rec_rank"] = pd.to_numeric(df["rec_rank"], errors="coerce")
    con.execute("create schema if not exists ncaa")
    con.register("_bt", df)
    con.execute("""create or replace table ncaa.player_seasons as
        select * exclude (height) from _bt order by season, pid""")
    con.unregister("_bt")
    return {"seasons": f"{min(files) - 1}-{max(files) - 1}", "rows": len(df),
            "players": int(df["pid"].nunique())}  # fmt: skip
