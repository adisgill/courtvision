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
| 6 | Presence     | How much do the team's ratings move when a given player is on the floor? |

Ratings exist at three levels — player → game → team/season — and are always
**era-adjusted**: every number is rated against league average for its own
season, never compared across eras raw.

> Status: **foundation**. Ingest and the data audit are done; the metric spec
> is a draft awaiting review. No metrics or UI yet.
>
> * [`docs/data_audit.md`](docs/data_audit.md): what's actually in the data, by era
> * [`docs/metrics_spec.md`](docs/metrics_spec.md): formulas, scaling, roll-ups, earliest seasons

## Data sources

1. **Kaggle** — [`eoinamoore/historical-nba-data-and-player-box-scores`](https://www.kaggle.com/datasets/eoinamoore/historical-nba-data-and-player-box-scores):
   box scores back to 1947, updated nightly. The historical backbone.
2. **[nba_api](https://github.com/swar/nba_api)** — play-by-play, shot
   coordinates, and the live feed. Required for anything zone-based. Requests
   are rate-limited and every raw response is cached to disk.

No player-tracking data is used; none is public.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). [`just`](https://just.systems) is
optional — every recipe is a one-line `uv run` command you can type yourself.

```bash
uv sync
cp .env.example .env                              # then put your Kaggle API token in it
uv run courtvision ingest kaggle                  # download (~1.1 GB) + load (~1 min)
uv run courtvision ingest nba-api --season 2025   # ~1 h cold (2,732 requests), seconds once cached
uv run courtvision audit kaggle                   # regenerate docs/audit/kaggle/*
uv run courtvision audit nba-api --season 2025    # regenerate docs/audit/nba_api_2025.md
```

Then query `data/courtvision.duckdb` with any DuckDB client:

```sql
select season, count(*) fga, avg(made::int) fg_pct
from stg.shots where season_type = 'regular' group by 1 order by 1;
```

## Data model

| Schema | What | Built by |
|---|---|---|
| `kaggle.*` | one view per Kaggle file, columns untouched | `ingest kaggle-load` |
| `nba_api.*` | `game_log`, `shots`, `pbp`, `rotations`, `shot_zone_league_avg` | `ingest nba-api` |
| `stg.*` | conformed keys (10-char `game_id`, `season`, `season_type`) and documented repairs: `games`, `players`, `player_games`, `team_games`, `pbp`, `shots` | `ingest kaggle-load` |

Parquet under `data/parquet/` is the durable copy; the DuckDB file holds views
over it and rebuilds in seconds. Raw downloads and every nba_api response
(gzipped, with request parameters) are kept under `data/raw/`.

## Layout

```
src/courtvision/   library + CLI (`courtvision ...`)
  ingest/          Kaggle download/load, nba_api client + pull, staging views
  audit.py         generates docs/audit/*
tests/             pytest suite (offline by default)
docs/              data audit, metric spec, generated audit appendices
data/              gitignored: raw downloads, parquet, courtvision.duckdb
```

## Development

```bash
just lint     # ruff check + format check
just test     # pytest
```

## License

MIT — see [LICENSE](LICENSE). NBA data is subject to its providers' terms;
this repository ships code only, never data.
