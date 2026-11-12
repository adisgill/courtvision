# Task runner. Install with `uv tool install rust-just` (or winget/brew/cargo).
# Every recipe is a thin wrapper around `uv run courtvision ...`, so the
# project works without `just` too.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# list recipes
default:
    @just --list

# create the virtualenv and install all dependencies
setup:
    uv sync

# lint + format check
lint:
    uv run ruff check .
    uv run ruff format --check .

# auto-fix lint and format
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# run the tests (integration tests skip until `just metrics` has run)
test:
    uv run pytest

# show version and data locations
info:
    uv run courtvision info

# download + load the Kaggle dataset into Parquet/DuckDB
kaggle:
    uv run courtvision ingest kaggle

# pull one nba_api season (cached; resumable), e.g. `just nba-api 2025`
nba-api season:
    uv run courtvision ingest nba-api --season {{season}}

# regenerate docs/audit/* (Kaggle profile + nba_api join report)
audit season="2025":
    uv run courtvision audit kaggle
    uv run courtvision audit nba-api --season {{season}}

# build every rating (possessions, lineups, counts, baselines, ratings)
metrics:
    uv run courtvision metrics build

# salaries + cap thresholds, then the player market
market:
    uv run courtvision ingest salaries
    uv run courtvision market build

# rebuild everything from the raw caches
build:
    uv run courtvision build

# regenerate docs/validation.md
validate:
    uv run courtvision validate

# open the visualizer
app:
    uv run courtvision app

# rate a live game, e.g. `just live 0022600216`; no id lists today's games
live game_id="":
    uv run courtvision live {{ if game_id == "" { "" } else { "--game-id " + game_id } }}

# predict each team's next game
forecast:
    uv run courtvision forecast
