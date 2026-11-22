"""``courtvision tracking ...`` (also ``python -m courtvision.tracking ...``).

    fetch [--limit N]      download the SportVU archives (all 636 unless --limit)
    convert [--workers N]  archives -> frames Parquet (skips games already converted)
    align [--workers N]    find shots in the tracking, match them to the play-by-play; QA

``courtvision ingest sportvu [--fetch]`` runs fetch (if asked) and convert.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ProcessPoolExecutor

import typer

from courtvision.tracking import sportvu

app = typer.Typer(help="Film and tracking (docs/tracking_spec.md).", add_completion=False,
                  no_args_is_help=True)  # fmt: skip


@app.command()
def fetch(limit: int = typer.Option(0, help="only N archives, spread out (0 = all)")) -> None:
    """Download the SportVU archives not yet here (3.6 GB in all)."""
    names = None
    if limit:
        names = [a["name"] for a in sportvu.list_archives()[:: max(1, 636 // limit)][:limit]]
    typer.echo(json.dumps(sportvu.fetch(names)))


@app.command()
def convert(workers: int = typer.Option(6), redo: bool = typer.Option(False)) -> None:
    """Archives -> data/tracking/frames/<game_id>.parquet (and rosters), in parallel."""
    archives = sorted((sportvu.raw_dir() / "7z").glob("*.7z"))
    record = sportvu.raw_dir() / "converted.json"
    seen = json.loads(record.read_text()) if record.exists() else {}
    done = set(sportvu.game_ids())
    if not redo and done:
        # an archive's game id is only known inside it: skip by a record of what each held
        archives = [a for a in archives if seen.get(a.name) not in done]
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(sportvu.convert_safe, [str(a) for a in archives]):
            results.append(r)
            if len(results) % 25 == 0:
                logging.info("converted %d/%d", len(results), len(archives))
    seen.update({r["archive"]: r["game_id"] for r in results if "game_id" in r})
    record.write_text(json.dumps(seen, indent=1))
    errors = [r for r in results if "error" in r]
    typer.echo(json.dumps({"converted": len(results) - len(errors), "errors": errors[:10],
                           "games": len(sportvu.game_ids())}))  # fmt: skip


@app.command()
def align(workers: int = typer.Option(6)) -> None:
    """Find shots in every game's tracking and match them to the play-by-play; QA summary."""
    from courtvision.tracking import align as A

    typer.echo(json.dumps(A.run(workers=workers), indent=1))
