"""The contract book: who is paid what, by whom, through when, with which options.

Three sources, newest wins for the season they cover (spec section 13.1):

1. **Basketball-Reference contracts** (Kaggle ``deocheng/nba-data-1946-2026``, table
   ``public.player_contracts``, scraped 2026-06-17 to 06-23, Apache-2.0 on Kaggle):
   every contract's salary for 2025-26 through 2030-31 and the option on each year.
   This is the only source of contract length.
2. **The July salary snapshot** (``salary.contracts``, HoopsHype via MrBridge, 2026-07-07):
   2026-27 salaries after the first week of free agency, two-way and waived flags.
3. **``data/salary_overrides.csv``** (optional, yours): ``player, season, salary, team,
   option`` rows that replace or add contracts (deals signed after the snapshots).

Rules, per player, for the target season S and the years after it:

* a July row for S is authoritative for S. If Basketball-Reference has the same contract
  for S (within 10%), its later years and options carry on; otherwise it is a new deal
  of unknown length.
* no July row: a guaranteed Basketball-Reference salary for S stands; an option year for
  S with no July row was declined (the player became a free agent).
* playing this season with no contract anywhere: ``unknown`` (signed after the
  snapshots); never treated as a free agent.
* a contract is charged to the team the player plays for now (trades since the
  snapshots); waived salary stays with the team that waived him. Two-way deals are
  listed but don't count against the cap.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from courtvision.ingest.salaries import match_players, norm_name, norm_team

BBREF_DATASET = "deocheng/nba-data-1946-2026"
BBREF_FILE = "csv/public.player_contracts.csv"
BBREF_AS_OF = "2026-06-23"
SNAPSHOT_AS_OF = "2026-07-07"
BBREF_TEAMS = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BRK": "Brooklyn Nets",
    "CHI": "Chicago Bulls", "CHO": "Charlotte Hornets", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHO": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}  # fmt: skip
YEARS = {2025: "2025_26", 2026: "2026_27", 2027: "2027_28", 2028: "2028_29",
         2029: "2029_30", 2030: "2030_31"}  # fmt: skip


def download_bbref(dest: Path) -> Path:
    from courtvision.ingest.kaggle_download import _api

    dest.mkdir(parents=True, exist_ok=True)
    _api().dataset_download_file(BBREF_DATASET, BBREF_FILE, path=str(dest), quiet=True)
    found = list(dest.rglob("public.player_contracts.csv"))
    if not found:
        raise FileNotFoundError("player_contracts.csv not in the download")
    return found[0]


def _team_ids(con) -> dict[str, int]:
    teams = con.execute(
        """select distinct cv_season(gameId) as season, teamId as team_id,
                  teamCity || ' ' || teamName as name
           from kaggle.team_statistics where cv_season(gameId) >= 2015"""
    ).df()
    names = {}
    for r in teams.sort_values("season").itertuples():
        names[norm_team(r.name)] = r.team_id
    return names


def fix_mojibake(text):
    """Undo UTF-8 read as Latin-1 and saved again ('Luka DonÄ\\x8diÄ\\x87' -> 'Dončić')."""
    if not isinstance(text, str):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def load_bbref(con, path: Path) -> pd.DataFrame:
    """Basketball-Reference contracts, long: one row per player x season with salary."""
    raw = pd.read_csv(path, encoding="utf-8")
    raw["player_name"] = raw.player_name.map(fix_mojibake)
    names = _team_ids(con)
    raw["nba_team_id"] = raw.team_abbr.map(lambda a: names.get(norm_team(BBREF_TEAMS[a])))
    raw["season_year"] = 2025  # the season the pages describe
    raw["player"] = raw.player_name
    players = con.execute(
        "select person_id, trim(coalesce(first_name, '') || ' ' || last_name) as name "
        "from stg.players"
    ).df()
    played = con.execute(
        "select distinct season, person_id, team_id from metrics.player_season_counts "
        "where team_id <> 0"
    ).df()
    raw["person_id"] = match_players(raw, players, played)
    rows = []
    for r in raw.itertuples():
        for year, suffix in YEARS.items():
            sal = getattr(r, f"salary_{suffix}")
            if pd.notna(sal) and sal > 0:
                opt = getattr(r, f"opt_{suffix}")
                rows.append({
                    "person_id": r.person_id, "player": r.player_name,
                    "team_id": r.nba_team_id, "season": year, "salary": float(sal),
                    "option": opt.replace("_option", "") if isinstance(opt, str) else None,
                    "two_way": str(r.is_two_way).lower() in ("t", "true"),
                })  # fmt: skip
    return pd.DataFrame(rows)


def load_current(con, raw: pd.DataFrame, season: int) -> pd.DataFrame:
    """Today's team contract pages with NBA ``team_id`` and matched ``person_id``."""
    if raw.empty:
        return raw
    names = _team_ids(con)
    raw = raw.copy()
    raw["team_id"] = raw.team_abbr.map(lambda a: names.get(norm_team(BBREF_TEAMS[a])))
    players = con.execute(
        "select person_id, trim(coalesce(first_name, '') || ' ' || last_name) as name "
        "from stg.players"
    ).df()
    played = con.execute(
        "select distinct season, person_id, team_id from metrics.player_season_counts "
        "where team_id <> 0"
    ).df()
    ids = raw.drop_duplicates("bbref_id")[["bbref_id", "player", "team_id"]].copy()
    ids["season_year"], ids["nba_team_id"] = season, ids.team_id
    ids["person_id"] = match_players(ids, players, played)
    return raw.merge(ids[["bbref_id", "person_id"]], on="bbref_id", how="left")


def read_overrides(path: Path, con) -> pd.DataFrame:
    """Your corrections: player, season, salary[, team][, option]. Missing file: none."""
    if not path.exists():
        return pd.DataFrame(columns=["person_id", "season", "salary", "team_id", "option"])
    o = pd.read_csv(path, comment="#")
    players = con.execute(
        "select person_id, trim(coalesce(first_name, '') || ' ' || last_name) as name "
        "from stg.players"
    ).df()
    key = players.assign(k=players.name.map(norm_name)).drop_duplicates("k", keep="last")
    pid = dict(zip(key.k, key.person_id, strict=True))
    names = _team_ids(con)
    abbr = {k: names.get(norm_team(v)) for k, v in BBREF_TEAMS.items()}
    o["person_id"] = o.player.map(lambda n: pid.get(norm_name(n)))
    o["team_id"] = o.get("team", pd.Series(dtype=str)).map(
        lambda t: abbr.get(str(t).upper()) or names.get(norm_team(str(t))) if pd.notna(t) else None
    )
    o["option"] = o.get("option", pd.Series(dtype=str)).where(lambda s: s.notna(), None)
    return o[["person_id", "season", "salary", "team_id", "option"]].dropna(subset=["person_id"])


def build_book(con, bb: pd.DataFrame, season: int, overrides: pd.DataFrame,
               current: pd.DataFrame | None = None, current_as_of: str = "") -> dict:  # fmt: skip
    """``salary.book`` (player x season rows) and ``salary.contract_summary``.

    ``current``: Basketball-Reference's team contract pages fetched today
    (:mod:`courtvision.ingest.bbref_contracts`, with ``person_id`` and ``team_id``). When
    given, they decide every standard contract and its later years; the July snapshot then
    only adds dead money and two-way deals (the pages list neither), and the June pages
    are not needed. Overrides still win.
    """
    snap = con.execute(
        """select person_id, player, team_id, salary, team_option, player_option, two_way,
                  terminated
           from salary.contracts where season = ? and person_id is not null""",
        [season],
    ).df()
    now = con.execute(
        """select person_id, arg_max(team_id, game_date) as team_now, count(*) as games
           from metrics.player_game_counts
           where season = ? and minutes > 0 group by person_id""",
        [season],
    ).df()
    team_now = dict(zip(now.person_id, now.team_now, strict=True))
    bb = bb[bb.person_id.notna()]
    bbs = {pid: g for pid, g in bb.groupby("person_id")}
    rows = []
    active_snap = snap[~snap.terminated.astype(bool)]

    def charge(pid, team):
        return team_now.get(pid, team)

    for r in snap[snap.terminated.astype(bool)].itertuples():  # dead money stays put
        rows.append({"person_id": r.person_id, "season": season, "salary": r.salary,
                     "team_id": r.team_id, "option": None, "kind": "dead money",
                     "source": f"snapshot {SNAPSHOT_AS_OF}"})  # fmt: skip
    length_known = {}
    use_current = current is not None and not current.empty
    if use_current:
        cur = current[current.person_id.notna() & (current.season >= season)]
        for x in cur.itertuples():
            rows.append({"person_id": int(x.person_id), "season": int(x.season),
                         "salary": x.salary, "team_id": x.team_id, "option": x.option,
                         "kind": "contract",
                         "source": f"basketball-reference {current_as_of}"})  # fmt: skip
            length_known[int(x.person_id)] = True
        # the pages list standard contracts only: keep July's two-way deals
        active_snap = active_snap[active_snap.two_way.astype(bool)
                                  & ~active_snap.person_id.isin(set(length_known))]  # fmt: skip
        bbs = {}  # the June pages are superseded
    for pid, g in active_snap.groupby("person_id"):
        r = g.sort_values("salary", ascending=False).iloc[0]
        opt = "player" if r.player_option else "team" if r.team_option else None
        kind = "two-way" if r.two_way else "contract"
        rows.append({"person_id": pid, "season": season, "salary": r.salary,
                     "team_id": charge(pid, r.team_id), "option": opt, "kind": kind,
                     "source": f"snapshot {SNAPSHOT_AS_OF}"})  # fmt: skip
        b = bbs.get(pid)
        same = (
            b is not None
            and (b.season == season).any()
            and abs(b[b.season == season].salary.iloc[0] - r.salary) <= 0.1 * r.salary
        )
        length_known[pid] = bool(same)
        if same:
            for x in b[b.season > season].itertuples():
                rows.append(
                    {
                        "person_id": pid,
                        "season": x.season,
                        "salary": x.salary,
                        "team_id": charge(pid, r.team_id),
                        "option": x.option,
                        "kind": kind,
                        "source": f"basketball-reference {BBREF_AS_OF}",
                    }
                )
    declined = set()
    for pid, b in bbs.items():
        if pid in length_known or not (b.season == season).any():
            continue
        cur = b[b.season == season].iloc[0]
        if cur.option is not None:  # an option year with no July contract: declined
            declined.add(pid)
            continue
        length_known[pid] = True
        for x in b[b.season >= season].itertuples():
            rows.append({"person_id": pid, "season": x.season, "salary": x.salary,
                         "team_id": charge(pid, x.team_id), "option": x.option,
                         "kind": "two-way" if x.two_way else "contract",
                         "source": f"basketball-reference {BBREF_AS_OF}"})  # fmt: skip
    book = pd.DataFrame(rows)
    # This year's first-round picks: the rookie scale fixes the length (2 guaranteed
    # years, then team options on years 3 and 4; raises of ~4.7%, 4.7% and 26%).
    rookies = (
        con.execute(
            "select person_id from stg.players where draft_round = 1 and draft_year = ?",
            [season],
        )
        .df()
        .person_id
    )
    for pid in set(rookies) & {p for p, known in length_known.items() if not known}:
        first = book[(book.person_id == pid) & (book.season == season)
                     & (book.kind == "contract")]  # fmt: skip
        if first.empty:
            continue
        r = first.iloc[0]
        for k, (mult, opt) in enumerate([(1.047, None), (1.047**2, "team"),
                                         (1.047**2 * 1.26, "team")], start=1):  # fmt: skip
            book.loc[len(book)] = {"person_id": pid, "season": season + k,
                                   "salary": r.salary * mult, "team_id": r.team_id,
                                   "option": opt, "kind": "contract",
                                   "source": "rookie scale (inferred)"}  # fmt: skip
        length_known[pid] = True
    # playing now with no contract anywhere: signed after the snapshots. With today's
    # contract pages, which list every standard contract, that means a two-way or
    # short-term (10-day) deal, which doesn't count against the cap.
    signed = set(book.person_id[book.kind.isin(["contract", "two-way"])])
    why = ("not on the contract pages: two-way or short-term deal" if use_current
           else "playing; no contract in the snapshots")  # fmt: skip
    for pid, team in team_now.items():
        if pid not in signed:
            book.loc[len(book)] = {"person_id": pid, "season": season, "salary": np.nan,
                                   "team_id": team, "option": None, "kind": "unknown",
                                   "source": why}  # fmt: skip
            length_known[pid] = False
    # your overrides replace whatever a source said for that player and season
    if not overrides.empty:
        o = overrides.copy()
        o["team_id"] = [t if pd.notna(t) else team_now.get(p) for p, t in
                        zip(o.person_id, o.team_id, strict=True)]  # fmt: skip
        keys = set(zip(o.person_id, o.season, strict=True))
        book = book[[(p, s) not in keys for p, s in zip(book.person_id, book.season,
                                                        strict=True)]]  # fmt: skip
        o["kind"], o["source"] = "contract", "override"
        book = pd.concat([book, o[book.columns]], ignore_index=True)
        for pid in set(o.person_id):
            length_known[pid] = True
    book["person_id"] = book.person_id.astype("int64")
    book["length_known"] = book.person_id.map(length_known).fillna(False).astype(bool)
    return {"book": book, "declined": declined}


def summarize(con, book: pd.DataFrame, declined: set, season: int) -> pd.DataFrame:
    """One row per player: this season's deal, how long it runs, and his status next
    summer (UFA, RFA, player / team option, under contract, length unknown)."""
    info = con.execute(
        """select person_id, draft_year, draft_round,
                  trim(coalesce(first_name, '') || ' ' || last_name) as player
           from stg.players"""
    ).df()
    last = con.execute(
        """select person_id, max(season) as last_season from metrics.player_game_counts
           where minutes > 0 and season_type = 'regular' group by person_id"""
    ).df()
    out = []
    for pid, g in book.groupby("person_id"):
        cur = g[(g.season == season) & g.kind.isin(["contract", "two-way", "unknown"])]
        if cur.empty:
            continue
        c = cur.iloc[0]
        later = g[(g.season > season) & g.kind.isin(["contract", "two-way"])].sort_values("season")
        nxt = later[later.season == season + 1]
        if c.kind == "unknown":
            status = ("two-way or short-term deal (salary not listed)"
                      if "contract pages" in str(c.source)
                      else "salary unknown (signed after the snapshots)")  # fmt: skip
        elif c.kind == "two-way" and later.empty:
            status = "two-way (restricted when it ends)"
        elif not bool(c.length_known):
            status = "new deal, length unknown"
        elif not nxt.empty:
            opt = nxt.option.iloc[0]
            status = f"{opt} option {season + 1}" if isinstance(opt, str) else "under contract"
        else:
            status = "RFA " if c.kind == "two-way" else "UFA "
            status += str(season + 1)
        out.append({"person_id": pid, "kind": c.kind, "salary": c.salary,
                    "team_id": c.team_id, "status": status,
                    "through": int(later.season.max()) if not later.empty else season,
                    "years_left": 1 + len(later),
                    "total_left": c.salary + later.salary.sum() if pd.notna(c.salary)
                    else np.nan,
                    "next_option": nxt.option.iloc[0] if not nxt.empty
                    and isinstance(nxt.option.iloc[0], str) else None})  # fmt: skip
    s = pd.DataFrame(out).merge(info, on="person_id", how="left")
    # rookie-scale contracts ending this season become restricted free agency
    rookie_end = (s.draft_round == 1) & (s.draft_year == season - 3)
    s.loc[rookie_end & s.status.str.startswith("UFA"), "status"] = f"RFA {season + 1}"
    # free agents now: played last season or this one, no deal this season, not playing
    signed = set(s.person_id)
    fa = last[(last.last_season >= season - 1) & ~last.person_id.isin(signed)]
    fa = fa.merge(info, on="person_id", how="left")
    fa["status"] = np.where(fa.person_id.isin(declined), "free agent (declined option)",
                            "free agent")  # fmt: skip
    fa["kind"], fa["years_left"], fa["salary"] = "none", 0, np.nan
    return pd.concat([s, fa[["person_id", "kind", "status", "years_left", "salary", "player",
                             "draft_year", "draft_round"]]], ignore_index=True)  # fmt: skip


def write(con, book: pd.DataFrame, summary: pd.DataFrame) -> None:
    for name, df in (("book", book), ("contract_summary", summary)):
        con.register("tmp_df", df)
        con.execute(f"create or replace table salary.{name} as select * from tmp_df")
        con.unregister("tmp_df")
