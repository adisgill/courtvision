"""Live mode: rate a game in progress against the season (``courtvision live``).

The NBA live feed (``cdn.nba.com`` liveData play-by-play, via ``nba_api.live``) has the
same schema as the Kaggle play-by-play's live-format rows, so a live game goes through
exactly the same pipeline as history:

1. actions -> :class:`courtvision.metrics.pbp.Event` (the ``stg.events`` live branch);
2. :func:`~courtvision.metrics.pbp.parse_game` -> possessions, rebound kinds, lineups;
3. team counts with the same column names as ``metrics.team_game_counts``;
4. the same :data:`~courtvision.metrics.measures.MEASURES` expressions, evaluated by
   DuckDB against this season's league baselines;
5. ratings on the **team-game** scale of this season (``metrics.reference_stats``), and
   the in-game signal: tonight minus the team's average game this season (spec section 2).

Box-score inputs (``b_*``) come from the same play-by-play in live mode, since the live
box score updates on a different cadence. Every raw response is cached under
``data/raw/live/<game_id>/`` and requests are rate-limited.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from courtvision.config import get_paths
from courtvision.metrics import measures as M
from courtvision.metrics import pbp, rate

MIN_POLL_SECONDS = 10.0
_last_request: dict[str, float] = {}

ZONE_VALUE = {"RA": 2, "PAINT": 2, "MID": 2, "C3": 3, "ATB3": 3}
ZONE_KEY = {"RA": "ra", "PAINT": "pnt", "MID": "mid", "C3": "c3", "ATB3": "atb3"}
_FT_N_OF = re.compile(r"(\d) ?of ?(\d)")


# ---- fetching -----------------------------------------------------------------------------


def _cache_dir(game_id: str) -> Path:
    d = get_paths().data / "raw" / "live" / game_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(game_id: str, kind: str, payload: dict) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _cache_dir(game_id) / f"{kind}_{stamp}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)


def _latest_cached(game_id: str, kind: str) -> dict | None:
    files = sorted(_cache_dir(game_id).glob(f"{kind}_*.json.gz"))
    if not files:
        return None
    with gzip.open(files[-1], "rt", encoding="utf-8") as f:
        return json.load(f)


def fetch(
    game_id: str, kind: str = "playbyplay", force: bool = False, offline: bool = False
) -> dict:
    """Live play-by-play or box score, at most one request per game every 10 s.

    Within the rate limit the latest cached response is returned instead; ``offline``
    always uses the cache (and fails if there is none).
    """
    from nba_api.live.nba.endpoints import boxscore, playbyplay

    if offline:
        cached = _latest_cached(game_id, kind)
        if cached is None:
            raise FileNotFoundError(f"no cached {kind} for {game_id}")
        return cached
    key = f"{game_id}:{kind}"
    if not force and time.time() - _last_request.get(key, 0) < MIN_POLL_SECONDS:
        cached = _latest_cached(game_id, kind)
        if cached is not None:
            return cached
    endpoint = {"playbyplay": playbyplay.PlayByPlay, "boxscore": boxscore.BoxScore}[kind]
    payload = endpoint(game_id).get_dict()
    _last_request[key] = time.time()
    _save(game_id, kind, payload)
    return payload


def scoreboard() -> pd.DataFrame:
    """Today's games from the live scoreboard (cached like every other response)."""
    from nba_api.live.nba.endpoints import scoreboard as sb

    payload = sb.ScoreBoard().get_dict()
    _save("scoreboard", "scoreboard", payload)
    rows = []
    for g in payload.get("scoreboard", {}).get("games", []):
        rows.append(
            {
                "game_id": g["gameId"],
                "status": g.get("gameStatusText", ""),
                "away": g["awayTeam"]["teamTricode"],
                "home": g["homeTeam"]["teamTricode"],
                "away_score": g["awayTeam"].get("score"),
                "home_score": g["homeTeam"].get("score"),
            }
        )
    return pd.DataFrame(rows)


# ---- actions -> events ----------------------------------------------------------------------


def _clock_s(clock: str | None) -> float:
    m = re.match(r"PT(\d+)M([\d.]+)S", clock or "")
    return int(m.group(1)) * 60 + float(m.group(2)) if m else 0.0


def _int(v) -> int | None:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i or None


def _is_team(i: int | None) -> bool:
    return i is not None and 1610612737 <= i <= 1610612766


def actions_to_events(game_id: str, actions: list[dict]) -> list[pbp.Event]:
    """The live branch of ``stg.events``, in Python."""
    out = []
    actions = sorted(actions, key=lambda a: (a.get("period", 0), a.get("orderNumber", 0)))
    for seq, a in enumerate(actions, start=1):
        act = (a.get("actionType") or "").lower()
        sub = (a.get("subType") or "").strip()
        desc = (a.get("descriptor") or "").lower()
        etype = {
            "2pt": "fga", "3pt": "fga", "freethrow": "ft", "rebound": "reb",
            "turnover": "tov", "foul": "foul", "substitution": "sub", "jumpball": "jump",
            "violation": "viol", "timeout": "timeout",
        }.get(act, "other")  # fmt: skip
        if act == "period":
            etype = "period_start" if sub.lower() == "start" else "period_end"
        person = _int(a.get("personId"))
        person = None if _is_team(person) else person
        team = _int(a.get("teamId"))
        if not _is_team(team) and etype in ("reb", "tov"):
            team = _int(a.get("possession"))
        period = int(a.get("period") or 1)
        start, end = pbp.period_bounds(period)
        n_of = _FT_N_OF.search(sub)
        result = (a.get("shotResult") or "").lower()
        out.append(
            pbp.Event(
                game_id=game_id,
                seq=seq,
                period=period,
                elapsed_s=end - _clock_s(a.get("clock")),
                etype=etype,
                team_id=team if _is_team(team) else None,
                person_id=person,
                player_name=a.get("playerName"),
                made=(result == "made") if etype in ("fga", "ft") else None,
                ft_technical=("technical" in desc or "technical" in sub.lower())
                if etype == "ft"
                else None,
                ft_last=(
                    bool(n_of)
                    and n_of.group(1) == n_of.group(2)
                    and not re.search("technical|flagrant|clear|away|take", f"{sub} {desc}".lower())
                )
                if etype == "ft"
                else None,
                reb_kind={"offensive": "off", "defensive": "def"}.get(sub.lower())
                if etype == "reb"
                else None,
                bench_possible=(etype == "foul" and "technical" in sub.lower())
                or act == "ejection",
                sub_dir=sub.lower() if etype == "sub" else None,
                sub_in_person_id=None,
                sub_in_name=None,
                sub_out_name=None,
                assist_person_id=_int(a.get("assistPersonId")),
                steal_person_id=_int(a.get("stealPersonId")),
                block_person_id=_int(a.get("blockPersonId")),
                foul_drawn_person_id=_int(a.get("foulDrawnPersonId")),
                pbp_format="live",
            )
        )
    # keep x/y and shot value alongside (not part of the parser's Event)
    return out


def _shot_details(actions: list[dict]) -> dict[int, tuple[int, float | None, float | None]]:
    actions = sorted(actions, key=lambda a: (a.get("period", 0), a.get("orderNumber", 0)))
    out = {}
    for seq, a in enumerate(actions, start=1):
        act = (a.get("actionType") or "").lower()
        if act in ("2pt", "3pt"):
            out[seq] = (3 if act == "3pt" else 2, a.get("xLegacy"), a.get("yLegacy"))
    return out


def zone(value: int, x, y) -> str | None:
    """Python twin of the ``cv_zone`` SQL macro."""
    if x is None or y is None:
        return None
    if value == 3 and y > 417.5:
        return "BACK"
    if value == 3 and abs(x) >= 220 and y <= 87:
        return "C3"
    if value == 3:
        return "ATB3"
    if math.hypot(x, y) < 40:
        return "RA"
    if abs(x) <= 80 and y <= 138:
        return "PAINT"
    return "MID"


# ---- counts and ratings ---------------------------------------------------------------------


def roster_from_box(box: dict) -> dict[int, pbp.RosterEntry]:
    out = {}
    g = box.get("game", {})
    for side in ("homeTeam", "awayTeam"):
        t = g.get(side, {})
        for p in t.get("players", []):
            mins = _clock_s(p.get("statistics", {}).get("minutes")) / 60
            out[int(p["personId"])] = pbp.RosterEntry(
                int(t["teamId"]), mins, p.get("familyName"), p.get("firstName")
            )
    return out


def team_counts(
    events: list[pbp.Event],
    shots: dict[int, tuple],
    parsed: pbp.GameResult,
    zone_xpts: dict[str, float],
    positions: dict[int, tuple[float, float]],
    teams: list[int],
) -> pd.DataFrame:
    """One row per team with the ``metrics.team_game_counts`` column names."""
    c: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    steals: list[tuple[int, int]] = []
    reb_kind = {e[1]: e[5] for e in parsed.events}
    for ev in events:
        t = ev.team_id
        if t is None:
            continue
        r = c[t]
        w_bc, w_fc = positions.get(ev.person_id, (0.0, 0.0)) if ev.person_id else (0.0, 0.0)
        if ev.etype == "fga":
            value, x, y = shots.get(ev.seq, (2, None, None))
            z = zone(value, x, y)
            made = bool(ev.made)
            r["fga"] += 1
            r["fgm"] += made
            r["pts_fg"] += value * made
            if z in ZONE_VALUE:
                xp = zone_xpts.get(z, 0.0)
                for pre, w in (("", 1.0), ("bc_", w_bc), ("fc_", w_fc)):
                    r[f"{pre}fga_r"] += w
                    r[f"{pre}pts_r"] += w * value * made
                    r[f"{pre}xp_r"] += w * xp
                r["fgm_r"] += made
                grp = "paint" if z in ("RA", "PAINT") else "perim"
                r[f"fga_{grp}"] += 1
                r[f"pts_{grp}"] += value * made
                r[f"xp_{grp}"] += xp
                r[f"fga_{ZONE_KEY[z]}"] += 1
                r[f"fgm_{ZONE_KEY[z]}"] += made
            r["bc_fga"] += w_bc
            r["fc_fga"] += w_fc
        elif ev.etype == "ft":
            r["fta"] += 1
            r["ftm"] += bool(ev.made)
            r["bc_fta"] += w_bc
            r["fc_fta"] += w_fc
            r["bc_b_ftm"] += w_bc * bool(ev.made)
            r["fc_b_ftm"] += w_fc * bool(ev.made)
        elif ev.etype == "tov":
            r["tov"] += 1
            r["bc_tov"] += w_bc
            r["fc_tov"] += w_fc
        elif ev.etype == "reb" and ev.person_id is not None:
            kind = reb_kind.get(ev.seq)
            if kind in ("off", "def"):
                r["oreb" if kind == "off" else "dreb"] += 1
        elif ev.etype == "foul" and ev.person_id is not None and not ev.bench_possible:
            r["bc_b_pf"] += w_bc
            r["fc_b_pf"] += w_fc
            r["b_pf"] += 1
        if ev.steal_person_id:
            # the steal belongs to the defense: the team that did not commit the turnover
            steals.append((t, ev.steal_person_id))
    for p in parsed.possessions:
        c[p[2]]["poss"] += 1
    teams = sorted(teams)
    for t, pid in steals:
        other = [o for o in teams if o != t]
        if other:
            s_bc, s_fc = positions.get(pid, (0.0, 0.0))
            c[other[0]]["bc_b_stl"] += s_bc
            c[other[0]]["fc_b_stl"] += s_fc
            c[other[0]]["b_stl"] += 1
    rows = []
    for t in teams:
        r = dict(c[t])
        opp = [o for o in teams if o != t]
        o = dict(c[opp[0]]) if opp else {}
        row = {"team_id": t}
        for col in rate.PRESENCE_COLS:
            if not col.startswith("o_"):
                row[col] = r.get(col, 0.0)
        for col in [k for k in rate.PRESENCE_COLS if not k.startswith("o_")]:
            row[f"o_{col}"] = o.get(col, 0.0)
        for k in ("fga_r", "fgm_r", "pts_r", "xp_r"):
            row[f"a_{k}"] = row.get(k, 0.0)
        # box-score inputs from the same play-by-play
        row.update(
            b_fga=row["fga"], b_fgm=row["fgm"], b_fta=row["fta"], b_ftm=row["ftm"],
            b_tov=row["tov"], b_oreb=row["oreb"], b_dreb=row["dreb"],
            b_pts=row["pts_fg"] + row["ftm"],
            b_plays=row["fga"] + 0.4 * row["fta"] + row["tov"],
            b_stl=r.get("b_stl", 0.0), b_pf=r.get("b_pf", 0.0), b_blk=0.0,
            bc_b_plays=r.get("bc_fga", 0) + 0.4 * r.get("bc_fta", 0) + r.get("bc_tov", 0),
            fc_b_plays=r.get("fc_fga", 0) + 0.4 * r.get("fc_fta", 0) + r.get("fc_tov", 0),
            bc_b_tov=r.get("bc_tov", 0.0), fc_b_tov=r.get("fc_tov", 0.0),
            bc_b_fga=r.get("bc_fga", 0.0), fc_b_fga=r.get("fc_fga", 0.0),
            bc_b_fta=r.get("bc_fta", 0.0), fc_b_fta=r.get("fc_fta", 0.0),
            bc_b_ftm=r.get("bc_b_ftm", 0.0), fc_b_ftm=r.get("fc_b_ftm", 0.0),
            bc_b_stl=r.get("bc_b_stl", 0.0), fc_b_stl=r.get("fc_b_stl", 0.0),
            bc_b_pf=r.get("bc_b_pf", 0.0), fc_b_pf=r.get("fc_b_pf", 0.0),
            share=1.0,
        )  # fmt: skip
        rows.append(row)
    return pd.DataFrame(rows)


def _season_of(game_id: str) -> int:
    yy = int(game_id[3:5])
    return (1900 if yy >= 46 else 2000) + yy


def rate_game(
    con: duckdb.DuckDBPyConnection, game_id: str, force: bool = False, offline: bool = False
) -> dict:
    """Tonight's team-game values and ratings, and the deltas vs each team's season."""
    payload = fetch(game_id, "playbyplay", force, offline)
    box = fetch(game_id, "boxscore", force, offline)
    actions = payload.get("game", {}).get("actions", [])
    events = actions_to_events(game_id, actions)
    roster = roster_from_box(box)
    parsed = pbp.parse_game(events, roster)

    season = _season_of(game_id)
    base_season = con.execute(
        "select max(season) from metrics.league_baselines where season <= ? and lg_pps is not null",
        [season],
    ).fetchall()[0][0]
    zx = dict(
        con.execute(
            "select zone, xpts from metrics.zone_baselines where season = ?", [base_season]
        ).fetchall()
    )
    pos = {
        pid: (w_bc or 0.0, w_fc or 0.0)
        for pid, w_bc, w_fc in con.execute(
            "select person_id, w_bc, w_fc from metrics.positions"
        ).fetchall()
    }
    teams = sorted({e.team_id for e in roster.values()})
    counts = team_counts(events, _shot_details(actions), parsed, zx, pos, teams)
    counts["season"] = base_season
    counts["season_type"] = "live"
    counts["game_id"] = game_id

    ms = [m for m in M.MEASURES if m.levels in ("both", "team") and m.tier == "A"]
    starts = {k: 0 for k in M.TIER_REQUIREMENTS} | {"A": 0}
    con.register("live_counts", counts)
    try:
        vals = pd.concat(
            [
                con.execute(
                    rate.measure_select(
                        m, "team_game", "live_counts", ["game_id", "team_id"], starts
                    )
                ).df()
                for m in ms
            ]
        )
    finally:
        con.unregister("live_counts")
    ref = con.execute(
        "select measure, mu, sigma from metrics.reference_stats "
        "where level = 'team_game' and tier = 'A' and season = ?",
        [base_season],
    ).df()
    team_avg = con.execute(
        """select team_id, measure, avg(value) as season_value, avg(rating) as season_rating,
                  count(*) as games
           from metrics.ratings_long
           where level = 'team_game' and tier = 'A' and season = ? and season_type = 'regular'
           group by all""",
        [season],
    ).df()
    out = vals.merge(ref, on="measure", how="left")
    out["rating"] = (50 + 10 * (out.value - out.mu) / out.sigma).clip(0, 100)
    out = out.merge(team_avg, on=["team_id", "measure"], how="left")
    out["delta_value"] = out.value - out.season_value
    out["delta_rating"] = out.rating - out.season_rating
    status = box.get("game", {})
    return {
        "game_id": game_id,
        "season": season,
        "baseline_season": base_season,
        "status": status.get("gameStatusText"),
        "period": status.get("period"),
        "clock": status.get("gameClock"),
        "home": status.get("homeTeam", {}).get("teamTricode"),
        "away": status.get("awayTeam", {}).get("teamTricode"),
        "home_id": status.get("homeTeam", {}).get("teamId"),
        "away_id": status.get("awayTeam", {}).get("teamId"),
        "home_score": status.get("homeTeam", {}).get("score"),
        "away_score": status.get("awayTeam", {}).get("score"),
        "ratings": out[
            [
                "team_id",
                "measure",
                "value",
                "rating",
                "season_value",
                "season_rating",
                "delta_value",
                "delta_rating",
                "exposure",
                "games",
            ]
        ],  # fmt: skip
        "counts": counts,
        "lineup_qa": parsed.qa,
    }


SIGNALS = {
    "shooting_sq": ("Shot selection", "getting better looks than usual",
                    "settling for worse looks"),
    "shooting_sm": ("Shot making", "hot: making more than the looks deserve",
                    "cold: missing looks they usually make"),
    "defense_dq": ("Shots allowed", "holding opponents to worse looks than usual",
                   "giving up better looks than usual (rim / corner threes)"),
    "defense_dm": ("Opponent shot making", "opponents cold", "opponents hot"),
    "defense_dt": ("Forcing turnovers", "forcing more turnovers", "forcing fewer turnovers"),
    "defense_df": ("Fouling", "fouling less than usual", "sending opponents to the line more"),
    "defense_dr": ("Defensive rebounding", "finishing possessions on the glass",
                   "giving up offensive rebounds"),
    "ball_bt": ("Ball security", "fewer turnovers than usual", "more turnovers than usual"),
    "ball_bo": ("Offensive rebounding", "winning the offensive glass",
                "not getting second chances"),
}  # fmt: skip
NOISY = {"shooting_sm", "defense_dm"}  # mostly variance in one game (docs/validation.md)


def signals(ratings: pd.DataFrame, team_id: int, top: int = 4) -> list[str]:
    """Plain-language in-game adjustment signals: the biggest deltas vs the team's season."""
    r = ratings[(ratings.team_id == team_id) & ratings.measure.isin(SIGNALS)].copy()
    r = r.dropna(subset=["delta_value"])
    r["abs"] = r.delta_value.abs()
    out = []
    for row in r.sort_values("abs", ascending=False).head(top).itertuples():
        label, good, bad = SIGNALS[row.measure]
        text = good if row.delta_value > 0 else bad
        note = " (mostly variance in one game)" if row.measure in NOISY else ""
        out.append(f"{label}: {text}, {row.delta_value:+.1f} pts/100 vs season{note}")
    return out
