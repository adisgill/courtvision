"""Adjusted plus-minus recovers known player effects from synthetic stints."""

import numpy as np
import pandas as pd

from courtvision.metrics.impact import Season, _sequential


def synthetic(seed: int = 0, n_games: int = 400, true=None):
    rng = np.random.default_rng(seed)
    teams = {t: list(range(t * 10, t * 10 + 10)) for t in range(6)}
    if true is None:
        true = {p: (rng.normal(0, 4), rng.normal(0, 4)) for ps in teams.values() for p in ps}
    rows = []
    for g in range(n_games):
        a, b = rng.choice(6, size=2, replace=False)
        for _ in range(20):
            fa = sorted(rng.choice(teams[a], 5, replace=False).tolist())
            fb = sorted(rng.choice(teams[b], 5, replace=False).tolist())
            for off, dfn, home in ((fa, fb, True), (fb, fa, False)):
                poss = int(rng.integers(3, 12))
                rate = 110 + sum(true[p][0] for p in off) - sum(true[p][1] for p in dfn)
                rate += 1.5 if home else -1.5
                pts = rng.normal(rate / 100 * poss, 1.1 * np.sqrt(poss))
                rows.append({"game_id": f"g{g}", "pts": pts, "poss": poss,
                             "off5": off, "def5": dfn, "home": home})  # fmt: skip
    return pd.DataFrame(rows), true


def test_ridge_recovers_effects_and_home_court():
    df, true = synthetic()
    s = Season(df, folds=True)
    beta = s.solve(50.0, np.zeros(s.p))
    est_o = {int(p): beta[2 * i] for i, p in enumerate(s.players)}
    est_d = {int(p): -beta[2 * i + 1] for i, p in enumerate(s.players)}
    o = np.array([[true[p][0], est_o[p]] for p in est_o])
    d = np.array([[true[p][1], est_d[p]] for p in est_d])
    assert np.corrcoef(o.T)[0, 1] > 0.8
    assert np.corrcoef(d.T)[0, 1] > 0.8
    assert abs(beta[-1] - 1.5) < 0.6  # home court, per side
    # cross-validation prefers some shrinkage over none and over a huge penalty
    errs = {lam: s.cv_error(lam, np.zeros(s.p)) for lam in (1e-3, 50.0, 1e6)}
    assert errs[50.0] < errs[1e6]


def test_prior_carries_last_season_forward():
    df1, true = synthetic(1)
    df2, _ = synthetic(2, n_games=40, true=true)  # a short second season
    seasons = {1: Season(df1), 2: Season(df2)}
    fits, _ = _sequential(seasons, 400.0, 1.0, {}, (0.0, 0.0))
    s2 = seasons[2]
    beta = fits[2][0]
    only = s2.solve(400.0, np.zeros(s2.p))
    t = np.array([true[int(p)][0] for p in s2.players])
    # with last season as the prior, the short season's estimates track the truth better
    assert np.corrcoef(t, beta[0:-1:2])[0, 1] > np.corrcoef(t, only[0:-1:2])[0, 1]


def test_schedule_effect_removes_opponent_strength():
    from courtvision.metrics.adjust import schedule_effects

    rng = np.random.default_rng(3)
    a = {t: rng.normal(0, 2) for t in range(8)}  # team strength
    b = {t: rng.normal(0, 2) for t in range(8)}  # what each team does to its opponents
    rows = []
    for _ in range(600):
        t, o = rng.choice(8, 2, replace=False)
        home = bool(rng.integers(2))
        v = a[t] + b[o] + (1.0 if home else -1.0) + rng.normal(0, 0.5)
        rows.append({"team_id": t, "opp_id": o, "home": home, "value": v})
    g = pd.DataFrame(rows)
    eff = schedule_effects(g)
    raw = g.groupby("team_id").value.mean()
    adj = raw - eff
    truth = pd.Series(a) - np.mean(list(a.values()))
    assert np.corrcoef(adj - adj.mean(), truth.loc[adj.index])[0, 1] > 0.97
    assert np.corrcoef(raw - raw.mean(), truth.loc[raw.index])[0, 1] < 0.99


def test_box_prior_helps_a_short_season():
    # A short season with an informative box prior beats the same season with prior 0.
    df, true = synthetic(4, n_games=30)
    s = Season(df)
    box = {int(p): (true[int(p)][0] + 1.0, true[int(p)][1] - 1.0, 0.8) for p in s.players}
    with_box = s.solve(400.0, s.prior({}, box, (0.0, 0.0), alpha=0.0))
    without = s.solve(400.0, np.zeros(s.p))
    t = np.array([true[int(p)][0] for p in s.players])
    err_box = np.mean((with_box[0:-1:2] - t) ** 2)
    err_none = np.mean((without[0:-1:2] - t) ** 2)
    assert err_box < err_none
