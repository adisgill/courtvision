"""A replay viewer: one game's tracking as an animated court, in a single local HTML page.

``courtvision tracking replay GAME_ID`` writes ``data/tracking/replays/<game_id>.html``: the ten
players (jersey numbers on team colors) and the ball (drawn bigger the higher it is) at 25
frames a second, with the game and shot clocks, the score, and every matched shot in a list.
Each shot shows its chance of going in (``shot_quality.parquet``) and, at the release, a line
to the closest defender. Space plays and pauses, the arrow keys step a second, and clicking a
shot jumps to just before it.

The page holds raw SportVU positions, so it stays on this machine (decision D1): it's written
under ``data/``, which git ignores, and nothing here uploads it.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import pandas as pd

from courtvision.config import get_paths
from courtvision.tracking import sportvu
from courtvision.tracking.sportvu import SLOTS

MISSING = -32768


def replays_dir() -> Path:
    return get_paths().data / "tracking" / "replays"


def _team_abbr(team_id: int) -> str:
    try:
        from nba_api.stats.static import teams

        t = teams.find_team_name_by_id(team_id)
        return t["abbreviation"] if t else str(team_id)
    except Exception:
        return str(team_id)


def _score_events(game_id: str, home: int) -> list[list]:
    """[period, seconds left, home points, away points] after each scoring play."""
    path = get_paths().data / "tracking" / "pbp_2015.parquet"
    if not path.exists():
        return []
    p = pd.read_parquet(path, filters=[("game_id", "==", game_id)])
    made = p.actionType.eq("Made Shot")
    ft = p.actionType.eq("Free Throw") & ~p.description.str.startswith("MISS")
    pts = np.where(made, np.where(p.description.str.contains("3PT"), 3, 2), np.where(ft, 1, 0))
    p = p.assign(pts=pts)[pts > 0].sort_values(["period", "t"], ascending=[True, False])
    h = a = 0
    out = []
    for r in p.itertuples(index=False):
        if r.team_id == home:
            h += r.pts
        else:
            a += r.pts
        out.append([int(r.period), float(r.t), h, a])
    return out


def payload(game_id: str, periods: list[int] | None = None) -> dict:
    """Everything the page needs, frames packed as base64 int16 (tenths of a foot/second)."""
    f = sportvu.load_frames(game_id)
    home, away = int(f.home_team_id.iloc[0]), int(f.away_team_id.iloc[0])
    keep = f.period.isin(periods) if periods else np.ones(len(f), bool)
    index = np.flatnonzero(keep)  # positions in the full game, to place the shots
    f = f.iloc[index].reset_index(drop=True)

    cols = [f[c].to_numpy(float) for c in ("period", "game_clock", "shot_clock",
                                           "ball_x", "ball_y", "ball_z")]  # fmt: skip
    for s in SLOTS:
        cols += [f[f"{s}_x"].to_numpy(float), f[f"{s}_y"].to_numpy(float)]
    a = np.column_stack(cols)
    a[:, 1:] *= 10  # period stays whole; the rest to tenths
    packed = np.where(np.isnan(a), MISSING, np.round(a)).clip(MISSING, 32767).astype("<i2")

    ids = f[[f"{s}_id" for s in SLOTS]].to_numpy()
    change = np.flatnonzero(np.r_[True, (ids[1:] != ids[:-1]).any(axis=1)])
    lineups = [[int(i), [int(x) for x in ids[i]]] for i in change]

    roster = pd.read_parquet(sportvu.frames_dir().parent / "rosters" / f"{game_id}.parquet")
    players = {int(r.person_id): {"name": f"{r.first_name} {r.last_name}", "jersey": str(r.jersey),
                                  "team": 0 if r.team_id == home else 1}
               for r in roster.itertuples(index=False)}  # fmt: skip

    shots = []
    sq_path = get_paths().data / "tracking" / "shot_quality.parquet"
    sh_path = get_paths().data / "tracking" / "shots" / f"{game_id}.parquet"
    if sh_path.exists():
        sh = pd.read_parquet(sh_path)
        if sq_path.exists():
            sq = pd.read_parquet(sq_path, filters=[("game_id", "==", game_id)])
            sh = sh.merge(sq[["action_number", "xfg", "closest_def", "catch_shoot", "dribbles",
                              "touch_s"]], left_on="actionNumber",
                          right_on="action_number", how="left")  # fmt: skip
        pos = {int(g): i for i, g in enumerate(index)}
        for r in sh.itertuples(index=False):
            if int(r.release) not in pos:
                continue
            get = r._asdict().get
            shots.append({
                "frame": pos[int(r.release)], "person_id": int(r.person_id),
                "made": r.shotResult == "Made", "desc": str(r.description),
                "three": "3PT" in str(r.description), "period": int(r.period),
                "xfg": None if pd.isna(get("xfg")) else round(float(get("xfg")), 3),
                "closest_def": None if pd.isna(get("closest_def"))
                else round(float(get("closest_def")), 1),
                "catch_shoot": bool(get("catch_shoot")) if get("catch_shoot") is not None
                and not pd.isna(get("catch_shoot")) else None,
                "dribbles": None if pd.isna(get("dribbles")) else int(get("dribbles")),
            })  # fmt: skip
    shots.sort(key=lambda s: s["frame"])
    return {
        "game_id": game_id, "teams": [_team_abbr(home), _team_abbr(away)],
        "n": len(f), "stride": a.shape[1], "missing": MISSING,
        "frames": base64.b64encode(packed.tobytes()).decode("ascii"),
        "lineups": lineups, "players": players, "shots": shots,
        "score": _score_events(game_id, home),
    }  # fmt: skip


def build(game_id: str, periods: list[int] | None = None, out: Path | None = None) -> Path:
    """Write the replay page for ``game_id`` (all periods unless ``periods``)."""
    data = payload(game_id, periods)
    out = out or replays_dir() / f"{game_id}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    title = f"{data['teams'][1]} at {data['teams'][0]} · {game_id}"
    page = (Path(__file__).with_name("replay.html")).read_text(encoding="utf-8")
    html = page.replace("__TITLE__", title).replace("__DATA__", json.dumps(data))
    out.write_text(html, encoding="utf-8")
    return out
