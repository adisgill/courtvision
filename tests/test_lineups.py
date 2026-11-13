"""Lineups (spec section 19): the five optimizer, the chemistry test, the built tables."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from courtvision.config import get_paths
from courtvision.lineups import best_fives, split_half


def test_best_fives_respects_positions():
    c = pd.DataFrame({
        "person_id": range(1, 9),
        "impact": [9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0],
        "grp": ["B", "B", "B", "W", "W", "G", "W", "G"],
    })  # fmt: skip
    best = best_fives(c, n=2)
    ids, total = best[0]
    grps = c.set_index("person_id").grp.loc[ids].tolist()
    assert grps.count("G") >= 1 and 1 <= grps.count("B") <= 2  # never three centers
    assert sorted(ids) == [1, 2, 4, 5, 6] and total == pytest.approx(32.0)
    assert best[1][1] <= total


def _stints(bonus_sd: float, seed: int) -> tuple[pd.DataFrame, pd.Series]:
    """Two seasons of team-stints: each lineup scores its players' expectation plus a
    lineup-specific bonus (``bonus_sd``) plus noise."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in (1, 2):
        for team in range(30):
            for lu in range(6):
                bonus = rng.normal(0, bonus_sd)
                exp = rng.normal(0, 4)
                for g in range(40):
                    off = rng.poisson(110 + (exp + bonus) / 2, 1)[0] / 100 * 30
                    dfn = rng.poisson(110 - (exp + bonus) / 2, 1)[0] / 100 * 30
                    rows.append({"season": season, "team_id": team, "lineup": f"{team}-{lu}",
                                 "game_id": f"{season}{team:02d}{g:03d}", "minutes": 6.0,
                                 "off_poss": 30, "off_pts": off, "def_poss": 30, "def_pts": dfn,
                                 "exp_net": exp, "exp_net_prev": exp})  # fmt: skip
    return pd.DataFrame(rows), pd.Series({1: 110.0, 2: 110.0})


def test_chemistry_test_finds_none_when_there_is_none():
    st, lg = _stints(0.0, 0)
    res = split_half(st, lg)
    assert abs(res["synergy_r"]) < 0.25 and res["k_poss"] > 2000


def test_chemistry_test_finds_it_when_it_is_there():
    st, lg = _stints(12.0, 1)
    res = split_half(st, lg)
    assert res["synergy_prev_r"] > 0.4 and res["k_poss"] < 2000


@pytest.mark.integration
def test_built_lineup_tables():
    import duckdb

    path = get_paths().duckdb
    if not path.exists():
        pytest.skip("no database")
    con = duckdb.connect(str(path), read_only=True)
    try:
        u = con.execute("select * from lineups.units where season = 2025").df()
        tests = con.execute("select * from lineups.tests").df()
    except duckdb.Error:
        pytest.skip("lineups not built; run `courtvision lineups`")
    finally:
        con.close()
    assert (u.players.map(len) == 5).all()
    # the projection sits between the players' expectation and the raw result
    lo = np.minimum(u.exp_net100, u.net100) - 1e-9
    hi = np.maximum(u.exp_net100, u.net100) + 1e-9
    ok = u.net100.notna() & u.exp_net100.notna()
    assert u[ok].proj_net100.between(lo[ok], hi[ok]).all()
    fw = tests[tests.test == "second half from first"].set_index("predictor").mae
    assert fw["projection: players + shrunk synergy"] <= fw["its players' impacts (last season)"]
    assert fw["its players' impacts (last season)"] < fw["lineup's own first-half net"]
