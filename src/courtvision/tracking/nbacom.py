"""NBA.com's own 2015-16 tracking stats: the known answers our tracking numbers are checked against.

The same SportVU system produced them, and NBA.com still serves them through ``nba_api`` (an
approved source). They're season splits, but a date range of one day is one game per team, so
asking day by day gives player-game numbers for every game we have frames for:

- ``leaguedashptstats``: SpeedDistance (distance, average speed, offense and defense),
  Possessions (touches, time of possession, dribbles per touch), CatchShoot and PullUpShot;
- ``leaguedashplayerptshot``: field goals by the closest defender's distance (0-2, 2-4, 4-6,
  6+ ft).

Responses go through courtvision's cached, rate-limited client (``data/raw/nba_api``), so a
second run never touches the network.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

import pandas as pd

from courtvision.config import get_paths
from courtvision.ingest.nba_api_client import CachedClient, read_envelope, result_sets
from courtvision.tracking import sportvu

log = logging.getLogger(__name__)

SEASON = "2015-16"
MEASURES = ("SpeedDistance", "Possessions", "CatchShoot", "PullUpShot")
DEFENDER_RANGES = {"0-2": "0-2 Feet - Very Tight", "2-4": "2-4 Feet - Tight",
                   "4-6": "4-6 Feet - Open", "6+": "6+ Feet - Wide Open"}  # fmt: skip


def games() -> pd.DataFrame:
    """Every converted game: game id, date (from the archive name) and the two team ids."""
    record = sportvu.raw_dir() / "converted.json"
    seen = json.loads(record.read_text()) if record.exists() else {}
    have = set(sportvu.game_ids())
    rows = []
    for archive, gid in seen.items():
        if gid not in have:
            continue
        # ten archives in the source carry a folder prefix: "2016.NBA.Raw...Logs12.05.2015..."
        day = datetime.strptime(re.search(r"(\d\d\.\d\d\.\d{4})\.", archive)[1],
                                "%m.%d.%Y").date()  # fmt: skip
        teams = pd.read_parquet(sportvu.frames_dir().parent / "rosters" / f"{gid}.parquet",
                                columns=["team_id"]).team_id.unique()  # fmt: skip
        rows.append({"game_id": gid, "date": day, "team_ids": sorted(int(t) for t in teams)})
    return pd.DataFrame(rows).sort_values(["date", "game_id"], ignore_index=True)


def _requests(days: list[date]) -> list[tuple[type, dict]]:
    from nba_api.stats.endpoints import leaguedashplayerptshot, leaguedashptstats

    out = []
    for d in days:
        day = d.strftime("%m/%d/%Y")
        common = {"season": SEASON, "date_from_nullable": day, "date_to_nullable": day}
        for m in MEASURES:
            out.append((leaguedashptstats.LeagueDashPtStats,
                        common | {"player_or_team": "Player", "pt_measure_type": m,
                                  "per_mode_simple": "Totals"}))  # fmt: skip
        for r in DEFENDER_RANGES.values():
            out.append((leaguedashplayerptshot.LeagueDashPlayerPtShot,
                        common | {"close_def_dist_range_nullable": r}))  # fmt: skip
    return out


def fetch() -> dict:
    """Pull every split for every game date we have frames for (cached, so it resumes)."""
    days = sorted(games().date.unique())
    client = CachedClient(get_paths().nba_api_raw)
    reqs = _requests(days)
    for i, (cls, kw) in enumerate(reqs, 1):
        client.get(cls, **kw)
        if i % 40 == 0:
            log.info("nba.com tracking: %d/%d (network %d)", i, len(reqs), client.network_calls)
    return {"dates": len(days), "requests": len(reqs), "network_calls": client.network_calls}


def load() -> dict[str, pd.DataFrame]:
    """The cached responses as player-game tables keyed by ``game_id`` and ``person_id``.

    ``speed``, ``possessions``, ``catch_shoot`` and ``pull_up`` have one row per player-game;
    ``defender`` has one row per player-game and closest-defender range. Rows from games we
    have no frames for are dropped."""
    g = games()
    by_team_day = {(t, d): gid for gid, d, ts in g[["game_id", "date", "team_ids"]].itertuples(
        index=False) for t in ts}  # fmt: skip
    client = CachedClient(get_paths().nba_api_raw)
    names = {"SpeedDistance": "speed", "Possessions": "possessions",
             "CatchShoot": "catch_shoot", "PullUpShot": "pull_up"}  # fmt: skip
    ranges = {v: k for k, v in DEFENDER_RANGES.items()}
    out: dict[str, list[pd.DataFrame]] = {n: [] for n in [*names.values(), "defender"]}
    for cls, kw in _requests(sorted(g.date.unique())):
        ep = cls(**kw, get_request=False)
        path = client.path_for(ep.endpoint, ep.parameters)
        if not path.exists():
            continue
        sets = result_sets(read_envelope(path)["response"])
        df = pd.DataFrame(next(iter(sets.values())))
        if df.empty:
            continue
        day = datetime.strptime(kw["date_from_nullable"], "%m/%d/%Y").date()
        team_col = "TEAM_ID" if "TEAM_ID" in df else "PLAYER_LAST_TEAM_ID"
        df["game_id"] = [by_team_day.get((int(t), day)) for t in df[team_col]]
        df = df.dropna(subset=["game_id"]).rename(columns={"PLAYER_ID": "person_id",
                                                           team_col: "team_id"})  # fmt: skip
        if "pt_measure_type" in kw:
            out[names[kw["pt_measure_type"]]].append(df)
        else:
            out["defender"].append(df.assign(defender=ranges[kw["close_def_dist_range_nullable"]]))
    return {k: pd.concat(v, ignore_index=True) if v else pd.DataFrame() for k, v in out.items()}
