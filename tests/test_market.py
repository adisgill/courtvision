"""Budget rules, tax and name matching of the market model."""

import math

import pandas as pd

from courtvision.ingest.salaries import match_players, norm_name, norm_team
from courtvision.market import (
    CAP_2023,
    budget_status,
    outgoing_needed,
    tax_bill,
)

CAP = {"salary_cap": 165e6, "luxury_tax_line": 200e6, "first_apron": 209e6,
       "second_apron": 222e6}  # fmt: skip


def test_tax_brackets():
    step = 6e6
    assert tax_bill(199e6, 200e6, step) == 0
    assert math.isclose(tax_bill(203e6, 200e6, step), 3e6 * 1.5)
    # 6M at 1.5, 6M at 1.75, 3M at 2.5
    assert math.isclose(tax_bill(215e6, 200e6, step), 6e6 * 1.5 + 6e6 * 1.75 + 3e6 * 2.5)
    # beyond four steps the rate keeps rising by 0.5
    over = 4 * step + step
    expect = step * (1.5 + 1.75 + 2.5 + 3.25 + 3.75)
    assert math.isclose(tax_bill(200e6 + over, 200e6, step), expect)


def test_budget_status_thresholds():
    assert budget_status(150e6, CAP) == "under the cap"
    assert budget_status(180e6, CAP) == "over the cap, under the tax"
    assert budget_status(205e6, CAP) == "over the tax"
    assert budget_status(215e6, CAP) == "over the first apron"
    assert budget_status(230e6, CAP) == "over the second apron"


def test_salary_matching():
    ratio = CAP["salary_cap"] / CAP_2023
    # cap space absorbs the whole salary
    assert outgoing_needed(10e6, 150e6, CAP) == 0
    # apron teams take back no more than they send
    assert outgoing_needed(20e6, 215e6, CAP) == 20e6
    # over the cap, small salary: 200% + 250k
    assert math.isclose(outgoing_needed(5e6, 180e6, CAP), (5e6 - 250_000) / 2)
    # middle band: outgoing + 7.5M (scaled)
    assert math.isclose(outgoing_needed(25e6, 180e6, CAP), 25e6 - 7.5e6 * ratio)
    # large salary: 125% + 250k
    assert math.isclose(outgoing_needed(60e6, 180e6, CAP), (60e6 - 250_000) / 1.25)


def test_names_normalize_accents_suffixes_and_teams():
    assert norm_name("Luka Dončić") == "luka doncic"
    assert norm_name("Jaren Jackson Jr.") == "jaren jackson"
    assert norm_name("De'Aaron Fox") == "deaaron fox"
    assert norm_name("Karl-Anthony Towns") == "karlanthony towns"
    assert norm_team("LA Clippers") == norm_team("Los Angeles Clippers")


def test_match_players_uses_roster_to_break_ties():
    players = pd.DataFrame({"person_id": [1, 2, 3], "name": ["Marcus Morris", "Marcus Morris",
                                                             "Luka Dončić"]})  # fmt: skip
    played = pd.DataFrame({"season": [2025, 2025], "person_id": [2, 3], "team_id": [10, 20]})
    sal = pd.DataFrame({"season_year": [2025, 2025, 2025], "nba_team_id": [10, 20, 30],
                        "player": ["Marcus Morris Sr.", "Luka Doncic", "Nobody Here"]})  # fmt: skip
    got = match_players(sal, players, played).tolist()
    assert got[0] == 2 and got[1] == 3 and pd.isna(got[2])


def test_match_players_nicknames_and_spellings():
    players = pd.DataFrame({"person_id": [1, 2, 3, 4],
                            "name": ["Nic Claxton", "Herbert Jones", "Dennis Schröder",
                                     "Nick Smith"]})  # fmt: skip
    played = pd.DataFrame({"season": [2025] * 4, "person_id": [1, 2, 3, 4],
                           "team_id": [1, 2, 3, 4]})  # fmt: skip
    names = ["Nicolas Claxton", "Herb Jones", "Dennis Schroeder"]
    sal = pd.DataFrame({"season_year": [2026] * 3, "nba_team_id": [9, 9, 9], "player": names})
    assert match_players(sal, players, played).tolist() == [1, 2, 3]


def test_selected_point_reads_every_customdata_shape():
    from types import SimpleNamespace

    from courtvision.app.charts import selected

    def ev(cd):
        return SimpleNamespace(selection={"points": [{"customdata": cd}]})

    assert selected(ev([1610612747])) == 1610612747
    assert selected(ev({"0": "Nikola Jokic", "1": 7})) == "Nikola Jokic"
    assert selected(ev({"0": "a", "1": 7}), 1) == 7
    assert selected(ev("x")) == "x"
    assert selected(SimpleNamespace(selection={"points": []})) is None
    assert selected(None) is None


def test_max_share_by_service():
    from courtvision.market import max_share

    assert list(max_share([0, 4, 6, 7, 9, 10, 15])) == [0.25, 0.25, 0.25, 0.30, 0.30, 0.35, 0.35]


def test_rank_pricing_maps_value_quantiles_to_salary_quantiles():
    import numpy as np

    from courtvision.market import fair_share, market_curve

    rng = np.random.default_rng(0)
    value = rng.normal(100, 50, 400)
    share = np.sort(rng.uniform(0.02, 0.35, 400))  # salaries unrelated in order
    curve = market_curve(value, share)
    assert np.isclose(fair_share(value.max(), curve), share.max())  # best player: top salary
    assert np.isclose(fair_share(value.min(), curve), share.min())
    assert np.isclose(fair_share(np.median(value), curve), np.median(share), atol=0.01)
    xs = np.linspace(-50, 250, 50)
    assert np.all(np.diff(fair_share(xs, curve)) >= 0)  # never decreasing


def test_pava_is_monotone_and_preserves_the_mean():
    import numpy as np

    from courtvision.market import pava

    y = np.array([1.0, 3.0, 2.0, 2.0, 5.0, 4.0])
    fit = pava(y, np.ones(len(y)))
    assert np.all(np.diff(fit) >= 0) and np.isclose(fit.mean(), y.mean())


def test_mojibake_repair():
    from courtvision.ingest.contracts import fix_mojibake

    assert fix_mojibake("Luka DonÄ\x8diÄ\x87") == "Luka Dončić"
    assert fix_mojibake("Plain Name") == "Plain Name"
    assert fix_mojibake(None) is None


def test_team_form_model_recovers_strength_and_opponent_effect():
    import numpy as np

    from courtvision.forecast import fit_team_model, predict_team

    rng = np.random.default_rng(1)
    form = {t: rng.normal(0, 3) for t in range(10)}
    opp = {t: rng.normal(0, 3) for t in range(10)}
    rows = []
    for _ in range(800):
        a, b = rng.choice(10, 2, replace=False)
        home = bool(rng.integers(2))
        rows.append({"team_id": a, "opp_id": b, "home": home,
                     "value": 2 + form[a] + opp[b] + (1.5 if home else -1.5)
                     + rng.normal(0, 2)})  # fmt: skip
    g = pd.DataFrame(rows)
    m = fit_team_model(g, np.ones(len(g)))
    pred = predict_team(m, 0, 1, True)
    truth = 2 + form[0] + opp[1] + 1.5
    assert abs(pred - truth) < 1.0
