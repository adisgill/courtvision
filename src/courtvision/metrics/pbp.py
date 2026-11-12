"""Possessions, rebound kinds and on-court lineups from the normalized event stream.

Input is ``stg.events`` (both play-by-play schemas in one vocabulary). For every game
this module produces:

* **Possessions** (docs/metrics_spec.md section 1, "tier A"). Each event that only the
  team with the ball can produce (a field-goal attempt, a non-technical free throw, a
  turnover, a rebound) names the offensive team. A possession is a maximal run of events
  with the same offensive team inside one period, so a possession ends exactly when the
  other team next shows up with the ball: after a make, the last free throw, a defensive
  rebound or a turnover. Technical free throws, fouls, timeouts, jump balls and
  substitutions carry no possession information and inherit the current one.
* **Rebound kind** for the legacy schema, which does not label rebounds: offensive when
  the rebounding team also took the latest missed shot or free throw, else defensive.
* **Stints**: the ten players on the floor between lineup changes. Live-format games
  have one substitution row per player in or out. Legacy games have one row per swap with
  the player out as ``person_id`` and the player in only by name ("SUB: <in> FOR <out>"),
  resolved against the names the same game's play-by-play uses for each player.
  Period starters are inferred: a player whose first involvement in the period is
  anything other than being substituted in was on the floor at the start. Players who
  play a whole period without any event are filled from the previous period's closing
  five, then (rarely) by box-score minutes; both are counted in the QA table.

Every game also gets a QA row; a game passes when both teams have exactly five players
on the floor at every event, every substitution resolves, and every player's
reconstructed minutes are within one minute of the box score.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import groupby
from typing import NamedTuple

EVENT_SQL_COLUMNS = (
    "game_id, seq, period, elapsed_s, etype, team_id, person_id, player_name, made, "
    "ft_technical, ft_last, reb_kind, bench_possible, sub_dir, sub_in_person_id, "
    "sub_in_name, sub_out_name, assist_person_id, steal_person_id, block_person_id, "
    "foul_drawn_person_id, pbp_format"
)

# Event types that only the team in possession can produce.
_OFFENSE_ETYPES = frozenset({"fga", "ft", "tov", "reb"})


class Event(NamedTuple):
    game_id: str
    seq: int
    period: int
    elapsed_s: float
    etype: str
    team_id: int | None
    person_id: int | None
    player_name: str | None
    made: bool | None
    ft_technical: bool | None
    ft_last: bool | None
    reb_kind: str | None
    bench_possible: bool | None
    sub_dir: str | None
    sub_in_person_id: int | None
    sub_in_name: str | None
    sub_out_name: str | None
    assist_person_id: int | None
    steal_person_id: int | None
    block_person_id: int | None
    foul_drawn_person_id: int | None
    pbp_format: str = "live"


class RosterEntry(NamedTuple):
    team_id: int
    minutes: float | None
    last_name: str | None
    first_name: str | None = None


@dataclass
class GameQA:
    game_id: str
    n_events: int = 0
    bad_five_events: int = 0
    unresolved_subs: int = 0
    ambiguous_subs: int = 0
    starters_from_prev_period: int = 0
    starters_by_minutes: int = 0
    too_many_starters: int = 0
    players_over_1min: int = 0
    max_abs_min_diff: float = 0.0
    n_possessions: int = 0

    @property
    def ok(self) -> bool:
        return (
            self.bad_five_events == 0
            and self.unresolved_subs == 0
            and self.players_over_1min == 0
            and self.too_many_starters == 0
        )


@dataclass
class GameResult:
    # (game_id, seq, off_team_id, poss_id, stint_id, reb_kind)
    events: list[tuple] = field(default_factory=list)
    # (game_id, poss_id, team_id, period, end_stint_id)
    possessions: list[tuple] = field(default_factory=list)
    # (game_id, stint_id, period, start_s, end_s, team_id, person_id)
    stints: list[tuple] = field(default_factory=list)
    qa: GameQA | None = None


def period_bounds(period: int) -> tuple[float, float]:
    """Game seconds at the start and end of a period."""
    if period <= 4:
        start = (period - 1) * 720.0
        return start, start + 720.0
    start = 2880.0 + (period - 5) * 300.0
    return start, start + 300.0


def _norm_name(name: str | None) -> str:
    return " ".join((name or "").lower().replace(".", " ").replace("'", "").split())


def offense_team(ev: Event) -> int | None:
    """The team in possession implied by this event, or None if the event doesn't say."""
    if ev.etype not in _OFFENSE_ETYPES or ev.team_id is None:
        return None
    if ev.etype == "ft" and ev.ft_technical:
        return None
    return ev.team_id


class _NameResolver:
    """Map the names a game's play-by-play uses to person ids, per team.

    Tried in order, stopping at the first rule that leaves exactly one player who is not
    already on the floor:

    0. the name this game's own substitution rows print for the player when he goes
       out ("SUB: X FOR <name>"): the same notation, so it also covers players renamed
       since (Enes Kanter, now Freedom in the players table);
    1. the name the same game's play-by-play prints for the player (e.g. "Van Exel");
    2. roster last name; 3. roster first name (one-name players, e.g. "Nene");
    4. initials + last name ("J. Smith" when a team has two Smiths, "J.R. Smith");
    5. any roster player whose full name contains every word of the name.

    If every rule matches several players, the one with more box-score minutes wins and
    the substitution is counted as ambiguous.
    """

    def __init__(self, events: list[Event], roster: dict[int, RosterEntry]):
        self.roster = roster
        self.tables: list[dict[int, dict[str, set[int]]]] = [
            defaultdict(lambda: defaultdict(set)) for _ in range(4)
        ]
        by_sub, by_pbp, by_last, by_first = self.tables
        for ev in events:
            if ev.person_id in roster:
                team = roster[ev.person_id].team_id
                if ev.player_name:
                    by_pbp[team][_norm_name(ev.player_name)].add(ev.person_id)
                if ev.sub_out_name:
                    by_sub[team][_norm_name(ev.sub_out_name)].add(ev.person_id)
        self.full: dict[int, list[tuple[int, str, str]]] = defaultdict(list)
        for pid, entry in roster.items():
            first, last = _norm_name(entry.first_name), _norm_name(entry.last_name)
            if last:
                by_last[entry.team_id][last].add(pid)
            if first:
                by_first[entry.team_id][first].add(pid)
            self.full[entry.team_id].append((pid, first, last))

    def _initials_match(self, words: list[str], team: int) -> set[int]:
        if len(words) < 2:
            return set()
        # Abbreviated first name + last name: "J. Smith", "Ja. Smith", "Shaw. Williams".
        head, rest = words[0], " ".join(words[1:])
        found = {
            p
            for p, first, last in self.full[team]
            if (last == rest or last.startswith(rest + " ") or last.endswith(" " + rest))
            and first.startswith(head)
        }
        if found:
            return found
        # Several initials: "J.R. Smith".
        initials = [w for w in words if len(w) == 1]
        rest = " ".join(w for w in words if len(w) > 1)
        if not initials or not rest:
            return set()
        return {
            p
            for p, first, last in self.full[team]
            if (last == rest or last.endswith(" " + rest)) and first.startswith(initials[0])
        }

    def _token_match(self, words: list[str], team: int) -> set[int]:
        want = set(words)
        return {p for p, first, last in self.full[team] if want <= set(f"{first} {last}".split())}

    def resolve(
        self, name: str | None, team: int | None, exclude: set[int]
    ) -> tuple[int | None, bool]:
        """Return (person_id, ambiguous). person_id is None when nothing matches."""
        if team is None or not name:
            return None, False
        key = _norm_name(name)
        words = key.split()
        rules = (
            lambda: self.tables[0][team].get(key, set()),
            lambda: self.tables[1][team].get(key, set()),
            lambda: self.tables[2][team].get(key, set()),
            lambda: self.tables[3][team].get(key, set()),
            lambda: self._initials_match(words, team),
            lambda: self._token_match(words, team),
        )
        fallback: set[int] = set()
        for rule in rules:
            cands = rule() - exclude
            if len(cands) == 1:
                return next(iter(cands)), False
            if len(cands) > 1 and not fallback:
                fallback = cands
        if fallback:
            return max(fallback, key=lambda p: self.roster[p].minutes or 0.0), True
        return None, False


def _involved(ev: Event) -> tuple[int | None, ...]:
    return (
        ev.person_id,
        ev.assist_person_id,
        ev.steal_person_id,
        ev.block_person_id,
        ev.foul_drawn_person_id,
    )


class _StintTracker:
    """The open stint of one game; closes it whenever the ten players on the floor change.

    Substitutions at the same game time are applied together: the lineup change is
    committed (``flush``) at the next non-substitution event, or when a substitution at a
    later time arrives.
    """

    def __init__(self, game_id: str, teams: list[int], rows: list, on_seconds: dict):
        self.game_id, self.teams, self.rows, self.on_seconds = game_id, teams, rows, on_seconds
        self.id = 0
        self.period = 0
        self.start = 0.0
        self.on: dict[int, set[int]] = {}
        self.lineup: dict[int, frozenset[int]] = {}
        self.pending: float | None = None  # time of the latest uncommitted substitution

    def _snapshot(self) -> dict[int, frozenset[int]]:
        return {t: frozenset(self.on[t]) for t in self.teams}

    def open_period(self, period: int, start: float, on: dict[int, set[int]]) -> None:
        self.id += 1
        self.period, self.start, self.on = period, start, on
        self.lineup = self._snapshot()
        self.pending = None

    def close(self, at: float) -> None:
        dur = max(0.0, at - self.start)
        for t in self.teams:
            for p in self.lineup[t]:
                self.rows.append((self.game_id, self.id, self.period, self.start, at, t, p))
                self.on_seconds[p] += dur
        self.id += 1
        self.start = at
        self.lineup = self._snapshot()

    def before_substitution(self, at: float) -> None:
        if self.pending is not None and at != self.pending:
            self.flush()

    def flush(self) -> None:
        if self.pending is not None:
            if self._snapshot() != self.lineup:
                self.close(self.pending)
            self.pending = None

    def end_period(self, at: float) -> None:
        self.flush()
        self.close(at)
        self.id -= 1  # close() opened a stint; the next period opens its own


def parse_game(events: list[Event], roster: dict[int, RosterEntry]) -> GameResult:
    """Possessions, rebound kinds and stints for one game (events sorted by ``seq``)."""
    if not events:
        return GameResult()
    game_id = events[0].game_id
    legacy = events[0].pbp_format == "legacy"
    qa = GameQA(game_id=game_id, n_events=len(events))
    out = GameResult(qa=qa)
    teams = sorted({e.team_id for e in roster.values()})
    names = _NameResolver(events, roster)
    team_players: dict[int, list[int]] = defaultdict(list)
    for pid, entry in roster.items():
        team_players[entry.team_id].append(pid)

    on_seconds: dict[int, float] = defaultdict(float)
    st = _StintTracker(game_id, teams, out.stints, on_seconds)
    prev_close: dict[int, set[int]] = {t: set() for t in teams}
    poss_id = 0

    for period, group in groupby(events, key=lambda e: e.period):
        evs = list(group)
        p_start, p_end = period_bounds(period)

        # ---- pass 1: who was on the floor when the period started --------------------
        first_role: dict[int, str] = {}
        for ev in evs:
            if ev.etype == "sub":
                if ev.sub_dir in ("in", "out"):
                    if ev.person_id in roster:
                        first_role.setdefault(ev.person_id, ev.sub_dir)
                else:  # legacy swap
                    if ev.person_id in roster:
                        first_role.setdefault(ev.person_id, "out")
                    if ev.sub_in_person_id in roster:
                        pid_in, ambiguous = ev.sub_in_person_id, False
                    else:
                        pid_in, ambiguous = names.resolve(ev.sub_in_name, ev.team_id, set())
                    if pid_in is not None and not ambiguous:
                        first_role.setdefault(pid_in, "in")
            elif not ev.bench_possible:
                for pid in _involved(ev):
                    if pid in roster:
                        first_role.setdefault(pid, "event")

        on: dict[int, set[int]] = {}
        for team in teams:
            starters = {
                p for p, role in first_role.items() if role != "in" and roster[p].team_id == team
            }
            if len(starters) < 5:
                for p in sorted(prev_close[team]):
                    if len(starters) == 5:
                        break
                    if p not in first_role:
                        starters.add(p)
                        qa.starters_from_prev_period += 1
            if len(starters) < 5:
                spare = [p for p in team_players[team] if p not in first_role and p not in starters]
                spare.sort(key=lambda p: -(roster[p].minutes or 0.0))
                for p in spare[: 5 - len(starters)]:
                    starters.add(p)
                    qa.starters_by_minutes += 1
            if len(starters) > 5:
                qa.too_many_starters += 1
            on[team] = starters

        # ---- pass 2: walk the period ----------------------------------------------------
        st.open_period(period, p_start, on)

        cur_off: int | None = None
        cur_poss: list | None = None  # [poss_id, team, period, end stint, first event index]
        cur_poss_real = False  # does the current run have anything besides team rebounds?
        last_inf: Event | None = None  # latest event that named the offense this period
        last_miss_team: int | None = None

        for ev in evs:
            if ev.etype == "sub":
                st.before_substitution(ev.elapsed_s)
                team = ev.team_id
                if team not in on:
                    qa.unresolved_subs += 1
                    continue
                if ev.sub_dir == "in":
                    if ev.person_id is None:
                        qa.unresolved_subs += 1
                    else:
                        on[team].add(ev.person_id)
                elif ev.sub_dir == "out":
                    on[team].discard(ev.person_id)
                else:
                    on[team].discard(ev.person_id)
                    if ev.sub_in_person_id in roster:
                        pid_in, ambiguous = ev.sub_in_person_id, False
                    else:
                        pid_in, ambiguous = names.resolve(ev.sub_in_name, team, on[team])
                    if pid_in is None:
                        qa.unresolved_subs += 1
                    else:
                        qa.ambiguous_subs += ambiguous
                        on[team].add(pid_in)
                st.pending = ev.elapsed_s
                out.events.append((game_id, ev.seq, cur_off, cur_poss[0] if cur_poss else None,
                                   st.id, None))  # fmt: skip
                continue

            st.flush()
            if any(len(on[t]) != 5 for t in teams):
                qa.bad_five_events += 1

            reb_kind = ev.reb_kind
            if ev.etype in ("fga", "ft") and ev.made is False and not ev.ft_technical:
                last_miss_team = ev.team_id
            elif ev.etype == "reb" and reb_kind is None and last_miss_team is not None:
                reb_kind = "off" if ev.team_id == last_miss_team else "def"

            off = offense_team(ev)
            if off is not None:
                if off != cur_off:
                    poss_id += 1
                    cur_off = off
                    cur_poss = [poss_id, off, period, st.id, len(out.events)]
                    cur_poss_real = False
                    out.possessions.append(cur_poss)
                else:
                    cur_poss[3] = st.id
                cur_poss_real |= not (ev.etype == "reb" and ev.person_id is None)
                last_inf = ev
            out.events.append((game_id, ev.seq, cur_off, cur_poss[0] if cur_poss else None,
                               st.id, reb_kind))  # fmt: skip

        st.flush()
        # Legacy schema: a period-ending run made only of team rebounds (a missed heave
        # rebounded at the buzzer) is not a possession in the official count. The live
        # schema's official count does include it (both measured in acceptance test T2).
        if legacy and cur_poss is not None and not cur_poss_real:
            out.possessions.pop()
            start = cur_poss[4]
            out.events[start:] = [(*e[:3], None, *e[4:]) for e in out.events[start:]]
            last_inf = None
        # After the period's last basket, final free throw or turnover the other team has
        # the ball even if nothing else is logged; the official count includes that
        # possession (acceptance test T2), so it is added with no events.
        if last_inf is not None and (
            (last_inf.etype == "fga" and last_inf.made)
            or (last_inf.etype == "ft" and last_inf.made and last_inf.ft_last)
            or last_inf.etype == "tov"
        ):
            other = [t for t in teams if t != last_inf.team_id]
            if len(other) == 1:
                poss_id += 1
                out.possessions.append([poss_id, other[0], period, st.id, len(out.events)])
        st.end_period(p_end)
        prev_close = {t: set(on[t]) for t in teams}

    out.possessions = [(game_id, *p[:4]) for p in out.possessions]
    qa.n_possessions = len(out.possessions)
    for pid, entry in roster.items():
        if entry.minutes is None:
            continue
        diff = abs(on_seconds.get(pid, 0.0) / 60.0 - entry.minutes)
        qa.max_abs_min_diff = max(qa.max_abs_min_diff, diff)
        if diff > 1.0:
            qa.players_over_1min += 1
    return out


# ---- season driver ----------------------------------------------------------------------

RATED_SEASON_TYPES = ("regular", "play_in", "playoffs", "cup_final")

ROSTER_SQL_COLUMNS = "game_id, person_id, team_id, minutes, last_name, first_name"

QA_COLUMNS = (
    "game_id", "n_events", "n_possessions", "bad_five_events", "unresolved_subs",
    "ambiguous_subs", "starters_from_prev_period", "starters_by_minutes", "too_many_starters",
    "players_over_1min", "max_abs_min_diff", "ok",
)  # fmt: skip


def parse_rows(event_rows: list[tuple], roster_rows: list[tuple]) -> dict[str, list]:
    """Run :func:`parse_game` over many games.

    ``event_rows`` hold :data:`EVENT_SQL_COLUMNS` sorted by (game_id, seq);
    ``roster_rows`` hold :data:`ROSTER_SQL_COLUMNS`.
    """
    rosters: dict[str, dict[int, RosterEntry]] = defaultdict(dict)
    for game_id, pid, team, minutes, last, first in roster_rows:
        rosters[game_id][pid] = RosterEntry(team, minutes, last, first)

    result: dict[str, list] = {"events": [], "possessions": [], "stints": [], "qa": []}
    for game_id, grp in groupby(event_rows, key=lambda r: r[0]):
        res = parse_game([Event(*r) for r in grp], rosters.get(game_id, {}))
        result["events"].extend(res.events)
        result["possessions"].extend(res.possessions)
        result["stints"].extend(res.stints)
        if res.qa is not None:
            q = res.qa
            result["qa"].append(tuple(getattr(q, c) for c in QA_COLUMNS))
    return result


OUTPUT_SCHEMAS: dict[str, list[tuple[str, str]]] = {
    "events": [("game_id", "string"), ("seq", "int64"), ("off_team_id", "int64"),
               ("poss_id", "int64"), ("stint_id", "int64"), ("reb_kind", "string")],
    "possessions": [("game_id", "string"), ("poss_id", "int64"), ("team_id", "int64"),
                    ("period", "int64"), ("stint_id", "int64")],
    "stints": [("game_id", "string"), ("stint_id", "int64"), ("period", "int64"),
               ("start_s", "float64"), ("end_s", "float64"), ("team_id", "int64"),
               ("person_id", "int64")],
    "qa": [("game_id", "string"), ("n_events", "int64"), ("n_possessions", "int64"),
           ("bad_five_events", "int64"), ("unresolved_subs", "int64"),
           ("ambiguous_subs", "int64"), ("starters_from_prev_period", "int64"),
           ("starters_by_minutes", "int64"), ("too_many_starters", "int64"),
           ("players_over_1min", "int64"), ("max_abs_min_diff", "float64"), ("ok", "bool")],
}  # fmt: skip


def write_result(result: dict[str, list], out_dir, season: int) -> dict[str, int]:
    """Write parse output as ``<out_dir>/<table>/season=<season>/data.parquet``."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    counts = {}
    for name, schema in OUTPUT_SCHEMAS.items():
        cols = list(zip(*result[name], strict=True)) if result[name] else [[] for _ in schema]
        table = pa.table(
            {
                col: pa.array(vals, type=pa.type_for_alias(t))
                for (col, t), vals in zip(schema, cols, strict=True)
            }
        )
        dest = out_dir / name / f"season={season}" / "data.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, dest, compression="zstd")
        counts[name] = table.num_rows
    return counts


def parse_season_files(inputs_dir, out_dir, season: int) -> tuple[int, dict[str, int], int, int]:
    """Worker: read one season's materialized inputs, parse, write outputs.

    Returns (season, row counts, games, games passing QA).
    """
    import duckdb

    con = duckdb.connect()
    ev_glob = (inputs_dir / "events" / f"season={season}" / "*.parquet").as_posix()
    ro_glob = (inputs_dir / "roster" / f"season={season}" / "*.parquet").as_posix()
    rows = con.execute(
        f"select {EVENT_SQL_COLUMNS} from read_parquet('{ev_glob}') order by game_id, seq"
    ).fetchall()
    roster = con.execute(f"select {ROSTER_SQL_COLUMNS} from read_parquet('{ro_glob}')").fetchall()
    con.close()
    result = parse_rows(rows, roster)
    counts = write_result(result, out_dir, season)
    ok = sum(1 for q in result["qa"] if q[-1])
    return season, counts, len(result["qa"]), ok
