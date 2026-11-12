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
ingest_app = typer.Typer(help="Download sources and load them into DuckDB.", no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")


@ingest_app.command("kaggle-download")
def kaggle_download(
    version: int | None = typer.Option(None, help="Pin a Kaggle dataset version."),
    force: bool = typer.Option(False, help="Re-download even if files exist."),
) -> None:
    """Download the Kaggle dataset into data/raw/kaggle and write a manifest."""
    from courtvision.ingest.kaggle_download import download

    manifest = download(get_paths().kaggle_raw, version=version, force=force)
    for name, size in manifest["files"].items():
        typer.echo(f"{size:>14,}  {name}")


@ingest_app.command("kaggle-load")
def kaggle_load() -> None:
    """Convert downloaded Kaggle files to Parquet; build kaggle.* and stg.* views."""
    from courtvision.db import connect
    from courtvision.ingest import kaggle_load as kl
    from courtvision.ingest import staging

    paths = get_paths()
    if not paths.kaggle_raw.exists():
        raise typer.BadParameter("No raw Kaggle files. Run `courtvision ingest kaggle-download`.")
    with connect() as con:
        for view in kl.load(con, paths.kaggle_raw, paths.parquet / "kaggle"):
            n = con.execute(f"select count(*) from {view}").fetchall()[0][0]
            typer.echo(f"{n:>12,}  {view}")
        staging.build(con)
    typer.echo("stg.* views rebuilt")


@ingest_app.command("kaggle")
def kaggle_all(
    version: int | None = typer.Option(None, help="Pin a Kaggle dataset version."),
) -> None:
    """Download + load in one step."""
    kaggle_download(version=version, force=True)
    kaggle_load()


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
