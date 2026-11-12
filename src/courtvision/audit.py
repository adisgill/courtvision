"""Data audit: profile every source table and write markdown appendices.

``courtvision audit kaggle`` writes ``docs/audit/kaggle/*.md``;
``courtvision audit nba-api --season N`` writes ``docs/audit/nba_api_<N>.md``.
The hand-written summary in ``docs/data_audit.md`` cites these files.

Conventions
-----------
* ``season`` is the start year (2025 = 2025-26), derived from ``gameId``.
* Eras are defined by what the data records (see ``ERAS``), not by decade.
* Many untracked stats are stored as **0, not NULL**, so availability of a
  numeric stat is measured as the share of team-games with a non-zero value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

# (label, first season, last season). Boundaries come from the audit itself:
# full regular-season coverage begins 1954-55; steals/blocks/OREB begin in
# player box scores 1973-74; 3PA 1979-80; play-by-play 1996-97; the
# live-feed PBP schema (x/y, possession) 2019-20.
ERAS: list[tuple[str, int, int]] = [
    ("1946–53", 1946, 1953),
    ("1954–72", 1954, 1972),
    ("1973–78", 1973, 1978),
    ("1979–95", 1979, 1995),
    ("1996–2018", 1996, 2018),
    ("2019–", 2019, 9999),
]

NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL",
)  # fmt: skip

# Box score stats checked for "first season tracked".
BOX_STATS = [
    ("points", "points"), ("fgm", "fieldGoalsMade"), ("fga", "fieldGoalsAttempted"),
    ("ftm", "freeThrowsMade"), ("fta", "freeThrowsAttempted"), ("reb", "reboundsTotal"),
    ("oreb", "reboundsOffensive"), ("dreb", "reboundsDefensive"), ("ast", "assists"),
    ("pf", "foulsPersonal"), ("stl", "steals"), ("blk", "blocks"), ("tov", "turnovers"),
    ("fg3m", "threePointersMade"), ("fg3a", "threePointersAttempted"),
    ("plus_minus", "plusMinusPoints"),
]  # fmt: skip


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def md_table(df: pd.DataFrame, align_right: bool = True) -> str:
    """Render a DataFrame as a GitHub markdown table (no tabulate dependency)."""
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |"]
    seps = []
    for c in df.columns:
        numeric = pd.api.types.is_numeric_dtype(df[c])
        seps.append("---:" if (numeric and align_right) else "---")
    lines.append("| " + " | ".join(seps) + " |")
    for row in df.itertuples(index=False):
        cells = []
        for v in row:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                cells.append("")
            elif isinstance(v, float):
                cells.append(f"{v:,.1f}")
            elif isinstance(v, int):
                cells.append(f"{v:,}")
            else:
                cells.append(str(v).replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_md(path: Path, text: str) -> None:
    """Write with LF line endings on every OS (the repo normalizes to LF)."""
    path.write_text(text, encoding="utf-8", newline="\n")


def season_label(s: int | float | None) -> str:
    if s is None or pd.isna(s):
        return ""
    s = int(s)
    return f"{s}-{(s + 1) % 100:02d}"


# --------------------------------------------------------------------- per table


@dataclass
class TableProfile:
    view: str
    rows: int
    columns: pd.DataFrame  # name, type, null_pct, first, last, per-era null %
    zero_by_era: pd.DataFrame | None
    era_rows: dict[str, int]
    season_basis: str | None
    season_range: tuple[int, int] | None
    by_type: pd.DataFrame | None
    date_range: tuple[str, str] | None


def _season_expr(cols: list[str], view: str) -> tuple[str | None, str | None]:
    if "gameId" in cols:
        return "cv_season(gameId)", "season from `gameId`"
    if view.endswith(".players") and "fromYear" in cols:
        return "fromYear", "era of the player's first season (`fromYear`)"
    return None, None


def profile_table(con: duckdb.DuckDBPyConnection, view: str) -> TableProfile:
    desc = con.execute(f"describe {view}").fetchall()
    cols = [(r[0], r[1]) for r in desc]
    names = [c for c, _ in cols]
    rows = con.execute(f"select count(*) from {view}").fetchall()[0][0]
    season, basis = _season_expr(names, view)

    aggs = []
    for i, (c, _t) in enumerate(cols):
        aggs.append(f"count(*) filter (where {q(c)} is null) as n{i}")
        if season:
            aggs.append(f"min({season}) filter (where {q(c)} is not null) as f{i}")
            aggs.append(f"max({season}) filter (where {q(c)} is not null) as l{i}")
    era_rows: dict[str, int] = {}
    era_null: dict[str, list[float]] = {}
    era_zero: dict[str, list[float | None]] = {}
    row = con.execute(f"select {', '.join(aggs)} from {view}").fetchall()[0]
    stride = 3 if season else 1

    if season:
        era_case = "case " + " ".join(
            f"when {season} between {lo} and {hi} then '{label}'" for label, lo, hi in ERAS
        ) + " end"  # fmt: skip
        era_aggs = [f"{era_case} as era", "count(*) as n"]
        for i, (c, t) in enumerate(cols):
            era_aggs.append(f"count(*) filter (where {q(c)} is null) as n{i}")
            if t.upper().startswith(NUMERIC_TYPES):
                era_aggs.append(f"count(*) filter (where {q(c)} = 0) as z{i}")
        by_era = con.sql(f"select {', '.join(era_aggs)} from {view} group by era").df()
        by_era = by_era.set_index("era")
        for label, _, _ in ERAS:
            if label not in by_era.index:
                continue
            r = by_era.loc[label]
            n = int(r["n"])
            era_rows[label] = n
            era_null[label] = [100 * r[f"n{i}"] / n for i in range(len(cols))]
            era_zero[label] = [
                100 * r[f"z{i}"] / n if f"z{i}" in r else None for i in range(len(cols))
            ]

    recs = []
    for i, (c, t) in enumerate(cols):
        rec = {"column": c, "type": t, "null %": 100 * row[i * stride] / rows if rows else None}
        if season:
            rec["first season"] = season_label(row[i * stride + 1])
            rec["last season"] = season_label(row[i * stride + 2])
            for label in era_null:
                rec[label] = era_null[label][i]
        recs.append(rec)
    columns = pd.DataFrame(recs)

    zero = None
    if season:
        zrecs = []
        for i, (c, t) in enumerate(cols):
            if t.upper().startswith(NUMERIC_TYPES) and c != "gameId":
                zrecs.append({"column": c, **{lbl: era_zero[lbl][i] for lbl in era_zero}})
        zero = pd.DataFrame(zrecs) if zrecs else None

    season_range = by_type = date_range = None
    if season:
        lo, hi = con.execute(f"select min({season}), max({season}) from {view}").fetchall()[0]
        season_range = (lo, hi)
    if "gameId" in names:
        by_type = con.sql(
            f"""select cv_season_type(gameId) as "season type", count(*) as rows,
                       count(distinct gameId) as games,
                       min(cv_season(gameId)) as first, max(cv_season(gameId)) as last
                from {view} group by 1 order by games desc"""
        ).df()
        by_type["first"] = by_type["first"].map(season_label)
        by_type["last"] = by_type["last"].map(season_label)
    date_col = next((c for c in ("gameDateTimeEst", "gameDate") if c in names), None)
    if date_col:
        d = con.execute(
            f"select min(try_cast({q(date_col)} as date)), max(try_cast({q(date_col)} as date)) "
            f"from {view}"
        ).fetchall()[0]
        date_range = (str(d[0]), str(d[1]))
    return TableProfile(
        view, rows, columns, zero, era_rows, basis, season_range, by_type, date_range
    )


def render_profile(p: TableProfile, source_file: str) -> str:
    out = [f"# `{p.view}`", ""]
    out.append(
        f"Source file: `{source_file}` · rows: **{p.rows:,}** · columns: **{len(p.columns)}**"
    )
    out.append("")
    if p.season_range:
        lo, hi = p.season_range
        out.append(f"Seasons: **{season_label(lo)} → {season_label(hi)}** ({p.season_basis})")
    if p.date_range:
        out.append(f"Dates: {p.date_range[0]} → {p.date_range[1]}")
    out.append("")
    if p.by_type is not None:
        out += ["## Rows and games by season type", "", md_table(p.by_type), ""]
    if p.era_rows:
        out += ["## Rows by era", "", md_table(pd.DataFrame([p.era_rows])), ""]
    out += ["## Columns", ""]
    if p.era_rows:
        out.append(
            "`null %` is over all rows; era columns are null % within that era; "
            "`first/last season` are the first and last seasons with any non-null value. "
            "A null rate of 0 does **not** mean the stat was tracked: see the zero-rate "
            "table below."
        )
        out.append("")
    out += [md_table(p.columns), ""]
    if p.zero_by_era is not None:
        out += [
            "## Zero rate by era (numeric columns)",
            "",
            "% of rows whose value is exactly 0. In this dataset untracked stats are "
            "usually stored as 0 rather than NULL, so a ~100% zero rate means *not recorded*.",
            "",
            md_table(p.zero_by_era),
            "",
        ]
    return "\n".join(out)


# ----------------------------------------------------------------- cross-table


def coverage_by_season(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Distinct games per season in each game-keyed table, regular season + playoffs."""
    tables = [
        ("games", "kaggle.games"),
        ("team_stats", "kaggle.team_statistics"),
        ("player_stats", "kaggle.player_statistics"),
        ("team_ext", "kaggle.team_statistics_extended"),
        ("player_ext", "kaggle.player_statistics_extended"),
        ("pbp", "kaggle.play_by_play"),
    ]
    parts = []
    for label, view in tables:
        parts.append(
            f"""select '{label}' as t, cv_season(gameId) as season,
                count(distinct gameId) filter (where cv_season_type(gameId) = 'regular') as reg,
                count(distinct gameId) filter (where cv_season_type(gameId) = 'playoffs') as po
                from {view} group by all"""
        )
    df = con.sql(" union all ".join(parts)).df()
    wide = df.pivot_table(index="season", columns="t", values=["reg", "po"], aggfunc="sum")
    out = pd.DataFrame({"season": [season_label(s) for s in wide.index]})
    for label, _ in tables:
        out[f"{label} (reg)"] = wide[("reg", label)].fillna(0).astype(int).values
    out["games (playoffs)"] = wide[("po", "games")].fillna(0).astype(int).values
    out["pbp (playoffs)"] = wide[("po", "pbp")].fillna(0).astype(int).values
    return out


def stat_availability(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per stat: first season in which >=50% / >=95% of regular-season team-games
    have a non-zero value, from player box scores (summed) and team box scores.
    Returns (summary, per-season detail for player box scores)."""
    sums = ", ".join(f"sum({q(src)}) as {name}" for name, src in BOX_STATS)
    con.execute(
        f"""create or replace temp table _pg as
            select gameId, home, cv_season(gameId) as season, {sums}
            from kaggle.player_statistics where cv_season_type(gameId) = 'regular'
            group by all"""
    )
    team_cols = {r[0] for r in con.execute("describe kaggle.team_statistics").fetchall()}
    pct = ", ".join(f"round(100 * avg(({name} <> 0)::int), 1) as {name}" for name, _ in BOX_STATS)
    player = con.sql(f"select season, {pct} from _pg group by season order by season").df()
    tpct = ", ".join(
        f"round(100 * avg(({q(src)} <> 0)::int), 1) as {name}"
        for name, src in BOX_STATS
        if src in team_cols
    )
    team = con.sql(
        f"""select cv_season(gameId) as season, {tpct} from kaggle.team_statistics
            where cv_season_type(gameId) = 'regular' group by 1 order by 1"""
    ).df()

    def first(df: pd.DataFrame, col: str, thr: float) -> str:
        if col not in df:
            return "n/a"
        hit = df.loc[df[col] >= thr, "season"]
        if hit.empty:
            return "never"
        # first season from which the threshold holds in every later season
        seasons = df["season"].tolist()
        ok = (df[col] >= thr).tolist()
        for i in range(len(seasons)):
            if all(ok[i:]):
                return season_label(seasons[i])
        return "never (sustained)"

    recs = []
    for name, src in BOX_STATS:
        recs.append(
            {
                "stat": name,
                "source column": src,
                "player box ≥50%": first(player, name, 50),
                "player box ≥95%": first(player, name, 95),
                "team box ≥50%": first(team, name, 50),
                "team box ≥95%": first(team, name, 95),
            }
        )
    detail = player.copy()
    detail["season"] = detail["season"].map(season_label)
    return pd.DataFrame(recs), detail


def integrity_checks(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str]]:
    """(check, result, sql) triples. Results are short strings."""
    checks: list[tuple[str, str]] = [
        ("Duplicate `games.gameId`",
         "select count(*) - count(distinct gameId) from kaggle.games"),
        ("Duplicate (gameId, personId) in player_statistics",
         "select count(*) - count(distinct (gameId, personId)) from kaggle.player_statistics"),
        ("Duplicate (gameId, teamId) in team_statistics",
         "select count(*) - count(distinct (gameId, teamId)) from kaggle.team_statistics"),
        ("Duplicate (gameId, personId) in player_statistics_extended",
         "select count(*) - count(distinct (gameId, personId)) "
         "from kaggle.player_statistics_extended"),
        ("Duplicate (gameId, teamId) in team_statistics_extended",
         "select count(*) - count(distinct (gameId, teamId)) from kaggle.team_statistics_extended"),
        ("Duplicate (gameId, actionNumber) in play_by_play",
         "select count(*) - count(distinct (gameId, actionNumber)) from kaggle.play_by_play"),
        ("Duplicate `players.personId`",
         "select count(*) - count(distinct personId) from kaggle.players"),
        ("Games without exactly 2 team_statistics rows",
         "select count(*) from (select g.gameId, count(t.teamId) n from kaggle.games g "
         "left join kaggle.team_statistics t using (gameId) group by 1) where n <> 2"),
        ("player_statistics rows whose personId is not in players",
         "select count(*) from kaggle.player_statistics p "
         "anti join kaggle.players using (personId)"),
        ("Distinct personIds in player_statistics missing from players",
         "select count(distinct personId) from kaggle.player_statistics p "
         "anti join kaggle.players using (personId)"),
        ("player_statistics rows with NULL playerteamId (repaired in stg)",
         "select count(*) from kaggle.player_statistics where playerteamId is null"),
        ("…of which unrepairable (no home flag or game)",
         "select count(*) from stg.player_games where team_id is null"),
        ("Regular-season team-games where team score ≠ sum of player points",
         "select count(*) from kaggle.team_statistics t join (select gameId, home, "
         "sum(points) pts from kaggle.player_statistics "
         "group by all) p on p.gameId = t.gameId and p.home = t.home "
         "where cv_season_type(t.gameId) = 'regular' and t.teamScore <> p.pts"),
        ("PBP gameIds not in games",
         "select count(distinct gameId) from kaggle.play_by_play p "
         "where cast(gameId as bigint) not in (select gameId from kaggle.games)"),
        ("Regular-season games (1996-97+) with no PBP",
         "select count(*) from kaggle.games where cv_season_type(gameId) = 'regular' "
         "and cv_season(gameId) >= 1996 and gameId not in "
         "(select distinct cast(gameId as bigint) from kaggle.play_by_play)"),
        ("Players with no position flag (guard/forward/center all 0)",
         "select count(*) from kaggle.players where coalesce(guard,0) + coalesce(forward,0) "
         "+ coalesce(center,0) = 0"),
        ("…of those, players who appear in a 1996-97+ box score",
         "select count(distinct p.personId) from kaggle.players p join kaggle.player_statistics "
         "s using (personId) where coalesce(guard,0) + coalesce(forward,0) "
         "+ coalesce(center,0) = 0 and cv_season(s.gameId) >= 1996"),
    ]  # fmt: skip
    out = []
    for name, sql in checks:
        try:
            v = con.execute(sql).fetchall()[0][0]
            res = f"{v:,}" if isinstance(v, int) else str(v)
        except duckdb.Error as e:
            res = f"error: {str(e).splitlines()[0]}"
        out.append((name, res, sql))
    return out


def pbp_shot_quality(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Per season: FGA rows in PBP vs box-score FGA, and coordinate sanity."""
    return con.sql(
        """with flag as (
               select cv_season(gameId) season, count(*) n from kaggle.play_by_play
               where cv_season_type(gameId) = 'regular' and isFieldGoal group by 1),
           box as (select season, sum(fga) fga from stg.team_games
                   where season_type = 'regular' group by 1)
           select s.season,
                  any_value(f.n)::bigint as "isFieldGoal rows",
                  count(*) as "stg.shots rows",
                  any_value(b.fga)::bigint as box_fga,
                  round(100.0 * count(*) / any_value(b.fga), 2) as "shots/box %",
                  round(100 * avg((x_legacy is not null)::int), 1) as "has xy %",
                  round(100 * avg((x_legacy = 0 and y_legacy = 0)::int), 1) as "at (0,0) %",
                  round(100 * avg((x_legacy = 0 and y_legacy = 0
                        and (sub_type ilike '%jump%' or shot_value = 3))::int), 2)
                        as "jumper at (0,0) %",
                  round(100 * avg((area is not null)::int), 1) as "area %",
                  round(100 * avg((shot_value = 3)::int), 1) as "3PA %",
                  round(avg(shot_distance), 1) as "avg dist ft",
                  round(100 * avg(team_id_repaired::int), 1) as "team repaired %"
           from stg.shots s left join box b using (season) left join flag f using (season)
           where s.season_type = 'regular'
           group by s.season order by s.season"""
    ).df()


# --------------------------------------------------------------- nba_api joins


def nba_api_join_report(con: duckdb.DuckDBPyConnection, season: int) -> str:
    """Markdown report: how nba_api tables for ``season`` join to the Kaggle tables."""
    s = int(season)
    out = [f"# nba_api ↔ Kaggle joins, {season_label(s)}", ""]

    def tbl(sql: str) -> str:
        return md_table(con.sql(sql).df(), align_right=True)

    out += ["## Rows loaded", ""]
    out.append(
        tbl(
            " union all ".join(
                f"select 'nba_api.{t}' as table, count(*) as rows, "
                f"count(distinct game_id) as games from nba_api.{t} where season = {s}"
                for t in ("game_log", "shots", "pbp", "rotations")
            )
        )
    )
    out += ["", "## Games: nba_api `leaguegamelog` vs Kaggle `games`", ""]
    out.append(
        tbl(
            f"""with a as (select distinct game_id, season_type from nba_api.game_log
                           where season = {s}),
                     k as (select game_id, season_type from stg.games where season = {s}
                           and season_type in ('regular', 'play_in', 'playoffs'))
                select coalesce(a.season_type, k.season_type) as season_type,
                       count(*) filter (where a.game_id is not null and k.game_id is not null)
                           as in_both,
                       count(*) filter (where k.game_id is null) as nba_api_only,
                       count(*) filter (where a.game_id is null) as kaggle_only
                from a full join k using (game_id) group by 1 order by 1"""
        )
    )
    out += ["", "## Team and player ids", ""]
    parts = []
    for t in ("game_log", "shots", "pbp", "rotations"):
        parts.append(
            f"""select 'nba_api.{t}' as table,
                count(distinct team_id) filter (where team_id is not null) as teams,
                count(distinct team_id) filter (where team_id is not null and team_id not in
                    (select teamId from kaggle.team_histories)) as teams_not_in_kaggle"""
            + (
                ""
                if t == "game_log"
                else """,
                count(distinct person_id) as persons,
                count(distinct person_id) filter (where person_id not in
                    (select person_id from stg.players)) as persons_not_in_players,
                round(100 * avg((exists (select 1 from stg.player_games pg
                    where pg.game_id = x.game_id and pg.person_id = x.person_id))::int), 2)
                    as "row's (game, person) in player_games %" """
            )
            + f" from nba_api.{t} x where season = {s}"
            + ("" if t == "game_log" else " and person_id is not null")
        )
    df = pd.concat([con.sql(p).df() for p in parts], ignore_index=True)
    out.append(md_table(df))

    out += [
        "",
        "## Shots: nba_api `shotchartdetail` vs Kaggle `stg.shots`",
        "",
        "Restricted to games present in both sources. Kaggle's 2019-20+ play-by-play is the "
        "live-feed schema, whose `actionNumber` is not the stats-API event number, so shots "
        "are matched on (game, shooter, period, game clock to the second) rather than on the "
        "event id.",
        "",
    ]
    con.execute(
        f"""create or replace temp table _api_shots as
            select game_id, season_type, person_id, PERIOD as period,
                   MINUTES_REMAINING * 60 + SECONDS_REMAINING as sec_left,
                   LOC_X as x, LOC_Y as y, SHOT_MADE_FLAG = 1 as made,
                   case when SHOT_TYPE ilike '3PT%' then 3 else 2 end as shot_value,
                   SHOT_ZONE_BASIC as zone_basic, SHOT_ZONE_AREA as zone_area,
                   SHOT_DISTANCE as dist, GAME_EVENT_ID as event_id
            from nba_api.shots where season = {s}"""
    )
    con.execute(
        f"""create or replace temp table _kg_shots as
            select game_id, person_id, period,
                   floor(coalesce(
                     try_cast(regexp_extract(clock, 'PT(\\d+)M', 1) as int) * 60
                     + try_cast(regexp_extract(clock, 'M(\\d+)', 1) as double), -1))::int
                       as sec_left,
                   x_legacy as x, y_legacy as y, made, shot_value, action_number
            from stg.shots where season = {s}
              and game_id in (select distinct game_id from _api_shots)"""
    )
    out.append(
        tbl(
            """with m as (
                 select a.*, k.x kx, k.y ky, k.made kmade, k.shot_value kval,
                        row_number() over (partition by a.game_id, a.person_id, a.period,
                            a.sec_left, a.event_id order by abs(coalesce(k.x - a.x, 999))
                            + abs(coalesce(k.y - a.y, 999))) rn
                 from _api_shots a left join _kg_shots k
                   on k.game_id = a.game_id and k.person_id = a.person_id
                  and k.period = a.period and abs(k.sec_left - a.sec_left) <= 1)
               select season_type,
                      count(*) as api_shots,
                      (select count(*) from _kg_shots k join (select distinct game_id,
                          season_type st from _api_shots) g on g.game_id = k.game_id
                          where g.st = m.season_type) as kaggle_shots,
                      round(100 * avg((kx is not null or kmade is not null)::int), 2)
                          as "matched %",
                      round(100 * avg((kx = x and ky = y)::int)
                          / nullif(avg((kmade is not null)::int), 0), 2) as "same x,y %",
                      round(100 * avg((abs(kx - x) <= 10 and abs(ky - y) <= 10)::int)
                          / nullif(avg((kmade is not null)::int), 0), 2) as "x,y within 1 ft %",
                      round(100 * avg((kmade = made)::int)
                          / nullif(avg((kmade is not null)::int), 0), 2) as "same made %",
                      round(100 * avg((kval = shot_value)::int)
                          / nullif(avg((kmade is not null)::int), 0), 2) as "same 2/3 %"
               from m where rn = 1 group by season_type order by season_type"""
        )
    )
    out += [
        "",
        "## Official shot zones (`SHOT_ZONE_BASIC`) vs coordinates",
        "",
        "Ground truth for the zone geometry in docs/metrics_spec.md. Distances in feet, "
        "coordinates in tenths of a foot from the rim.",
        "",
    ]
    out.append(
        tbl(
            """select zone_basic, count(*) as fga, round(100.0 * count(*) / sum(count(*)) over (),
                      1) as "share %", round(avg(made::int) * 100, 1) as "FG %",
                      min(dist) as min_ft, max(dist) as max_ft,
                      min(abs(x)) as min_abs_x, max(abs(x)) as max_abs_x,
                      min(y) as min_y, max(y) as max_y
               from _api_shots group by 1 order by fga desc"""
        )
    )
    out += ["", "## Rotations: stint minutes vs box-score minutes", ""]
    out.append(
        tbl(
            f"""with r as (select game_id, person_id,
                         sum(out_seconds - in_seconds) / 60.0 as stint_min
                         from nba_api.rotations where season = {s} group by all),
                    j as (select r.*, pg.minutes as box_min from r
                          left join stg.player_games pg using (game_id, person_id))
               select count(*) as player_games,
                      round(100 * avg((box_min is not null)::int), 2) as "found in box %",
                      round(100 * avg((abs(stint_min - box_min) <= 0.5)::int)
                          / avg((box_min is not null)::int), 2) as "within 0.5 min %",
                      round(100 * avg((abs(stint_min - box_min) <= 1.0)::int)
                          / avg((box_min is not null)::int), 2) as "within 1 min %",
                      round(avg(abs(stint_min - box_min)), 3) as "mean abs diff (min)"
               from j"""
        )
    )
    out += ["", "## Team box scores: nba_api `leaguegamelog` vs Kaggle `team_statistics`", ""]
    out.append(
        tbl(
            f"""select count(*) as team_games,
                   round(100 * avg((t.team_id is not null)::int), 2) as "found %",
                   round(100 * avg((g.PTS = t.points)::int), 2) as "same PTS %",
                   round(100 * avg((g.FGA = t.fga)::int), 2) as "same FGA %",
                   round(100 * avg((g.TOV = t.tov)::int), 2) as "same TOV %",
                   round(100 * avg((g.TOV = t.tov + t.team_tov)::int), 2)
                       as "TOV = Kaggle tov + team_tov %",
                   round(100 * avg((g.OREB = t.oreb)::int), 2) as "same OREB %",
                   round(100 * avg((g.STL = t.stl)::int), 2) as "same STL %"
               from nba_api.game_log g left join stg.team_games t
                 on t.game_id = g.game_id and t.team_id = g.team_id
               where g.season = {s}"""
        )
    )
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------- top level


def audit_kaggle(con: duckdb.DuckDBPyConnection, out_dir: Path, raw_dir: Path) -> dict:
    from courtvision.ingest.kaggle_load import table_name

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    manifest = raw_dir / "_manifest.json"
    dl = ""
    if manifest.exists():
        import json

        dl = json.loads(manifest.read_text())["downloaded_at_utc"]
    header = (
        f"> Generated by `courtvision audit kaggle` at {stamp} from the Kaggle download of "
        f"{dl or 'unknown date'}. Do not edit by hand.\n"
    )
    summary = {}
    files = sorted(p.name for p in raw_dir.iterdir() if p.suffix in (".csv", ".parquet"))
    for f in files:
        view = f"kaggle.{table_name(f)}"
        p = profile_table(con, view)
        write_md(out_dir / f"{table_name(f)}.md", header + "\n" + render_profile(p, f))
        summary[view] = {"rows": p.rows, "cols": len(p.columns), "seasons": p.season_range}

    cov = coverage_by_season(con)
    write_md(
        out_dir / "_coverage.md",
        header + "\n# Games per season, by table\n\nDistinct `gameId`s per season. "
        "`(reg)` = regular season (`gameId` type digit 2, which includes NBA Cup "
        "group/knockout games but not the Cup final).\n\n" + md_table(cov) + "\n",
    )
    avail, detail = stat_availability(con)
    write_md(
        out_dir / "_stat_availability.md",
        header + "\n# When each box-score stat starts being recorded\n\n"
        "A stat counts as recorded for a team-game when its value is non-zero (summing player "
        "rows for the player box score). The season shown is the first from which the share "
        "of regular-season team-games stays at or above the threshold in **every** later "
        "season.\n\n" + md_table(avail) + "\n\n## Player box scores: % of team-games with a "
        "non-zero value, per season\n\n" + md_table(detail) + "\n",
    )
    checks = integrity_checks(con)
    chk = pd.DataFrame([(n, r) for n, r, _ in checks], columns=["check", "result"])
    sqls = "\n\n".join(f"**{n}**\n\n```sql\n{s}\n```" for n, _, s in checks)
    write_md(
        out_dir / "_checks.md",
        header + "\n# Integrity checks\n\n" + md_table(chk, align_right=False)
        + "\n\n## SQL\n\n" + sqls + "\n",
    )  # fmt: skip
    shots = pbp_shot_quality(con)
    shots["season"] = shots["season"].map(season_label)
    write_md(
        out_dir / "_pbp_shots.md",
        header + "\n# Play-by-play shots vs box-score FGA (regular season)\n\n"
        "`isFieldGoal rows` counts the raw flag; `stg.shots rows` identifies shots by "
        "`actionType` across both PBP schemas (the flag is NULL in 323 games of 2019-20). "
        "`at (0,0) %` = shots logged exactly at the rim (almost all layups/dunks before "
        "2010-11); `jumper at (0,0) %` = jump shots or 3s at the rim, i.e. a truly missing "
        "location. Coordinates are `xLegacy`/`yLegacy`, tenths of a foot from the rim.\n\n"
        + md_table(shots) + "\n",
    )  # fmt: skip
    return {"tables": summary, "checks": {n: r for n, r, _ in checks}}
