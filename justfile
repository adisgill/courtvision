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
