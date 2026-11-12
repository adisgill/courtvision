# courtvision

Rate and visualize NBA teams, games, and players on a common **0–100 scale**
across six dimensions:

| # | Dimension    | Question it answers |
|---|--------------|---------------------|
| 1 | Shooting     | Are they getting good shots, and making more than they should, by court zone? |
| 2 | Defense      | What shot quality and shot making do they allow by zone; do they force turnovers without fouling? |
| 3 | Ball control | How often do possessions end in a shot rather than a turnover? |
| 4 | Backcourt    | Dimensions 1–3 restricted to guards and perimeter zones |
| 5 | Frontcourt   | Dimensions 1–3 restricted to forwards/centers and the paint |
| 6 | Presence     | How many points per 100 possessions does a player add, adjusted for who he plays with and against? |

Ratings exist at three levels — player → game → team/season — and are always
**era-adjusted**: every number is rated against league average for its own
season, never compared across eras raw. Team ratings also come **opponent-adjusted**.

On top of the ratings, a **player market** prices every player (market comparables:
his rank in value priced at the same rank of veteran salaries), keeps every contract
through 2030-31 with its options, reads each team's payroll against the cap, tax and
aprons, finds where each team is short, lists the players who would fix it (trades,
unsigned players, next summer's free agents) with cost and risk, and projects every
team's cap space next summer. **Forecasts** predict each team's next game: dimension
ratings, every expected player, the margin and the win chance.

> Status: **v1.3** for every season from 1974-75 (box-score tiers) and 1996-97
> (play-by-play), with a validation report, a visualizer, a live mode, the market and
> next-game forecasts.
>
> * [`docs/metrics_spec.md`](docs/metrics_spec.md): formulas, scaling, roll-ups, tiers, acceptance tests
> * [`docs/validation.md`](docs/validation.md): stabilization, persistence, signal beyond Four Factors
> * [`docs/data_audit.md`](docs/data_audit.md): what's actually in the data, by era

## Data sources

1. **Kaggle** — [`eoinamoore/historical-nba-data-and-player-box-scores`](https://www.kaggle.com/datasets/eoinamoore/historical-nba-data-and-player-box-scores):
   box scores back to 1947 and play-by-play with shot coordinates from 1996-97,
   updated nightly. Every rating is built from it.
2. **[nba_api](https://github.com/swar/nba_api)** — official shot charts, play-by-play
   and rotations (used to validate the Kaggle data and the lineup reconstruction), and
   the live feed for live mode. Requests are rate-limited and every raw response is
   cached to disk.
3. **Kaggle** — [`mrbridge/nba-salary-cap-contracts-2016-2026`](https://www.kaggle.com/datasets/mrbridge/nba-salary-cap-contracts-2016-2026)
   (attribution: MrBridge, mr-bridge.com; research and educational use): player
   salaries (a HoopsHype snapshot of 2026-07-07), payrolls, and cap / tax / apron
   levels. Used only by the market model.
4. **Kaggle** — [`deocheng/nba-data-1946-2026`](https://www.kaggle.com/datasets/deocheng/nba-data-1946-2026)
   (Apache-2.0 on Kaggle; scraped from Basketball-Reference, 17–23 June 2026): only
   `public.player_contracts`, every contract's future years and options.
5. **[Basketball-Reference](https://www.basketball-reference.com/contracts/)** team contract
   pages, fetched on request (`courtvision ingest salaries --fetch-current`: 30 pages,
   4 s apart, cached per day, kept on your machine): today's contracts, the newest source.
6. **You** — optional `data/salary_overrides.csv` to correct or add any deal
   (format: [`docs/salary_overrides.example.csv`](docs/salary_overrides.example.csv)).

No player-tracking data is used; none is public.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). [`just`](https://just.systems) is
optional — every recipe is a one-line `uv run` command you can type yourself.

```bash
uv sync
cp .env.example .env                              # then put your Kaggle API token in it
uv run courtvision ingest kaggle                  # download (~1.1 GB) + load (~1 min)
uv run courtvision ingest nba-api --season 2025   # optional: ~1 h cold, for validation (T3, T6)
uv run courtvision metrics build                  # every rating, all seasons (~9 min)
uv run courtvision ingest salaries --fetch-current  # salaries, contracts (today's), cap
uv run courtvision market build                   # value, budgets, needs, options, free agency
uv run courtvision forecast                       # each team's next game + backtest (seconds)
uv run courtvision validate                       # regenerate docs/validation.md
uv run courtvision app                            # league / upcoming / team / game / player / market / live
uv run courtvision live                           # today's games; --game-id X rates one
```

`uv run courtvision build` reruns the whole chain from the raw caches.

Then query `data/courtvision.duckdb` with any DuckDB client:

```sql
-- 2025-26 team Defense, best to worst
select team_id, defense_value, defense_rating
from metrics.team_season
where season = 2025 and season_type = 'regular'
order by defense_rating desc;
```

## Data model

| Schema | What | Built by |
|---|---|---|
| `kaggle.*` | one view per Kaggle file, columns untouched | `ingest kaggle-load` |
| `nba_api.*` | `game_log`, `shots`, `pbp`, `rotations`, `shot_zone_league_avg` | `ingest nba-api` |
| `stg.*` | conformed keys (10-char `game_id`, `season`, `season_type`) and documented repairs: `games`, `players`, `player_games`, `team_games`, `pbp`, `shots`, `events` | `ingest kaggle-load` |
| `metrics.*` | possessions, lineups, counts, league baselines, ratings (`team_game`, `team_season`, `player_game`, `player_season`, `presence_season`, `ratings_long`, ...), `player_impact` (per-possession impact), `team_season_adj` (opponent-adjusted), `player_role` (usage); see spec section 7 | `metrics build` |
| `salary.*` | `book` (every contract by season, with options), `contract_summary` (status next summer), `contracts` (July snapshot, NBA ids), `cap`, `min_salaries`, ... | `ingest salaries` |
| `market.*` | `players` (value, price, contract, risk), `teams` (budget, needs, next summer's space), `options`, `fa_class`, `fa_options`, `params` | `market build` |
| `forecast.*` | `games` (margin, win chance), `team` (predicted dimensions), `players`, `backtest_team`, `backtest_games` | `forecast` |

Parquet under `data/parquet/` is the durable copy; the DuckDB file holds views
over it and rebuilds in seconds. Raw downloads and every nba_api response
(gzipped, with request parameters) are kept under `data/raw/`.

## Layout

```
src/courtvision/   library + CLI (`courtvision ...`)
  ingest/          Kaggle download/load, nba_api client + pull, staging views
  metrics/         play-by-play parser (possessions, lineups), counts, measures, ratings
  app/             Streamlit visualizer: league, team, game, player, market, live (click-through charts)
  live.py          live-game ratings from the NBA live feed
  market.py        player value, team budgets and needs, options and risk
  validation.py    generates docs/validation.md
  audit.py         generates docs/audit/*
notebooks/         validation notebook (same numbers as docs/validation.md)
tests/             pytest suite; integration tests run against the built database
docs/              data audit, metric spec, validation, generated audit appendices
data/              gitignored: raw downloads and caches, parquet, courtvision.duckdb
```

## Development

```bash
just lint     # ruff check + format check
just test     # pytest
```

## License

MIT — see [LICENSE](LICENSE). NBA data is subject to its providers' terms;
this repository ships code only, never data.
