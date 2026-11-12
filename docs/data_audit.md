# Data audit

What the two sources actually contain, how complete they are by era, and the
defects the pipeline has to handle. Written after inspecting the data, not
from the dataset descriptions.

Every number here comes from generated appendices that are rebuilt by one
command each (see [Reproduce](#reproduce)):

| Appendix | Contents |
|---|---|
| [`audit/kaggle/<table>.md`](audit/kaggle/) | One per file: columns, types, null % overall and by era, first/last season each column is populated, zero % by era |
| [`audit/kaggle/_coverage.md`](audit/kaggle/_coverage.md) | Games per season in every game-keyed file |
| [`audit/kaggle/_stat_availability.md`](audit/kaggle/_stat_availability.md) | First season each box-score stat is recorded, per season detail |
| [`audit/kaggle/_pbp_shots.md`](audit/kaggle/_pbp_shots.md) | Play-by-play shots vs box-score FGA, coordinate quality, per season |
| [`audit/kaggle/_checks.md`](audit/kaggle/_checks.md) | Key uniqueness, referential integrity, reconciliation (with the SQL) |
| [`audit/nba_api_2025.md`](audit/nba_api_2025.md) | nba_api 2025-26 pull and how it joins to Kaggle |

Snapshot audited: Kaggle download of 2026-11-12 00:47 UTC (games through
2026-11-10, i.e. ~160 games into 2026-27); nba_api pulled 2026-11-12.

---

## TL;DR: what the data can support

| Capability | Earliest season | Source | Caveat |
|---|---|---|---|
| Game results, scores | 1946-47 | Kaggle box | Only ~5–80% of games exist before **1954-55** |
| FGM, FTM, points | 1946-47 | Kaggle player box | |
| FGA | 1974-75 (≥95% of games); 1958-59 (≥50%) | Kaggle player box | Team box FGA is 0 until 1982-83: sum player rows instead |
| AST, PF | 1974-75 (≥95%) | Kaggle player box | |
| STL, BLK | 1983-84 (≥95%); 1974-75 (≥50%) | Kaggle player box | The NBA recorded these from 1973-74; this dataset is incomplete until 1983-84 |
| TOV | 1983-84 (≥95%); 1977-78 (≥50%) | Kaggle player box | Team TOV excludes team turnovers (~0.7/game) |
| OREB / DREB | 1983-84 (≥95%); 1982-83 (≥50%) | Kaggle player box | NBA recorded them from 1973-74; missing here before 1982-83 |
| 3PA | 1986-87 (≥95%); 1979-80 (≥50%) | Kaggle player box | Zero-share before 1986-87 mixes real zeros and unrecorded games |
| Box-score plus/minus | 1996-97 | Kaggle player box | |
| Possessions, pace, ratings (precomputed) | 1996-97 | Kaggle `*Extended` | 100% of regular-season games |
| **Play-by-play** | **1996-97** | Kaggle `PlayByPlay.parquet` | Two schemas; see below |
| **Shot coordinates → court zones** | **1996-97** | Kaggle PBP | Before 2010-11 almost every rim attempt is logged at exactly (0, 0) |
| Shot coordinates matching official shot charts | verified 2025-26 | nba_api `shotchartdetail` | 100% shot match, 99.9% identical x/y |
| Lineups / stints | 1996-97 (derived from PBP subs) | Kaggle PBP | Legacy schema names the incoming player only as a last name in text |
| Exact stints | 2025-26 (pulled) | nba_api `gamerotation` | Matches box minutes 100% within 0.5 min |
| Player position (G/F/C) | all eras | Kaggle `Players.csv` | Career-level flags, not per season; ~10–35 multi-flag players per debut year |
| Possession owner per event | 2019-20 | Kaggle PBP (live schema) | |
| Official shot-zone labels | 2025-26 (pulled) | nba_api `shotchartdetail` | Used to validate our zone geometry |

"≥95%" means the stat is non-zero in at least 95% of regular-season team-games
in that season **and every later season**. Zero is the test, not NULL: see the
next section.

## The most important finding: missing stats are stored as 0, not NULL

In both box-score files, a stat that was not recorded appears as **0**. Null
rates are ~0% in every era, which looks complete but isn't. Example from
`TeamStatistics.csv` (regular season):

| Season | steals = 0 | turnovers = 0 | 3PA = 0 | assists = 0 |
|---|---:|---:|---:|---:|
| 1980-81 | 100% | 100% | 100% | 100% |
| 1984-85 | 94% | 92% | 92% | 0% |
| 1985-86 | 0% | 0% | 10% | 0% |

So availability is measured as the share of team-games with a **non-zero**
value ([`_stat_availability.md`](audit/kaggle/_stat_availability.md)). The
test can understate rare events: a team genuinely making zero 3s in 1983 looks
the same as an unrecorded game.

## Player box scores are more complete than team box scores

Team-level stats before the mid-1980s are mostly empty, but player rows summed
to the team-game recover far more:

| Stat | Player box ≥50% | Player box ≥95% | Team box ≥95% |
|---|---|---|---|
| FGA | 1958-59 | 1974-75 | 1982-83 |
| Total rebounds | 1955-56 | 1968-69 | 1982-83 |
| Assists | 1962-63 | 1974-75 | 1982-83 |
| Steals / blocks | 1974-75 | 1983-84 | 1985-86 |
| Turnovers | 1977-78 | 1983-84 | 1985-86 |
| 3PA | 1979-80 | 1986-87 | 1986-87 |

**Rule for the pipeline:** team-game totals come from summing player rows, not
from `TeamStatistics.csv`, for every season before 1986-87. Points still
reconcile: team score equals the sum of player points in 99.5% of
regular-season team-games overall (708 of 133,990 differ, 96% of them before
1994; see `_checks.md`).

Separately, the actual NBA started recording steals, blocks, and offensive/defensive
rebounds in 1973-74 and turnovers in 1977-78. The dataset lags that for OREB/DREB
(1982-83) and has steals/blocks in only 28–90% of team-games between 1973-74
and 1982-83. The gap is in this source, not in history.

## Coverage by era

Regular-season games per season ([`_coverage.md`](audit/kaggle/_coverage.md)):

| Era | Games present | Notes |
|---|---|---|
| 1946-47 → 1953-54 | 18 → 252 per season | **Incomplete.** e.g. 1946-47 has 18 of ~331 BAA games |
| 1954-55 → 1995-96 | complete | Counts match the real schedule (8 teams × 72 = 288 in 1954-55 … 29 × 82 / 2 = 1,189 in 1995-96) |
| 1996-97 → 2025-26 | complete, incl. lockouts (725 in 1998-99, 990 in 2011-12), 1,229 in 2012-13 (cancelled BOS–IND game), COVID seasons (1,059 / 1,080) | Extended files and PBP cover the same games |
| 2026-27 | 160 of 1,230 so far | In progress; refreshed nightly upstream |

Gaps worth knowing:
* `Games.csv` is **missing 4 regular-season 2024-25 games** (`0022400532`,
  `…537`, `…538`, `…617`) that do exist in PBP.
* PBP is missing 1 regular-season game in each of 2006-07 and 2007-08.
* `PlayerStatistics.csv` has no rows for 2 regular-season games in 2024-25.

## How seasons and game types are identified

No file has a season column. NBA game ids encode it:
`00` + **type digit** + **2-digit season start year** + 5-digit game number
(`0022500001` = 2025-26 regular season, game 1). Kaggle stores ids as
integers, so the leading `00` is lost (`22500001`); staging re-pads to the
10-character string used by nba_api.

| Type digit | Meaning | In Kaggle `Games.csv` |
|---|---|---|
| 1 | Preseason | 1,940 (from 2003-04; includes games vs EuroLeague/NBL clubs with team ids `9xxx`) |
| 2 | Regular season (**includes NBA Cup group + knockout games**) | 66,994 |
| 3 | All-Star | 7 |
| 4 | Playoffs | 4,526 |
| 5 | Play-in | 37 (from 2019-20) |
| 6 | NBA Cup final (does not count in standings or stats) | 3 |

The free-text `gameType` label disagrees with the id in places (e.g. 66 Cup
games labelled "NBA Emirates Cup", one Cup final labelled "Regular Season"),
so the pipeline always uses the id.

## Play-by-play: two schemas in one file

`PlayByPlay.parquet` (18.9M rows, 89 columns, 1996-97 → today) is two
different feeds concatenated:

| | Legacy schema | Live-feed schema |
|---|---|---|
| Seasons | 1996-97 → part of 2019-20 | part of 2019-20 → today |
| Shot rows | `actionType` = `Made Shot` / `Missed Shot` (some with trailing spaces) | `actionType` = `2pt` / `3pt` |
| Shot value | `shotValue` | implied by `actionType` (`shotValue` is NULL) |
| Coordinates | `xLegacy`, `yLegacy` | `xLegacy`, `yLegacy` **and** `x`, `y` (0–100 court %) |
| Named zone | none | `area`, `areaDetail` — **only 2022-23 → 2025-26** |
| Possession owner | none | `possession` |
| Substitutions | one row; `personId` = player **out**; player in only in text: `SUB: Sefolosha FOR Bazemore` | two rows, `subType` `in` / `out`, each with `personId` |
| `teamId` | present | **NULL for all of 2026-27** |

Defects found and handled in `stg.shots`:

* `isFieldGoal` is NULL in **323 live-schema games of 2019-20**, so filtering
  on it keeps only 69.5% of that season's FGA. Identifying shots by
  `actionType` instead recovers 99.5% of box-score FGA.
* 12.7k of those 2019-20 shots (6.8%) have a `: Made` / `: Missed` result
  and **no coordinates**, so 2019-20 zone metrics cover 93% of its shots.
  Every other season has coordinates for ≥ 99.9% of shots.
* With shots identified by `actionType`, PBP FGA matches box-score FGA to
  within 0.1% in **every season** 1996-97 → 2026-27
  ([`_pbp_shots.md`](audit/kaggle/_pbp_shots.md)).

### Shot coordinates

`xLegacy` / `yLegacy` are in tenths of a foot with the rim at (0, 0) and +y
toward half court. They are populated for 100% of shots in every season except
2019-20 (93.2%).

From 1996-97 through 2009-10, **22–36% of shots sit at exactly (0, 0)**; from
2010-11 on, 0% (2–3% from 2020-21, after the format change). Checking what those
shots are:

| Seasons | Shots at (0, 0) | …that are layups/dunks/tips | …that are jumpers or 3s |
|---|---:|---:|---:|
| 1996-97 → 2009-10 | 664,484 | 99.3% | 0.5% |
| 2010-11 → 2018-19 | 164 | 99.4% | 0.6% |

So (0, 0) means "logged at the rim", not "location missing". Zone
classification (restricted area vs paint vs mid-range vs 3) works from 1996-97.
What does **not** exist before 2010-11 is any location detail *within* the
restricted area. Jumpers at (0, 0), the truly missing locations, are ≤0.35%
per season.

## Positions

`Players.csv` has `guard`, `forward`, `center` 0/1 flags for 6,820 players.

* They are **career-level**, not per season, so a player who moved from forward
  to center is one or the other forever.
* Multi-flag players (G+F or F+C) are rare before 1984 (0–1 per debut year)
  and common since (3–37 per debut year, e.g. 37 of 117 players debuting in
  2019). Backcourt/Frontcourt need a rule for them (see metrics spec).
* 1,590 players have no flag. 1,211 of them appear in a 1996-97+ box score,
  but they account for **≤0.45% of minutes in any season**: mostly
  two-way/G-League call-ups and players with no `fromYear`.
* `PlayerStatistics.startingPosition` (G / F / C, starters only) exists from
  1996-97. In 2026-27 it switched to PG/SG/SF/PF for a handful of rows: a
  schema change in the current season.

## Defects and how staging handles them

| Defect | Scope | Handling |
|---|---|---|
| Untracked stats stored as 0 | all box stats, by era | Availability measured by non-zero share; metrics gated by era (see spec) |
| `PlayerStatistics.playerteamId` NULL | 99.9% of 2021-22, 50.2% of 2000-01, <1% of ~12 other seasons (109,299 rows) | Recovered from `home` flag + game's home/away ids (agree 100% where both exist); `stg.player_games.team_id_repaired`. 3,534 preseason/All-Star rows stay unrepaired (no game row) |
| Team `turnovers` excludes team turnovers | all seasons | Official TOV = `turnovers + turnoversTeam` (verified 99.96% vs nba_api for 2025-26) |
| Placeholder `teamId = 0` rows | 2 rows, games `0022500651`, `0022500652` (postponed from 2026-01-25) | Dropped in `stg.team_games` |
| `isFieldGoal` NULL | 323 games, 2019-20 | `stg.shots` keys on `actionType` |
| Padded `actionType` strings | ~4.8k legacy rows | `trim()` |
| PBP `teamId` NULL | all of 2026-27 | `stg.shots` takes the shooter's team from the box score (`team_id_repaired`) |
| `gameType` label vs id disagree | ~70 Cup games | Use the id |
| PBP column `gameType` has type `NULL` | entire column | Ignored |
| Missing games | 4 (2024-25) in Games; 2 in PBP; 2 in PlayerStatistics | Reported; metrics use whatever source has the game |
| 149 personIds in box scores not in `Players.csv` | 234 rows | Reported; no position for them |

No duplicate keys anywhere: `games.gameId`, `(gameId, personId)` in both
player files, `(gameId, teamId)` in both team files, `(gameId, actionNumber)`
in PBP, `players.personId`.

## Per-file notes

| File | Rows × cols | Grain | Seasons | Notes |
|---|---|---|---|---|
| `Games.csv` | 73,507 × 23 | game | 1946-47 → 2026-27 | `attendance`, `arenaName`, `officials` only from 2024-25 (97.8% null overall) |
| `TeamStatistics.csv` | 147,016 × 59 | team-game | 1946-47 → 2026-27 | Pre-1985 stats mostly 0 (see above). Quarter scores, bench/paint/fast-break points NULL for all of 2021-22 and half of 2000-01 |
| `TeamStatisticsExtended.csv` | 80,180 × 104 | team-game | 1996-97 → | Possessions, pace, off/def/net rating, Four-Factor %s, opponent factors; 100% populated. `personalFoulsDrawn` only 69% non-zero before 2019-20 |
| `PlayerStatistics.csv` | 1,677,321 × 40 | player-game | 1946-47 → | **The** historical backbone. `numMinutes` is text (e.g. `'24.0'`); DNPs are `'0.0'` or NULL |
| `PlayerStatisticsExtended.csv` | 844,433 × 110 | player-game | 1996-97 → | Usage, ratings, %-of-team shares |
| `Players.csv` | 6,820 × 20 | player | debuts 1946 → 2026 | Height/weight 24% null overall (12% for 1996–2018 debuts); position flags 0.7% null |
| `TeamHistories.csv` | 140 × 7 | team × franchise era | | Includes BAA/NBL and 9xxx international clubs (EuroLeague, CBA, NBB…) that appear in preseason exhibitions |
| `PlayByPlay.parquet` | 18,864,106 × 89 | event | 1996-97 → | See the play-by-play section. Preseason only from 2013-14 |
| `LeagueSchedule24_25.csv` / `25_26` / `26_27` | 1,408 / 1,399 / 1,274 × 15–17 | scheduled game | one season each | Column names differ between files (`hometeamId` vs `homeTeamId`). Future games of 2026-27 included |

## nba_api pull (2025-26) and joins

`courtvision ingest nba-api --season 2025` pulled the regular season, play-in,
and playoffs: `leaguegamelog` (3 requests), `shotchartdetail` per team (90),
and `playbyplayv3` + `gamerotation` per game (2 × 1,321). Every raw response is
cached gzipped under `data/raw/nba_api/` with its exact parameters.

**Trap found:** the league-wide `shotchartdetail` query (`TeamID=0`) silently
returns exactly **102,400 rows**, 47% of the season. The pull therefore always
goes team by team. Per-team totals: 219,160 regular-season FGA vs 219,159 in
the box scores.

Joins to Kaggle ([`audit/nba_api_2025.md`](audit/nba_api_2025.md)):

<!-- NBA_API_JOIN_SUMMARY -->

## Reproduce

```bash
uv run courtvision ingest kaggle-download      # ~1.1 GB zip; writes data/raw/kaggle/_manifest.json
uv run courtvision ingest kaggle-load          # Parquet + kaggle.* and stg.* views (~45 s)
uv run courtvision audit kaggle                # docs/audit/kaggle/*.md (~3 min)
uv run courtvision ingest nba-api --season 2025   # ~1.5 h on a cold cache; seconds when cached
uv run courtvision audit nba-api --season 2025    # docs/audit/nba_api_2025.md
```

The Kaggle dataset is republished nightly, so counts for the current season
move. `--version N` on `kaggle-download` pins a release.
