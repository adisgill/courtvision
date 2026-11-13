"""The NBA's official injury report (user-approved fetch).

The league publishes it as a PDF every 15 minutes on game days
(``https://official.nba.com/nba-injury-report-<season>-season/``, files on
``ak-static.cms.nba.com``): every listed player of every game with his status (Out,
Doubtful, Questionable, Probable, Available) and the reason. ``courtvision ingest
injuries --fetch`` downloads the latest one (two requests: the index page, the PDF;
robots.txt allows both, crawl delay 1 s), cached under ``data/raw/injuries/<date>/``
(gitignored, kept on this machine). Plain ``build`` and ``forecast`` reuse the cache.

The PDF is simple (GemBox, WinAnsi text drawn word by word in a fixed table), so it is
read with the standard library: decompress each page's content stream, collect every
word with its position, sort words into rows by height and into columns by the header
cells' positions, and carry the date, time, matchup and team down to the rows that leave
them blank. No PDF library is needed.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
import zlib
from datetime import datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

INDEX = "https://official.nba.com/nba-injury-report-{s}-{e:02d}-season/"
USER_AGENT = "Mozilla/5.0 (compatible; courtvision-research/1.6)"
PDF_RE = re.compile(r"https://ak-static\.cms\.nba\.com/referee/injury/"
                    r"Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_?(\d{2})?(AM|PM)\.pdf")  # fmt: skip
COLUMNS = ["Game Date", "Game Time", "Matchup", "Team", "Player Name", "Current Status",
           "Reason"]  # fmt: skip
STATUSES = ["Out", "Doubtful", "Questionable", "Probable", "Available"]
MAX_AGE_DAYS = 2  # a report older than this (before the game's date) is ignored


# ---- fetch -------------------------------------------------------------------------------


def latest_url(season: int) -> tuple[str, datetime]:
    """The newest report linked from the season's index page, and its time (ET)."""
    import requests

    r = requests.get(INDEX.format(s=season, e=(season + 1) % 100),
                     headers={"User-Agent": USER_AGENT}, timeout=30)  # fmt: skip
    r.raise_for_status()
    found = []
    for m in PDF_RE.finditer(r.text):
        day, hh, mm, ampm = m.groups()
        t = datetime.strptime(f"{day} {hh}:{mm or '00'} {ampm}", "%Y-%m-%d %I:%M %p")
        found.append((t, m.group(0)))
    if not found:
        raise RuntimeError("no injury report linked from the index page")
    t, url = max(found)
    return url, t


def fetch(root: Path, season: int) -> Path:
    """Download the latest report into ``root/<date>/`` (once; cached by file name)."""
    import requests

    url, t = latest_url(season)
    dest = root / t.date().isoformat() / url.rsplit("/", 1)[-1]
    if dest.exists():
        return dest
    time.sleep(1.5)  # robots.txt: crawl delay 1 s
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"not a PDF: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    log.info("fetched %s (%d bytes)", dest.name, len(r.content))
    return dest


def latest_cached(root: Path) -> Path | None:
    files = sorted(root.glob("*/Injury-Report_*.pdf"), key=report_time)
    return files[-1] if files else None


def report_time(path: Path) -> datetime:
    m = re.search(r"Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_?(\d{2})?(AM|PM)", path.name)
    if not m:
        return datetime.min
    day, hh, mm, ampm = m.groups()
    return datetime.strptime(f"{day} {hh}:{mm or '00'} {ampm}", "%Y-%m-%d %I:%M %p")


# ---- read the PDF ------------------------------------------------------------------------

_TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|\[(?:\((?:\\.|[^\\)])*\)|[^\]])*\]|"
                    rb"[-+]?\d*\.\d+|[-+]?\d+|/[^\s/\[\]()<>]+|[A-Za-z*'\"]+")  # fmt: skip


def _unescape(s: bytes) -> str:
    out, i = bytearray(), 0
    while i < len(s):
        c = s[i]
        if c == 0x5C and i + 1 < len(s):  # backslash
            n = s[i + 1]
            if n in b"01234567":
                j = i + 1
                while j < len(s) and j < i + 4 and s[j] in b"01234567":
                    j += 1
                out.append(int(s[i + 1 : j], 8) & 0xFF)
                i = j
                continue
            out.append({ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8,
                        ord("f"): 12}.get(n, n))  # fmt: skip
            i += 2
            continue
        out.append(c)
        i += 1
    return out.decode("cp1252", errors="replace")


def page_words(stream: bytes) -> list[tuple[float, float, str, tuple]]:
    """(x, y, word, cell) for every string drawn in one decompressed content stream;
    ``cell`` is the rectangle last drawn or clipped before the text (the table cell)."""
    words, stack = [], []
    x0 = y0 = 0.0
    rect: tuple = ()
    cell: tuple = ()
    for tok in _TOKEN.findall(stream):
        if tok[:1] in b"([" or tok[:1].isdigit() or tok[:1] in b"-+." or tok[:1] == b"/":
            stack.append(tok)
            continue
        op = tok
        if op == b"re" and len(stack) >= 4:
            rect = tuple(round(float(v), 1) for v in stack[-4:])
        elif op == b"BT":
            x0 = y0 = 0.0
            cell = rect
        elif op == b"Td" and len(stack) >= 2:
            x0 += float(stack[-2])
            y0 += float(stack[-1])
        elif op == b"Tm" and len(stack) >= 6:
            x0, y0 = float(stack[-2]), float(stack[-1])
        elif op == b"Tj" and stack and stack[-1][:1] == b"(":
            words.append((x0, y0, _unescape(stack[-1][1:-1]), cell))
        elif op == b"TJ" and stack and stack[-1][:1] == b"[":
            parts = re.findall(rb"\((?:\.|[^\)])*\)", stack[-1])
            words.append((x0, y0, "".join(_unescape(p[1:-1]) for p in parts), cell))
        stack = []
    return [w for w in words if w[2].strip()]


def _cell_text(ws: list) -> str:
    """Words of one cell in reading order; a word ending in '-' joins the next line."""
    out = ""
    for _, _, w, _ in sorted(ws, key=lambda w: (round(w[1], 0), w[0])):
        out += w if not out or (out.endswith("-") and not out.endswith(" -")) else " " + w
    return out


def parse_pdf(data: bytes) -> pd.DataFrame:
    """One row per listed player (or per team that hasn't submitted yet)."""
    pages = []
    for raw in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            words = page_words(zlib.decompress(raw))
        except zlib.error:
            continue
        text = " ".join(w[2] for w in words)
        m = re.search(r"Page (\d+) of (\d+)", text)
        if m:
            pages.append((int(m.group(1)), words))
    pages.sort(key=lambda p: p[0])
    stamp = None
    bounds: list[tuple[str, float]] = []
    rows: list[dict] = []
    for _, words in pages:
        cells: dict[tuple, list] = {}
        for w in words:
            if len(w[3]) == 4:
                cells.setdefault(w[3], []).append(w)
        texts = {c: _cell_text(ws) for c, ws in cells.items()}
        if stamp is None:
            for t in texts.values():
                m = re.search(r"Injury Report: (\d{2}/\d{2}/\d{2}) (\d{2}:\d{2}) (AM|PM)", t)
                if m:
                    stamp = datetime.strptime(" ".join(m.groups()), "%m/%d/%y %I:%M %p")
        head = {t: c for c, t in texts.items() if t in COLUMNS}
        if len(head) == len(COLUMNS):  # this page repeats the header: columns from it
            bounds = sorted(((t, c[0]) for t, c in head.items()), key=lambda b: b[1])
        if not bounds:
            continue
        top = max((c[1] for t, c in head.items()), default=-1e9)
        by_row: dict[float, dict] = {}
        for c, t in texts.items():
            if c[1] <= top or t in COLUMNS or re.fullmatch(r"Page \d+ of \d+", t):
                continue
            if t.startswith("Injury Report:"):
                continue
            col = [n for n, x in bounds if x <= c[0] + 1]
            if not col:
                continue
            by_row.setdefault(c[1], {})[col[-1]] = t
        for _, r in sorted(by_row.items()):
            rows.append({k: r.get(k, "") for k in COLUMNS})
    df = pd.DataFrame(rows, columns=COLUMNS)
    for c in ("Game Date", "Game Time", "Matchup", "Team"):
        df[c] = df[c].replace("", pd.NA).ffill()
    df = df.rename(columns={"Game Date": "game_date", "Game Time": "game_time",
                            "Matchup": "matchup", "Team": "team", "Player Name": "player",
                            "Current Status": "status", "Reason": "reason"})  # fmt: skip
    df["game_date"] = pd.to_datetime(df.game_date, format="%m/%d/%Y", errors="coerce")
    df["report_time"] = stamp
    return df


def _has_reports(con) -> bool:
    return bool(con.execute("select count(*) from information_schema.tables where "
                            "table_schema = 'injury' and table_name = 'reports'"
                            ).fetchall()[0][0])  # fmt: skip


# ---- match and load ----------------------------------------------------------------------


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]", " ", s.lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return " ".join(s.split())


def match(con, df: pd.DataFrame, season: int) -> pd.DataFrame:
    """Team ids from full team names; person ids from "Last, First", preferring a player
    who has played for that team this season or last."""
    from courtvision.ingest.contracts import _team_ids
    from courtvision.ingest.salaries import norm_team

    ids = _team_ids(con)
    df = df.copy()
    df["team_id"] = df.team.map(lambda t: ids.get(norm_team(str(t))))
    ppl = con.execute(
        """select p.person_id, p.first_name, p.last_name,
                  list(distinct c.team_id) filter (where c.team_id is not null) as teams,
                  max(c.season) as last_season
           from stg.players p
           left join metrics.player_game_counts c
             on c.person_id = p.person_id and c.season >= ? and c.season_type = 'regular'
           group by all""", [season - 1]).df()  # fmt: skip
    ppl["key"] = (ppl.first_name.fillna("") + " " + ppl.last_name.fillna("")).map(_norm)
    by_key = {k: g for k, g in ppl.groupby("key")}

    def pid(row) -> float:
        name = str(row.player or "")
        if "," not in name:
            return float("nan")
        last, first = name.split(",", 1)
        g = by_key.get(_norm(f"{first} {last}"))
        if g is None:
            return float("nan")
        if len(g) > 1:
            on = g[g.teams.map(lambda t: hasattr(t, "__iter__") and row.team_id in list(t))]
            g = on if len(on) else g.sort_values("last_season", ascending=False)
        return float(g.person_id.iloc[0])

    df["person_id"] = df.apply(pid, axis=1)
    return df


def load(con, path: Path, season: int) -> dict:
    """Parse ``path`` and add it to ``injury.reports`` (one row per player per report)."""
    df = match(con, parse_pdf(path.read_bytes()), season)
    df["source_file"] = path.name
    con.execute("create schema if not exists injury")
    exists = _has_reports(con)
    con.register("tmp_df", df)
    if exists:
        con.execute("delete from injury.reports where source_file = ?", [path.name])
        con.execute("insert into injury.reports by name select * from tmp_df")
    else:
        con.execute("create table injury.reports as select * from tmp_df")
    con.unregister("tmp_df")
    players = df[df.player.fillna("").str.contains(",")]
    return {"report": str(df.report_time.iloc[0]) if len(df) else None, "rows": len(df),
            "players": len(players), "matched": float(players.person_id.notna().mean())
            if len(players) else 0.0,
            "by_status": players.status.value_counts().to_dict()}  # fmt: skip


def for_games(con, games: pd.DataFrame) -> pd.DataFrame:
    """Each upcoming game's listed players from the newest report: the rows for that
    game's date, or, when the report doesn't reach that date yet, the players it lists as
    Out on their latest game (``carried``: long injuries rarely clear overnight; day-to-day
    statuses aren't carried). Columns: game_id, team_id, person_id, player, status,
    reason, carried, report_time."""
    has = _has_reports(con)
    cols = ["game_id", "team_id", "person_id", "player", "status", "reason", "carried",
            "report_time"]  # fmt: skip
    if not has or games.empty:
        return pd.DataFrame(columns=cols)
    rep = con.execute("""select * from injury.reports
                         where report_time = (select max(report_time) from injury.reports)""").df()
    rep["game_date"] = pd.to_datetime(rep.game_date)
    out = []
    for g in games.itertuples():
        gd = pd.Timestamp(g.game_date).normalize()
        if (gd - pd.Timestamp(rep.report_time.max()).normalize()).days > MAX_AGE_DAYS:
            continue  # the newest report is too old to speak for this game
        for team in (g.home_team_id, g.away_team_id):
            r = rep[(rep.team_id == team) & (rep.game_date == gd)]
            carried = False
            if r.empty:
                r = rep[(rep.team_id == team) & (rep.status == "Out") & (rep.game_date < gd)]
                carried = True
            for x in r.itertuples():
                out.append({"game_id": g.game_id, "team_id": team, "person_id": x.person_id,
                            "player": x.player, "status": x.status, "reason": x.reason,
                            "carried": carried, "report_time": x.report_time})  # fmt: skip
    return pd.DataFrame(out, columns=cols)
