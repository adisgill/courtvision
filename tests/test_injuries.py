"""The official injury report: PDF reading, per-game statuses, minute redistribution."""

from __future__ import annotations

import zlib
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from courtvision.ingest.injuries import _norm, for_games, parse_pdf, report_time
from courtvision.walkforward import expected_minutes

HEAD_X = {"Game Date": 19.1, "Game Time": 115.6, "Matchup": 196.0, "Team": 260.2,
          "Player Name": 421.0, "Current Status": 581.7, "Reason": 662.2}  # fmt: skip


def _cell(x: float, y: float, lines: list[str]) -> str:
    """One table cell the way the report draws it: clip rectangle, then its words."""
    out = f"q {x} {y} 80 21 re W* n BT /F0 -10 Tf\n"
    for i, line in enumerate(lines):
        words = line.split(" ")
        out += f"{x + 4} {y + 12 + 11 * i} Td ({words[0]}) Tj\n" if i == 0 else (
            f"0 11 Td ({words[0]}) Tj\n")  # fmt: skip
        for w in words[1:]:
            out += f"20 0 Td ({w}) Tj\n"
    return out + "ET Q\n"


def _pdf() -> bytes:
    s = "q 1 0 0 -1 0 595.35 cm\n"
    s += _cell(84.2, 35.4, ["Injury Report: 11/12/26 07:45 PM"])
    s += _cell(380, 540, ["Page 1 of 1"])
    for name, x in HEAD_X.items():
        s += _cell(x, 100.4, [name])
    row1 = {"Game Date": ["11/12/2026"], "Game Time": ["07:00 (ET)"], "Matchup": ["TOR@PHI"],
            "Team": ["Toronto Raptors"], "Player Name": ["Barnes, Scottie"],
            "Current Status": ["Out"],
            "Reason": ["Injury/Illness - Left", "Groin Strain"]}  # fmt: skip
    for col, lines in row1.items():
        s += _cell(HEAD_X[col], 130.0, lines)
    # second row: blank date, time, matchup, team (carried down); a wrapped hyphenated name
    row2 = {"Player Name": ["Jackson-", "Davis, Trayce"], "Current Status": ["Questionable"],
            "Reason": ["Injury/Illness - Illness"]}  # fmt: skip
    for col, lines in row2.items():
        s += _cell(HEAD_X[col], 160.0, lines)
    s += _cell(HEAD_X["Team"], 190.0, ["Philadelphia 76ers"])
    s += _cell(HEAD_X["Player Name"], 190.0, ["Embiid, Joel"])
    s += _cell(HEAD_X["Current Status"], 190.0, ["Probable"])
    s += _cell(HEAD_X["Reason"], 190.0, ["Injury/Illness - Right Knee \\(Rest\\)"])
    s += "Q\n"
    body = zlib.compress(s.encode("cp1252"))
    return b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\n" + body + b"\nendstream\n"


def test_parse_report_rows_and_carry_down():
    df = parse_pdf(_pdf())
    assert df.player.tolist() == ["Barnes, Scottie", "Jackson-Davis, Trayce", "Embiid, Joel"]
    assert df.status.tolist() == ["Out", "Questionable", "Probable"]
    assert df.reason.iloc[0] == "Injury/Illness - Left Groin Strain"  # wrapped cell joined
    assert df.reason.iloc[2].endswith("(Rest)")  # escaped parentheses
    assert df.team.tolist() == ["Toronto Raptors", "Toronto Raptors", "Philadelphia 76ers"]
    assert (df.matchup == "TOR@PHI").all()
    assert str(df.report_time.iloc[0]) == "2026-11-12 19:45:00"


def test_names_and_file_times():
    assert _norm("Dončić, Luka") == _norm("Doncic, Luka")
    assert _norm("Jaren Jackson Jr.") == "jaren jackson"
    t = report_time(Path("Injury-Report_2026-11-12_07_45PM.pdf"))
    assert (t.hour, t.minute) == (19, 45)


def test_statuses_for_games_and_carrying():
    con = duckdb.connect()
    con.execute("create schema injury")
    rep = pd.DataFrame({
        "game_date": pd.to_datetime(["2026-11-12"] * 3), "team_id": [1, 1, 2],
        "person_id": [10.0, 11.0, 20.0], "player": ["A, a", "B, b", "C, c"],
        "status": ["Out", "Questionable", "Out"], "reason": ["x", "y", "z"],
        "report_time": pd.Timestamp("2026-11-12 19:45"),
    })  # fmt: skip
    con.register("r", rep)
    con.execute("create table injury.reports as select * from r")
    games = pd.DataFrame({"game_id": ["g1", "g2", "g3"],
                          "game_date": pd.to_datetime(["2026-11-12", "2026-11-13", "2026-11-20"]),
                          "home_team_id": [1, 1, 1], "away_team_id": [2, 3, 3]})  # fmt: skip
    a = for_games(con, games)
    g1 = a[a.game_id == "g1"]
    assert set(g1.person_id) == {10.0, 11.0, 20.0} and not g1.carried.any()
    g2 = a[a.game_id == "g2"]  # next day: only Out carries, never day-to-day statuses
    assert set(g2.person_id) == {10.0} and g2.carried.all()
    assert a[a.game_id == "g3"].empty  # a week later the report is too old to use


def test_ruled_out_minutes_go_to_teammates():
    rows = []
    for g in range(6):
        for pid, m in ((1, 34), (2, 30), (3, 28), (4, 26), (5, 24), (6, 20), (7, 18), (8, 12)):
            rows.append({"season": 2026, "game_id": f"g{g}", "team_id": 1, "person_id": pid,
                         "minutes": m, "game_date": pd.Timestamp("2026-11-01")
                         + pd.Timedelta(days=g)})  # fmt: skip
    p = pd.DataFrame(rows)
    cut = pd.Timestamp("2026-11-10")
    base = expected_minutes(p, cut, 2026)
    out = expected_minutes(p, cut, 2026, unavailable={1})
    assert 1 not in set(out.person_id)
    assert out.exp_minutes.sum() == pytest.approx(base.exp_minutes.sum())  # nothing lost
    b = base.set_index("person_id").exp_minutes
    o = out.set_index("person_id").exp_minutes
    assert np.allclose(o / b.loc[o.index], o.iloc[0] / b.loc[o.index[0]])  # proportional


def test_questionable_minutes_split_by_chance_of_playing():
    rows = []
    for g in range(6):
        for pid, m in ((1, 34), (2, 30), (3, 28), (4, 26), (5, 24), (6, 20), (7, 18), (8, 12)):
            rows.append({"season": 2026, "game_id": f"g{g}", "team_id": 1, "person_id": pid,
                         "minutes": m, "game_date": pd.Timestamp("2026-11-01")
                         + pd.Timedelta(days=g)})  # fmt: skip
    p = pd.DataFrame(rows)
    cut = pd.Timestamp("2026-11-10")
    base = expected_minutes(p, cut, 2026).set_index("person_id")
    q = expected_minutes(p, cut, 2026, maybe={1: 0.5}).set_index("person_id")
    assert q.loc[1, "p_play"] == 0.5 and (q.drop(1).p_play == 1).all()
    assert q.loc[1, "exp_minutes"] > base.loc[1, "exp_minutes"] * 0.99  # if he plays: as usual
    assert (q.drop(1).exp_minutes > base.drop(1).exp_minutes).all()  # teammates get some
    # the team's expected total is unchanged: his half-chance of sitting is shared out
    assert (q.exp_minutes * q.p_play).sum() == pytest.approx(base.exp_minutes.sum())
    # a sure thing either way is the same as no listing, or ruled out
    assert np.allclose(expected_minutes(p, cut, 2026, maybe={1: 1.0}).exp_minutes,
                       base.exp_minutes)  # fmt: skip
    gone = expected_minutes(p, cut, 2026, maybe={1: 0.0})
    out = expected_minutes(p, cut, 2026, unavailable={1})
    assert np.allclose(gone.exp_minutes, out.exp_minutes) and 1 not in set(gone.person_id)
