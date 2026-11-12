"""Command-line entry point. Every pipeline step is one subcommand."""

from __future__ import annotations

import typer

from courtvision import __version__
from courtvision.config import get_paths

app = typer.Typer(
    help="Six-dimension 0-100 ratings for NBA players, games, and teams.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def info() -> None:
    """Print the version and where data lives."""
    paths = get_paths()
    typer.echo(f"courtvision {__version__}")
    typer.echo(f"data dir : {paths.data}")
    typer.echo(f"database : {paths.duckdb} ({'exists' if paths.duckdb.exists() else 'missing'})")


@app.callback()
def _root() -> None:
    """Keep ``info`` a subcommand even while it is the only one."""
