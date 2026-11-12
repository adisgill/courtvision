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

# run the offline test suite
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
