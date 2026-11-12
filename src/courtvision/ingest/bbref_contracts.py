"""Current contracts from Basketball-Reference's team contract pages (user-approved fetch).

``https://www.basketball-reference.com/contracts/<TEAM>.html``: every player under
contract with the team today, salary by season (this season and five more), player /
team options (cell classes ``salary-pl`` / ``salary-tm``) and guaranteed money. The 30
pages are fetched once per day, 4 seconds apart (robots.txt asks for a 3-second crawl
delay and allows ``/contracts/``), with a plain research User-Agent, and cached gzipped
under ``data/raw/salaries/bbref_current/<date>/`` (gitignored). Basketball-Reference's
terms limit automated collection and redistribution: this is a once-a-day personal
research pull, and the data never leaves the machine.
"""

from __future__ import annotations

import gzip
import logging
import re
import time
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

URL = "https://www.basketball-reference.com/contracts/{team}.html"
USER_AGENT = "Mozilla/5.0 (compatible; courtvision-research/1.3)"
DELAY = 4.0  # seconds between requests (robots.txt: Crawl-delay 3)


def fetch_all(dest: Path, teams: list[str], force: bool = False, delay: float = DELAY):
    """Download each team page once into ``dest`` (skips cached pages unless ``force``)."""
    import requests

    dest.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for team in teams:
        path = dest / f"{team}.html.gz"
        if path.exists() and not force:
            continue
        if fetched:
            time.sleep(delay)
        for attempt in range(2):
            r = requests.get(URL.format(team=team), headers={"User-Agent": USER_AGENT},
                             timeout=30)  # fmt: skip
            if r.status_code == 429 and attempt == 0:  # rate limited: back off once
                log.warning("429 on %s; waiting 60 s", team)
                time.sleep(60)
                continue
            r.raise_for_status()
            break
        path.write_bytes(gzip.compress(r.content))
        fetched += 1
        log.info("fetched %s (%d bytes)", team, len(r.content))
    return fetched


class _ContractsTable(HTMLParser):
    """Rows of ``<table id="contracts">``: header season labels and every body cell's
    data-stat, class, csk and text."""

    def __init__(self):
        super().__init__()
        self.in_table = self.in_body = False
        self.seasons: dict[str, str] = {}
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._cell: tuple[str, dict] | None = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id") == "contracts":
            self.in_table = True
        if not self.in_table:
            return
        if tag == "th" and a.get("data-stat", "").startswith("y") and not self.in_body:
            self.seasons[a["data-stat"]] = a.get("aria-label", "")
        if tag == "tbody":
            self.in_body = True
        elif tag == "tr" and self.in_body:
            self._row = {}
        elif tag in ("th", "td") and self._row is not None:
            self._cell = (a.get("data-stat", ""), a)
            self._text = ""

    def handle_data(self, data):
        if self._cell is not None:
            self._text += data

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag in ("th", "td") and self._cell is not None and self._row is not None:
            stat, a = self._cell
            self._row[stat] = {"class": a.get("class", ""), "csk": a.get("csk"),
                               "text": self._text.strip()}  # fmt: skip
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row.get("player", {}).get("csk"):
                self.rows.append(self._row)
            self._row = None
        elif tag == "tbody":
            self.in_body = False
        elif tag == "table":
            self.in_table = False


def parse(html: str, team: str) -> pd.DataFrame:
    """One row per player x season with salary and option, from one team page."""
    p = _ContractsTable()
    p.feed(html)
    out = []
    for r in p.rows:
        for stat, label in p.seasons.items():
            cell = r.get(stat)
            m = re.match(r"(\d{4})-\d{2}", label)
            if not cell or not m:
                continue
            raw = cell["csk"] or re.sub(r"[^\d]", "", cell["text"])
            if not raw or not raw.isdigit() or int(raw) <= 0:
                continue
            cls = cell["class"]
            option = "player" if "salary-pl" in cls else "team" if "salary-tm" in cls else None
            out.append({"team_abbr": team, "bbref_id": r["player"]["csk"],
                        "player": r["player"]["text"], "season": int(m.group(1)),
                        "salary": float(raw), "option": option,
                        "guaranteed": float(r.get("remain_gtd", {}).get("csk") or 0)})  # fmt: skip
    return pd.DataFrame(out)


def load_dir(src: Path) -> pd.DataFrame:
    frames = [parse(gzip.decompress(f.read_bytes()).decode("utf-8"), f.name.split(".")[0])
              for f in sorted(src.glob("*.html.gz"))]  # fmt: skip
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def latest_dir(root: Path) -> Path | None:
    dirs = sorted(d for d in root.glob("????-??-??") if d.is_dir() and any(d.glob("*.gz")))
    return dirs[-1] if dirs else None


def today_dir(root: Path) -> Path:
    return root / date.today().isoformat()
