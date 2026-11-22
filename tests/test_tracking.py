"""The tracking module on synthetic frames: reading SportVU logs, who has the ball, finding
shots in the ball's flight, and matching them to the play-by-play."""

from __future__ import annotations

import numpy as np
import pandas as pd

from courtvision.tracking import align, detect, sportvu

HOME, AWAY = 10, 20


def _moment(ts, clock, ball, players, q=1, shot=20.0):
    ents = [[-1, -1, *ball]] + [[t, pid, x, y, 0.0] for t, pid, x, y in players]
    return [q, ts, clock, shot, None, ents]


def test_parse_dedupes_overlapping_events_and_sorts_slots():
    players = [(AWAY, 9, 50, 25), (HOME, 3, 40, 20), (HOME, 1, 41, 21), (HOME, 2, 42, 22),
               (HOME, 5, 43, 23), (HOME, 4, 44, 24), (AWAY, 7, 51, 26), (AWAY, 8, 52, 27),
               (AWAY, 6, 53, 28), (AWAY, 10, 54, 29)]  # fmt: skip
    m1, m2, m3 = (_moment(1000 + 40 * i, 700 - 0.04 * i, [45, 25, 5], players) for i in range(3))
    roster = {"home": {"teamid": HOME, "players": [
        {"playerid": p, "firstname": "A", "lastname": f"H{p}", "jersey": "1", "position": "G"}
        for p in (1, 2, 3, 4, 5)]},
              "visitor": {"teamid": AWAY, "players": [
        {"playerid": p, "firstname": "B", "lastname": f"A{p}", "jersey": "2", "position": "F"}
        for p in (6, 7, 8, 9, 10)]}}  # fmt: skip
    game = {
        "gameid": "0021500001",
        "gamedate": "2015-10-27",
        "events": [
            {"eventId": "1", **roster, "moments": [m1, m2]},
            {"eventId": "2", **roster, "moments": [m2, m3]},  # m2 again: events overlap
            {"eventId": "3", **roster, "moments": []},
        ],
    }
    frames, ros = sportvu.parse(game)
    assert len(frames) == 3 and frames.ts.is_monotonic_increasing
    assert frames.event_id.tolist() == [1, 1, 2]  # a frame keeps the first event it was in
    assert [frames[f"h{i}_id"].iloc[0] for i in range(5)] == [1, 2, 3, 4, 5]
    assert [frames[f"a{i}_id"].iloc[0] for i in range(5)] == [6, 7, 8, 9, 10]
    assert frames.h0_x.iloc[0] == 41 and frames.ball_z.iloc[0] == 5
    assert len(ros) == 10 and set(ros.team_id) == {HOME, AWAY}


def _game(n, ball, xy, clock0=600.0):
    ids = np.tile(np.arange(1, 11), (n, 1))
    return detect.Game(period=np.ones(n, int), clock=clock0 - 0.04 * np.arange(n),
                       shot_clock=np.full(n, 20.0), ball=ball, xy=xy, ids=ids,
                       ts=np.arange(n) * 40)  # fmt: skip


def _court(n):
    """10 players spread out on the right half, nobody near the left hoop."""
    xy = np.zeros((n, 10, 2))
    xy[:, :, 0] = np.linspace(55, 85, 10)
    xy[:, :, 1] = np.linspace(5, 45, 10)
    return xy


def test_holder_and_possessor_ignore_a_contesting_defender():
    n = 30
    xy = _court(n)
    xy[:, 2] = [30, 25]  # the shooter (home slot 2) has the ball
    xy[:, 7] = [31.5, 25]  # an away defender right on him
    ball = np.tile([30.4, 25.0, 4.0], (n, 1))
    g = _game(n, ball, xy)
    hold = detect.holder(g)
    # the defender is a touch nearer the ball on a few frames
    hold[-3:] = 7
    assert detect.possessor(hold, n - 1) == 2


def _jump_shot(n_hold=30, n_flight=30):
    """A shooter at (30, 25) holds the ball, then it arcs into the left hoop."""
    n = n_hold + n_flight + 10
    xy = _court(n)
    xy[:, 2] = [30, 25]
    ball = np.zeros((n, 3))
    ball[:n_hold] = [30.5, 25, 6]
    s = np.linspace(0, 1, n_flight)
    ball[n_hold : n_hold + n_flight, 0] = 30 + (5.25 - 30) * s
    ball[n_hold : n_hold + n_flight, 1] = 25
    ball[n_hold : n_hold + n_flight, 2] = 7 + 3 * s + 16 * s * (1 - s)  # up to ~14 ft, ends at 10
    ball[n_hold + n_flight :] = [5.25, 25, 4]  # through the net
    return _game(n, ball, xy), n_hold


def test_shots_finds_the_release_shooter_and_distance():
    g, n_hold = _jump_shot()
    s = detect.shots(g)
    assert len(s) == 1
    shot = s.iloc[0]
    assert shot.kind == "rim" and shot.shooter_id == 3 and shot.hoop == 0
    assert abs(shot.release - (n_hold - 1)) <= 2
    assert abs(shot.distance - (30 - 5.25)) < 0.5
    assert not shot.free_throw


def test_logged_location_toward_either_hoop():
    # 10 ft straight out, then 10 ft to the side (tenths of a foot from the hoop)
    assert align.logged_location(0, 100, 0) == (15.25, 25.0)
    assert align.logged_location(0, 100, 1) == (78.75, 25.0)
    assert align.logged_location(100, 0, 0) == (5.25, 15.0)
    assert align.logged_location(100, 0, 1) == (88.75, 35.0)


def _tracked(rows):
    cols = {"period": 1, "free_throw": False, "hoop": 0, "shooter_y": 25.0}
    return pd.DataFrame([cols | r for r in rows])


def test_match_uses_the_lag_the_distance_gate_and_one_to_one():
    fg = pd.DataFrame(
        {
            "period": [1, 1, 1],
            "t": [500.0, 450.0, 400.0],
            "person_id": [3, 4, 5],
            "xLegacy": [0, 0, 0],
            "yLegacy": [240, 150, 10],
        }
    )  # 24 ft, 15 ft, a 1 ft layup
    tr = _tracked(
        [
            {"clock": 502.7, "shooter_id": 3, "shooter_x": 29.2, "distance": 23.95},  # matches
            {"clock": 452.7, "shooter_id": 4, "shooter_x": 30.0, "distance": 24.75},  # 9.75 ft off
            {"clock": 402.5, "shooter_id": 5, "shooter_x": 12.0, "distance": 6.75},  # layup slack
            {"clock": 300.0, "shooter_id": 3, "shooter_x": 29.2, "distance": 23.95},  # wrong time
        ]
    )
    m = align.match(fg, tr)
    pairs = dict(zip(m.pbp_row, m.shot_row, strict=True))
    assert pairs == {0: 0, 2: 2}
    # without the distance cue the 15 ft shot pairs with the 24 ft one; QA catches that
    m2 = align.match(fg, tr, use_location=False)
    assert dict(zip(m2.pbp_row, m2.shot_row, strict=True))[1] == 1


def test_clock_running():
    n = 40
    g = _game(n, np.zeros((n, 3)), _court(n))
    g.clock[20:] = g.clock[20]  # the clock stops (a free throw)
    run = detect.clock_running(g)
    assert run[5:18].all() and not run[35:].any()
