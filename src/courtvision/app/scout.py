"""Scouting report for an upcoming game: danger zones, plays to watch, players to watch.

Every number is this season's, with last season's games at a quarter weight (so the
first weeks aren't read off ten games), against league average from the same spots or
on the same play, shrunk by sample size. Matchup edges add the offense's number and the
defense's allowance: on play types that sum predicts later games better than the
offense alone (``plays.matchup_test``); the court map and the player columns are
descriptive.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from courtvision import plays as P
from courtvision.app import charts as C
from courtvision.app import court
from courtvision.app import data as D

PREV_W = 0.25  # weight of last season's games
ZONE_LABELS = court.ZONE_LABELS


def _w(df: pd.DataFrame, season: int) -> pd.DataFrame:
    return df.assign(w=np.where(df.season == season, 1.0, PREV_W))


# ---- danger zones ------------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def _league_hex(_con, season: int) -> pd.DataFrame:
    return court.league_hex(D.league_shots(_con, season, "regular"))


def _edge_hex(con, off_team: int, def_team: int, season: int) -> tuple:
    seasons = [season - 1, season]
    lg = {s: _league_hex(con, s) for s in seasons}
    off = court.unit_hex(_w(D.shots(con, seasons, team_id=off_team), season), lg)
    dfn = court.unit_hex(_w(D.shots(con, seasons, against=def_team), season), lg)
    if off.empty or dfn.empty:
        return off, pd.Series(dtype=float)
    zone_def = 100 * dfn.groupby("zone").zone_diff.first()
    d = dfn.set_index(["q", "rr"])["diff"]
    off["def"] = d.reindex(pd.MultiIndex.from_frame(off[["q", "rr"]])).to_numpy()
    off["def"] = off["def"].fillna(off.zone.map(zone_def)).fillna(0.0)
    off["off"] = off["diff"]
    off["edge"] = off.off + off["def"]
    zones = off.groupby("zone").apply(
        lambda g: pd.Series({"share": g.share.sum(),
                             "edge": np.average(g.edge, weights=g.n)}),
        include_groups=False)  # fmt: skip
    return off, zones


def danger_zones(con, home: int, away: int, season: int, names: dict, mode: str) -> None:
    cols = st.columns(2)
    for col, (att, dfn) in zip(cols, ((away, home), (home, away)), strict=True):
        h, zones = _edge_hex(con, att, dfn, season)
        with col:
            st.markdown(f"**Where {names[att]} can hurt {names[dfn]}**")
            if h.empty:
                st.info("Not enough shots yet.")
                continue
            hover = [f"{names[att]} take {r.share:.1%} of their shots here<br>"
                     f"they score {r.off:+.1f} · {names[dfn]} allow {r['def']:+.1f}<br>"
                     f"<b>edge {r.edge:+.1f}</b> points per 100 shots vs league"
                     for _, r in h.iterrows()]  # fmt: skip
            st.plotly_chart(court.hex_court(h, mode, value="edge", hover=hover),
                            width="stretch", key=f"danger_{att}")  # fmt: skip
            z = zones.assign(gain=zones.share * zones.edge).sort_values("gain")
            best, worst = z.iloc[-1], z.iloc[0]
            st.caption(
                f"Biggest edge: **{ZONE_LABELS[z.index[-1]]}** ({best.share:.0%} of their "
                f"shots, {best.edge:+.1f} per 100). Where {names[dfn]} hold up best: "
                f"**{ZONE_LABELS[z.index[0]]}** ({worst.edge:+.1f})."
            )
    st.plotly_chart(court.hex_key(mode, unit="edge: points per 100 shots vs league "
                                  "(offense's shooting + defense's allowance)"),
                    width="stretch", key="danger_key")  # fmt: skip
    st.caption(
        "Hexagon size: how often the attacking team shoots from there. Color: what "
        "it scores from there against league average plus what the defense allows "
        "there, both shrunk toward zone and league averages. Blue: the attack's edge."
    )


# ---- plays to watch ----------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def _play_tables(_con, season: int) -> tuple[pd.DataFrame, dict]:
    lg = D.q(_con, "select season, play, share as lg_share, pps as lg_pps from plays.league "
             "where season in (?, ?)", [season - 1, season])  # fmt: skip
    rel = D.q(_con, "select side, play, k from plays.reliability")
    k = {s: dict(zip(r.play, r.k, strict=True)) for s, r in rel.groupby("side")}
    return lg, k


def play_edges(con, att: int, dfn: int, season: int) -> pd.DataFrame:
    lg, k = _play_tables(con, season)
    shots = D.q(con, """select season, team_id, opp_id, play, pts, made::int made,
                               assisted::int assisted, person_id
                        from plays.shots where season in (?, ?) and (team_id = ? or opp_id = ?)""",
                [season - 1, season, att, dfn])  # fmt: skip
    shots = _w(shots, season)
    off = P.team_profile(shots[shots.team_id == att], lg, "offense", k["offense"])
    de = P.team_profile(shots[shots.opp_id == dfn], lg, "defense", k["defense"])
    t = off.set_index("play")[["share", "lg_share", "pp100_rel_shrunk", "shots"]].join(
        de.set_index("play")[["pp100_rel_shrunk"]].rename(
            columns={"pp100_rel_shrunk": "allow"}), how="left")  # fmt: skip
    t = t.rename(columns={"pp100_rel_shrunk": "own"}).fillna({"allow": 0.0})
    t["edge"] = t.own + t.allow
    t["impact"] = t.share * t.edge  # points per 100 of the attack's shots
    return t.sort_values("impact", ascending=False)


def plays_to_watch(con, home: int, away: int, season: int, names: dict, mode: str) -> None:
    if not D.has_schema(con, "plays"):
        st.info("Run `courtvision plays` for play types.")
        return
    cols = st.columns(2)
    for col, (att, dfn) in zip(cols, ((away, home), (home, away)), strict=True):
        t = play_edges(con, att, dfn, season)
        top = t[t.share >= 0.02]
        z = pd.DataFrame({
            "How often": 0.0,
            f"{names[att]} score": np.clip(top.own / 8, -1, 1),
            f"{names[dfn]} allow": np.clip(top.allow / 8, -1, 1),
            "Edge": np.clip(top.edge / 8, -1, 1),
        }, index=top.index)  # fmt: skip
        text = pd.DataFrame({
            "How often": [f"{s:.0%} ({s - lg:+.0%})" for s, lg in zip(top.share, top.lg_share,
                                                                      strict=True)],
            f"{names[att]} score": top.own.map(lambda v: f"{v:+.1f}"),
            f"{names[dfn]} allow": top.allow.map(lambda v: f"{v:+.1f}"),
            "Edge": top.edge.map(lambda v: f"{v:+.1f}"),
        }, index=top.index)  # fmt: skip
        with col:
            st.markdown(f"**{names[att]} attacking**: plays ranked by what they're worth here")
            st.plotly_chart(C.heat_grid(z, text, mode), width="stretch", key=f"ptw_{att}")
            pos = top[top.impact > 0].head(2)
            if not pos.empty:
                st.caption("Watch: " + "; ".join(
                    f"**{p}** ({r.share:.0%} of their shots, edge {r.edge:+.1f})"
                    for p, r in pos.iterrows()) + ".")  # fmt: skip
    st.caption(
        "Play types from the play-by-play (no tracking data): how often a team uses each "
        "(and vs league), points per 100 shots on it vs league for the attack and what this "
        "defense allows on it, each shrunk by its measured reliability. Edge = the two "
        "added; this sum predicted later games better than the offense's own numbers alone. "
        "Rows are sorted by edge x frequency."
    )


# ---- players to watch --------------------------------------------------------------------


def players_to_watch(con, gid: str, home: int, away: int, season: int, names: dict,
                     players: pd.DataFrame, mode: str, goto) -> None:  # fmt: skip
    if not D.has_schema(con, "plays"):
        return
    pl = D.q(con, "select * from forecast.player_lines where game_id = ? and exp_minutes >= 15",
             [gid])  # fmt: skip
    if pl.empty:
        st.info("No player lines for this game.")
        return
    pl = pl.merge(players, on="person_id", how="left")
    ids = pl.person_id.tolist()
    marks = ",".join("?" * len(ids))
    # form: last 5 games' points per 36 vs his earlier games (this season, else last)
    g = D.q(con, f"""select c.person_id, g.game_date, c.minutes, c.b_pts as pts
                     from metrics.player_game_counts c join stg.games g using (game_id)
                     where c.season in (?, ?) and c.season_type = 'regular' and c.minutes > 0
                       and c.person_id in ({marks})""", [season - 1, season, *ids])  # fmt: skip
    g = g.sort_values("game_date")
    g["rk"] = g.groupby("person_id").cumcount(ascending=False)
    rec = g[g.rk < 5].groupby("person_id")[["pts", "minutes"]].sum()
    old = g[(g.rk >= 5) & (g.rk < 45)].groupby("person_id")[["pts", "minutes"]].sum()
    usual = old.pts / old.minutes
    # shrink the last five toward his usual rate with 60 minutes of prior: a 12-minute
    # player's hot week shouldn't read as a 60% swing
    recent = (rec.pts + usual.reindex(rec.index) * 60) / (rec.minutes + 60)
    form = recent / usual - 1
    # matchup: his play mix against the opponent defense's allowance on each play
    lg, k = _play_tables(con, season)
    shots = _w(D.q(con, f"""select season, person_id, team_id, opp_id, play, pts, made::int made,
                                   assisted::int assisted
                            from plays.shots where season in (?, ?)
                              and (person_id in ({marks}) or opp_id in (?, ?))""",
                   [season - 1, season, *ids, home, away]), season)  # fmt: skip
    allow = {t: P.team_profile(shots[shots.opp_id == t], lg, "defense", k["defense"])
             .set_index("play").pp100_rel_shrunk for t in (home, away)}  # fmt: skip
    mine = shots[shots.person_id.isin(ids)]
    mix = mine.groupby(["person_id", "play"]).w.sum()
    mix = (mix / mix.groupby(level=0).transform("sum")).rename("share").reset_index()
    own = mine.merge(lg[["season", "play", "lg_pps"]], on=["season", "play"])
    own["ex"] = own.w * (own.pts - own.lg_pps)
    eff = own.groupby(["person_id", "play"]).agg(ex=("ex", "sum"), n=("w", "sum"))
    eff["rel"] = 100 * eff.ex / (eff.n + eff.index.get_level_values("play").map(
        k["offense"]).to_numpy())  # fmt: skip
    mix = mix.merge(eff.rel.reset_index(), on=["person_id", "play"], how="left")
    cols = st.columns(2)
    for col, (team, opp) in zip(cols, ((away, home), (home, away)), strict=True):
        sub = pl[pl.team_id == team].sort_values("pts", ascending=False).head(8)
        rows = []
        for r in sub.itertuples():
            m = mix[mix.person_id == r.person_id]
            edge = float((m.share * m.play.map(allow[opp]).fillna(0)).sum()) if len(m) else 0.0
            go_to = m.sort_values("share").iloc[-1] if len(m) else None
            rows.append({"player": r.player, "pts": r.pts, "lo": r.pts_lo, "hi": r.pts_hi,
                         "form": form.get(r.person_id, np.nan), "edge": edge,
                         "go_to": None if go_to is None else go_to.play,
                         "go_to_share": np.nan if go_to is None else go_to.share,
                         "go_to_rel": np.nan if go_to is None else go_to.rel})  # fmt: skip
        d = pd.DataFrame(rows).set_index("player")
        z = pd.DataFrame({
            "Points": np.clip((d.pts - 12) / 14, -1, 1),
            "Form (last 5)": np.clip(d.form / 0.35, -1, 1),
            "Matchup": np.clip(d.edge / 6, -1, 1),
            "Go-to play": np.clip(d.go_to_rel / 15, -1, 1),
        }, index=d.index)  # fmt: skip
        text = pd.DataFrame({
            "Points": [f"{p:.0f} ({lo:.0f}-{hi:.0f})" for p, lo, hi in zip(d.pts, d.lo, d.hi,
                                                                          strict=True)],
            "Form (last 5)": d.form.map(lambda v: "–" if pd.isna(v) else f"{v:+.0%}"),
            "Matchup": d.edge.map(lambda v: f"{v:+.1f}"),
            "Go-to play": [f"{p} {s:.0%}" if p else "–"
                           for p, s in zip(d.go_to, d.go_to_share, strict=True)],
        }, index=d.index)  # fmt: skip
        hover = text.copy()
        trio = zip(d.go_to, d.go_to_share, d.go_to_rel, strict=True)
        hover["Go-to play"] = [f"{p}: {s:.0%} of his shots, {v:+.1f} per 100 vs league"
                               if p else "–" for p, s, v in trio]  # fmt: skip
        with col:
            st.markdown(f"**{names[team]}**")
            st.plotly_chart(C.heat_grid(z, text, mode, hover), width="stretch",
                            key=f"pw_{team}")  # fmt: skip
    st.caption(
        "Points: expected, with the 80% range. Form: points per minute in his last 5 games "
        "against his 40 before (pulled toward his usual rate by 60 minutes, so a few hot "
        "minutes don't read as a hot streak). Matchup: his shot mix against what this "
        "defense allows on each play type (points per 100 shots vs league). Go-to play: "
        "his most-used play "
        "and how well he scores on it. Form and matchup describe the game; the tested "
        "player model found that matchup adjustments add little to his expected line."
    )


def scouting_report(con, gid: str, home: int, away: int, season: int, names: dict,
                    players: pd.DataFrame, mode: str, goto) -> None:  # fmt: skip
    st.subheader("Scouting report")
    t1, t2, t3, t4 = st.tabs(["Danger zones", "Plays to watch", "Players to watch", "Lineups"])
    with t1:
        danger_zones(con, home, away, season, names, mode)
    with t2:
        plays_to_watch(con, home, away, season, names, mode)
    with t3:
        players_to_watch(con, gid, home, away, season, names, players, mode, goto)
    with t4:
        lineups_to_watch(con, gid, home, away, season, names, players, mode)


# ---- lineups -----------------------------------------------------------------------------


def _team_fives(con, gid: str, team: int, season: int, pnames: pd.Series) -> pd.DataFrame:
    """Rows: likely starters, the most-used fives available tonight, the best possible
    five; with projected net per 100 against an average five and possessions together."""
    from courtvision import lineups as L
    from courtvision.app.lineups_page import five_label, roster

    avail = set(D.q(con, "select person_id from forecast.player_lines where game_id = ? "
                    "and team_id = ? and exp_minutes >= 8", [gid, team]).person_id)  # fmt: skip
    u = D.q(con, "select lineup, players, poss, starts, proj_net100 from lineups.units "
            "where season = ? and team_id = ? order by poss desc", [season, team])  # fmt: skip
    ok = u[u.players.map(lambda p: set(p) <= avail)] if avail else u
    ro = roster(con, team, season)
    imp = ro.set_index("person_id").impact
    rows = []
    starters = ok[ok.starts > 0].sort_values("starts", ascending=False)
    if not starters.empty:
        r = starters.iloc[0]
        rows.append(("Likely starters", list(r.players), r.proj_net100, r.poss))
    for r in ok.head(3).itertuples():
        if rows and list(r.players) == rows[0][1]:
            continue
        rows.append(("Most used", list(r.players), r.proj_net100, r.poss))
    cand = ro[ro.person_id.isin(avail)] if avail else ro[(ro.minutes >= 100) & ro.current]
    best = L.best_fives(cand, n=1)
    if best:
        ids, val = best[0]
        key = "-".join(str(i) for i in sorted(ids))
        poss = u.set_index("lineup").poss.get(key, 0.0)
        rows.append(("Best possible", ids, val, poss))
    out = pd.DataFrame(rows, columns=["kind", "players", "proj", "poss"])
    out["label"] = [f"{k}: {five_label(sorted(p, key=lambda i: -imp.get(i, 0.0)), pnames)}"
                    for k, p in zip(out.kind, out.players, strict=True)]  # fmt: skip
    out["impact_sum"] = out.players.map(lambda p: float(sum(imp.get(i, 0.0) for i in p)))
    return out


def lineups_to_watch(con, gid: str, home: int, away: int, season: int, names: dict,
                     players: pd.DataFrame, mode: str) -> None:  # fmt: skip
    if not D.has_schema(con, "lineups"):
        st.info("Run `courtvision lineups` for lineups.")
        return
    pnames = players.set_index("person_id").player
    fives = {}
    cols = st.columns(2)
    for col, team in zip(cols, (away, home), strict=True):
        f = _team_fives(con, gid, team, season, pnames)
        fives[team] = f
        with col:
            st.markdown(f"**{names[team]}**")
            if f.empty:
                st.info("No lineups yet.")
                continue
            idx = f.label
            z = pd.DataFrame({"Projected net": np.clip(f.proj.to_numpy() / 10, -1, 1),
                              "Together": 0.0}, index=idx)  # fmt: skip
            text = pd.DataFrame({
                "Projected net": [f"{v:+.1f}" for v in f.proj],
                "Together": ["untried" if p < 50 else f"{p:.0f} poss" for p in f.poss],
            }, index=idx)  # fmt: skip
            st.plotly_chart(C.heat_grid(z, text, mode, row_px=34), width="stretch",
                            key=f"lw_{team}")  # fmt: skip
    a, h = fives.get(away), fives.get(home)
    if a is not None and h is not None and not a.empty and not h.empty:
        sa, shh = a.iloc[0], h.iloc[0]
        edge = shh.impact_sum - sa.impact_sum
        fav = names[home] if edge > 0 else names[away]
        st.caption(
            f"Opening fives head to head: by their players' impacts, **{fav}** by "
            f"{abs(edge):.1f} points per 100 possessions while they share the floor. "
            "Lineups shown only if all five are expected tonight (no injury feed: expected = "
            "played in one of the team's last three games). Projected net: the players' "
            "impacts plus the lineup's own record, shrunk (see Lineups)."
        )
