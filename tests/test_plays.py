"""Play types (spec section 17) and the hexagon shot maps (section 18)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from courtvision.app import court
from courtvision.config import get_paths
from courtvision.plays import team_profile


def test_hexagons_round_trip_and_nearest_center():
    q = np.array([0, 1, -3, 5, 2])
    r = np.array([0, 0, 4, -2, 7])
    cx, cy = court.hex_center(q, r)
    q2, r2 = court.hex_of(cx, cy)
    assert (q2 == q).all() and (r2 == r).all()
    rng = np.random.default_rng(0)
    x, y = rng.uniform(-250, 250, 2000), rng.uniform(-40, 300, 2000)
    hq, hr = court.hex_of(x, y)
    hx, hy = court.hex_center(hq, hr)
    d_own = np.hypot(x - hx, y - hy)
    assert d_own.max() <= court.HEX_R + 1e-9  # inside its hexagon's circumcircle
    # no other center (neighbours) is closer
    for dq, dr in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, -1), (-1, 1)):
        nx, ny = court.hex_center(hq + dq, hr + dr)
        assert (np.hypot(x - nx, y - ny) >= d_own - 1e-9).all()


def test_unit_like_the_league_scores_zero():
    rng = np.random.default_rng(1)
    n = 20000
    lg = pd.DataFrame({"x": rng.uniform(-200, 200, n), "y": rng.uniform(0, 250, n)})
    lg["shot_value"] = np.where(np.hypot(lg.x, lg.y) > 237.5, 3, 2)
    lg["made"] = (rng.random(n) < 0.45).astype(int)
    base = court.league_hex(lg)
    h = court.unit_hex(lg.assign(season=2025), {2025: base})
    assert h["diff"].abs().max() < 1e-6  # the league against itself: no edge anywhere
    assert h.share.sum() == pytest.approx(1.0)
    hot = lg.assign(season=2025, made=1)  # a unit that makes everything
    assert (court.unit_hex(hot, {2025: base})["diff"] > 0).all()


def test_team_profile_shares_shrinkage_and_sides():
    rows = []
    for team, opp, n, make in ((1, 2, 300, 0.6), (2, 1, 300, 0.4)):
        for i in range(n):
            rows.append({"season": 2025, "team_id": team, "opp_id": opp,
                         "play": "Drive" if i % 3 else "Pull-up", "made": int(i < n * make),
                         "assisted": 0})  # fmt: skip
    s = pd.DataFrame(rows)
    s["pts"] = 2 * s.made
    lg = pd.DataFrame({"season": 2025, "play": ["Drive", "Pull-up"], "lg_pps": [1.0, 1.0],
                       "lg_share": [2 / 3, 1 / 3]})  # fmt: skip
    off = team_profile(s, lg, "offense", k=100)
    assert off.groupby("team_id").share.sum().tolist() == pytest.approx([1.0, 1.0])
    o = off.set_index(["team_id", "play"])
    assert (o.pp100_rel_shrunk.abs() <= o.pp100_rel.abs() + 1e-9).all()  # shrinks
    de = team_profile(s, lg, "defense", k=100).set_index(["team_id", "play"])
    # team 2's defense allowed what team 1 scored
    assert de.loc[(2, "Drive"), "pts"] == o.loc[(1, "Drive"), "pts"]


@pytest.mark.integration
def test_built_play_types_do_not_leak_results():
    import duckdb

    path = get_paths().duckdb
    if not path.exists():
        pytest.skip("no database")
    con = duckdb.connect(str(path), read_only=True)
    try:
        lg = con.execute("select * from plays.league").df()
    except duckdb.Error:
        pytest.skip("plays not built; run `courtvision plays`")
    finally:
        con.close()
    # a play defined by its outcome would show FG% near 0 or 1
    assert lg.fg.between(0.2, 0.9).all(), lg[~lg.fg.between(0.2, 0.9)]
    assert lg.groupby("season").share.sum().to_numpy() == pytest.approx(1.0)
    spot = lg[lg.play == "Spot-up three"]
    assert spot.fg.between(0.30, 0.42).all()  # a three-point shot's FG%, every season
