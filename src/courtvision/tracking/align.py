"""Link the tracking to the play-by-play.

SportVU numbers its events with NBA's old PlayByPlayV2 event numbers. That endpoint is retired
and courtvision's play-by-play uses the newer numbering, so the link comes from the tracking:
shots found in the ball's flight (``detect.shots``) are matched to the logged field goals by
quarter, clock and shot location (the play-by-play records where each shot was taken).

QA per game, against the spec's targets: the share of logged field goals matched, whether the
player nearest the ball at release is the logged shooter, and how well the tracked shot
distance agrees with the logged one.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ProcessPoolExecutor

import duckdb
import numpy as np
import pandas as pd

from courtvision.config import get_paths
from courtvision.tracking import detect, sportvu

log = logging.getLogger(__name__)

# play-by-play clock minus the release clock, s: shots are logged ~2.7 s after the release
# (median on games matched by shooter alone; half within 2.1-3.3 s)
WINDOW = (-9.0, 1.5)  # a few shots are logged 6-8 s after the release
LAG = -2.7
# Logged spots' distance from the hoop agrees with the tracking (r 0.96); their angle can be
# 10+ ft off on long shots. So the gate is on the difference in distance from the hoop.
MAX_RADIAL = 6.0
# ...except that layups are logged at 0-2 ft wherever the ball left the hand: a shot logged
# within this of the rim may have been released up to RIM_SLACK farther out
RIM_LOGGED, RIM_SLACK = 4.0, 9.0


def pbp_path():
    return get_paths().data / "tracking" / "pbp_2015.parquet"


def export_pbp() -> int:
    """The 2015-16 regular-season play-by-play the tracking needs, to a Parquet file (so the
    tracking jobs don't hold the database open)."""
    with duckdb.connect(str(get_paths().duckdb), read_only=True) as con:
        df = con.execute(
            """select game_id, actionNumber, period, clock, actionType, subType, person_id,
                      team_id, isFieldGoal, shotResult, shotDistance, xLegacy, yLegacy,
                      location, description
               from stg.pbp where season = 2015 and season_type = 'regular'"""
        ).df()
    df["t"] = [_secs(c) for c in df.clock]
    pbp_path().parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(pbp_path(), index=False)
    return len(df)


def _secs(clock: str | None) -> float | None:
    m = re.match(r"PT(\d+)M([\d.]+)S", clock or "")
    return int(m[1]) * 60 + float(m[2]) if m else None


def logged_location(x_legacy: float, y_legacy: float, hoop: int) -> tuple[float, float]:
    """A logged shot location (tenths of a foot from the hoop: x across, y up the floor) on
    the tracking court, toward ``hoop`` (0 = left, 1 = right)."""
    hx, hy = detect.HOOPS[hoop]
    if hoop == 0:
        return hx + y_legacy / 10, hy - x_legacy / 10
    return hx - y_legacy / 10, hy + x_legacy / 10


def match(
    fg: pd.DataFrame, tracked: pd.DataFrame, use_shooter: bool = True, use_location: bool = True
) -> pd.DataFrame:
    """One-to-one matches of logged field goals to tracked shots in the same quarter, within
    the clock window, cheapest first. The cost adds the clock gap to the usual lag, the
    difference in distance from the hoop (gated at ``MAX_RADIAL``) and, lightly, the gap to
    the logged spot, minus a bonus when the tracked shooter is the logged one. QA turns a cue
    off to check it independently."""
    cand = []
    tr = tracked[~tracked.free_throw]
    logged_dist = np.hypot(fg.xLegacy.to_numpy(), fg.yLegacy.to_numpy()) / 10
    for a, ld in zip(fg.itertuples(), logged_dist, strict=True):
        same = tr[tr.period == a.period]
        for b in same.itertuples():
            dt = a.t - b.clock
            if not WINDOW[0] <= dt <= WINDOW[1]:
                continue
            lx, ly = logged_location(a.xLegacy, a.yLegacy, b.hoop)
            gap = float(np.hypot(lx - b.shooter_x, ly - b.shooter_y))
            radial = abs(ld - b.distance)
            if ld < RIM_LOGGED and ld <= b.distance <= ld + RIM_SLACK:
                radial = min(radial, max(0.0, b.distance - ld - MAX_RADIAL))
            if use_location and radial > MAX_RADIAL:
                continue
            cost = abs(dt - LAG)
            if use_location:
                cost += radial / 3 + gap / 10
            if use_shooter and b.shooter_id == a.person_id:
                cost -= 2.0
            cand.append((cost, a.Index, b.Index, dt, gap))
    used_a, used_b, out = set(), set(), []
    for _cost, ia, ib, dt, gap in sorted(cand):
        if ia in used_a or ib in used_b:
            continue
        used_a.add(ia)
        used_b.add(ib)
        out.append({"pbp_row": ia, "shot_row": ib, "dt": dt, "gap": gap})
    return pd.DataFrame(out, columns=["pbp_row", "shot_row", "dt", "gap"])


def guided(g: detect.Game, fg: pd.DataFrame, done: pd.DataFrame) -> pd.DataFrame:
    """A second pass for logged field goals still unmatched: within the clock window before
    the log entry, the logged shooter's own releases toward a hoop (``detect.releases_by``);
    the one nearest the usual lag that passes the distance gate. Marked ``guided`` (the
    shooter came from the log), so analyses can leave them out."""
    hold = detect.holder(g)
    rows = []
    matched = set(done.actionNumber)
    for a in fg.itertuples():
        if a.actionNumber in matched:
            continue
        frames = np.flatnonzero((g.period == a.period) & (a.t - g.clock >= WINDOW[0])
                                & (a.t - g.clock <= WINDOW[1]))  # fmt: skip
        if not len(frames):
            continue
        best = None
        for j in detect.releases_by(g, hold, int(a.person_id), frames[0], frames[-1] + 1):
            k = int(np.flatnonzero(g.ids[j] == a.person_id)[0])
            sxy = g.xy[j, k]
            dist, hoop = detect.hoop_distance(sxy)
            ld = a.logged_distance
            radial = abs(ld - dist)
            if ld < RIM_LOGGED and ld <= dist <= ld + RIM_SLACK:
                radial = 0.0
            if radial > MAX_RADIAL:
                continue
            dt = a.t - float(g.clock[j])
            if best is None or abs(dt - LAG) < abs(best[1] - LAG):
                best = (j, dt, k, float(dist), int(hoop), sxy)
        if best is None:
            continue
        j, dt, k, dist, hoop, sxy = best
        lx, ly = logged_location(a.xLegacy, a.yLegacy, hoop)
        rows.append(fg.loc[a.Index].to_dict() | {
            "release": j, "kind": "guided", "period_trk": int(g.period[j]),
            "clock_trk": float(g.clock[j]), "shooter_slot": k,
            "shooter_id": int(a.person_id), "shooter_x": float(sxy[0]),
            "shooter_y": float(sxy[1]), "hoop": hoop, "distance": dist, "dt": dt,
            "gap": float(np.hypot(lx - sxy[0], ly - sxy[1])), "method": "guided"})  # fmt: skip
    return pd.DataFrame(rows)


def align_game(game_id: str, pbp: pd.DataFrame | None = None) -> dict:
    """Detect, match and score one game; writes ``tracking/shots/<game_id>.parquet``."""
    if pbp is None:
        pbp = pd.read_parquet(pbp_path(), filters=[("game_id", "==", game_id)])
    g = detect.Game.from_frames(sportvu.load_frames(game_id))
    tracked = detect.shots(g)
    fg = pbp[(pbp.game_id == game_id) & (pbp.isFieldGoal == 1)].reset_index(drop=True)
    # logged distance from the logged location (the shotDistance field is sometimes 0)
    fg["logged_distance"] = np.hypot(fg.xLegacy, fg.yLegacy) / 10
    # covered: the tracking has frames where the release should be (the log trails it)
    fg["covered"] = [bool(((g.period == a.period) & (g.clock >= a.t) & (g.clock <= a.t + 6))
                          .sum() >= 2 * detect.FPS) for a in fg.itertuples()]  # fmt: skip

    def joined(**cues) -> pd.DataFrame:
        if not (len(tracked) and len(fg)):
            return fg.iloc[:0].assign(dt=[], gap=[])
        m = match(fg, tracked, **cues)
        return fg.loc[m.pbp_row].reset_index(drop=True).join(
            tracked.loc[m.shot_row].reset_index(drop=True), rsuffix="_trk"
        ).assign(dt=m.dt.to_numpy(), gap=m.gap.to_numpy())  # fmt: skip

    final = joined().assign(method="detected")
    final = pd.concat([final, guided(g, fg, final)], ignore_index=True)
    out = get_paths().data / "tracking" / "shots"
    out.mkdir(parents=True, exist_ok=True)
    final.to_parquet(out / f"{game_id}.parquet", index=False)
    # independent checks: the shooter on matches made without the shooter cue, the distance
    # on matches made without the location cue
    no_shooter, no_location = joined(use_shooter=False), joined(use_location=False)
    covered = int(fg.covered.sum())
    return {
        "game_id": game_id, "fg": len(fg), "covered": covered,
        "tracked": int((~tracked.free_throw).sum()) if len(tracked) else 0,
        "matched": len(final), "matched_covered": int(final.covered.sum()),
        "guided": int((final.method == "guided").sum()),
        "shooter_agrees": float((no_shooter.shooter_id == no_shooter.person_id).mean())
        if len(no_shooter) else np.nan,
        "distance_r": float(np.corrcoef(no_location.logged_distance,
                                        no_location.distance)[0, 1])
        if len(no_location) > 2 else np.nan,
        # the fair test of the tracked distance: jump shots (layups are logged at 0-2 ft
        # wherever the ball left the hand) that time and shooter alone pin to one tracked
        # shot (so a miss and the same player's putback can't be confused)
        **_distance_check(fg, tracked),
        "distance_mae": float((no_location.logged_distance - no_location.distance)
                              .abs().median()) if len(no_location) else np.nan,
    }  # fmt: skip


def _distance_check(fg: pd.DataFrame, tracked: pd.DataFrame) -> dict:
    """Logged vs tracked distance on jump shots (logged 8+ ft) that exactly one tracked shot
    by the logged shooter fits in time: their pairs, for a correlation over all games."""
    pairs = []
    tr = tracked[~tracked.free_throw] if len(tracked) else tracked
    for a in fg[fg.logged_distance >= 8].itertuples():
        if not len(tr):
            break
        off = a.t - tr.clock
        mine = tr[(tr.period == a.period) & (tr.shooter_id == a.person_id)
                  & (off >= WINDOW[0]) & (off <= WINDOW[1])]  # fmt: skip
        if len(mine) == 1:
            pairs.append((a.logged_distance, float(mine.distance.iloc[0])))
    p = np.array(pairs).reshape(-1, 2)
    return {"jump_pairs": p.tolist(),
            "jump_distance_mae": float(np.median(np.abs(p[:, 0] - p[:, 1]))) if len(p)
            else np.nan}  # fmt: skip


def align_safe(game_id: str) -> dict:
    try:
        return align_game(game_id)
    except Exception as e:
        return {"game_id": game_id, "error": f"{type(e).__name__}: {e}"}


def run(workers: int = 6) -> dict:
    """Align every converted game; writes the QA table and returns its summary."""
    if not pbp_path().exists():
        export_pbp()
    games = sportvu.game_ids()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(align_safe, games))
    qa = pd.DataFrame(rows)
    pairs = np.array([p for ps in qa.pop("jump_pairs").dropna() for p in ps]).reshape(-1, 2)
    qa.to_parquet(get_paths().data / "tracking" / "align_qa.parquet", index=False)
    ok = qa.dropna(subset=["matched"]) if "matched" in qa else qa
    jump_r = float(np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1]) if len(pairs) > 2 else np.nan
    return summarize(ok) | {
        "jump_distance_r": round(jump_r, 4), "jump_pairs": len(pairs),
        "games": len(qa),
        "errors": int(qa["error"].notna().sum()) if "error" in qa else 0}  # fmt: skip


def summarize(qa: pd.DataFrame) -> dict:
    """The spec's targets: matched share of the field goals the tracking covers (≥95%),
    shooter agreement (≥95%); the distance r (≥0.95) is computed over all games' pairs in
    ``run``."""
    qa = qa.assign(share=qa.matched_covered / qa.covered)
    w = qa.matched / qa.matched.sum()
    return {"field_goals": int(qa.fg.sum()), "covered": int(qa.covered.sum()),
            "coverage": round(float(qa.covered.sum() / qa.fg.sum()), 4),
            "matched_share_of_covered": round(float(qa.matched_covered.sum()
                                                    / qa.covered.sum()), 4),
            "guided_share": round(float(qa.guided.sum() / qa.matched.sum()), 4),
            "shooter_agrees": round(float((qa.shooter_agrees * w).sum()), 4),
            "distance_r_all_shots_median_game": round(float(qa.distance_r.median()), 4),
            "jump_distance_mae_median_game": round(float(qa.jump_distance_mae.median()), 2),
            "worst_games": qa.nsmallest(3, "share")[["game_id", "share"]]
            .round(3).to_dict("records")}  # fmt: skip
