"""The draft model (spec section 22): links to NBA careers, outcomes, the fit, the order."""

from __future__ import annotations

import gzip
import math
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from courtvision import draft
from courtvision.ingest import barttorvik


def test_norm_name_drops_accents_punctuation_and_suffixes():
    assert draft.norm_name("Nikola Jokić") == "nikola jokic"
    assert draft.norm_name("D'Angelo Russell Jr.") == "dangelo russell"
    assert draft.norm_name("Shai Gilgeous-Alexander") == "shai gilgeousalexander"


def _last():
    return pd.DataFrame({
        "pid": [1, 2, 3, 4], "player": ["Al Pick", "Bo Free", "Cy Same", "Cy Same"],
        "team": ["X", "Y", "Z", "W"], "last_year": [2015, 2016, 2016, 2016],
        "pick": [3.0, np.nan, np.nan, np.nan],
    })  # fmt: skip


def _nba():
    return pd.DataFrame({
        "person_id": [10, 20, 30, 31, 40],
        "name": ["Allen Pick", "Bo Free", "Cy Same", "Cy Same", "Al Other"],
        "draft_year": [2015, None, None, None, 2015], "draft_number": [3, None, None, None, 4],
        "from_year": [2015, 2017, 2016, 2016, 2015],
    })  # fmt: skip


def test_link_by_pick_needs_the_last_name_and_by_name_needs_a_unique_match():
    links = draft.link(_last(), _nba()).set_index("pid")
    assert links.loc[1, "person_id"] == 10 and links.loc[1, "how"] == "pick"  # 'Al' vs 'Allen'
    assert links.loc[2, "person_id"] == 20 and links.loc[2, "how"] == "name"  # undrafted, later
    assert 3 not in links.index and 4 not in links.index  # two NBA players of that name


def test_outcomes_sum_war_over_the_horizon_and_flag_complete():
    last = _last()
    links = pd.DataFrame({"pid": [1], "person_id": [10], "how": ["pick"]})
    war = pd.DataFrame({"person_id": [10] * 6, "season": range(2014, 2020),
                        "war": [9.0, 1.0, 2.0, 3.0, 4.0, 5.0]})  # fmt: skip
    o = draft.outcomes(last, links, war, complete_through=2018).set_index("pid")
    assert o.loc[1, "war"] == 1 + 2 + 3 + 4  # seasons 2015-2018 only (not 2014 college, not 2019)
    assert o.loc[1, "complete"] and o.loc[1, "nba_seasons"] == 4
    assert o.loc[2, "war"] == 0 and not o.loc[2, "complete"]  # left in 2016: through 2019 needed


def test_trainable_keeps_complete_outcomes_from_complete_cohorts():
    rows = pd.DataFrame({"last_year": [2010, 2012, 2010], "year": [2008, 2012, 2010]})
    t = draft.trainable(rows, complete_through=2016)  # outcomes complete for last_year <= 2013
    assert list(t.index) == [0]  # 2012: cohort too recent (2013 - 4 = 2009); 2010 row likewise


def test_fit_logit_recovers_signs_and_survives_a_constant_column_and_rare_positives():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(4000, 2))
    y = (rng.random(4000) < 1 / (1 + np.exp(-(-5 + 1.5 * X[:, 0] - 1.0 * X[:, 1])))).astype(float)
    Z = np.column_stack([X, np.zeros(4000)])  # a feature that never varies
    w = draft.fit_logit(Z, y)
    assert np.isfinite(w).all() and w[1] > 1.0 and w[2] < -0.6 and abs(w[3]) < 1e-6


def test_model_probabilities_never_rise_with_the_threshold():
    rng = np.random.default_rng(1)
    rows = pd.DataFrame(rng.normal(size=(3000, len(draft.FEATURES))), columns=draft.FEATURES)
    war = pd.Series(np.maximum(0, rng.normal(0, 4, 3000) + 3 * rows.bpm))
    m = draft.Model(rows, war)
    P = m.probs(rows.head(50))
    assert (np.diff(P, axis=1) <= 1e-12).all()
    ev = m.expected(P)
    assert (ev >= 0).all() and ev.max() <= draft.THRESHOLDS[-1]


def test_norm_cdf_matches_erf():
    for x in (-3, -1.2, 0, 0.4, 2.5):
        assert abs(draft._norm_cdf(x) - 0.5 * (1 + math.erf(x / math.sqrt(2)))) < 1e-6


def test_lottery_odds_and_order():
    rng = np.random.default_rng(3)
    seeds = np.arange(14)
    firsts = [draft.lottery_draw(seeds, rng)[0] for _ in range(20000)]
    assert abs(np.mean(np.array(firsts) == 0) - 0.14) < 0.01
    assert abs(np.mean(np.array(firsts) == 13) - 0.005) < 0.003
    one = draft.lottery_draw(seeds, rng)
    rest = one[4:]
    assert sorted(one) == list(range(14)) and list(rest) == sorted(rest)  # the rest by seed


def test_owners_apply_protection_and_swaps():
    teams = [1, 2, 3]
    slots = np.array([[1, 2, 3], [3, 1, 2]])  # sims x teams
    abbrev = {1: "AAA", 2: "BBB", 3: "CCC"}
    trades = pd.DataFrame({"original": ["AAA", "BBB"], "owner": ["CCC", "CCC"],
                           "protected_through": [1, np.nan], "kind": ["convey", "swap"],
                           "note": ["", ""]})  # fmt: skip
    own, conv = draft.owners(slots, teams, abbrev, trades)
    assert own[0, 0] == 1 and own[1, 0] == 3  # AAA keeps a #1 pick, conveys a #3
    assert own[1, 1] == 3 and own[1, 2] == 2  # BBB's #1 beats CCC's #2: CCC swaps into it
    assert own[0, 1] == 3 and own[0, 2] == 2  # sim 0: BBB's #2 beats CCC's #3 too
    assert set(conv.kind) == {"convey", "swap"}


def test_mock_takes_value_and_lets_fit_decide_close_calls():
    board = pd.DataFrame({"pid": [1, 2, 3], "ev": [10.0, 6.0, 5.8]})
    fit = pd.DataFrame([[0.0, -1.0, 2.0], [0.0, 0.0, 0.0]], index=[7, 8], columns=[1, 2, 3])
    m = draft.mock([7, 7, 8], board, fit)
    assert list(m.pid) == [1, 3, 2]  # 5.8 x 1.2 beats 6.0 x 0.9 for team 7
    assert m.best_available.tolist() == [1, 2, 2]


def test_team_fit_averages_strengths_over_needs():
    needs = pd.DataFrame({"shooting": [2.0, 0.0], "defense": [0.0, 0.0]}, index=[1, 2])
    strength = pd.DataFrame({"shooting": [1.5, -1.0], "defense": [0.0, 2.0]}, index=[11, 12])
    f = draft.team_fit(needs, strength)
    assert f.loc[1, 11] == 1.5 and f.loc[1, 12] == -1.0 and (f.loc[2] == 0).all()


def test_read_trades_skips_blank_rows_and_other_years(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text(
        "year,round,original,owner,protected_through,kind,note\n"
        "2027,1,AAA,BBB,4,,top-4 protected\n2028,1,AAA,BBB,,,\n,,,,,,\n",
        encoding="utf-8",
    )
    t = draft.read_trades(p, 2027)
    assert len(t) == 1 and t.iloc[0].protected_through == 4 and t.iloc[0].kind == "convey"
    assert draft.read_trades(tmp_path / "missing.csv", 2027).empty


# ------------------------------------------------------------------ Barttorvik files


ROW = (
    '"A B","Duke",ACC,30,70.0,120.0,25.0,55.0,58.0,5.0,15.0,20.0,12.0,100,130,0.77,150,280,'
    '0.536,60,160,0.375,2.0,2.5,40.0,Fr,6-8,1,4.0,125.0,2.5,{year},{pid},"Town, ST",99.5,'
    "2.0,90,130,40,100,0.69,0.40,30,32,0.94,{pick},95.0,92.0,3.0,200.0,9.0,6.0,3.0,9.5,32.0,"
    '6.5,3.0,1.5,4.5,6.0,3.0,1.2,0.8,19.0,"Wing F",8.0,2006-01-20,1,2'
)


def _csv(year, pid=7, pick="3"):
    return "\n".join([ROW.format(year=year, pid=pid, pick=pick)] * 3).encode()


def test_parse_names_columns_and_keeps_extra_ones():
    df = barttorvik.parse(_csv(2025))
    assert df.loc[0, "player"] == "A B" and df.loc[0, "pick"] == 3 and df.loc[0, "pts"] == 19.0
    assert df.loc[0, "birth_date"] == "2006-01-20" and "unk_67" in df.columns
    assert barttorvik._inches(pd.Series(["6-8", "7-0", "", None])).tolist()[:2] == [80, 84]


def test_load_reads_the_newest_current_file(tmp_path):
    (tmp_path / "current").mkdir()
    (tmp_path / "players_2025.csv.gz").write_bytes(gzip.compress(_csv(2025)))
    cur = tmp_path / "current"
    (cur / "players_2026_2025-11-20.csv.gz").write_bytes(gzip.compress(_csv(2026, pick="")))
    (cur / "players_2026_2025-11-21.csv.gz").write_bytes(gzip.compress(_csv(2026, 8, "")))
    con = duckdb.connect()
    res = barttorvik.load(con, tmp_path, 2026)
    assert res["seasons"] == "2024-2025"
    rows = con.execute("select season, pid, height_in, as_of from ncaa.player_seasons "
                       "order by season, pid").fetchall()  # fmt: skip
    assert rows[0][:3] == (2024, 7, 80) and rows[0][3] is None
    assert rows[-1][1] == 8 and rows[-1][3] == date(2025, 11, 21)  # the newer download


def test_college_season_turns_over_in_november():
    from courtvision.cli import _college_season

    assert _college_season(date(2026, 11, 24)) == 2027
    assert _college_season(date(2027, 6, 1)) == 2027
    assert _college_season(date(2027, 10, 31)) == 2027
