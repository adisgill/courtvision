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
