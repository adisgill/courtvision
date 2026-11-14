"""The daily run (``courtvision daily``), meant to start every morning from a scheduled task:

1. **Score** every logged day of the past week that isn't fully scored (yesterday, after a
   normal night), then **forecast** right away and **publish** the demo, so the live app
   shows last night's results and today's games from the morning on.
2. **Forecast** again ``LEAD`` before the day's first tip-off, with the latest injury
   report, and publish. A forecast never predicts a game that has started (those teams
   get their next game), so every logged prediction for a game was made before it, and
   the latest one before tip-off is the one scored.
3. **Refresh the data** once Kaggle publishes its nightly update (polled every ``POLL``;
   downloads can fail for a while after publication, so they're retried): download and
   ``build``, then forecast again (every game not yet started, latest injury report) and
   publish. A rebuild starts only if it can't delay the pre-tip forecast and while games
   are still to come (on a day without games, until ``NO_GAMES_LAST_BUILD``).

Each step runs as its own ``courtvision`` process, logged to
``data/logs/daily-<date>.log``. Times are the machine's clock, which must be US Eastern
like the schedule. While it runs, the computer is kept from idle sleep (Windows).
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from courtvision.config import REPO_ROOT, get_paths

log = logging.getLogger(__name__)

LEAD = timedelta(minutes=40)  # forecast this long before a tip-off
BUILD_TIME = timedelta(minutes=60)  # room a rebuild needs (they take 15-50 minutes)
NO_GAMES_LAST_BUILD = (17, 15)  # on a day without games, the latest a rebuild starts
POLL = timedelta(minutes=15)
LOOKBACK = 7  # days of unscored predictions to catch up on


def tips(con: duckdb.DuckDBPyConnection, day: date) -> list[datetime]:
    """Scheduled tip-offs of ``day``'s games (Eastern), earliest first."""
    rows = con.execute(
        """select distinct gameDateTimeEst from kaggle.league_schedule_26_27
           where gameDateTimeEst::date = ? and gameLabel is distinct from 'Preseason'
             and homeTeamId > 0 and awayTeamId > 0 order by 1""",
        [day],
    ).fetchall()
    return [r[0] for r in rows]


def plan(today: date, day_tips: list[datetime]) -> dict:
    """When the first forecast runs (None without games) and the latest a rebuild may start
    (it must leave time to forecast the day's last game)."""
    if not day_tips:
        h, m = NO_GAMES_LAST_BUILD
        return {"forecast_at": None, "build_by": datetime(today.year, today.month, today.day,
                                                          h, m)}  # fmt: skip
    return {"forecast_at": day_tips[0] - LEAD, "build_by": day_tips[-1] - LEAD - BUILD_TIME}


def can_build(now: datetime, p: dict, forecast_done: bool) -> bool:
    """Room to rebuild now: before ``build_by``, and without pushing the first forecast late
    (before it with time to spare, or once it has run)."""
    if now >= p["build_by"]:
        return False
    return p["forecast_at"] is None or forecast_done or now + BUILD_TIME <= p["forecast_at"]


def kaggle_newer(published: datetime | None, downloaded: datetime | None) -> bool:
    """A Kaggle version newer than the one downloaded (both UTC)."""
    return published is not None and (downloaded is None or published > downloaded)


def unscored_days(con: duckdb.DuckDBPyConnection, today: date) -> list[date]:
    """Days of the last ``LOOKBACK`` with logged predictions not all scored yet."""
    tables = {r[0] for r in con.execute(
        "select table_name from information_schema.tables where table_schema = 'forecast'"
    ).fetchall()}  # fmt: skip
    if "ledger_games" not in tables:
        return []
    scored = ("select day d, count(distinct game_id) k from forecast.scorecard_games group by 1"
              if "scorecard_games" in tables else "select null::date d, 0 k")  # fmt: skip
    rows = con.execute(
        f"""with l as (select game_date::date d, count(distinct game_id) n
                       from forecast.ledger_games where game_date::date between ? and ?
                       group by 1),
                 s as ({scored})
            select l.d from l left join s using (d) where coalesce(s.k, 0) < l.n
            order by l.d""",
        [today - timedelta(days=LOOKBACK), today - timedelta(days=1)],
    ).fetchall()
    return [r[0] for r in rows]


class Runner:
    """Runs ``courtvision`` steps as child processes, logging their output."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        exe = Path(sys.executable)
        console = exe.with_name("python.exe")  # under pythonw, children still get a pipe
        # started by scripts/daily_task.pyw, this is the base interpreter: steps use the venv's
        self.python = os.environ.get("COURTVISION_PYTHON") or str(
            console if console.exists() else exe
        )

    def __call__(self, *args: str) -> bool:
        log.info("$ courtvision %s", " ".join(args))
        if self.dry_run:
            return True
        r = subprocess.run(
            [self.python, "-m", "courtvision", *args], cwd=REPO_ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )  # fmt: skip
        for line in (r.stdout + r.stderr).splitlines()[-25:]:
            if "%|" not in line and "▕" not in line:  # progress bars
                log.info("  %s", line.rstrip())
        log.info("  exit %d", r.returncode)
        return r.returncode == 0


def _keep_awake() -> None:
    if sys.platform == "win32":  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED, until the process ends
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)


def run(dry_run: bool = False) -> dict:
    """One day's run; returns what it did."""
    from courtvision.ingest import kaggle_download

    _keep_awake()
    step = Runner(dry_run)
    paths = get_paths()
    today = date.today()
    done = {"scored": [], "forecasts": 0, "rebuilt": False}
    with duckdb.connect(str(paths.duckdb), read_only=True) as con:
        days = unscored_days(con, today)
        day_tips = tips(con, today)
    p = plan(today, day_tips)
    log.info("tip-offs %s; first forecast at %s; rebuilds start by %s; to score: %s",
             [f"{t:%H:%M}" for t in day_tips] or "none", p["forecast_at"], p["build_by"],
             days or "nothing")  # fmt: skip

    for d in days:
        if step("scorecard", "--date", f"{d}"):
            done["scored"].append(f"{d}")

    def forecast() -> None:
        step("ingest", "injuries", "--fetch")
        if step("forecast"):
            done["forecasts"] += 1
        step("demo", "--publish")

    def games_left() -> bool:
        return any(t - timedelta(minutes=5) > datetime.now() for t in day_tips)

    # first thing: today's games (and last night's scores) on the live app, from the data so
    # far; the forecast that counts runs LEAD before the first tip-off (started after that,
    # because the computer was off, the one just made is it)
    forecast()
    pretip = p["forecast_at"] is None or not games_left() or datetime.now() >= p["forecast_at"]
    try:
        while not dry_run:
            now = datetime.now()
            if not pretip and now >= p["forecast_at"]:
                forecast()
                pretip = True
                continue
            if not done["rebuilt"] and can_build(now, p, pretip):
                try:
                    have = kaggle_download.downloaded_at(paths.kaggle_raw)
                    newer = kaggle_newer(kaggle_download.last_updated(), have)
                except Exception as e:  # network blips: try again next round
                    log.info("kaggle check failed: %s", e)
                    newer = False
                if newer and step("ingest", "kaggle"):
                    done["rebuilt"] = step("build")
                    forecast()  # games not yet started, on the fresh data; published
                    continue
            if pretip and (done["rebuilt"] or not can_build(now, p, True)):
                break
            wake = now + POLL
            if not pretip:
                wake = min(wake, p["forecast_at"])
            log.info("waiting until %s", f"{wake:%H:%M}")  # a trace of when it was alive
            time.sleep(max(1.0, (wake - datetime.now()).total_seconds()))
    except KeyboardInterrupt:  # Ctrl+C, or its console window closed (see daily_task.pyw)
        log.info("stopped: interrupted (Ctrl+C or its console window closed)")
        raise
    if not done["rebuilt"] and not dry_run:
        log.info("no rebuild today: Kaggle's update didn't arrive (or download) in time")
    log.info("done: %s", done)
    return done


def log_to(path: Path) -> None:
    """Log to ``path`` (and the console, when there is one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=handlers, force=True)  # fmt: skip
