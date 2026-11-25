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


def _tracking_app() -> typer.Typer:
    from courtvision.tracking.cli import app as tracking

    return tracking


app.add_typer(_tracking_app(), name="tracking")


@ingest_app.command("sportvu")
def ingest_sportvu(
    fetch: bool = typer.Option(False, "--fetch", help="download missing archives first (3.6 GB)"),
    workers: int = typer.Option(6),
) -> None:
    """The public 2015-16 SportVU tracking logs -> data/tracking/frames
    (docs/tracking_spec.md)."""
    from courtvision.tracking import cli as tracking

    if fetch:
        tracking.fetch(limit=0)
    tracking.convert(workers=workers, redo=False)


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


@ingest_app.command("nba-api")
def nba_api(
    season: int = typer.Option(..., help="Season start year, e.g. 2025 for 2025-26."),
    season_type: list[str] = typer.Option(
        ["regular", "play_in", "playoffs"], help="regular / play_in / playoffs (repeatable)."
    ),
    only: list[str] = typer.Option(
        ["shots", "pbp", "rotations"], help="Endpoint groups to pull: shots / pbp / rotations."
    ),
    load_only: bool = typer.Option(False, help="Skip the network; rebuild from the cache."),
    min_interval: float = typer.Option(1.0, help="Minimum seconds between requests."),
) -> None:
    """Pull one season (game log, shots, play-by-play, rotations) and load it into DuckDB."""
    import logging

    from courtvision.db import connect
    from courtvision.ingest import nba_api_pull
    from courtvision.ingest.nba_api_client import CachedClient

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths = get_paths()
    if not load_only:
        client = CachedClient(cache_dir=paths.nba_api_raw, min_interval=min_interval)
        summary = nba_api_pull.pull(client, season, season_type, only=only)
        typer.echo(f"pulled: {summary}")
    with connect() as con:
        counts = nba_api_pull.load(con, paths.nba_api_raw, paths.parquet / "nba_api", season)
    for name, n in counts.items():
        typer.echo(f"{n:>10,}  nba_api.{name} ({season})")


audit_app = typer.Typer(help="Profile sources and write docs/audit/*.md.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


@audit_app.command("kaggle")
def audit_kaggle() -> None:
    """Profile every Kaggle table; write docs/audit/kaggle/*.md."""
    from courtvision.audit import audit_kaggle as run
    from courtvision.config import REPO_ROOT
    from courtvision.db import connect

    with connect() as con:
        result = run(con, REPO_ROOT / "docs" / "audit" / "kaggle", get_paths().kaggle_raw)
    for view, info in result["tables"].items():
        typer.echo(f"{info['rows']:>12,} rows {info['cols']:>4} cols  {view}")
    for name, res in result["checks"].items():
        typer.echo(f"  {res:>12}  {name}")


@audit_app.command("nba-api")
def audit_nba_api(
    season: int = typer.Option(..., help="Season start year, e.g. 2025 for 2025-26."),
) -> None:
    """Check how nba_api tables join to Kaggle; write docs/audit/nba_api_<season>.md."""
    from datetime import UTC, datetime

    from courtvision.audit import nba_api_join_report, write_md
    from courtvision.config import REPO_ROOT
    from courtvision.db import connect

    with connect() as con:
        body = nba_api_join_report(con, season)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    dest = REPO_ROOT / "docs" / "audit" / f"nba_api_{season}.md"
    write_md(dest, f"> Generated by `courtvision audit nba-api --season {season}` at {stamp}. "
             "Do not edit by hand.\n\n" + body)  # fmt: skip
    typer.echo(body)


metrics_app = typer.Typer(help="Build ratings (docs/metrics_spec.md).", no_args_is_help=True)
app.add_typer(metrics_app, name="metrics")


@metrics_app.command("build")
def metrics_build(
    season: list[int] = typer.Option(
        [], help="Re-parse only these seasons' play-by-play (repeatable); default all."
    ),
    skip_pbp: bool = typer.Option(False, help="Reuse the existing play-by-play parse."),
    workers: int | None = typer.Option(None, help="Parallel play-by-play workers."),
) -> None:
    """Possessions, lineups, counts, baselines, ratings -> metrics.* and data/parquet/metrics.

    League baselines, reference distributions and shrinkage are always rebuilt over all
    seasons, so ``--season`` only limits which seasons' play-by-play is re-parsed.
    """
    import logging

    from courtvision.db import connect
    from courtvision.metrics import build

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    paths = get_paths()
    with connect() as con:
        starts = build.build_all(
            con, paths.data, paths.kaggle_raw, seasons=season or None, workers=workers,
            skip_pbp=skip_pbp,
        )  # fmt: skip
        summary = con.execute(
            "select level, count(*) from metrics.ratings_long group by 1 order by 1"
        ).fetchall()
    typer.echo(f"tier start seasons: {starts}")
    for level, n in summary:
        typer.echo(f"{n:>12,}  ratings ({level})")


@ingest_app.command("salaries")
def ingest_salaries(
    download: bool = typer.Option(True, help="Download from Kaggle first (else use the cache)."),
    fetch_current: bool = typer.Option(
        False,
        "--fetch-current",
        help="Fetch today's Basketball-Reference team contract pages (30 requests, 4 s "
        "apart; cached per day). Without it the newest cached pages are used.",
    ),
) -> None:
    """Salaries, contracts, cap / tax / aprons -> salary.* (book, contract_summary, ...).

    Sources, newest first: your ``data/salary_overrides.csv``; Basketball-Reference's
    current team contract pages (``--fetch-current``, cached under
    data/raw/salaries/bbref_current/<date>); the July 2026 salary snapshot (Kaggle
    mrbridge: two-way deals, dead money, cap thresholds); Basketball-Reference contracts
    of June 2026 (Kaggle deocheng; used only when no current pages are cached).
    """  # fmt: skip
    import logging

    from courtvision.db import connect
    from courtvision.ingest import bbref_contracts, contracts, salaries

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    data = get_paths().data
    raw = data / "raw" / "salaries" / "mrbridge"
    bb_dir = data / "raw" / "salaries" / "deocheng"
    cur_root = data / "raw" / "salaries" / "bbref_current"
    if download or not raw.exists():
        salaries.download(raw)
    bb_path = next(iter(bb_dir.rglob("public.player_contracts.csv")), None)
    if download or bb_path is None:
        bb_path = contracts.download_bbref(bb_dir)
    if fetch_current:
        bbref_contracts.fetch_all(bbref_contracts.today_dir(cur_root),
                                  list(contracts.BBREF_TEAMS))  # fmt: skip
    cur_dir = bbref_contracts.latest_dir(cur_root)
    with connect() as con:
        stats = salaries.load(con, raw)["by_season"]
        season = con.execute("select max(season) from salary.contracts").fetchall()[0][0]
        bb = contracts.load_bbref(con, bb_path)
        over = contracts.read_overrides(data / "salary_overrides.csv", con)
        current = (contracts.load_current(con, bbref_contracts.load_dir(cur_dir), season)
                   if cur_dir else None)  # fmt: skip
        if current is not None:
            matched = current.drop_duplicates("bbref_id").person_id.notna().mean()
            typer.echo(f"current contract pages {cur_dir.name}: {current.bbref_id.nunique()} "
                       f"players, matched {matched:.1%}")  # fmt: skip
        res = contracts.build_book(con, bb, season, over, current,
                                   cur_dir.name if cur_dir else "")  # fmt: skip
        summary = contracts.summarize(con, res["book"], res["declined"], season)
        contracts.write(con, res["book"], summary)
        status = con.execute(
            "select status, count(*) n from salary.contract_summary group by 1 order by 2 desc"
        ).df()
    typer.echo(stats.to_string(index=False))
    typer.echo(f"\nBasketball-Reference contracts matched: {bb.person_id.notna().mean():.1%}; "
               f"overrides: {len(over)}")  # fmt: skip
    typer.echo(status.to_string(index=False))


market_app = typer.Typer(help="Player market: value, budgets, needs, options.",
                         no_args_is_help=True)  # fmt: skip
app.add_typer(market_app, name="market")


@market_app.command("build")
def market_build(
    season: int | None = typer.Option(None, help="Salary season; default the latest."),
) -> None:
    """Price every player, read every team's budget and needs, list the options."""
    import logging

    from courtvision import market
    from courtvision.db import connect

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with connect() as con:
        params = market.build(con, season)
    for k, v in params.items():
        typer.echo(f"{k:>22}: {v:,.4g}" if isinstance(v, float) else f"{k:>22}: {v}")


@app.command("forecast")
def forecast_cmd() -> None:
    """Predict each team's next game (team and player level) and backtest the method."""
    import logging

    from courtvision import forecast
    from courtvision.db import connect

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with connect() as con:
        res = forecast.build(con)
        bt = con.execute("select * from forecast.backtest_games").df()
    typer.echo(f"{res['games']} upcoming games predicted; blend (team form) {res['blend']:.2f}, "
               f"margin sigma {res['sigma']:.1f}")  # fmt: skip
    typer.echo(bt.round(3).to_string(index=False))


def _coach_raw():
    """Coach records live with the rest of the Kaggle deocheng (Basketball-Reference) files."""
    return get_paths().data / "raw" / "salaries" / "deocheng"


def _has_coach_files() -> bool:
    return any(_coach_raw().rglob("public.coach_stats.csv")) if _coach_raw().exists() else False


@ingest_app.command("coaches")
def ingest_coaches(
    download: bool = typer.Option(True, help="Download from Kaggle first (else use the cache)."),
    fetch_current: bool = typer.Option(
        False,
        "--fetch-current",
        help="Fetch this season's coaches page from Basketball-Reference (one request, "
        "cached per day). Without it the newest cached page is used.",
    ),
) -> None:
    """Head coach of every team-game since 1996-97 -> coach.coaches, coach.assignments,
    coach.current. This season comes from Basketball-Reference's season coaches page
    (``--fetch-current``, cached under data/raw/coaches/bbref_current/<date>); without one,
    each team keeps last season's coach, flagged ``assumed``."""
    import logging

    from courtvision.db import connect
    from courtvision.ingest import bbref_coaches, coaches

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if download or not _has_coach_files():
        coaches.download(_coach_raw())
    root = get_paths().data / "raw" / "coaches" / "bbref_current"
    with connect() as con:
        season = con.execute("select max(season) from stg.games where season_type = 'regular'"
                             ).fetchall()[0][0]  # fmt: skip
        if fetch_current:
            bbref_coaches.fetch(bbref_coaches.today_dir(root), season)
        cached = bbref_coaches.load_latest(root, season)
        current, as_of = cached if cached else (None, "")
        res = coaches.load(con, _coach_raw(), current, season, as_of)
    src = f"Basketball-Reference page of {as_of}" if as_of else "assumed (last season's coach)"
    typer.echo(f"this season's coaches: {src}")
    typer.echo(res)


def _college_season(today=None) -> int:
    """Barttorvik's year of the college season in progress (2027 = 2026-27): seasons
    tip off in November; before that, the last one finished."""
    from datetime import date

    today = today or date.today()
    return today.year + 1 if today.month >= 11 else today.year


@ingest_app.command("ncaa")
def ingest_ncaa(
    fetch: bool = typer.Option(
        False,
        "--fetch",
        help="Download from Barttorvik first: seasons not cached yet (2007-08 on, one request "
        "each, 11 s apart) and the season in progress once a day. Without it the cache is "
        "loaded.",
    ),
) -> None:
    """Every Division I player-season (Barttorvik) -> ncaa.player_seasons, for the draft
    model (`courtvision draft`)."""
    import logging

    from courtvision.db import connect
    from courtvision.ingest import barttorvik

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    root = get_paths().data / "raw" / "barttorvik"
    current = _college_season()
    if fetch:
        barttorvik.fetch(root, current)
    with connect() as con:
        typer.echo(barttorvik.load(con, root, current))


@app.command("draft")
def draft_cmd() -> None:
    """The draft from college stats -> draft.*: NBA outcomes of every college player,
    the walk-forward test on past drafts, this season's big board and comparables, the
    projected order (season simulations and the lottery; traded picks from
    data/draft_pick_trades.csv) and a mock first round. Needs `ingest ncaa` and the
    market model."""
    from courtvision import draft
    from courtvision.db import connect

    with connect() as con:
        typer.echo(draft.run(con, get_paths().data))


@ingest_app.command("injuries")
def ingest_injuries(
    fetch: bool = typer.Option(
        False,
        "--fetch",
        help="Fetch the NBA's latest official injury report (two requests: the index page "
        "and the PDF). Without it the newest cached report is loaded.",
    ),
) -> None:
    """The NBA's official injury report -> injury.reports; `forecast` uses the newest one
    (players ruled out leave the expected lineups and their minutes go to teammates)."""
    import logging

    from courtvision.db import connect
    from courtvision.ingest import injuries

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    root = get_paths().data / "raw" / "injuries"
    with connect() as con:
        season = con.execute("select max(season) from stg.games where season_type = 'regular'"
                             ).fetchall()[0][0]  # fmt: skip
        path = injuries.fetch(root, season) if fetch else injuries.latest_cached(root)
        if path is None:
            typer.echo("no cached injury report: run with --fetch")
            return
        typer.echo(f"{path.name}: {injuries.load(con, path, season)}")


@app.command("coaching")
def coaching_cmd() -> None:
    """Coaching analytics from play-by-play -> coach.* (possessions, timeouts, profiles,
    tempo); coach value and coach vs coach when the walk-forward has run."""
    import logging

    from courtvision import coaching
    from courtvision.db import connect

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with connect() as con:
        typer.echo(coaching.build(con))
        has_wf = con.execute("select count(*) from information_schema.tables where "
                             "table_schema = 'forecast' and table_name = 'wf_games'"
                             ).fetchall()[0][0]  # fmt: skip
        if has_wf:
            typer.echo(coaching.value(con)["reliability"].round(3).to_string(index=False))


@app.command("plays")
def plays_cmd() -> None:
    """Play types from play-by-play (2015-16 on; after `coaching`) -> plays.* (shots, team
    and player mix and efficiency, passing, turnovers, reliability, matchup test)."""
    import logging

    from courtvision import plays
    from courtvision.db import connect

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with connect() as con:
        typer.echo(plays.build(con))
        mt = con.execute("select * from plays.matchup_test").df()
    typer.echo(mt.round(3).to_string(index=False))


@app.command("demo")
def demo_cmd(
    since: int = typer.Option(2025, help="First season (start year) with game-level rows."),
    out: str = typer.Option("demo/courtvision.duckdb", help="Output file, under data/."),
    publish: bool = typer.Option(
        False, "--publish", help="Then upload it to the repo's demo-data release (needs `gh`)."
    ),
) -> None:
    """Write the slim database the hosted demo app reads (play-level and game-level data
    for recent seasons, season-level history whole) -> data/demo/courtvision.duckdb;
    ``--publish`` replaces the copy the live app downloads."""
    import logging

    from courtvision import demo

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    paths = get_paths()
    typer.echo(demo.build(paths.duckdb, paths.data / out, since))
    if publish:
        typer.echo(f"published {demo.publish(paths.data / out)}")


@app.command("site")
def site_cmd(
    db: str = typer.Option("demo/courtvision.duckdb", help="Demo database to export, under data/."),
    publish: bool = typer.Option(
        False, "--publish", help="Then build the web app and push it to GitHub Pages (gh-pages)."
    ),
) -> None:
    """Export the demo database (``courtvision demo``) for the web app: content-addressed
    Parquet files and a manifest -> app/public/data; ``--publish`` builds the app and deploys
    it with the data."""
    from courtvision import site

    paths = get_paths()
    typer.echo(site.build(paths.data / db))
    if publish:
        typer.echo(f"published data version {site.publish()} to {site.SITE_URL}")


@app.command("daily")
def daily_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show today's plan; run nothing."),
) -> None:
    """The daily run for a scheduled task: score the past week's unscored days, refresh the
    data when Kaggle publishes, forecast before the first tip-off, publish the demo. Logs to
    data/logs/daily-<date>.log."""
    from datetime import date

    from courtvision import daily

    daily.log_to(get_paths().data / "logs" / f"daily-{date.today()}.log")
    daily.run(dry_run=dry_run)


@app.command("lineups")
def lineups_cmd() -> None:
    """Lineups from the rebuilt stints (after `coaching`) -> lineups.* (five-man units with
    projections, pairs, starters, closers) and the tests of how far lineup numbers hold."""
    import logging

    from courtvision import lineups
    from courtvision.db import connect

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with connect() as con:
        typer.echo(lineups.build(con))
        tests = con.execute("select * from lineups.tests").df()
    typer.echo(tests.round(3).to_string(index=False))


@app.command("walkforward")
def walkforward_cmd(
    availability_test: bool = typer.Option(
        False,
        "--availability-test",
        help="Also measure what knowing who plays is worth (re-runs the game model with "
        "the players who actually played; ~8 min) -> forecast.availability_test.",
    ),
) -> None:
    """Walk-forward test of game and player predictions (2021-22 to 2025-26, weekly, only
    earlier data each week) -> forecast.wf_*; then coach value and coach vs coach."""
    import logging

    from courtvision import coaching, walkforward
    from courtvision.db import connect

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if availability_test:  # read-only while it runs, so the app stays usable
        with connect(read_only=True) as con:
            av = walkforward.availability_test(con)
        with connect() as con:
            con.register("tmp_df", av)
            con.execute("create or replace table forecast.availability_test as "
                        "select * from tmp_df")  # fmt: skip
            con.unregister("tmp_df")
        typer.echo(av.round(4).to_string(index=False))
    with connect() as con:
        res = walkforward.run(con)
        rel = coaching.value(con)["reliability"]
    typer.echo(res["overall"].round(4).to_string(index=False))
    typer.echo(f"\nchosen: {res['best']}\n")
    typer.echo(res["players"].round(3).to_string(index=False))
    typer.echo(rel.round(3).to_string(index=False))


@app.command("scorecard")
def scorecard_cmd(
    date: str = typer.Option(..., "--date", help="Game day to score, e.g. 2026-11-12."),
    source: str = typer.Option(
        "auto",
        help="auto (database where it has the game, else the NBA live feed), db, or live.",
    ),
) -> None:
    """Score a day's logged predictions against what happened -> forecast.scorecard_*."""
    import logging

    from courtvision import scorecard
    from courtvision.db import connect

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with connect() as con:
        res = scorecard.score(con, date, source)
        games = con.execute(
            "select * from forecast.scorecard_games where day = ?::date", [date]).df()  # fmt: skip
    for k, v in res.items():
        typer.echo(f"{k:>20}: {v:.3f}" if isinstance(v, float) else f"{k:>20}: {v}")
    if not games.empty:
        cols = [
            "game_id",
            "pred_margin",
            "actual_margin",
            "error",
            "winner_right",
            "home_win_prob",
            "after_tipoff",
        ]
        typer.echo(games[cols].round(2).to_string(index=False))


@app.command("app")
def run_app(port: int = typer.Option(8501, help="Port for the Streamlit server.")) -> None:
    """Open the visualizer (Streamlit): team, game, player and live views."""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).parent / "app" / "streamlit_app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(script), "--server.port", str(port)]
    raise typer.Exit(subprocess.call(cmd))


@app.command("live")
def live_cmd(
    game_id: str | None = typer.Option(None, help="Game id; default: list today's games."),
) -> None:
    """Rate a live (or finished) game against both teams' seasons; print the signals."""
    from courtvision import live
    from courtvision.db import connect

    if not game_id:
        board = live.scoreboard()
        typer.echo(board.to_string(index=False) if not board.empty else "No games today.")
        return
    with connect(read_only=True) as con:
        res = live.rate_game(con, game_id)
    typer.echo(
        f"{res['away']} {res['away_score']} @ {res['home']} {res['home_score']}  ({res['status']})"
    )
    r = res["ratings"]
    for team_id, abbr in ((res["away_id"], res["away"]), (res["home_id"], res["home"])):
        t = r[(r.team_id == team_id) & r.measure.isin(
            ["shooting", "defense", "ball_control", "backcourt", "frontcourt"])]  # fmt: skip
        typer.echo(f"\n{abbr}")
        for row in t.itertuples():
            typer.echo(
                f"  {row.measure:<13} {row.value:+6.1f} pts/100  rating {row.rating:5.1f}"
                f"  (season avg game {row.season_rating:5.1f})"
            )
        for s in live.signals(r, team_id):
            typer.echo(f"  - {s}")


@app.command()
def validate() -> None:
    """Stabilization, persistence, signal beyond Four Factors, tiers -> docs/validation.md."""
    from datetime import UTC, datetime

    from courtvision.audit import write_md
    from courtvision.config import REPO_ROOT
    from courtvision.db import connect
    from courtvision.validation import report

    with connect(read_only=True) as con:
        body = report(con)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    marker = "Generated by `courtvision validate`"
    body = body.replace(marker, f"{marker} at {stamp}", 1)
    write_md(REPO_ROOT / "docs" / "validation.md", body)
    typer.echo(body)


@app.command()
def build(
    workers: int | None = typer.Option(None, help="Parallel play-by-play workers."),
) -> None:
    """Rebuild everything from the raw caches: kaggle-load -> nba-api load -> metrics ->
    salaries -> market -> coaches -> coaching -> plays -> lineups -> walk-forward ->
    college seasons -> draft -> injuries (cached) -> forecast."""
    from courtvision.db import connect
    from courtvision.ingest import nba_api_pull

    kaggle_load()
    paths = get_paths()
    with connect() as con:
        for s in _cached_nba_api_seasons(paths.nba_api_raw):
            nba_api_pull.load(con, paths.nba_api_raw, paths.parquet / "nba_api", s)
            typer.echo(f"nba_api {s} loaded from cache")
    metrics_build(season=[], skip_pbp=False, workers=workers)
    if (paths.data / "raw" / "salaries" / "mrbridge").exists():
        # pass every option: called as a function, an unset typer option is a truthy
        # OptionInfo, and build must never fetch
        ingest_salaries(download=False, fetch_current=False)
        market_build(season=None)
    else:
        typer.echo("no salary cache: run `courtvision ingest salaries` for the market model")
    if _has_coach_files():
        ingest_coaches(download=False, fetch_current=False)
        coaching_cmd()
        plays_cmd()
        lineups_cmd()
        walkforward_cmd(availability_test=False)
    else:
        typer.echo("no coach cache: run `courtvision ingest coaches` for coaching and the "
                   "walk-forward model")  # fmt: skip
    if (paths.data / "raw" / "barttorvik").exists() and (
        paths.data / "raw" / "salaries" / "mrbridge"
    ).exists():
        ingest_ncaa(fetch=False)
        draft_cmd()
    else:
        typer.echo("no college or salary cache: run `courtvision ingest ncaa --fetch` for the "
                   "draft")  # fmt: skip
    ingest_injuries(fetch=False)  # the newest cached report, if any; never fetches
    forecast_cmd()


def _cached_nba_api_seasons(cache_dir) -> list[int]:
    """Seasons with a cached league game log (``Season-2025-26_...`` cache keys)."""
    import re

    found = set()
    for f in (cache_dir / "leaguegamelog").glob("*.json.gz") if cache_dir.exists() else []:
        m = re.search(r"Season-(\d{4})-\d{2}", f.name)
        if m:
            found.add(int(m.group(1)))
    return sorted(found)


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
