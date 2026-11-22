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
    winprob                EPV and live win probability: training rows, models, held-out report
    live GAME_ID [--check] the Live view (local page fed by the streaming engine); --check runs
                           the game through the engine, times every frame, compares with batch

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


@app.command()
def winprob(workers: int = typer.Option(6), rows: bool = typer.Option(True)) -> None:
    """EPV and film-aware win probability: rows for every game (one a second), the EPV model,
    the score-and-clock baseline (ten seasons of play-by-play) and the film terms; reports on
    the held-out games."""
    from courtvision.tracking import epv as E
    from courtvision.tracking import winprob as W

    if rows:
        typer.echo(json.dumps(E.build(workers=workers)))
    rep = E.fit()
    typer.echo(json.dumps({k: rep[k] for k in ("r2_film", "r2_clock_only", "rmse_film",
                                                "rmse_constant")}))  # fmt: skip
    w = W.fit()
    typer.echo(json.dumps({k: w[k] for k in ("live_model", "epv_point_worth", "gain_ci95",
                                              "anticipation")}, indent=1))  # fmt: skip


@app.command()
def live(
    game_id: str,
    check: bool = typer.Option(False, "--check", help="time the engine on the whole game"),
    port: int = typer.Option(8770),
    open_: bool = typer.Option(False, "--open", help="open it in the browser"),
) -> None:
    """The Live view: replay a game through the streaming engine to a local page at 25 frames
    a second (http://127.0.0.1:8770/; stays on this machine)."""
    if check:
        from courtvision.tracking import engine as G

        typer.echo(json.dumps(G.check(game_id), indent=1))
        return
    from courtvision.tracking import server as S

    S.serve(game_id, port=port, open_browser=open_)
