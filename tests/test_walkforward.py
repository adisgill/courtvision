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


def test_margin_range_moves_from_the_backtest_to_the_season():
    from courtvision.walkforward import range_sigma

    assert range_sigma(14.0, 0, 0.0, 25) == pytest.approx(14.0)  # no games yet: the backtest
    # 175 games missing by 17 on average (RMS) against a prior worth 25 games at 14
    s = range_sigma(14.0, 175, 175 * 17.0**2, 25)
    assert s == pytest.approx(np.sqrt((25 * 14**2 + 175 * 17**2) / 200))
    assert 16.5 < s < 17.0


def test_season_misses_counts_games_before_each_week():
    from courtvision.walkforward import season_misses

    d = pd.DataFrame({
        "game_date": pd.to_datetime(["2026-10-20", "2026-10-22", "2026-10-27", "2026-11-04"]),
        "margin": [10.0, -4.0, 6.0, 0.0],
    })  # fmt: skip
    m = season_misses(d, pd.Series([0.0, 0.0, 0.0, 0.0]))
    assert m.n.tolist() == [0, 0, 2, 3]  # week 1: nothing before; week 2: two games; week 3
    assert m.sse.tolist() == [0, 0, 116, 152]


def test_two_sided_player_ranges_split_the_misses():
    from courtvision.walkforward import fit_player_range, player_bounds, range_params

    rng = np.random.default_rng(1)
    mu = rng.uniform(1, 30, 40000)
    act = rng.poisson(rng.gamma(2.0, mu / 2.0))  # counts with a long upper tail
    p = fit_player_range(mu[:20000], act[:20000])
    lo, hi = player_bounds(mu[20000:], p)
    below, above = (act[20000:] < lo).mean(), (act[20000:] > hi).mean()
    assert 0.08 < below < 0.12 and 0.08 < above < 0.12
    assert p["hi_c"] * 10 ** p["hi_a"] > p["lo_c"] * 10 ** p["lo_a"]  # longer upper side
    # a database from before: one symmetric width per stat
    old = range_params({"spread_pts": 2.0}, "pts")
    assert old["lo_c"] == old["hi_c"] == pytest.approx(1.2816 * 2.0) and old["hi_a"] == 0.5


def test_minutes_share_fit_recovers_how_absences_were_shared():
    from courtvision.walkforward import fit_minutes_share, share_minutes

    rng = np.random.default_rng(2)
    rows = []
    for g in range(3000):
        exp = rng.uniform(4, 38, 10)
        rows += [{"g": g, "exp_minutes": e} for e in exp]
    pl = pd.DataFrame(rows)
    pl["p"] = (rng.random(len(pl)) > 0.15).astype(float)  # about one in seven sits
    true = {"absorb": 0.5, "cap": 40, "power": 1.0}
    want = share_minutes(pl.exp_minutes, pl.p, pl.g, true)
    pl["minutes"] = np.where(pl.p > 0, want + rng.normal(0, 3, len(pl)), 0.0)
    rule = fit_minutes_share(pl[["g", "exp_minutes", "minutes"]])
    assert rule["absorb"] in (0.4, 0.5, 0.6) and rule["cap"] in (38, 40, 42)
    got = share_minutes(pl.exp_minutes, pl.p, pl.g, rule)[pl.p > 0]
    assert np.abs(got - want[pl.p > 0]).mean() < 0.5
