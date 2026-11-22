"""Phase T4 on synthetic frames: the causal trackers, the score feed, EPV inputs, the
streaming engine against the batch path, and the Live view's WebSocket."""

from __future__ import annotations

import asyncio
import struct

import numpy as np
import pandas as pd

from courtvision.tracking import detect, engine, epv, server, state, winprob

TABLE = {"p": np.full((2, 121, 19), 0.5), "dist_step": 0.5, "angle_step": 5.0}


def _game(n=900, seed=0):
    """A synthetic game: ten players drifting, the ball passed between home players for the
    first half of it, then held by an away player (a change of possession)."""
    rng = np.random.default_rng(seed)
    xy = np.cumsum(rng.normal(0, 0.15, (n, 10, 2)), axis=0) + rng.uniform(10, 80, (1, 10, 2))
    xy[..., 1] = np.clip(xy[..., 1], 1, 49)
    holder = np.where(np.arange(n) < n // 2, (np.arange(n) // 60) % 5, 7)
    ball = np.zeros((n, 3))
    ball[:, :2] = xy[np.arange(n), holder] + 0.5
    ball[:, 2] = 3.0 + np.abs(np.sin(np.arange(n) / 3))  # dribbling, always in reach
    ball[100:110, 2] = 14.0  # a pass up high: nobody has it
    clock = 600.0 - 0.04 * np.arange(n)
    clock[400:450] = clock[400]  # a dead ball: the clock stops
    return detect.Game(period=np.ones(n, np.int64), clock=clock,
                       shot_clock=np.clip(24 - 0.04 * np.arange(n) % 24, 0, 24),
                       ball=ball, xy=xy.astype(np.float32),
                       ids=np.tile(np.arange(1, 11), (n, 1)).astype(np.int64),
                       ts=np.arange(n) * 40)  # fmt: skip


def test_holder_is_causal_and_window_exact():
    g = _game()
    full = state.holder(g.ball, g.xy, g.ids)
    assert full[50] == 0 and full[105] == -1 and full[-1] == 7
    for i in range(0, len(full), 37):  # any window of 5+ frames ending at i gives row i
        lo = max(i - 6, 0)
        assert state.holder(g.ball[lo : i + 1], g.xy[lo : i + 1], g.ids[lo : i + 1])[-1] == full[i]


def test_possession_needs_the_other_team_twelve_frames():
    p = state.Possession()
    seq = [0] * 20 + [1] * 5 + [-1] * 3 + [0] * 10 + [1] * 12 + [1] * 3
    out = [p.update(i, 1, t) for i, t in enumerate(seq)]
    assert all(o == -1 for o, _ in out[:11])  # nobody on offense until 12 frames held
    assert all(o == 0 for o, _ in out[11:49])  # the 5-frame deflection doesn't flip it
    o, start = out[49]
    assert o == 1 and start == 38  # the twelfth frame in a row; the run began at 38
    p.update(60, 2, -1)
    assert p.offense == -1  # a new period starts with nobody on offense


def test_hoops_vote_then_carry_over_halftime():
    h = state.Hoops()
    for _ in range(state.HOOP_VOTES):
        h.update(1, 0, 80.0)  # home holds it on the right
    assert h.hoop(0) == 1 and h.hoop(1) == 0
    h.update(2, -1, np.nan)
    assert h.hoop(0) == 1  # second quarter: same ends
    h.update(3, -1, np.nan)
    assert h.hoop(0) == 0 and h.hoop(1) == 1  # after halftime: switched


def test_score_feed_counts_every_point_once():
    n = 300
    period = np.ones(n, np.int64)
    clock = 600.0 - 0.04 * np.arange(n)
    offense = np.r_[np.zeros(150, np.int8), np.ones(150, np.int8)]
    pbp = pd.DataFrame({
        "actionNumber": [1, 2, 3], "period": [1, 1, 1],
        "t": [clock[60], clock[140], clock[250]],
        "team_id": [10, 10, 20], "actionType": ["Made Shot", "Free Throw", "Made Shot"],
        "description": ["Jump Shot", "Free Throw 1 of 1", "3PT Jump Shot"]})  # fmt: skip
    shots = pd.DataFrame({"actionNumber": [1], "release": [40], "end": [60.0]})
    f = epv.score_feed(pbp, shots, 10, period, clock, offense)
    ev = f["events"].set_index("actionNumber")
    assert ev.known[1] == 72  # 12 frames after the ball reached the rim
    assert ev.known[2] == 150  # a free throw goes up when the possession ends
    assert ev.segment[3] == 1 and ev.known[3] == 263  # half a second of clock after it
    assert f["margin"][71] == 0 and f["margin"][72] == 2 and f["margin"][-1] == 0
    assert f["pending"][0] == 3 and f["pending"][100] == 1 and f["pending"][160] == 3
    assert f["pending"][-1] == 0
    # every point: on the board or still pending in the possession it belongs to
    home = f["margin"] + np.where(offense == 0, f["pending"], 0)
    assert home[0] == 3 and home[149] == 3


def test_raw_features_are_mirror_symmetric():
    g = _game()
    st = state.track(g.period, g.ball, g.xy, g.ids)
    i = np.arange(20, 400, 20)
    args = dict(hold=st["holder"][i], offense=np.zeros(len(i), int), shot_clock=g.shot_clock[i],
                game_clock=g.clock[i], running=np.ones(len(i), bool), age_s=np.full(len(i), 3.0),
                table=TABLE)  # fmt: skip
    a = epv.raw_features(g.ball[i], g.xy[i], g.ids[i], hoop=np.zeros(len(i), int),
                         vball=np.zeros((len(i), 3)), **args)  # fmt: skip
    mxy, mball = g.xy[i].copy(), g.ball[i].copy()
    mxy[..., 0], mball[:, 0] = 94 - mxy[..., 0], 94 - mball[:, 0]
    b = epv.raw_features(mball, mxy, g.ids[i], hoop=np.ones(len(i), int),
                         vball=np.zeros((len(i), 3)), **args)  # fmt: skip
    assert a.shape == (len(i), len(epv.RAW))
    np.testing.assert_allclose(a, b, atol=1e-9)
    X = epv.basis(a)
    assert np.isfinite(X).all() and X.shape[0] == len(i)


class _EPV:
    table = TABLE

    def __init__(self, k):
        self.w = np.random.default_rng(1).normal(0, 0.1, k)

    def value(self, R):
        return np.maximum(epv.basis(R) @ self.w, 0.0)


class _WP:
    def predict(self, margin, s_left, poss, pregame, value, lineup=0.0):
        z = margin / 5 + poss * value / np.sqrt(s_left + 20)
        return 1 / (1 + np.exp(-z)), 1 / (1 + np.exp(-z - 0.1))


def _engine():
    k = epv.basis(np.zeros((1, len(epv.RAW)))).shape[1]
    return engine.Engine(_EPV(k), _WP(), 0.0, {}, 0.0)


def _frame(g, i):
    return engine.Frame(i=i, period=int(g.period[i]), clock=float(g.clock[i]),
                        shot_clock=float(g.shot_clock[i]), ball=g.ball[i], xy=g.xy[i],
                        ids=g.ids[i])  # fmt: skip


def test_engine_frame_by_frame_equals_the_batch_path():
    g = _game()
    st = state.track(g.period, g.ball, g.xy, g.ids)
    gi = {"g": g, "st": st}
    eng = _engine()
    idx = np.arange(0, len(g.ts), 25)
    raw, off = [], []
    for i in range(len(g.ts)):
        out = eng.step(_frame(g, i))
        if i % 25 == 0:
            raw.append(eng.raw[0])
            off.append(out["offense"])
    batch = epv.frame_features(gi, idx, TABLE)
    np.testing.assert_allclose(np.asarray(raw), batch, atol=1e-9)
    assert (np.asarray(off) == st["offense"][idx]).all()
    assert off[-1] == 1  # the away team took it over


def test_resume_mid_game_matches_stepping_through():
    g = _game()
    st = state.track(g.period, g.ball, g.xy, g.ids)
    a = _engine()
    outs = [a.step(_frame(g, i)) for i in range(len(g.ts))]
    b = _engine()
    b.resume(g, st, 499, [0, 0])
    for i in range(500, len(g.ts)):
        o = b.step(_frame(g, i))
        assert o["offense"] == outs[i]["offense"] and o["holder"] == outs[i]["holder"]
        assert abs(o["epv"] - outs[i]["epv"]) < 1e-12


def test_engine_is_fast():
    g = _game()
    eng = _engine()
    for i in range(50):
        eng.step(_frame(g, i))
    import time

    t0 = time.perf_counter()
    for i in range(50, 450):
        eng.step(_frame(g, i))
    assert (time.perf_counter() - t0) / 400 * 1000 < engine.BUDGET_MS / 10


def test_win_probability_designs():
    X = winprob.base_design([0, 5, 5], [600, 600, 30], [0, 0, 0], [0, 0, 0])
    assert X[1, 0] > 0 and X[2, 0] > X[1, 0]  # a lead matters more with less time left
    F = winprob.film_design(np.zeros(2), [1, -1], [1.2, 1.2], 0.9, [100, 100], [3, 3])
    assert F.shape == (2, 3) and F[0, 1] == -F[1, 1] > 0


def test_websocket_handshake_and_frames():
    # RFC 6455's own example
    assert server.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert server.encode("hi")[:2] == b"\x81\x02"
    assert server.encode("x" * 200)[:4] == b"\x81\x7e\x00\xc8"
    assert server.encode("x" * 70000)[:2] == b"\x81\x7f"

    async def roundtrip():
        r = asyncio.StreamReader()
        payload, mask = b'{"cmd":"play"}', b"\x01\x02\x03\x04"
        masked = bytes(b ^ mask[k % 4] for k, b in enumerate(payload))
        r.feed_data(struct.pack("!BB", 0x81, 0x80 | len(payload)) + mask + masked)
        return await server.read_message(r)

    assert asyncio.run(roundtrip()) == (1, b'{"cmd":"play"}')


def test_live_view_answers_only_this_machine():
    ok = {"host": "127.0.0.1:8770"}
    assert server._allowed(ok, 8770)
    assert server._allowed({**ok, "origin": "http://127.0.0.1:8770"}, 8770)
    assert not server._allowed({**ok, "origin": "http://evil.example"}, 8770)
    assert not server._allowed({"host": "evil.example"}, 8770)
