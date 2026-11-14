"""courtvision visualizer: ``uv run courtvision app``.

League, upcoming, scorecard, team, game, player, lineups, plays, coaches, market and
live views. Every chart is interactive: hover for numbers, and click a team or a player
to open its page. Read-only over ``data/courtvision.duckdb``; run ``courtvision build``
first.
"""

from __future__ import annotations

import threading

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from courtvision.app import charts as C
from courtvision.app import court
from courtvision.app import data as D

st.set_page_config(page_title="courtvision", layout="wide")

# ---- navigation: a click sets the target view's widgets on the next run ------------------
if "_goto" in st.session_state:
    target = st.session_state.pop("_goto")
    for k in target.pop("_clear", []):
        st.session_state.pop(k, None)
    for k, v in target.items():
        st.session_state[k] = v


def goto(view: str, clear: str | None = None, **keys) -> None:
    """Open ``view`` with its widgets preset (e.g. t_team=..., p_name=...)."""
    st.session_state["_goto"] = {"view": view, "_clear": [clear] if clear else [], **keys}
    st.rerun()


@st.cache_resource(ttl=3 * 3600)
def _db():
    return D.connect()


_THREAD = threading.local()
_CURSORS = threading.Lock()


def _con():
    """This thread's cursor on the shared connection. Every session runs its script in its
    own thread, and DuckDB refuses a query on a connection busy with another one."""
    db = _db()
    if getattr(_THREAD, "db", None) is not db:
        with _CURSORS:  # making a cursor is itself a call on the shared connection
            _THREAD.db, _THREAD.cur = db, db.cursor()
    return _THREAD.cur


@st.cache_data(ttl=600, show_spinner=False)
def _published() -> str | None:
    version = D.published_version()
    if version is not None or D.loaded_version() is not None:  # the hosted demo: say so
        print(f"demo copy check: published {version}, have {D.loaded_version()}", flush=True)
    return version


def _pick_up_new_data() -> None:
    """At most every 10 minutes, check whether the published demo copy (the daily run
    publishes after each forecast) differs from the one downloaded here; if so, drop every
    cache, so the next query reconnects to it, downloading it first."""
    version, have = _published(), D.loaded_version()
    if version and version != have:
        D.log.warning("newer demo copy published: %s (have %s)", version, have)
        st.cache_data.clear()
        _db.clear()


@st.cache_data(ttl=3 * 3600)
def _team_names():
    return D.team_names(_con())


@st.cache_data(ttl=3 * 3600)
def _players():
    return D.player_names(_con())


@st.cache_data(ttl=3 * 3600)
def _seasons():
    return D.seasons(_con())


def season_label(s: int) -> str:
    return f"{s}-{(s + 1) % 100:02d}"


def team_label(team_id: int, season: int) -> str:
    t = _team_names()
    row = t[(t.team_id == team_id) & (t.season == season)]
    if row.empty:
        row = t[t.team_id == team_id]
    return row.team.iloc[0] if not row.empty else str(team_id)


def short_team(team_id: int, season: int) -> str:
    return team_label(team_id, season).split()[-1]


def _theme() -> str:
    theme = getattr(st.context, "theme", None)
    return "dark" if theme is not None and theme.type == "dark" else "light"


def _valid(key: str, options: list, default=None) -> None:
    """Drop a preset widget value that isn't among this run's options."""
    if key in st.session_state and st.session_state[key] not in options:
        st.session_state.pop(key)
        if default is not None:
            st.session_state[key] = default


def label_of(m: str) -> str:
    return D.LABELS.get(m) or D.COMPONENT_LABELS.get(m, m)


def rating_table(df: pd.DataFrame, measures: list[str]) -> pd.DataFrame:
    d = df.set_index("measure")
    rows = []
    for m in measures:
        if m in d.index:
            r = d.loc[m]
            rows.append({
                "measure": label_of(m), "pts / 100 vs league": round(float(r.value), 2),
                "rating": None if pd.isna(r.rating) else round(float(r.rating)),
                "tier": r.tier, "low sample": bool(r.low_sample),
            })  # fmt: skip
    return pd.DataFrame(rows)


def dimension_charts(r: pd.DataFrame, mode: str, key: str, adj: pd.DataFrame | None = None):
    """Headline dimensions, then their components, as diverging bars (value; rating)."""
    d = r.set_index("measure")
    if adj is not None and not adj.empty:
        d = d.assign(value=adj.value_adj.reindex(d.index).fillna(d.value),
                     rating=adj.rating_adj.reindex(d.index).fillna(d.rating))  # fmt: skip
    heads = [m for m in D.HEADLINE if m in d.index]
    comps = [m for m in D.ordered(d.index) if m not in D.HEADLINE and m in D.COMPONENT_LABELS
             and not m.startswith(("defense_stl", "defense_blk", "defense_pf", "ball_ato",
                                   "ball_ast", "ball_shot"))]  # fmt: skip
    t1, t2 = st.tabs(["Dimensions", "What drives them"])
    with t1:
        st.plotly_chart(
            C.diverging_bars([label_of(m) for m in heads], [d.value.get(m) for m in heads],
                             mode, "points per 100 vs league",
                             ratings=[d.rating.get(m) for m in heads]),
            width="stretch", key=f"dims_{key}",
        )  # fmt: skip
    with t2:
        st.plotly_chart(
            C.diverging_bars([label_of(m) for m in comps], [d.value.get(m) for m in comps],
                             mode, "points per 100 vs league",
                             ratings=[d.rating.get(m) for m in comps]),
            width="stretch", key=f"comps_{key}",
        )  # fmt: skip
    st.caption("Bar: points per 100 vs league average (blue helps, red hurts). Number after "
               "the dot: the 0-100 rating.")  # fmt: skip
    with st.expander("Table"):
        st.dataframe(rating_table(r, D.ordered(r.measure)), hide_index=True)


@st.cache_data(show_spinner=False, ttl=3 * 3600)
def _league_hex(season: int, season_type: str) -> pd.DataFrame:
    return court.league_hex(D.league_shots(_con(), season, season_type))


def shot_map(shots: pd.DataFrame, season: int, stype: str, mode: str, allowed: bool,
             key: str) -> None:  # fmt: skip
    """Hexagon map of one unit's shots: size = how often, color = points per shot vs league
    from the same spots (for a defense, what opponents scored; blue = good defense)."""
    h = court.unit_hex(shots, {season: _league_hex(season, stype)})
    if h.empty:
        st.info("No shot locations for this selection.")
        return
    if allowed:
        h["diff"] = -h["diff"]
    hover = [f"{r.share:.1%} of {'opponent ' if allowed else ''}shots · FG {r.fg:.0%} "
             f"(league {r.lg_fg:.0%})<br>{-r.diff if allowed else r.diff:+.1f} points per 100 "
             "shots vs league" for r in h.itertuples()]  # fmt: skip
    st.plotly_chart(court.hex_court(h, mode, hover=hover), width="stretch",
                    key=f"hex_{key}")  # fmt: skip
    st.plotly_chart(court.hex_key(mode, unit="points per 100 shots vs league from the same "
                                  "spots"), width="stretch", key=f"hexkey_{key}")  # fmt: skip


def zone_charts(prof: pd.DataFrame, who: str, allowed: bool = False, key: str = "",
                shots: pd.DataFrame | None = None, season: int | None = None,
                stype: str = "regular") -> None:  # fmt: skip
    """Shot map or zone court (how well) beside bars (how often, by zone), plus the table."""
    if prof.empty or prof.fga.sum() == 0:
        st.info("No zone data (shot locations exist from 1996-97).")
        return
    mode = _theme()
    whose = "Opponents'" if allowed else who + ("'" if who.endswith("s") else "'s")
    c1, c2 = st.columns([3, 2], gap="large")
    with c1:
        view = "Zones"
        if shots is not None and season is not None and not shots.empty:
            view = st.segmented_control(
                "Map",
                ["Shot map", "Zones"],
                default="Shot map",
                key=f"mapview_{key}",
                label_visibility="collapsed",
            )
        if view == "Shot map":
            st.markdown(f"**{whose[:1].upper() + whose[1:]} shots**: where from, and how "
                        "well against the league from the same spots")  # fmt: skip
            shot_map(shots, season, stype, mode, allowed, key)
            st.caption(
                "Bigger hexagons: more shots from there. Color: points per 100 shots against "
                "league average from the same spots, so a corner three is compared with corner "
                "threes. Small samples are pulled toward the zone's average. Blue is good for "
                + ("this defense (opponents scored less)." if allowed else f"{who}.")
            )
        else:
            st.markdown(f"**{whose[:1].upper() + whose[1:]} FG% by zone**, against league "
                        "average")  # fmt: skip
            st.plotly_chart(court.zone_court(prof, mode, allowed), width="stretch",
                            key=f"court_{key}")  # fmt: skip
            st.caption(
                "Each zone shows FG% and, below it, the gap to league average that season in "
                "percentage points. Blue is good for "
                + ("this defense (opponents shot worse)." if allowed else f"{who}; red is bad.")
            )
    with c2:
        st.markdown(f"**Where {'opponents' if allowed else who} shot from**, share of shots")
        legend = "Opponents" if allowed else who[:1].upper() + who[1:]
        st.plotly_chart(court.shot_mix(prof, legend, mode),
                        width="stretch", key=f"mix_{key}")  # fmt: skip
        sel, mak = prof.selection_pp100.sum(), prof.making_pp100.sum()
        if allowed:
            st.caption(
                f"Against this defense, where opponents shot from was worth **{sel:+.1f}** "
                f"points per 100 shots compared with league average, and how many they made "
                f"**{mak:+.1f}**. Positive is bad for the defense. Tick = league average."
            )
        else:
            st.caption(
                f"Where {who} shot from was worth **{sel:+.1f}** points per 100 shots "
                f"compared with league average, and how many went in **{mak:+.1f}** (the "
                "Shot quality and Shot making values). Tick = league average."
            )
    with st.expander("Zone table"):
        t = prof.set_index("zone").reindex(court.ZONES).reset_index()
        st.dataframe(pd.DataFrame({
            "zone": t.zone.map(court.ZONE_LABELS), "shots": t.fga.astype(int),
            "share": (100 * t.unit_share).round(1), "league share": (100 * t.share).round(1),
            "FG%": (100 * t.unit_fg_pct).round(1), "league FG%": (100 * t.fg_pct).round(1),
            "pts/100 shots from selection": t.selection_pp100.round(2),
            "pts/100 shots from making": t.making_pp100.round(2),
        }), hide_index=True)  # fmt: skip


def trend_chart(g: pd.DataFrame, mode: str, title: str) -> go.Figure:
    g = g.sort_values("game_date")
    g["rolling"] = g.groupby("measure").rating.transform(
        lambda s: s.rolling(10, min_periods=3).mean()
    )
    g["dimension"] = g.measure.map(D.LABELS)
    fig = px.line(g, x="game_date", y="rolling", color="dimension",
                  labels={"rolling": title, "game_date": ""},
                  color_discrete_sequence=["#2a78d6", "#eb6834", "#1baf7a", "#e87ba4",
                                           "#6250d6"])  # fmt: skip
    fig.add_hline(y=50, line_color=C.THEME[mode]["muted"], line_width=1)
    C.base_layout(fig, 320, legend={"orientation": "h", "y": -0.2, "title": ""})
    return fig


# ---- views --------------------------------------------------------------------------------


def league_view() -> None:
    con, mode = _con(), _theme()
    c1, c2 = st.columns([1, 3])
    season = c1.selectbox("Season", _seasons(), format_func=season_label, key="l_season")
    tm = D.team_map(con, season)
    if tm.empty:
        st.warning("No games this season.")
        return
    tm["name"] = [short_team(t, season) for t in tm.team_id]
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Team map")
        top = max(tm.offense.abs().max(), tm.defense.abs().max()) * 1.2
        fig = go.Figure()
        for c in range(-24, 25, 4):  # lines of equal net rating
            fig.add_trace(go.Scatter(x=[-top, top], y=[c + top, c - top], mode="lines",
                                     line={"color": C.THEME[mode]["grid"], "width": 1},
                                     hoverinfo="skip", showlegend=False))  # fmt: skip
        fig.add_trace(go.Scatter(
            x=tm.offense, y=tm.defense, mode="markers+text", text=tm.name,
            textposition="top center", textfont={"size": 11, "color": C.THEME[mode]["ink2"]},
            marker={"size": 13, "color": [C.good_bad(v, 10, mode) for v in tm.net],
                    "line": {"width": 1, "color": C.THEME[mode]["muted"]}},
            customdata=tm[["team_id"]].to_numpy(),
            hovertemplate="%{text}<br>offense %{x:+.1f}, defense %{y:+.1f} per 100"
                          "<extra></extra>", showlegend=False,
        ))  # fmt: skip
        fig.add_hline(y=0, line_color=C.THEME[mode]["muted"], line_width=1)
        fig.add_vline(x=0, line_color=C.THEME[mode]["muted"], line_width=1)
        C.base_layout(fig, 520, xaxis={"title": "offense: points per 100 vs league",
                                       "range": [-top, top], "zeroline": False},
                      yaxis={"title": "defense: points per 100 saved vs league",
                             "range": [-top, top], "zeroline": False})  # fmt: skip
        ev = st.plotly_chart(fig, width="stretch", on_select="rerun",
                             selection_mode="points", key="l_map")  # fmt: skip
        st.caption("Top right is good at both ends; diagonal lines are equal net rating. "
                   "**Click a team** to open it.")  # fmt: skip
        if (pick := C.selected(ev)) is not None:
            goto("Team", clear="l_map", t_season=season, t_team=int(pick))
    with right:
        st.subheader("Five dimensions")
        adjusted = st.toggle("Adjust for opponents", value=True, key="l_adj")
        dims = D.team_dims(con, season, adjusted)
        if not dims.empty:
            dims = dims.merge(tm[["team_id", "net", "name"]], on="team_id")
            dims = dims.sort_values("net", ascending=False)
            cols = [c for c in D.HEADLINE if c in dims.columns]
            st.plotly_chart(C.rating_heatmap(dims, "name", cols, [D.LABELS[c] for c in cols],
                                             mode), width="stretch", key="l_heat")  # fmt: skip
            st.caption("0-100 ratings (50 = league average), teams by net rating.")

    st.subheader("Impact per possession: leaders")
    lead = D.impact_leaders(con, season)
    if lead.empty:
        st.info("Per-possession impact needs play-by-play lineups (1996-97 on).")
    else:
        f1, f2 = st.columns([2, 1])
        group = f1.segmented_control("Position", ["All", "Backcourt", "Frontcourt"],
                                     default="All", key="l_pos")  # fmt: skip
        side = f2.segmented_control("Side", ["Total", "Offense", "Defense"], default="Total",
                                    key="l_side")  # fmt: skip
        col = {"Total": "impact", "Offense": "impact_off", "Defense": "impact_def"}[side]
        q = lead[lead.qualified]
        if group == "Backcourt":
            q = q[q.w_bc.fillna(0) >= 0.5]
        elif group == "Frontcourt":
            q = q[q.w_bc.fillna(0) < 0.5]
        q = q.sort_values(col, ascending=False).head(20)
        fig = go.Figure(go.Bar(
            y=q.player, x=q[col], orientation="h", marker_color=C.THEME[mode]["good"],
            marker_cornerradius=4,
            error_x={"type": "data", "array": q.impact_se if col == "impact" else None,
                     "color": C.THEME[mode]["muted"], "thickness": 1, "width": 3},
            customdata=q[["player"]].to_numpy(),
            hovertemplate="%{y}: %{x:+.1f} per 100<extra></extra>",
        ))  # fmt: skip
        C.base_layout(fig, 560, yaxis={"autorange": "reversed"},
                      xaxis={"title": "points per 100 possessions vs league"})  # fmt: skip
        ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                             key="l_lead")  # fmt: skip
        st.caption("Adjusted plus-minus with a box-score prior; whiskers: ± 1 standard error. "
                   "**Click a player** to open him.")  # fmt: skip
        if (pick := C.selected(ev)) is not None:
            goto("Player", clear="l_lead", p_name=pick, p_season=season)


def team_view() -> None:
    con, mode = _con(), _theme()
    c1, c2, c3, c4 = st.columns([1, 2, 1, 2])
    seasons = _seasons()
    _valid("t_season", seasons)
    season = c1.selectbox("Season", seasons, format_func=season_label, key="t_season")
    teams = D.q(con, "select distinct team_id from metrics.team_season where season = ? "
                "order by 1", [season]).team_id.tolist()  # fmt: skip
    _valid("t_team", teams)
    team = c2.selectbox("Team", teams, format_func=lambda t: team_label(t, season), key="t_team")
    stype = c3.selectbox("Games", ["regular", "playoffs"], key="t_stype")
    others = [t for t in teams if t != team]
    _valid("t_cmp", [None, *others])
    cmp = c4.selectbox(
        "Compare with",
        [None, *others],
        key="t_cmp",
        format_func=lambda t: "—" if t is None else team_label(t, season),
    )
    r = D.ratings(con, "team_season", "r.season = ? and r.team_id = ? and r.season_type = ?",
                  [season, team, stype])  # fmt: skip
    if r.empty:
        st.warning("No ratings for this selection.")
        return
    adj_q = """select a.measure, a.rating_adj, a.value_adj, a.schedule
               from metrics.team_season_adj a join metrics.best_tier b
                 on b.level = 'team_season' and b.season = a.season
                and b.measure = a.measure and b.tier = a.tier
               where a.season = ? and a.team_id = ?"""
    adj = D.q(con, adj_q, [season, team]).set_index("measure")
    use_adj = stype == "regular" and not adj.empty and st.toggle(
        "Adjust for opponents", value=False, key="t_adj",
        help="What the team would have posted against an average schedule at a neutral site.",
    )  # fmt: skip
    d = r.set_index("measure")
    axes = [m for m in D.HEADLINE if m in d.index]
    src = adj.rating_adj if use_adj else d.rating
    series = {team_label(team, season): [src.get(m) for m in axes]}
    if cmp is not None:
        rc = D.ratings(con, "team_season", "r.season = ? and r.team_id = ? and "
                       "r.season_type = ?", [season, cmp, stype]).set_index("measure")  # fmt: skip
        ac = D.q(con, adj_q, [season, cmp]).set_index("measure")
        csrc = ac.rating_adj if use_adj and not ac.empty else rc.rating
        series[team_label(cmp, season)] = [csrc.get(m) for m in axes]
    left, right = st.columns([1, 1])
    with left:
        st.plotly_chart(C.radar([D.LABELS[m] for m in axes], series, mode), width="stretch",
                        key="t_radar")  # fmt: skip
        st.caption("Presence is a player measure (see the roster)."
                   + (" Opponent-adjusted." if use_adj else ""))  # fmt: skip
    with right:
        dimension_charts(r, mode, "team", adj if use_adj else None)

    from courtvision.app import season_stats

    season_stats.render_team(con, team, season, stype, mode, team_label)

    st.subheader("Shot zones")
    tab_o, tab_d = st.tabs(["Offense", "Defense (opponent shots)"])
    where = "c.season = ? and c.season_type = ? and c.team_id = ?"
    short = "the " + short_team(team, season)
    with tab_o:
        zone_charts(D.zone_profile(con, "metrics.team_season_counts", "a_", where,
                                   [season, stype, team]), short, key="off",
                    shots=D.shots(con, [season], stype, team_id=team), season=season,
                    stype=stype)  # fmt: skip
    with tab_d:
        zone_charts(D.zone_profile(con, "metrics.team_season_counts", "o_", where,
                                   [season, stype, team]), short, allowed=True, key="def",
                    shots=D.shots(con, [season], stype, against=team), season=season,
                    stype=stype)  # fmt: skip

    st.subheader("Season trend")
    g = D.ratings(con, "team_game", "r.season = ? and r.team_id = ? and r.measure in "
                  "('shooting', 'defense', 'ball_control', 'backcourt', 'frontcourt')",
                  [season, team]).merge(D.q(con, "select game_id, game_date from stg.games"),
                                        on="game_id")  # fmt: skip
    if not g.empty:
        st.plotly_chart(trend_chart(g, mode, "game rating, 10-game mean"), width="stretch",
                        key="t_trend")  # fmt: skip
        st.caption("Click a legend entry to hide or show a dimension.")

    st.subheader("Roster")
    roster = D.q(
        con,
        """select p.person_id, p.shooting_rating, p.defense_rating, p.ball_control_rating,
                  p.backcourt_rating, p.frontcourt_rating, i.impact_rating as presence_rating,
                  i.impact, i.impact_se, i.points_added, r.usage, c.minutes
           from metrics.player_season p
           join metrics.player_season_counts c using (season, season_type, person_id, team_id)
           left join metrics.player_impact i on i.season = p.season and i.person_id = p.person_id
           left join metrics.player_role r using (season, season_type, person_id, team_id)
           where p.season = ? and p.team_id = ? and p.season_type = ?
           order by c.minutes desc""",
        [season, team, stype],
    ).merge(_players(), on="person_id", how="left")
    roster["usage"] = 100 * roster.usage
    plot = roster.dropna(subset=["impact", "usage"])
    if not plot.empty:
        fig = go.Figure(go.Scatter(
            x=plot.usage, y=plot.impact, mode="markers+text",
            text=plot.player.str.split().str[-1], textposition="top center",
            textfont={"size": 11, "color": C.THEME[mode]["ink2"]},
            marker={"size": (plot.minutes.fillna(0) / plot.minutes.max() * 34 + 8),
                    "color": [C.good_bad(v, 5, mode) for v in plot.impact],
                    "line": {"width": 1, "color": C.THEME[mode]["muted"]}},
            customdata=plot[["player"]].to_numpy(),
            hovertemplate="%{customdata[0]}<br>usage %{x:.0f}%, impact %{y:+.1f} per 100"
                          "<extra></extra>",
        ))  # fmt: skip
        fig.add_hline(y=0, line_color=C.THEME[mode]["muted"], line_width=1)
        pad = (plot.impact.max() - plot.impact.min()) * 0.15 + 0.5
        C.base_layout(
            fig,
            420,
            xaxis={"title": "usage: % of team plays he finishes"},
            yaxis={
                "title": "impact: points per 100 vs league",
                "range": [plot.impact.min() - pad, plot.impact.max() + pad],
            },
        )
        ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                             key="t_roster")  # fmt: skip
        st.caption("Bubble size: minutes. **Click a player** to open him.")
        if (pick := C.selected(ev)) is not None:
            goto("Player", clear="t_roster", p_name=pick, p_season=season)
    with st.expander("Roster table"):
        cols = ["player", "minutes", "usage", "shooting_rating", "defense_rating",
                "ball_control_rating", "backcourt_rating", "frontcourt_rating",
                "presence_rating", "impact", "points_added"]  # fmt: skip
        st.dataframe(roster[cols].round(1), hide_index=True)


def game_view() -> None:
    con, mode = _con(), _theme()
    c1, c2 = st.columns([1, 3])
    season = c1.selectbox("Season", _seasons(), format_func=season_label, key="g_season")
    games = D.q(
        con,
        """select game_id, game_date, home_team_id, away_team_id, home_score, away_score
           from stg.games where season = ? and game_id in
                (select distinct game_id from metrics.team_game where season = ?)
           order by game_date desc""",
        [season, season],
    )
    if games.empty:
        st.warning("No rated games.")
        return

    def label(gid):
        r = games[games.game_id == gid].iloc[0]
        away, home = team_label(r.away_team_id, season), team_label(r.home_team_id, season)
        return f"{r.game_date:%Y-%m-%d}  {away} {r.away_score} @ {home} {r.home_score}"

    gid = c2.selectbox("Game", games.game_id.tolist(), format_func=label, key="g_game")
    r = D.ratings(con, "team_game", "r.game_id = ?", [gid])
    base = D.q(
        con,
        """select team_id, measure, avg(rating) as season_avg
           from metrics.ratings_long
           where level = 'team_game' and season = ? and season_type = 'regular'
             and team_id in (select team_id from metrics.team_game where game_id = ?)
           group by all""",
        [season, gid],
    )
    r = r.merge(base, on=["team_id", "measure"], how="left")
    r["delta"] = r.rating - r.season_avg
    teams = r.team_id.unique().tolist()
    cols = st.columns(len(teams))
    for col, team in zip(cols, teams, strict=True):
        with col:
            st.markdown(f"**{team_label(team, season)}**")
            t = r[r.team_id == team].set_index("measure")
            axes = [m for m in D.HEADLINE if m in t.index]
            st.plotly_chart(
                C.radar([D.LABELS[m] for m in axes],
                        {"tonight": [t.rating.get(m) for m in axes],
                         "season average": [t.season_avg.get(m) for m in axes]}, mode),
                width="stretch", key=f"radar_{team}",
            )  # fmt: skip
            show = [m for m in D.ordered(t.index) if m in D.LABELS or m in D.COMPONENT_LABELS]
            show = [m for m in show if not m.startswith(("defense_stl", "defense_blk",
                                                         "defense_pf", "ball_ato", "ball_ast",
                                                         "ball_shot"))]  # fmt: skip
            st.plotly_chart(
                C.diverging_bars([label_of(m) for m in show], [t.delta.get(m) for m in show],
                                 mode, "rating tonight minus season average"),
                width="stretch", key=f"delta_{team}",
            )  # fmt: skip
    st.caption("Bars: tonight's rating minus the team's average game rating this season, the "
               "in-game adjustment signal (blue = better than usual).")  # fmt: skip
    st.subheader("Players")
    p = D.q(
        con,
        """select g.*, c.minutes from metrics.player_game g
           join metrics.player_game_counts c using (game_id, person_id, team_id)
           where g.game_id = ? and c.minutes >= 8 order by g.team_id, c.minutes desc""",
        [gid],
    ).merge(_players(), on="person_id", how="left")
    hcols = [f"{m}_rating" for m in D.HEADLINE if f"{m}_rating" in p.columns]
    pcols = st.columns(len(teams))
    for col, team in zip(pcols, teams, strict=True):
        with col:
            sub = p[p.team_id == team]
            if sub.empty:
                continue
            st.markdown(f"**{short_team(team, season)}** (8+ minutes)")
            st.plotly_chart(C.rating_heatmap(sub, "player", hcols,
                                             [D.LABELS[c[:-7]] for c in hcols], mode),
                            width="stretch", key=f"heat_{team}")  # fmt: skip


def player_view() -> None:
    con, mode = _con(), _theme()
    players = _players()
    rated = D.q(con, "select person_id, count(*) n from metrics.player_season "
                "where team_id = 0 group by 1").merge(players, on="person_id")  # fmt: skip
    names = sorted(rated.player.dropna().unique())
    _valid("p_name", names)
    c1, c2, c3 = st.columns([2, 1, 2])
    preset = {} if "p_name" in st.session_state else {"index": None}
    name = c1.selectbox("Player", names, placeholder="Type a name", key="p_name", **preset)
    if not name:
        st.info("Pick a player, or click one in the League, Team or Market view.")
        return
    pid = int(rated[rated.player == name].person_id.iloc[0])
    seasons = D.q(con, "select distinct season from metrics.player_season where person_id = ? "
                  "order by 1 desc", [pid]).season.tolist()  # fmt: skip
    _valid("p_season", seasons)
    season = c2.selectbox("Season", seasons, format_func=season_label, key="p_season")
    cmp_name = c3.selectbox("Compare with", [n for n in names if n != name], index=None,
                            placeholder="Another player, same season", key="p_cmp")  # fmt: skip

    def radar_vals(person: int) -> tuple[pd.DataFrame, list]:
        rr = D.ratings(con, "player_season", "r.person_id = ? and r.season = ? and "
                       "r.season_type = 'regular' and r.team_id = 0", [person, season])  # fmt: skip
        dd = rr.set_index("measure") if not rr.empty else pd.DataFrame()
        ii = D.impact(con, person)
        ii = ii[ii.season == season]
        vals = [dd.rating.get(m) if not dd.empty else None for m in D.HEADLINE]
        vals.append(ii.impact_rating.iloc[0] if not ii.empty else None)
        return rr, vals

    r, vals = radar_vals(pid)
    series = {name: vals}
    if cmp_name:
        cpid = int(rated[rated.player == cmp_name].person_id.iloc[0])
        series[cmp_name] = radar_vals(cpid)[1]
    left, right = st.columns(2)
    with left:
        axes = D.HEADLINE + ["presence"]
        st.plotly_chart(C.radar([D.LABELS[m] for m in axes], series, mode), width="stretch",
                        key="p_radar")  # fmt: skip
        st.caption("Backcourt / Frontcourt are rated only among players at least half guard / "
                   "half forward-center. Presence is per-possession impact.")  # fmt: skip
    with right:
        if not r.empty:
            dimension_charts(r, mode, "player")

    from courtvision.app import season_stats

    season_stats.render_player(con, pid, season, mode, team_label)

    imp = D.impact(con, pid)
    now = imp[imp.season == season]
    st.subheader("Impact per possession")
    if now.empty:
        st.info("No lineup data for this season (play-by-play lineups exist from 1996-97).")
    else:
        i = now.iloc[0]
        c = st.columns(5)
        c[0].metric("Impact per 100 poss.", f"{i.impact:+.1f}", help="Points per 100 "
                    "possessions vs league average, offense + defense, adjusted for "
                    "teammates and opponents, with a box-score prior.")  # fmt: skip
        c[1].metric("Offense / defense", f"{i.impact_off:+.1f} / {i.impact_def:+.1f}")
        c[2].metric("Uncertainty (± 1 se)", f"{i.impact_se:.1f}")
        c[3].metric("Points added", f"{i.points_added:+.0f}",
                    help="Over the season vs league average: impact x possessions.")  # fmt: skip
        per36 = i.points_added / i.minutes * 36 if pd.notna(i.minutes) and i.minutes else None
        c[4].metric("Per 36 minutes", f"{per36:+.1f}" if per36 is not None else "—",
                    help="Points added per 36 minutes. Prefer the per-possession number to "
                    "compare players on fast and slow teams.")  # fmt: skip
        role = (f" Usage {100 * i.usage:.0f}% of team plays; assists on "
                f"{100 * i.ast_pct:.0f}% of teammates' baskets while on court."
                if pd.notna(i.usage) else "")  # fmt: skip
        st.caption(f"{int(i.poss):,} possessions on court.{role}")
        hist = imp.dropna(subset=["impact"])
        t = C.THEME[mode]
        fig = go.Figure([
            go.Scatter(x=list(hist.season) + list(hist.season[::-1]),
                       y=list(hist.impact + hist.impact_se)
                       + list((hist.impact - hist.impact_se)[::-1]),
                       fill="toself", line={"width": 0}, hoverinfo="skip",
                       fillcolor=C.rgba(t["good"], 0.18), showlegend=False),
            go.Scatter(x=hist.season, y=hist.impact, mode="lines+markers", name="impact",
                       line={"color": t["series"][0], "width": 2.5},
                       hovertemplate="%{x}: %{y:+.1f}<extra>impact</extra>"),
            go.Scatter(x=hist.season, y=hist.box, mode="lines", name="box-score model",
                       line={"color": t["series"][1], "width": 1.5, "dash": "dot"},
                       hovertemplate="%{x}: %{y:+.1f}<extra>box model</extra>"),
            go.Scatter(x=hist.season, y=hist.season_only, mode="markers",
                       name="this season's plus-minus alone",
                       marker={"color": t["series"][2], "size": 7},
                       hovertemplate="%{x}: %{y:+.1f}<extra>season only</extra>"),
        ])  # fmt: skip
        fig.add_hline(y=0, line_color=t["muted"], line_width=1)
        C.base_layout(fig, 300, yaxis={"title": "points per 100 vs league"},
                      xaxis={"title": "season (start year)"},
                      legend={"orientation": "h", "y": -0.25})  # fmt: skip
        st.plotly_chart(fig, width="stretch", key="p_hist")
        st.caption(
            "Impact (band ± 1 se) combines the plus-minus regression with two "
            "starting points: last season's impact and what the box score says."
        )
    advanced_panel(pid, season, mode)
    if D.has_market(con):
        mk = D.q(con, "select * from market.players where person_id = ?", [pid])
        if not mk.empty and pd.notna(mk.impact.iloc[0]):
            m = mk.iloc[0]
            st.subheader(f"Contract and market ({season_label(int(m.season))})")
            c = st.columns(4)
            sal = (f"${m.salary / 1e6:.1f}M" if pd.notna(m.salary)
                   else "unknown" if m.contract_kind == "unknown" else "unsigned")  # fmt: skip
            c[0].metric("Salary", sal)
            c[1].metric("Estimated fair salary", f"${m.fair_salary / 1e6:.1f}M",
                        help="Market comparables: his rank in value (points above replacement "
                        "from per-possession impact) priced at the same rank of veteran "
                        f"salaries, capped at his max ({100 * m.max_share:.0f}% of the cap for "
                        f"{m.service:.0f} years of service). From the box score alone: "
                        f"${m.fair_box / 1e6:.1f}M.")  # fmt: skip
            c[2].metric("Wins above replacement", f"{m.war:.1f}",
                        help=f"Bad / good case: {m.war_p10:.1f} / {m.war_p90:.1f}")  # fmt: skip
            c[3].metric("Risk", m.risk)
            status = m.status if isinstance(m.status, str) else "—"
            if pd.notna(m.through) and "unknown" not in status and m.contract_kind == "contract":
                status += (f"; runs through {season_label(int(m.through))} "
                           f"({int(m.years_left)} season(s), "
                           f"${(m.total_left or 0) / 1e6:.0f}M left)")  # fmt: skip
            st.caption(f"**Contract:** {status}.")
            if pd.notna(m.salary):
                st.plotly_chart(
                    C.bullet(m.salary / 1e6,
                             {"fair": m.fair_salary / 1e6, "box-score view": m.fair_box / 1e6},
                             mode, band=(m.fair_p10 / 1e6, m.fair_p90 / 1e6), label="salary"),
                    width="stretch", key="p_bullet",
                )  # fmt: skip
                st.caption("Bar: salary. Lines: fair salary from impact, and from the box score "
                           "alone; band: the impact-based price's bad-to-good range.")  # fmt: skip
            if m.risk_flags is not None and len(m.risk_flags):
                st.caption("Risk factors: " + "; ".join(m.risk_flags))
    st.subheader("Shot zones")
    zone_charts(D.zone_profile(con, "metrics.player_season_counts", "a_",
                               "c.season = ? and c.season_type = 'regular' and "
                               "c.person_id = ? and c.team_id = 0", [season, pid]),
                name, key="player", shots=D.shots(con, [season], "regular", person_id=pid),
                season=season)  # fmt: skip
    st.subheader("Game log")
    g = D.ratings(con, "player_game", "r.person_id = ? and r.season = ? and r.measure in "
                  "('shooting', 'defense', 'ball_control')", [pid, season]).merge(
        D.q(con, "select game_id, game_date from stg.games"), on="game_id")  # fmt: skip
    if not g.empty:
        st.plotly_chart(trend_chart(g, mode, "rating, 10-game mean"), width="stretch",
                        key="p_log")  # fmt: skip


ADVANCED = [  # NBA.com's advanced player table: (column, label, format, higher is better)
    ("off_rating", "Offensive rating", "{:.1f}", True), ("def_rating", "Defensive rating",
                                                           "{:.1f}", False),
    ("net_rating", "Net rating", "{:+.1f}", True), ("ts_pct", "True shooting %", "{:.1%}", True),
    ("efg_pct", "Effective FG %", "{:.1%}", True), ("usg_pct", "Usage %", "{:.1%}", None),
    ("ast_pct", "Assist %", "{:.1%}", True), ("ast_to", "Assist / turnover", "{:.2f}", True),
    ("ast_ratio", "Assist ratio", "{:.1f}", True), ("tov_ratio", "Turnover ratio", "{:.1f}",
                                                      False),
    ("oreb_pct", "Off. rebound %", "{:.1%}", True), ("dreb_pct", "Def. rebound %", "{:.1%}", True),
    ("reb_pct", "Rebound %", "{:.1%}", True), ("pace", "Pace", "{:.1f}", None),
    ("pie", "PIE (NBA.com)", "{:.1%}", True),
]  # fmt: skip


def advanced_panel(pid: int, season: int, mode: str) -> None:
    """NBA.com's advanced stats, each against the league as a percentile bar."""
    con = _con()
    lg = D.q(con, """select * from metrics.player_advanced where season = ? and team_id = 0
                     and season_type = 'regular' and possessions >= 1000""", [season])  # fmt: skip
    me = D.q(con, """select * from metrics.player_advanced where season = ? and team_id = 0
                     and season_type = 'regular' and person_id = ?""", [season, pid])  # fmt: skip
    if me.empty or lg.empty:
        return
    me = me.iloc[0]
    st.subheader("Advanced stats (NBA.com set)")
    rows = []
    for col, label, fmt, up in ADVANCED:
        v = me.get(col)
        if v is None or pd.isna(v):
            continue
        pct = float((lg[col] < v).mean() * 100)
        good = pct if up is not False else 100 - pct
        rows.append((label, fmt.format(v), pct, good, up))
    t = C.THEME[mode]
    # bar length = share of players he is better than (style stats: plain percentile)
    fig = go.Figure(go.Bar(
        y=[r[0] for r in rows], x=[r[2] if r[4] is None else r[3] for r in rows],
        orientation="h", marker_cornerradius=4,
        marker_color=[t["bar"] if r[4] is None else C.good_bad(r[3] - 50, 50, mode)
                      for r in rows],
        text=[f"{r[1]}  ·  " + (f"{r[2]:.0f}th percentile" if r[4] is None
                                else f"better than {r[3]:.0f}%") for r in rows],
        textposition="outside",
        cliponaxis=False, textfont={"color": t["ink2"], "size": 11},
        hovertemplate="%{y}: %{text}<extra></extra>",
    ))  # fmt: skip
    fig.add_vline(x=50, line_color=t["muted"], line_width=1)
    C.base_layout(fig, 26 * len(rows) + 60, yaxis={"autorange": "reversed"},
                  xaxis={"range": [0, 125],
                         "title": "% of players with 1,000+ possessions he beats"})  # fmt: skip
    st.plotly_chart(fig, width="stretch", key="p_adv")
    st.caption("Computed from our play-by-play counts and lineups (validated against NBA.com's "
               "own numbers: ratings, usage and assist rate agree at r 0.99+). Longer bars are "
               "better (for defensive rating and turnover ratio, lower numbers beat more "
               "players); blue above the median, red below; gray: style, not quality (usage, "
               "pace). PIE is NBA.com's official figure.")  # fmt: skip


def live_view() -> None:
    from courtvision.app import live_page

    live_page.render(_con())


def market_view() -> None:
    from courtvision.app import market_page

    market_page.render(_con(), team_label, _theme(), goto)


def upcoming_view() -> None:
    from courtvision.app import upcoming_page

    upcoming_page.render(_con(), team_label, short_team, _players(), _theme(), goto)


def coaches_view() -> None:
    from courtvision.app import coaches_page

    coaches_page.render(_con(), team_label, _theme(), goto)


def scorecard_view() -> None:
    from courtvision.app import scorecard_page

    scorecard_page.render(_con(), short_team, _players(), _theme(), goto)


def lineups_view() -> None:
    from courtvision.app import lineups_page

    lineups_page.render(_con(), team_label, _players(), _theme(), goto)


def plays_view() -> None:
    from courtvision.app import plays_page

    plays_page.render(_con(), team_label, _players(), _theme(), goto)


VIEWS = {"League": league_view, "Upcoming": upcoming_view, "Scorecard": scorecard_view,
         "Team": team_view,
         "Game": game_view, "Player": player_view, "Lineups": lineups_view,
         "Plays": plays_view, "Coaches": coaches_view, "Market": market_view,
         "Live": live_view}  # fmt: skip

_pick_up_new_data()

# a link can open a view (?view=scorecard); read on a session's first run, then the sidebar rules
_linked = {k.lower(): k for k in VIEWS}.get(str(st.query_params.get("view", "")).lower())
if "view" not in st.session_state and _linked:
    st.session_state["view"] = _linked

st.sidebar.title("courtvision")
choice = st.sidebar.radio("View", list(VIEWS), key="view")
st.sidebar.caption(
    "0-100 ratings: 50 = league average that season, 10 = one standard deviation. Blue "
    "helps, red hurts. Click teams and players in any chart to open them."
)
_demo = D.demo_info(_con())
if _demo:
    _built, _thru = pd.Timestamp(_demo["built_at"]), pd.Timestamp(_demo["data_through"])
    st.sidebar.caption(
        f"Demo data, built {_built:%b} {_built.day}, {_built:%I:%M %p}".replace(" 0", " ")
        + " ET: games through "
        f"{_thru:%b} {_thru.day}. Game-level detail and shot maps "
        f"cover {season_label(int(_demo['since']))} on; season ratings go back to "
        f"{season_label(int(_demo['first_season']))}. "
        "[Source and full pipeline](https://github.com/adisgill/courtvision)"
    )
VIEWS[choice]()
