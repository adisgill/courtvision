"""Phase T2 on synthetic frames: movement (smoothing, distance, touches, dribbles, zones,
spacing), the shot-quality model's pieces, and the replay page's packing."""

from __future__ import annotations

import base64

import numpy as np
import pandas as pd

from courtvision.tracking import detect, movement, replay, shotquality


def _game(n, xy=None, ball=None, ids=None, period=None):
    xy = np.zeros((n, 10, 2)) if xy is None else xy
    if ids is None:
        ids = np.tile(np.arange(1, 11), (n, 1))
    return detect.Game(period=np.ones(n, int) if period is None else period,
                       clock=600.0 - 0.04 * np.arange(n), shot_clock=np.full(n, 20.0),
                       ball=np.zeros((n, 3)) if ball is None else ball, xy=xy, ids=ids,
                       ts=np.arange(n) * 40)  # fmt: skip


def test_distance_ignores_jitter_breaks_and_glitches():
    n = 251  # 10 s
    rng = np.random.default_rng(0)
    xy = np.zeros((n, 10, 2))
    xy[:, :, 0] = 10 + np.linspace(0, 50, n)[:, None]  # everyone runs 50 ft at 5 ft/s
    xy[:, :, 1] = 25
    noisy = xy + rng.normal(0, 0.15, xy.shape)  # a couple of inches of tracking jitter
    g = _game(n, noisy)
    raw = movement.steps(g, 1).sum(axis=0)
    smooth = movement.steps(g, 9).sum(axis=0)
    assert (raw > 60).all()  # jitter alone adds 20%+
    assert (np.abs(smooth - 50) < 2.5).all()

    ids = np.tile(np.arange(1, 11), (n, 1))
    ids[100:, 0] = 99  # a substitution in slot 0: the new man isn't joined to the old one
    xy2 = xy.copy()
    xy2[100:, 0, 0] += 30
    xy2[200, 1] = [80, 40]  # one frame where the tracking jumps 30+ ft
    s = movement.steps(_game(n, xy2, ids=ids), 1)
    assert s[100, 0] == 0 and abs(s[:, 0].sum() - 50 * 249 / 250) < 0.5
    assert s[200:202, 1].max() == 0  # the glitch adds nothing


def test_smooth_never_averages_across_a_break():
    v = np.r_[np.zeros(10), np.full(10, 100.0)][:, None]
    brk = np.zeros((20, 1), bool)
    brk[[0, 10]] = True
    out = movement.smooth(v, brk, 5)
    assert (out[:10] == 0).all() and (out[10:] == 100).all()


def test_touches_merge_short_gaps_and_count_dribbles():
    n = 200
    hold = np.full(n, -1)
    hold[10:60] = 2  # player 2 dribbles: the ball leaves his reach for 8 frames once
    hold[68:110] = 2
    hold[130:132] = 7  # a deflection: too short to be a touch
    hold[150:190] = 6
    ball = np.zeros((n, 3))
    ball[:, 2] = 4.0
    ball[[30, 31, 80, 95], 2] = 0.8  # three bounces (30-31 is one)
    g = _game(n, ball=ball)
    t = movement.touches(g, hold, np.ones(n, bool))
    assert t.slot.tolist() == [2, 6]
    assert t.start.iloc[0] == 10 and t.end.iloc[0] == 109 and t.held.iloc[0] == 92
    assert t.dribbles.tolist() == [3, 0]


def test_possession_and_attacking_hoop():
    n = 120
    hold = np.full(n, -1)
    hold[10:40] = 1  # home has it
    hold[70:100] = 8  # then away
    ball = np.zeros((n, 3))
    ball[:60, 0] = 80  # home holds it on the right
    ball[60:, 0] = 15  # away on the left
    g = _game(n, ball=ball)
    poss = movement.possession(g, hold)
    assert (poss[:10] == -1).all() and (poss[10:70] == 0).all() and (poss[70:] == 1).all()
    assert movement.attacking(g, hold) == {(1, 0): 1, (1, 1): 0}


def test_zone_and_hull():
    pts = np.array([[6.25, 25], [20, 25], [5.25, 49], [30, 25], [5.25 + 23, 25]])
    assert movement.zone(pts, np.zeros(5, int)).tolist() == [0, 1, 2, 2, 1]
    square = np.array([[0, 0], [10, 0], [10, 10], [0, 10], [5, 5]], float)
    assert abs(movement._hull_area(square) - 100) < 1e-9


def test_natural_spline_is_linear_past_the_last_knot():
    knots = [0, 2, 5, 10]
    x = np.array([12.0, 14.0, 16.0])
    b = shotquality.ns_basis(x, knots)
    assert b.shape == (3, 3)
    assert np.allclose(np.diff(b, 2, axis=0), 0)  # second differences vanish: linear


def test_logistic_recovers_known_coefficients():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(20000, 2))
    p = 1 / (1 + np.exp(-(0.3 + 1.2 * X[:, 0] - 0.8 * X[:, 1])))
    y = (rng.random(20000) < p).astype(float)
    m = shotquality.Logistic(lam=0.0).fit(X, y)
    coef = m.w[1:] / m.sd
    assert np.allclose(coef, [1.2, -0.8], atol=0.08)
    assert abs(shotquality.auc(y, m.predict(X)) - shotquality.auc(y, p)) < 0.01
    cal = shotquality.calibration(y, m.predict(X))
    assert abs(cal["slope"] - 1) < 0.08 and cal["ece"] < 0.02


def test_auc_and_log_loss_basics():
    y = np.array([0, 0, 1, 1])
    assert shotquality.auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert shotquality.auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
    assert abs(shotquality.log_loss(y, np.full(4, 0.5)) - np.log(2)) < 1e-12


def test_design_has_no_post_release_inputs():
    df = pd.DataFrame({
        "distance": [3.0, 24.0], "three": [False, True], "angle": [10.0, 80.0],
        "closest_def": [2.0, 8.0], "def_front": [0.9, 0.1], "closing": [5.0, np.nan],
        "defs_6ft": [2, 0], "speed_toward": [8.0, 0.0], "speed_across": [1.0, 0.0],
        "touch_s": [1.5, 0.8], "dribbles": [3, 0], "catch_shoot": [False, True],
        "shot_clock": [12.0, np.nan], "game_clock": [300.0, 1.0], "poss_s": [9.0, 3.0],
        "putback": [False, False], "hull": [500.0, np.nan],
        # after the release: never inputs
        "peak_z": [12.0, 15.0], "flight_s": [0.6, 1.1], "made": [True, False],
    })  # fmt: skip
    full = shotquality.design(df, "full")
    assert np.isfinite(full).all()
    changed = df.assign(peak_z=[1.0, 2.0], flight_s=[9.0, 9.0], made=[False, True])
    assert np.array_equal(full, shotquality.design(changed, "full"))
    assert shotquality.design(df, "location").shape[1] < full.shape[1]


def test_replay_packs_frames_as_tenths(tmp_path, monkeypatch):
    n = 5
    cols = {"game_id": "0021500001", "ts": np.arange(n) * 40, "period": 1,
            "game_clock": 700.0 - 0.04 * np.arange(n),
            "shot_clock": [24.0, 23.9, np.nan, 23.8, 23.7],
            "event_id": 1, "ball_x": 47.25, "ball_y": 25.0, "ball_z": 4.0,
            "home_team_id": 1610612741, "away_team_id": 1610612752}  # fmt: skip
    for i, s in enumerate(replay.SLOTS):
        cols[f"{s}_id"] = i + 1
        cols[f"{s}_x"] = 10.0 + i
        cols[f"{s}_y"] = 20.0
    frames = pd.DataFrame(cols)
    monkeypatch.setattr(replay.sportvu, "load_frames", lambda gid: frames)
    roster = pd.DataFrame({"person_id": range(1, 11), "first_name": "A", "last_name": "B",
                           "jersey": "0",
                           "team_id": [1610612741] * 5 + [1610612752] * 5})  # fmt: skip
    (tmp_path / "rosters").mkdir()
    roster.to_parquet(tmp_path / "rosters" / "0021500001.parquet")
    monkeypatch.setattr(replay.sportvu, "frames_dir", lambda: tmp_path / "frames")
    monkeypatch.setattr(replay, "get_paths", lambda: type("P", (), {"data": tmp_path})())
    d = replay.payload("0021500001")
    a = np.frombuffer(base64.b64decode(d["frames"]), "<i2").reshape(d["n"], d["stride"])
    assert d["stride"] == 26 and a[0, 0] == 1 and a[0, 1] == 7000 and a[0, 3] == 472
    assert a[2, 2] == replay.MISSING and a[0, 6] == 100 and a[0, 24] == 190
    assert d["lineups"] == [[0, list(range(1, 11))]] and d["players"][6]["team"] == 1
    out = replay.build("0021500001", out=tmp_path / "r.html")
    assert out.read_text(encoding="utf-8").count("__DATA__") == 0
