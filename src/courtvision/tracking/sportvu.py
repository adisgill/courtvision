"""The public 2015-16 SportVU logs -> tracking frames, one Parquet file per game.

The logs NBA.com briefly published in 2016 are collected in the GitHub repo ``REPO`` (636
regular-season games, Oct 2015 - Jan 2016, one 7z archive of one JSON file each; the repo has no
license, so only derived numbers are ever published, never these positions). Each JSON lists the
game's play-by-play events, each with its own run of frames; runs overlap, so frames are
de-duplicated by timestamp.

A frame: quarter, timestamp (ms), game clock, shot clock, then the ball (x, y, z) and 10 players
(team, NBA person id, x, y), in feet on a 94 x 50 court (hoops at x 5.25 and 88.75, y 25).
Stored wide: home players in slots h0-h4 and away players in a0-a4, each team sorted by person
id, so a player keeps his slot while the same five are on the floor.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from courtvision.config import get_paths

log = logging.getLogger(__name__)

REPO = "linouk23/NBA-Player-Movements"
FOLDER = "data/2016.NBA.Raw.SportVU.Game.Logs"
LIST_URL = f"https://api.github.com/repos/{REPO}/contents/{FOLDER}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/master/{FOLDER}/" + "{name}"
SLOTS = [f"h{i}" for i in range(5)] + [f"a{i}" for i in range(5)]


def raw_dir() -> Path:
    return get_paths().data / "raw" / "sportvu"


def frames_dir() -> Path:
    return get_paths().data / "tracking" / "frames"


def list_archives() -> list[dict]:
    """Every game archive in the repo: name and size (one GitHub API request)."""
    req = urllib.request.Request(LIST_URL, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return [{"name": f["name"], "size": f["size"]} for f in json.load(r)
                if f["name"].endswith(".7z")]  # fmt: skip


def fetch(names: list[str] | None = None, pause: float = 0.3) -> dict:
    """Download the archives not yet here (all of them unless ``names``), with a manifest."""
    dest = raw_dir() / "7z"
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = raw_dir() / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    listing = list_archives()
    wanted = [a for a in listing if names is None or a["name"] in names]
    got = 0
    for a in wanted:
        path = dest / a["name"]
        if path.exists() and path.stat().st_size == a["size"]:
            continue
        part = path.with_suffix(".part")
        with urllib.request.urlopen(RAW_URL.format(name=a["name"]), timeout=120) as r, \
                open(part, "wb") as f:  # fmt: skip
            shutil.copyfileobj(r, f, 2**20)
        if part.stat().st_size != a["size"]:
            part.unlink()
            raise OSError(f"{a['name']}: got {part.stat().st_size} bytes, expected {a['size']}")
        part.replace(path)
        manifest[a["name"]] = {"size": a["size"], "fetched_at_utc": datetime.now(UTC).isoformat()}
        got += 1
        if got % 25 == 0:
            log.info("sportvu: %d downloaded", got)
            manifest_path.write_text(json.dumps(manifest, indent=1))
        time.sleep(pause)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    return {"listed": len(listing), "wanted": len(wanted), "downloaded": got}


def _tar() -> str:
    """A tar that reads 7z: Windows' built-in bsdtar (libarchive), or bsdtar elsewhere."""
    win = Path(r"C:\Windows\System32\tar.exe")
    if sys.platform == "win32" and win.exists():
        return str(win)
    found = shutil.which("bsdtar")
    if not found:
        raise RuntimeError("extracting 7z needs bsdtar (libarchive); install it")
    return found


def extract(archive: Path, dest: Path) -> Path:
    """Unpack one game archive into ``dest``; returns the JSON file."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.stat().st_size < 100:  # empty in the source repo
        raise ValueError(f"{archive.name}: empty archive in the source (no tracking)")
    before = set(dest.glob("*.json"))
    subprocess.run([_tar(), "-xf", str(archive), "-C", str(dest)], check=True,
                   capture_output=True)  # fmt: skip
    new = sorted(set(dest.glob("*.json")) - before)
    if len(new) != 1:
        raise RuntimeError(f"{archive.name}: expected one JSON, found {len(new)}")
    return new[0]


def parse(game: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One game's JSON -> (frames, roster)."""
    gid = str(game["gameid"])
    first = next(e for e in game["events"] if e.get("home"))
    home, away = int(first["home"]["teamid"]), int(first["visitor"]["teamid"])
    roster: dict[int, dict] = {}
    for e in game["events"]:
        for side, team in (("home", home), ("visitor", away)):
            for p in e.get(side, {}).get("players", []):
                roster.setdefault(int(p["playerid"]), {
                    "game_id": gid, "team_id": team, "person_id": int(p["playerid"]),
                    "first_name": p["firstname"], "last_name": p["lastname"],
                    "jersey": p["jersey"], "position": p["position"]})  # fmt: skip

    seen: dict[int, tuple] = {}
    for e in game["events"]:
        eid = int(e["eventId"])
        for q, ts, gclock, sclock, _, ents in e["moments"]:
            if ts not in seen:
                seen[ts] = (q, gclock, sclock, eid, ents)
    stamps = sorted(seen)
    n = len(stamps)
    if not n:  # five games in the source have no tracking (four empty archives, one like this)
        raise ValueError(f"{gid}: the log has events but no tracking frames")
    period = np.empty(n, np.int8)
    gclock = np.empty(n, np.float32)
    sclock = np.full(n, np.nan, np.float32)
    event = np.empty(n, np.int16)
    ball = np.full((n, 3), np.nan, np.float32)
    ids = np.zeros((n, 10), np.int32)
    xy = np.full((n, 10, 2), np.nan, np.float32)
    for i, ts in enumerate(stamps):
        q, gc, sc, eid, ents = seen[ts]
        period[i], gclock[i], event[i] = q, gc, eid
        if sc is not None:
            sclock[i] = sc
        hs = sorted((r for r in ents if r[0] == home), key=lambda r: r[1])[:5]
        aw = sorted((r for r in ents if r[0] == away), key=lambda r: r[1])[:5]
        for r in ents:
            if r[0] == -1:
                ball[i] = r[2:5]
                break
        for k, r in enumerate(hs):
            ids[i, k], xy[i, k] = r[1], r[2:4]
        for k, r in enumerate(aw):
            ids[i, 5 + k], xy[i, 5 + k] = r[1], r[2:4]

    cols = {"game_id": gid, "ts": np.array(stamps, np.int64), "period": period,
            "game_clock": gclock, "shot_clock": sclock, "event_id": event,
            "ball_x": ball[:, 0], "ball_y": ball[:, 1], "ball_z": ball[:, 2]}  # fmt: skip
    for k, s in enumerate(SLOTS):
        cols[f"{s}_id"] = ids[:, k]
        cols[f"{s}_x"] = xy[:, k, 0]
        cols[f"{s}_y"] = xy[:, k, 1]
    frames = pd.DataFrame(cols)
    frames.attrs.update(home_team_id=home, away_team_id=away)
    return frames, pd.DataFrame(list(roster.values()))


def convert(archive: Path, keep_json: bool = False) -> dict:
    """Archive -> ``frames/<game_id>.parquet`` and ``rosters/<game_id>.parquet``."""
    tmp = raw_dir() / "json" / archive.stem  # one folder per archive: workers run side by side
    path = extract(archive, tmp)
    try:
        game = json.loads(path.read_text(encoding="utf-8"))
        frames, roster = parse(game)
    finally:
        if not keep_json:
            shutil.rmtree(tmp, ignore_errors=True)
    gid = frames.game_id.iloc[0]
    out = frames_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out.parent / "rosters").mkdir(parents=True, exist_ok=True)
    frames.assign(home_team_id=frames.attrs["home_team_id"],
                  away_team_id=frames.attrs["away_team_id"]).to_parquet(
        out / f"{gid}.parquet", compression="zstd", index=False)  # fmt: skip
    roster.to_parquet(out.parent / "rosters" / f"{gid}.parquet", index=False)
    return {"archive": archive.name, "game_id": gid, "frames": len(frames),
            "game_date": game.get("gamedate")}  # fmt: skip


def convert_safe(archive: str) -> dict:
    """``convert`` for a worker process: an error comes back as a result, not a crash."""
    try:
        return convert(Path(archive))
    except Exception as e:  # one bad archive shouldn't stop the rest
        return {"archive": Path(archive).name, "error": f"{type(e).__name__}: {e}"}


def load_frames(game_id: str) -> pd.DataFrame:
    return pd.read_parquet(frames_dir() / f"{game_id}.parquet")


def game_ids() -> list[str]:
    return sorted(p.stem for p in frames_dir().glob("*.parquet"))
