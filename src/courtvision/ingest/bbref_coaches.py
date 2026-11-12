"""This season's head coaches from Basketball-Reference (user-approved fetch, one page).

``https://www.basketball-reference.com/leagues/NBA_<year>_coaches.html`` lists every
head coach of the season with his team and this season's games and wins, so a summer
hire or a mid-season change is known before the Kaggle coach records catch up. Fetched
on request only (``courtvision ingest coaches --fetch-current``), cached gzipped under
``data/raw/coaches/bbref_current/<date>/`` (gitignored); the data never leaves the
machine. Same User-Agent and politeness as the contract pages.
"""

from __future__ import annotations

import gzip
import logging
import re
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

from courtvision.ingest.bbref_contracts import USER_AGENT, today_dir

log = logging.getLogger(__name__)

URL = "https://www.basketball-reference.com/leagues/NBA_{year}_coaches.html"
FILE = "coaches_{season}.html.gz"

__all__ = ["fetch", "parse", "load_latest", "today_dir"]


def fetch(dest: Path, season: int, force: bool = False) -> Path:
    """Download the coaches page of ``season`` (start year) once into ``dest``."""
    import requests

    dest.mkdir(parents=True, exist_ok=True)
    path = dest / FILE.format(season=season)
    if path.exists() and not force:
        return path
    r = requests.get(URL.format(year=season + 1), headers={"User-Agent": USER_AGENT},
                     timeout=30)  # fmt: skip
    r.raise_for_status()
    path.write_bytes(gzip.compress(r.content))
    log.info("fetched coaches %s (%d bytes)", season, len(r.content))
    return path


class _CoachesTable(HTMLParser):
    """Body rows of ``<table id="NBA_coaches">``: each cell's data-stat, link and text."""

    def __init__(self):
        super().__init__()
        self.depth = 0  # >0 inside the table
        self.in_body = False
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._cell: str | None = None
        self._href = ""
        self._text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and (a.get("id") == "NBA_coaches" or self.depth):
            self.depth += 1
        if not self.depth:
            return
        if tag == "tbody":
            self.in_body = True
        elif tag == "tr" and self.in_body:
            self._row = {}
        elif tag in ("th", "td") and self._row is not None:
            self._cell, self._href, self._text = a.get("data-stat", ""), "", ""
        elif tag == "a" and self._cell is not None and not self._href:
            self._href = a.get("href", "")

    def handle_data(self, data):
        if self._cell is not None:
            self._text += data

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag in ("th", "td") and self._cell is not None and self._row is not None:
            self._row[self._cell] = {"href": self._href, "text": self._text.strip()}
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "tbody":
            self.in_body = False
        elif tag == "table":
            self.depth -= 1


def _int(cell: dict | None) -> int | None:
    t = (cell or {}).get("text", "")
    return int(t) if t.isdigit() else None


def parse(html: str) -> pd.DataFrame:
    """One row per coach-team of the season: coach_id, coach_name, team_abbr, this
    season's regular-season games, wins and losses (0 before his first game)."""
    p = _CoachesTable()
    p.feed(html)
    out = []
    for r in p.rows:
        coach = r.get("coach", {})
        m = re.search(r"/coaches/([^/.]+)\.html", coach.get("href", ""))
        team = r.get("team", {})
        t = re.search(r"/teams/([A-Z]{3})/", team.get("href", "")) or re.match(
            r"^([A-Z]{3})$", team.get("text", ""))  # fmt: skip
        if not m or not t:
            continue
        out.append({"coach_id": m.group(1), "coach_name": coach.get("text", ""),
                    "team_abbr": t.group(1), "g": _int(r.get("cur_g")) or 0,
                    "wins": _int(r.get("cur_w")) or 0,
                    "losses": _int(r.get("cur_l")) or 0})  # fmt: skip
    return pd.DataFrame(out, columns=["coach_id", "coach_name", "team_abbr", "g", "wins",
                                      "losses"])  # fmt: skip


def load_latest(root: Path, season: int) -> tuple[pd.DataFrame, str] | None:
    """The newest cached page of ``season``, parsed, and its date; None if none."""
    name = FILE.format(season=season)
    days = sorted(d for d in root.glob("????-??-??") if (d / name).exists())
    if not days:
        return None
    return parse(gzip.decompress((days[-1] / name).read_bytes()).decode("utf-8")), days[-1].name
