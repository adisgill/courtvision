"""Possession counting, rebound kinds and lineup reconstruction on hand-built games."""

from __future__ import annotations

from collections import Counter, defaultdict

from courtvision.metrics import pbp

A, B = 100, 200


def ev(seq, etype, team=None, person=None, *, t=0.0, period=1, fmt="live", **kw):
    fields = dict.fromkeys(pbp.Event._fields)
    fields.update(
        game_id="g1", seq=seq, period=period, elapsed_s=t, etype=etype, team_id=team,
        person_id=person, pbp_format=fmt,
    )  # fmt: skip
    fields.update(kw)
    return pbp.Event(**fields)


def roster(minutes: dict[int, float], names: dict[int, tuple[str, str]] | None = None):
    names = names or {}
    out = {}
    for pid, m in minutes.items():
        first, last = names.get(pid, (f"First{pid}", f"Last{pid}"))
        out[pid] = pbp.RosterEntry(A if pid < 20 else B, m, last, first)
    return out


def live_game():
    """One 12-minute period. A = players 1-6, B = players 21-26."""
    events = [
        ev(1, "period_start", t=0),
        ev(2, "jump", A, 1, t=2),
        ev(3, "fga", A, 1, t=10, made=False),                       # A poss 1
        ev(4, "reb", B, 21, t=12, reb_kind="def"),                   # B poss 2
        ev(5, "fga", B, 22, t=20, made=True, assist_person_id=24),
        ev(6, "tov", A, 2, t=30),                                    # A poss 3
        ev(7, "sub", B, 23, t=100, sub_dir="out"),
        ev(8, "sub", B, 26, t=100, sub_dir="in"),
        ev(9, "fga", B, 26, t=110, made=True),                       # B poss 4
        ev(10, "fga", A, 3, t=130, made=False),                      # A poss 5
        ev(11, "reb", A, 4, t=132, reb_kind="off"),
        ev(12, "fga", A, 4, t=140, made=True),
        ev(13, "foul", B, 25, t=140),
        ev(14, "ft", A, 4, t=140, made=True, ft_technical=False, ft_last=True),
        ev(15, "period_end", t=720),                                 # + B poss 6 (no events)
    ]  # fmt: skip
    minutes = {p: 12.0 for p in (1, 2, 3, 4, 5, 21, 22, 24, 25)}
    minutes.update({6: 0.0, 23: 100 / 60, 26: 620 / 60})
    return events, roster(minutes)


def test_possessions_are_runs_of_the_offensive_team():
    events, r = live_game()
    res = pbp.parse_game(events, r)
    teams = [p[2] for p in res.possessions]
    assert teams == [A, B, A, B, A, B]
    # the and-one free throw stays in the possession of the made basket
    by_seq = {e[1]: e for e in res.events}
    assert by_seq[12][3] == by_seq[14][3] == 5
    # the offensive rebound continues A's possession
    assert by_seq[10][3] == by_seq[11][3]


def test_period_end_possession_after_last_basket():
    events, r = live_game()
    res = pbp.parse_game(events, r)
    last = res.possessions[-1]
    assert last[2] == B
    assert not any(e[3] == last[1] for e in res.events)  # it has no events


def test_technical_free_throw_does_not_start_a_possession():
    events, r = live_game()
    events.insert(8, ev(8.5, "ft", A, 1, t=105, made=True, ft_technical=True))
    res = pbp.parse_game(events, r)
    assert [p[2] for p in res.possessions] == [A, B, A, B, A, B]


def test_lineups_and_starters():
    events, r = live_game()
    res = pbp.parse_game(events, r)
    assert res.qa.ok, res.qa
    # player 5 never appears in the period: filled by box minutes as the fifth starter
    assert res.qa.starters_by_minutes == 1
    stints = defaultdict(lambda: defaultdict(set))
    times = {}
    for _, sid, _, start, end, team, person in res.stints:
        stints[sid][team].add(person)
        times[sid] = (start, end)
    assert len(stints) == 2
    first, second = sorted(stints)
    assert stints[first][A] == {1, 2, 3, 4, 5}
    assert stints[first][B] == {21, 22, 23, 24, 25}
    assert stints[second][B] == {21, 22, 24, 25, 26}
    assert times[first] == (0.0, 100.0) and times[second] == (100.0, 720.0)
    # every event after the substitution belongs to the second stint
    assert {e[4] for e in res.events if e[1] >= 9} == {second}


def test_possession_is_credited_to_the_stint_where_it_ends():
    events, r = live_game()
    res = pbp.parse_game(events, r)
    stint_of = {p[1]: p[4] for p in res.possessions}
    stints = sorted({s[1] for s in res.stints})
    assert stint_of[1] == stints[0] and stint_of[4] == stints[1]


def legacy_game():
    """Legacy schema: unlabeled rebounds, swaps by name, two Smiths on team A."""
    names = {
        1: ("Josh", "Smith"), 2: ("Donta", "Smith"), 3: ("Al", "Horford"),
        4: ("Joe", "Johnson"), 5: ("Mike", "Bibby"), 6: ("Zaza", "Pachulia"),
        21: ("Tim", "Duncan"), 22: ("Tony", "Parker"), 23: ("Manu", "Ginobili"),
        24: ("Bruce", "Bowen"), 25: ("Fabricio", "Oberto"),
    }  # fmt: skip
    L = dict(fmt="legacy")
    events = [
        ev(1, "period_start", t=0, **L),
        ev(2, "fga", A, 1, t=10, made=False, player_name="Smith", **L),
        ev(3, "reb", B, 21, t=12, player_name="Duncan", **L),        # defensive
        ev(4, "fga", B, 22, t=20, made=False, player_name="Parker", **L),
        ev(5, "reb", B, 23, t=22, player_name="Ginobili", **L),      # offensive
        ev(6, "fga", B, 24, t=25, made=True, player_name="Bowen", **L),
        ev(7, "fga", A, 3, t=40, made=True, player_name="Horford", **L),
        ev(8, "fga", B, 25, t=50, made=True, player_name="Oberto", **L),
        ev(9, "fga", A, 4, t=60, made=True, player_name="Johnson", **L),
        ev(10, "fga", B, 21, t=70, made=True, player_name="Duncan", **L),
        ev(11, "tov", A, 5, t=80, player_name="Bibby", **L),
        ev(12, "sub", A, 1, t=300, sub_dir="swap", sub_in_name="Pachulia",
           sub_out_name="J. Smith", player_name="Smith", **L),
        ev(13, "sub", A, 2, t=400, sub_dir="swap", sub_in_name="J. Smith",
           sub_out_name="D. Smith", player_name="Smith", **L),
        ev(14, "fga", B, 22, t=719, made=False, player_name="Parker", **L),
        ev(15, "reb", A, None, t=720, **L),  # team rebound at the buzzer
        ev(16, "period_end", t=720, **L),
    ]  # fmt: skip
    minutes = {p: 12.0 for p in (3, 4, 5, 21, 22, 23, 24, 25)}
    minutes.update({1: 300 / 60 + 320 / 60, 2: 400 / 60, 6: 420 / 60})
    return events, roster(minutes, names)


def test_legacy_rebound_kind_from_latest_miss():
    events, r = legacy_game()
    res = pbp.parse_game(events, r)
    kinds = {e[1]: e[5] for e in res.events}
    assert kinds[3] == "def" and kinds[5] == "off"


def test_legacy_swaps_resolve_initials_and_lineups_pass():
    events, r = legacy_game()
    res = pbp.parse_game(events, r)
    assert res.qa.unresolved_subs == 0 and res.qa.ambiguous_subs == 0
    final = max(s[1] for s in res.stints)
    a_final = {s[6] for s in res.stints if s[1] == final and s[5] == A}
    assert a_final == {1, 3, 4, 5, 6}  # Josh Smith back in for Donta Smith
    assert res.qa.ok, res.qa


def test_legacy_buzzer_team_rebound_is_not_a_possession():
    events, r = legacy_game()
    res = pbp.parse_game(events, r)
    counts = Counter(p[2] for p in res.possessions)
    # A: 2,7,9,11 runs = 4; B: 3-6, 8, 10, 14 = 4; the buzzer team rebound adds nothing
    assert counts == {A: 4, B: 4}
