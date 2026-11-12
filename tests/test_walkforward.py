"""Walk-forward engine: stage-2 fit, rest, scoring, and the built evaluation (spec 16)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from courtvision.config import get_paths
from courtvision.walkforward import apply_stage2, fit_stage2, rest_days, score


def test_stage2_recovers_coefficients():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"form": rng.normal(0, 5, 20000), "b2b_h": rng.integers(0, 2, 20000)})
    df["margin"] = 1.0 + 0.8 * df.form - 2.5 * df.b2b_h + rng.normal(0, 3, 20000)
    m = fit_stage2(df, ["form", "b2b_h"])
    assert m["beta"] == pytest.approx([1.0, 0.8, -2.5], abs=0.15)
    assert m["sigma"] == pytest.approx(3.0, abs=0.15)
    assert np.corrcoef(apply_stage2(m, df), df.margin)[0, 1] > 0.8


def test_rest_days_back_to_back_and_cap():
    g = pd.DataFrame({
        "game_id": ["a", "b", "c", "d"], "season": [1, 1, 1, 1],
        "game_date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-08", "2026-01-10"]),
        "home_team_id": [1, 1, 1, 2], "away_team_id": [2, 3, 2, 1],
    })  # fmt: skip
    r = rest_days(g)
    assert r[("a", 1)] == 3  # first game: treated as rested
    assert r[("b", 1)] == 1  # second night of a back-to-back
    assert r[("c", 1)] == 3  # six days off, capped
    assert r[("d", 1)] == 2 and r[("d", 2)] == 2


def test_score_perfect_and_coin_flip():
    df = pd.DataFrame({"margin": [5.0, -3.0, 8.0, -1.0]})
    s = score(df, df.margin.to_numpy(), 10.0)
    assert s["accuracy"] == 1 and s["margin_mae"] == 0
    flip = score(df, np.zeros(4), 10.0)
    assert flip["brier"] == pytest.approx(0.25)


@pytest.mark.integration
def test_walkforward_results_hold():
    import duckdb

    path = get_paths().duckdb
    if not path.exists():
        pytest.skip("no database")
    con = duckdb.connect(str(path), read_only=True)
    try:
        ev = con.execute("select * from forecast.wf_eval where season = 0").df()
        pe = con.execute("select * from forecast.wf_player_eval").df()
    except duckdb.Error:
        pytest.skip("walk-forward not run; run `courtvision walkforward`")
    finally:
        con.close()
    ll = ev.set_index("model").log_loss
    assert ll.min() < ll["home court only"] - 0.05  # far better than home court alone
    assert ll["form + roster"] < ll["team form"]  # the roster view adds information
    cov = pe.dropna(subset=["coverage_80"]).coverage_80
    assert ((cov > 0.75) & (cov < 0.85)).all()  # 80% ranges hold on unseen seasons
