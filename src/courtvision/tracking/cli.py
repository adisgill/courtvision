"""``courtvision tracking ...`` (also ``python -m courtvision.tracking ...``).

    fetch [--limit N]      download the SportVU archives (all 636 unless --limit)
    convert [--workers N]  archives -> frames Parquet (skips games already converted)
    align [--workers N]    find shots in the tracking, match them to the play-by-play; QA
    nbacom                 pull NBA.com's 2015-16 tracking stats for those games (known answers)
    movement               distance, speed, touches, zones, spacing per player-game; vs NBA.com
    shots                  shot quality at each release; model, out-of-sample report; vs NBA.com
    replay GAME_ID         a local animated-court page for one game
    defense                matchups, man/zone, ball screens and coverages, help; outcomes
    labels sample|draw|evaluate|spot   the hand-labeled check of the defense detectors

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


@app.command()
def nbacom() -> None:
    """Pull NBA.com's 2015-16 tracking stats for every game date we have (the known answers)."""
    from courtvision.tracking import nbacom as N

    typer.echo(json.dumps(N.fetch()))


@app.command()
def movement(workers: int = typer.Option(6), check: bool = typer.Option(True)) -> None:
    """Distance, speed, touches, zones and spacing per player-game; checked against NBA.com."""
    from courtvision.tracking import movement as M

    typer.echo(json.dumps(M.run(workers=workers)))
    if check:
        typer.echo(json.dumps(M.validate(), indent=1))


@app.command()
def shots(workers: int = typer.Option(6), check: bool = typer.Option(True)) -> None:
    """Shot quality: features at each release, the model and its out-of-sample report."""
    from courtvision.tracking import shotquality as Q

    typer.echo(json.dumps(Q.build_features(workers=workers)))
    report = Q.fit()
    typer.echo(json.dumps({k: report[k] for k in ("models", "gain", "shot_making")}, indent=1))
    if check:
        typer.echo(json.dumps(Q.validate(), indent=1))


@app.command()
def replay(
    game_id: str,
    period: list[int] = typer.Option(None, help="only these quarters"),
    open_: bool = typer.Option(False, "--open", help="open it in the browser"),
) -> None:
    """Write a local animated-court replay page for one game (never published: raw positions)."""
    from courtvision.tracking import replay as R

    path = R.build(game_id, periods=period or None)
    typer.echo(str(path))
    if open_:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())


@app.command()
def defense(workers: int = typer.Option(6)) -> None:
    """Matchups, man or zone, ball screens and their coverages, help on drives (every game)."""
    from courtvision.tracking import defense as D

    typer.echo(json.dumps(D.run(workers=workers)))
    typer.echo(json.dumps(D.outcomes(workers=workers), indent=1))


@app.command()
def labels(step: str = typer.Argument(..., help="sample, draw, evaluate or spot")) -> None:
    """The hand-labeled check of the defense detectors (docs/tracking_labeling_rules.md)."""
    from courtvision.tracking import labels as L

    if step == "sample":
        s = L.sample()
        counts = s.groupby(["kind", "stratum"]).size()
        typer.echo(json.dumps({f"{k}:{st}": int(v) for (k, st), v in counts.items()}))
    elif step == "draw":
        typer.echo(json.dumps({"drawn": L.draw()}))
    elif step == "evaluate":
        typer.echo(json.dumps(L.evaluate(), indent=1, default=str))
    elif step == "spot":  # the 30-screen page for checking the labels by eye
        import webbrowser

        path = L.spot_check()
        typer.echo(str(path))
        webbrowser.open(path.resolve().as_uri())
    else:
        raise typer.BadParameter("sample, draw, evaluate or spot")
