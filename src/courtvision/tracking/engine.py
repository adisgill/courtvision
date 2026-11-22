"""The streaming engine (spec section 3): frames in, the live state out, one frame at a time.

Each frame updates who has the ball and which team is on offense (``state``), the expected
value of the possession (``epv``) and the win probability, from score and clock alone and
with the film (``winprob``). Nothing looks ahead, so the engine runs the same on a replayed
2016 game (:class:`Replay`) as it would on a live feed (Phase T5 makes the same frames from
video).

:func:`check` runs a whole game through the engine frame by frame, compares it with the batch
tables the models were trained on (they must agree), and times every frame against the
200 ms budget.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from courtvision.tracking import epv, state, winprob
from courtvision.tracking.detect import FPS

BUDGET_MS = 200.0


@dataclass
class Frame:
    """One tracking frame, the schema every source produces (SportVU now, video in T5)."""

    i: int
    period: int
    clock: float  # seconds left in the period
    shot_clock: float
    ball: np.ndarray  # (3,) x, y, z in feet
    xy: np.ndarray  # (10, 2) players: home slots 0-4, away 5-9
    ids: np.ndarray  # (10,) NBA person ids (0 = empty slot)
    ts: int = 0  # ms


class Engine:
    """The live state, updated by :meth:`step` one frame at a time; scoring plays arrive
    through :meth:`add_points` as the score feed reports them."""

    def __init__(self, epv_model: epv.Model, wp_model: winprob.Model, pregame: float,
                 impact: dict[int, float], fill: float) -> None:  # fmt: skip
        H = state.HISTORY
        self.ball = np.zeros((H, 3))
        self.xy = np.zeros((H, 10, 2), np.float32)
        self.ids = np.zeros((H, 10), np.int64)
        self.clock = np.zeros(H)
        self.period = np.zeros(H, np.int64)
        self.seen = 0
        self.poss, self.hoops = state.Possession(), state.Hoops()
        self.score = [0, 0]
        self.epv_model, self.wp_model = epv_model, wp_model
        self.pregame = pregame
        self.impact, self.fill = impact, fill
        self._lineup: tuple[tuple, float] = ((), 0.0)
        self.raw: np.ndarray | None = None  # the last frame's EPV inputs

    def add_points(self, side: int, pts: int) -> None:
        self.score[side] += pts

    @property
    def margin(self) -> int:
        return self.score[0] - self.score[1]

    def _push(self, f: Frame) -> None:
        for buf, v in ((self.ball, f.ball), (self.xy, f.xy), (self.ids, f.ids),
                       (self.clock, f.clock), (self.period, f.period)):  # fmt: skip
            buf[:-1] = buf[1:]
            buf[-1] = v
        self.seen += 1

    def lineup(self, ids: np.ndarray) -> float:
        key = tuple(int(x) for x in ids)
        if key != self._lineup[0]:
            v = [self.impact.get(p, self.fill) if p > 0 else 0.0 for p in key]
            self._lineup = (key, sum(v[:5]) - sum(v[5:]))
        return self._lineup[1]

    def step(self, f: Frame, full: bool = True) -> dict:
        """Take one frame; return the state after it. ``full=False`` updates only the
        trackers (fast-forwarding)."""
        self._push(f)
        m = min(self.seen, state.HISTORY)
        sl = slice(state.HISTORY - m, None)
        h = int(state.holder(self.ball[sl], self.xy[sl], self.ids[sl])[-1])
        team = int(state.team_of(h))
        o, start = self.poss.update(f.i, int(f.period), team)
        self.hoops.update(int(f.period), team, float(f.ball[0]))
        hoop = self.hoops.hoop(o)
        out = {"i": f.i, "period": int(f.period), "clock": float(f.clock),
               "shot_clock": float(f.shot_clock), "holder": h, "offense": o, "hoop": hoop,
               "score": list(self.score)}  # fmt: skip
        if not full:
            return out
        running = bool(m == state.HISTORY and self.clock[0] - self.clock[-1] > 0.2
                       and self.period[0] == self.period[-1])  # fmt: skip
        vb = np.zeros((1, 3))
        if self.seen > state.VEL_BALL:
            vb[0] = np.nan_to_num((self.ball[-1] - self.ball[-1 - state.VEL_BALL])
                                  * (FPS / state.VEL_BALL))  # fmt: skip
        R = epv.raw_features(self.ball[-1:], self.xy[-1:], self.ids[-1:], np.array([h]),
                             np.array([o]), np.array([hoop]), np.array([f.shot_clock], float),
                             np.array([f.clock], float), np.array([running]),
                             np.array([(f.i - start) / FPS]), vb, self.epv_model.table)  # fmt: skip
        self.raw = R
        value = float(self.epv_model.value(R)[0]) if o >= 0 else 0.0
        s_left = float(epv.seconds_left(np.array([f.period]), np.array([f.clock]))[0])
        poss = 1.0 if o == 0 else -1.0 if o == 1 else 0.0
        lu = self.lineup(f.ids)
        wpb, wpf = self.wp_model.predict(self.margin, max(s_left, 0.0), poss, self.pregame,
                                         value, lu)  # fmt: skip
        out.update({"epv": value, "s_left": s_left, "wp_base": wpb, "wp": wpf, "lineup": lu})
        return out

    def resume(self, g, st: dict, i: int, score: list[int]) -> None:
        """Pick up after frame ``i`` of a recorded game as if every frame before had been
        stepped: the trackers from the batch record, the last frames into the history."""
        self.poss, self.hoops = state.restore(st, g.period, i)
        H = state.HISTORY
        lo = max(i - H + 1, 0)
        k = i - lo + 1
        for buf, src in ((self.ball, g.ball), (self.xy, g.xy), (self.ids, g.ids),
                         (self.clock, g.clock), (self.period, g.period)):  # fmt: skip
            buf[H - k :] = src[lo : i + 1]
        self.seen = i + 1
        self.score = list(score)


class Replay:
    """A recorded game as a frame source, with its score feed (``epv.score_feed``)."""

    def __init__(self, game_id: str) -> None:
        self.game_id = game_id
        self.gi = epv.game_inputs(game_id)
        self.g, self.st = self.gi["g"], self.gi["st"]
        self.n = len(self.g.ts)
        ev = self.gi["feed"]["events"]
        self.scores: dict[int, list[tuple[int, int]]] = {}
        for r in ev.itertuples(index=False):
            self.scores.setdefault(int(r.known), []).append((int(r.side), int(r.pts)))
        self.home_id, self.away_id = self.gi["home_id"], self.gi["away_id"]

    def frame(self, i: int) -> Frame:
        g = self.g
        return Frame(i=i, period=int(g.period[i]), clock=float(g.clock[i]),
                     shot_clock=float(g.shot_clock[i]), ball=g.ball[i], xy=g.xy[i],
                     ids=g.ids[i], ts=int(g.ts[i]))  # fmt: skip

    def score_after(self, i: int) -> list[int]:
        """[home, away] on the scoreboard once frame ``i`` is in."""
        s = [0, 0]
        for k, plays in self.scores.items():
            if k <= i:
                for side, pts in plays:
                    s[side] += pts
        return s


def models_for(game_id: str) -> dict:
    """The fitted models plus this game's pre-game margin and the lineup ratings."""
    import duckdb

    from courtvision.config import get_paths

    con = duckdb.connect(str(get_paths().duckdb), read_only=True)
    try:
        pre = winprob.pregame(con, [winprob.TRACKING_SEASON])
        impact, fill = winprob.impact_table(con)
    finally:
        con.close()
    p = pre.loc[pre.game_id == game_id, "pregame"]
    return {"epv": epv.Model(), "wp": winprob.Model(),
            "pregame": float(p.iloc[0]) if len(p) else 0.0, "impact": impact,
            "fill": fill}  # fmt: skip


def make_engine(models: dict) -> Engine:
    return Engine(models["epv"], models["wp"], models["pregame"], models["impact"],
                  models["fill"])  # fmt: skip


def history(replay: Replay, models: dict, every: int = FPS) -> pd.DataFrame:
    """EPV and win probability at every ``every``-th frame by the batch path (vectorized,
    the code the training tables use), for filling a chart after jumping in mid-game."""
    gi, g = replay.gi, replay.g
    idx = np.arange(0, replay.n, every)
    R = epv.frame_features(gi, idx, models["epv"].table)
    o = gi["st"]["offense"][idx]
    value = np.where(o >= 0, models["epv"].value(R), 0.0)
    s_left = epv.seconds_left(g.period[idx], g.clock[idx])
    poss = np.select([o == 0, o == 1], [1.0, -1.0], 0.0)
    lu = winprob.lineup_strength(g.ids[idx], models["impact"], models["fill"])
    pre = np.full(len(idx), models["pregame"])
    margin = gi["feed"]["margin"][idx]
    wpb, wpf = models["wp"].predict_many(margin, np.maximum(s_left, 0), poss, pre, value, lu)
    return pd.DataFrame({"frame": idx, "period": g.period[idx], "s_left": s_left, "epv": value,
                         "wp_base": wpb, "wp": wpf})  # fmt: skip


def message(out: dict, f: Frame) -> str:
    """The per-frame message the live page receives (what the server sends)."""
    xy = np.nan_to_num(np.round(f.xy.astype(np.float64), 1), nan=-99.0)
    return json.dumps({"t": "f", "i": out["i"], "p": out["period"],
                       "gc": round(out["clock"], 1), "sc": round(out["shot_clock"], 1)
                       if np.isfinite(out["shot_clock"]) else None,
                       "b": np.nan_to_num(np.round(f.ball, 2), nan=-99.0).tolist(),
                       "xy": xy.ravel().tolist(), "h": out["holder"], "o": out["offense"],
                       "epv": round(out["epv"], 3), "wp": round(out["wp"], 4),
                       "wpb": round(out["wp_base"], 4), "s": out["score"]})  # fmt: skip


def check(game_id: str, every: int = FPS) -> dict:
    """Run a whole game through the engine, frame by frame, as fast as it goes: the time
    each frame takes (engine, and engine plus the page's message), and agreement with the
    batch path at every ``every``-th frame (EPV inputs, offense, margin, EPV, win prob)."""
    replay = Replay(game_id)
    models = models_for(game_id)
    eng = make_engine(models)
    n = replay.n
    t_step = np.empty(n)
    t_msg = np.empty(n)
    keep = {"raw": [], "offense": [], "margin": [], "epv": [], "wp": [], "wpb": []}
    for i in range(n):
        f = replay.frame(i)
        t0 = time.perf_counter()
        for side, pts in replay.scores.get(i, ()):
            eng.add_points(side, pts)
        out = eng.step(f)
        t1 = time.perf_counter()
        message(out, f)
        t2 = time.perf_counter()
        t_step[i], t_msg[i] = (t1 - t0) * 1000, (t2 - t0) * 1000
        if i % every == 0:
            keep["raw"].append(eng.raw[0])
            keep["offense"].append(out["offense"])
            keep["margin"].append(eng.margin)
            keep["epv"].append(out["epv"])
            keep["wp"].append(out["wp"])
            keep["wpb"].append(out["wp_base"])
    idx = np.arange(0, n, every)
    batch_raw = epv.frame_features(replay.gi, idx, models["epv"].table)
    hist = history(replay, models, every)
    raw = np.asarray(keep["raw"])
    agree = {
        "frames_compared": len(idx),
        "raw_max_abs_diff": float(np.nanmax(np.abs(raw - batch_raw))),
        "offense_equal": bool((np.asarray(keep["offense"]) == replay.st["offense"][idx]).all()),
        "margin_equal": bool((np.asarray(keep["margin"])
                              == replay.gi["feed"]["margin"][idx]).all()),
        "epv_max_abs_diff": float(np.max(np.abs(np.asarray(keep["epv"]) - hist.epv))),
        "wp_max_abs_diff": float(np.max(np.abs(np.asarray(keep["wp"]) - hist.wp))),
        "wp_base_max_abs_diff": float(np.max(np.abs(np.asarray(keep["wpb"]) - hist.wp_base))),
    }  # fmt: skip

    def pct(a: np.ndarray) -> dict:
        return {"p50": round(float(np.percentile(a, 50)), 3),
                "p99": round(float(np.percentile(a, 99)), 3),
                "max": round(float(a.max()), 3), "mean": round(float(a.mean()), 3)}  # fmt: skip

    return {"game_id": game_id, "frames": n, "engine_ms": pct(t_step),
            "engine_plus_message_ms": pct(t_msg),
            "over_budget": int((t_msg > BUDGET_MS).sum()),
            "real_time_factor": round(1000 / FPS / float(t_msg.mean()), 1),
            "agreement": agree}  # fmt: skip
