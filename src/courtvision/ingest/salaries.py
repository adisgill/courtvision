"""Salaries, payrolls and cap thresholds for the market model.

Source: the Kaggle dataset ``mrbridge/nba-salary-cap-contracts-2016-2026`` (HoopsHype
salary snapshot of 2026-07-07 for players; cap, tax and apron levels from the CBA and
NBA releases). Research and educational use; attribution: MrBridge (mr-bridge.com).
The files stay in ``data/raw/salaries`` (gitignored), like every other raw download.

HoopsHype has its own player and team ids, so players are matched to NBA ``person_id``
by normalized name, using who actually played that season (and for which team) to
break ties. Unmatched players are kept with a NULL ``person_id`` and counted.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from courtvision.db import sql_path

DATASET = "mrbridge/nba-salary-cap-contracts-2016-2026"
TABLES = {
    "player_salaries": "player_salaries.csv",
    "cap": "cap_tax_aprons.csv",
    "min_salaries": "min_salaries.csv",
    "max_salaries": "max_salaries.csv",
    "team_payrolls": "team_payrolls.csv",
    "cap_projections": "cap_projections.csv",
}
_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def download(dest: Path, force: bool = False) -> dict:
    from courtvision.ingest.kaggle_download import _api

    dest.mkdir(parents=True, exist_ok=True)
    _api().dataset_download_files(DATASET, path=str(dest), unzip=True, quiet=True, force=force)
    files = sorted(p for p in dest.iterdir() if p.is_file() and p.name != "_manifest.json")
    manifest = {
        "dataset": DATASET,
        "downloaded_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": {p.name: p.stat().st_size for p in files},
    }
    (dest / "_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def norm_name(name: str | None) -> str:
    """Lowercase ASCII, no punctuation or generational suffix: 'Luka Dončić' -> 'luka doncic'."""
    if not isinstance(name, str) or not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`’\-]", "", s)
    s = _SUFFIX.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_team(name: str) -> str:
    s = norm_name(name)
    return re.sub(r"^la ", "los angeles ", s)


def match_players(sal: pd.DataFrame, players: pd.DataFrame, played: pd.DataFrame) -> pd.Series:
    """NBA person_id for each salary row (NaN when no unique match).

    ``players``: person_id, name. ``played``: season, person_id, team_id (who played
    for whom). A name shared by several players is resolved by the season and team.
    """
    from difflib import SequenceMatcher

    p = players.assign(key=players.name.map(norm_name))
    by_key = p.groupby("key").person_id.apply(list).to_dict()
    roster = set(zip(played.season, played.person_id, played.team_id, strict=True))
    in_season = set(zip(played.season, played.person_id, strict=True))
    last_season = played.groupby("person_id").season.max().to_dict()
    split = {
        pid: (k.split(" ")[0], k.split(" ")[-1])
        for pid, k in zip(p.person_id, p.key, strict=True)
        if k
    }
    active_by_season = played.groupby("season").person_id.apply(set).to_dict()

    def fuzzy(r) -> int | None:
        """Nicknames and spellings: Nic/Nicolas, Herb/Herbert, Schroder/Schroeder.
        Among players active that season or the one before: last names >= 0.85 similar,
        and one first name a prefix of the other."""
        key = norm_name(r.player).split(" ")
        if len(key) < 2:
            return None
        first, last = key[0], key[-1]
        pool = active_by_season.get(r.season_year, set()) | active_by_season.get(
            r.season_year - 1, set()
        )
        hits = []
        for pid in pool:
            f, la = split.get(pid, ("", ""))
            if not f or not (f.startswith(first) or first.startswith(f)):
                continue
            if la == last or SequenceMatcher(None, la, last).ratio() >= 0.85:
                hits.append(pid)
        if len(hits) > 1:
            hits = [h for h in hits if (r.season_year, h, r.nba_team_id) in roster] or hits
        return hits[0] if len(hits) == 1 else None

    out = []
    for r in sal.itertuples():
        key = norm_name(r.player)
        cands = by_key.get(key, [])
        if not cands and " " in key:  # family name first ("Yang Hansen" / "Hansen Yang")
            first, _, rest = key.partition(" ")
            cands = by_key.get(f"{rest} {first}", [])
        if len(cands) > 1:
            narrowed = [c for c in cands if (r.season_year, c, r.nba_team_id) in roster] or [
                c for c in cands if (r.season_year, c) in in_season
            ]
            if not narrowed:  # e.g. a current player sharing a retired player's name
                recent = sorted(cands, key=lambda c: last_season.get(c, 0), reverse=True)
                if (
                    last_season.get(recent[0], 0)
                    >= r.season_year - 2
                    > last_season.get(recent[1], 0)
                ):
                    narrowed = recent[:1]
            if not narrowed:  # duplicate ids for one man: the one who has played in the NBA
                ever = [c for c in cands if c in last_season]
                narrowed = ever if len(ever) == 1 else []
            cands = narrowed
        if not cands:
            hit = fuzzy(r)
            cands = [hit] if hit is not None else []
        out.append(cands[0] if len(cands) == 1 else None)
    return pd.Series(out, index=sal.index, dtype="Int64")


def load(con: duckdb.DuckDBPyConnection, raw: Path) -> dict:
    """Load the CSVs into schema ``salary`` and build ``salary.contracts`` with NBA ids."""
    con.execute("create schema if not exists salary")
    for table, file in TABLES.items():
        con.execute(
            f"create or replace table salary.{table} as "
            f"select * from read_csv_auto({sql_path(raw / file)}, header = true)"
        )
    teams = con.execute(
        """select distinct cv_season(gameId) as season, teamId as team_id,
                  teamCity || ' ' || teamName as name
           from kaggle.team_statistics where cv_season(gameId) >= 2015"""
    ).df()
    # a franchise's current name for every season (salaries use the newest naming)
    names = {}
    for r in teams.sort_values("season").itertuples():
        names[norm_team(r.name)] = r.team_id
    sal = con.execute("select * from salary.player_salaries").df()
    sal["nba_team_id"] = sal.team.map(lambda t: names.get(norm_team(t)))
    players = con.execute(
        "select person_id, trim(coalesce(first_name, '') || ' ' || last_name) as name "
        "from stg.players"
    ).df()
    played = con.execute(
        "select distinct season, person_id, team_id from metrics.player_season_counts "
        "where team_id <> 0"
    ).df()
    sal["person_id"] = match_players(sal, players, played)
    keep = [
        "season_year", "player", "person_id", "nba_team_id", "team", "salary",
        "team_option", "player_option", "qualifying_offer", "two_way", "terminated",
    ]  # fmt: skip
    con.register("sal_df", sal[keep])
    con.execute(
        """create or replace table salary.contracts as
           select season_year as season, player, person_id, nba_team_id as team_id,
                  team as team_name, salary, team_option, player_option, qualifying_offer,
                  two_way, terminated
           from sal_df"""
    )
    con.unregister("sal_df")
    stats = con.execute(
        """select season, count(*) as rows, avg((person_id is not null)::int) as matched,
                  sum(salary) filter (where person_id is not null) / sum(salary) as matched_pay,
                  avg((team_id is not null)::int) as team_matched
           from salary.contracts group by season order by season"""
    ).df()
    return {"by_season": stats}
