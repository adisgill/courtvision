"""Market view: budgets, needs and trade options, contracts, free agency, league pricing.

Every part is a chart: payroll against the cap lines, needs as bars, options as points
(cost vs wins added, colored by risk) that open a trade card, each team's contracts as a
timeline, next summer's cap space and free-agent class, and league-wide pricing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from courtvision.app import charts as C
from courtvision.app import data as D

RISK_ICON = {"low": "● low", "medium": "▲ medium", "high": "■ high"}
THRESH = {"cap": "salary_cap", "tax": "luxury_tax_line", "1st apron": "first_apron",
          "2nd apron": "second_apron"}  # fmt: skip
FA_COLORS = {"UFA": "#2a78d6", "RFA": "#1baf7a", "player option": "#eb6834",
             "team option": "#6250d6"}  # fmt: skip
KIND_COLORS = {"guaranteed": "#2a78d6", "player option": "#eb6834", "team option": "#6250d6",
               "length unknown": "#a8a69e", "dead money": "#e34948", "two-way": "#1baf7a",
               "salary unknown": "#d8d6cf"}  # fmt: skip


def _m(x: float | None) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{'−' if x < 0 else ''}${abs(x) / 1e6:,.1f}M"


def _season(s: int) -> str:
    return f"{s}-{(s + 1) % 100:02d}"


def _marks(p: pd.Series) -> dict[str, float]:
    return {k: p[v] / 1e6 for k, v in THRESH.items()}


def _x0(team: pd.Series, p: pd.Series) -> float:
    """Axis start for payroll charts: below both the payroll and the cap, so the cap,
    tax and apron lines (within ~$60M of each other) get room."""
    return 0.8 * min(team.payroll, p.salary_cap) / 1e6


# ---- needs and trade options -------------------------------------------------------------


def options_chart(o: pd.DataFrame, mode: str) -> go.Figure:
    fig = go.Figure()
    for risk in ("low", "medium", "high"):
        d = o[o.risk == risk]
        if d.empty:
            continue
        fig.add_trace(go.Scatter(
            x=d.salary / 1e6, y=d.wins_added, mode="markers+text", name=f"{risk} risk",
            text=d.player.str.split().str[-1], textposition="top center",
            textfont={"size": 10, "color": C.THEME[mode]["ink2"]},
            marker={"color": C.RISK[risk], "size": 12,
                    "symbol": ["diamond" if f else "circle" for f in d.free_agent],
                    "line": {"width": 1, "color": "rgba(0,0,0,0.35)"}},
            error_y={"type": "data", "symmetric": False, "array": [0] * len(d),
                     "arrayminus": (d.wins_added - d.wins_added_p10).clip(lower=0),
                     "color": C.THEME[mode]["muted"], "thickness": 1, "width": 0},
            customdata=d[["person_id"]].to_numpy(),
            hovertemplate="%{text}<br>$%{x:.1f}M, %{y:.1f} wins added<extra></extra>",
        ))  # fmt: skip
    C.base_layout(fig, 420, xaxis={"title": "salary this season ($M)", "rangemode": "tozero"},
                  yaxis={"title": "wins added (whisker: bad case)", "rangemode": "tozero"},
                  legend={"orientation": "h", "y": -0.2})  # fmt: skip
    return fig


def trade_card(row: pd.Series, team: pd.Series, p: pd.Series, mode: str, goto) -> None:
    """What the move does: wins, cost, salary that must go out, budget after, risk."""
    st.markdown(f"#### {row.player}" + (" (unsigned)" if row.free_agent else ""))
    c = st.columns(2) + st.columns(2)  # a 2 x 2 grid fits the narrow card
    c[0].metric(
        "Wins added",
        f"{row.wins_added:.1f}",
        help=f"Bad case (10th percentile of his impact): {row.wins_added_p10:.1f}",
    )
    c[1].metric("Salary", _m(row.salary), help=f"Estimated fair salary {_m(row.fair_salary)}")
    c[2].metric("Salary to send out", _m(row.outgoing_salary_needed),
                help="Least outgoing salary that makes the trade legal for this payroll "
                "(simplified 2023 CBA matching).")  # fmt: skip
    c[3].metric("Tax change", _m(row.tax_change) if row.tax_change else "none")
    contract = ("free agent: signs for the minimum" if row.free_agent else
                f"contract: {row.contract_status}" + (
                    f", through {_season(int(row.through))}" if pd.notna(row.through)
                    and "unknown" not in str(row.contract_status) else ""))  # fmt: skip
    st.caption(
        f"Replaces the minutes of **{row.replaces}** (impact {row.replaces_impact:+.1f} "
        f"→ {row.impact:+.1f} per 100). Team net rating {row.net_gain_per100:+.1f} "
        f"per 100 over the minutes he plays. {contract[:1].upper() + contract[1:]}."
    )
    st.plotly_chart(C.bullet(row.payroll_after / 1e6, _marks(p), mode,
                             band=tuple(sorted((team.payroll / 1e6, row.payroll_after / 1e6))),
                             label="payroll after", x0=_x0(team, p)),
                    width="stretch", key="m_after")  # fmt: skip
    st.caption(f"Payroll {_m(team.payroll)} → {_m(row.payroll_after)} ({row.status_after}). "
               "Shaded: the change.")  # fmt: skip
    flags = list(row.risk_flags) if row.risk_flags is not None else []
    st.markdown(f"**Risk: {RISK_ICON[row.risk]}**"
                + (f", {row.overpay_prob:.0%} chance he's worth less than he's paid"
                   if pd.notna(row.overpay_prob) else "")
                + ("  \n" + "; ".join(flags) if flags else ""))  # fmt: skip
    if st.button(f"Open {row.player}", key="m_open"):
        goto("Player", p_name=row.player, p_season=int(p.season))


def needs_tab(con, t: pd.Series, p: pd.Series, mode: str, team_label, goto) -> None:
    season = int(p.season)
    c1, c2, c3 = st.columns(3)
    c1.metric("Payroll", _m(t.payroll), help="Contracts and dead money; two-way deals "
              "don't count against the cap.")  # fmt: skip
    c2.metric("Projected luxury tax", _m(t.tax_bill) if t.tax_bill > 0 else "none")
    c3.metric("Room under the tax", _m(t.room_luxury_tax_line))
    st.plotly_chart(C.bullet(t.payroll / 1e6, _marks(p), mode, label="payroll", x0=_x0(t, p)),
                    width="stretch", key="m_budget")  # fmt: skip
    note = f"**{t.status[:1].upper() + t.status[1:]}.** Lines: cap, tax, first and second apron."
    if t.unknown:
        note += (f" {int(t.unknown)} player(s) on the roster aren't on the standard-contract "
                 "pages: two-way or short-term deals, which don't count against the cap. "
                 "Correct anything in `data/salary_overrides.csv`.")  # fmt: skip
    st.caption(note)

    st.subheader("Where the team is short")
    dims = ["shooting", "defense", "ball_control", "backcourt", "frontcourt"]
    needs = list(t.needs) if t.needs is not None else []
    ratings = [t.get(f"{d}_adj") for d in dims]
    st.plotly_chart(
        C.diverging_bars([D.LABELS[d] + ("  ← need" if d in needs else "") for d in dims],
                         [None if pd.isna(r) else r - 50 for r in ratings], mode,
                         "rating minus league average (opponent-adjusted)", ratings=ratings),
        width="stretch", key="m_needs",
    )  # fmt: skip
    st.caption("A need is a dimension rated below 47 this season after adjusting for "
               "opponents (the weakest one if none is).")  # fmt: skip
    opts = D.q(con, "select * from market.options where team_id = ? order by need, rank",
               [int(t.team_id)])  # fmt: skip
    st.subheader("Options now: trades and unsigned players")
    if opts.empty or not needs:
        st.info("No player in the league is both strong in a need and an upgrade on the "
                "weakest rotation player at his position.")  # fmt: skip
        return
    need = st.segmented_control("Need", needs, default=needs[0],
                                format_func=lambda d: D.LABELS[d], key="m_need")  # fmt: skip
    o = opts[opts.need == (need or needs[0])].reset_index(drop=True)
    if o.empty:
        st.info("No upgrade found for this need.")
        return
    left, right = st.columns([3, 2], gap="large")
    with left:
        ev = st.plotly_chart(options_chart(o, mode), width="stretch", on_select="rerun",
                             selection_mode="points", key=f"m_opts_{need}")  # fmt: skip
        st.caption("Up and to the left: more wins for less money. Diamond = unsigned. "
                   "**Click a player** for the trade card.")  # fmt: skip
    with right:
        pick = C.selected(ev)
        row = o[o.person_id == pick].iloc[0] if pick is not None and (
            o.person_id == pick).any() else o.iloc[0]  # fmt: skip
        trade_card(row, t, p, mode, goto)
    with st.expander("All options as a table"):
        st.dataframe(pd.DataFrame({
            "player": o.player + o.free_agent.map({True: " (unsigned)", False: ""}),
            "from": [team_label(x, season) if pd.notna(x) else "—" for x in o.from_team_id],
            "need rating": o.need_rating.round(0), "impact /100": o.impact.round(1),
            "replaces": o.replaces, "wins added": o.wins_added.round(1),
            "wins (bad case)": o.wins_added_p10.round(1), "salary": o.salary.map(_m),
            "fair salary": o.fair_salary.map(_m), "contract": o.contract_status,
            "salary to send out": o.outgoing_salary_needed.map(_m),
            "status after": o.status_after, "risk": o.risk.map(RISK_ICON),
        }), hide_index=True)  # fmt: skip


# ---- contracts ---------------------------------------------------------------------------------


def contracts_tab(con, t: pd.Series, p: pd.Series, mode: str, goto) -> None:
    season = int(p.season)
    book = D.q(
        con,
        """select b.*, trim(coalesce(pl.first_name, '') || ' ' || pl.last_name) as player
           from salary.book b left join stg.players pl using (person_id)
           where b.team_id = ? and b.season between ? and ?""",
        [int(t.team_id), season, season + 4],
    )
    if book.empty:
        st.info("No contracts on file for this team.")
        return

    def kind(r) -> str:
        if r.kind in ("dead money", "two-way"):
            return r.kind
        if r.kind == "unknown":
            return "salary unknown"
        if isinstance(r.option, str):
            return f"{r.option} option"
        return "guaranteed" if r.length_known else "length unknown"

    book["type"] = book.apply(kind, axis=1)
    order = (book[book.season == season].sort_values("salary", ascending=False).player.tolist()
             + [x for x in book.player.unique() if x not in set(book[book.season == season]
                                                               .player)])  # fmt: skip
    seasons = list(range(season, season + 5))
    fig = go.Figure()
    for typ, color in KIND_COLORS.items():
        d = book[book.type == typ]
        if d.empty:
            continue
        fig.add_trace(go.Scatter(
            x=[_season(s) for s in d.season], y=d.player, mode="markers+text", name=typ,
            marker={"symbol": "square", "size": 30, "color": color},
            text=["?" if pd.isna(v) else f"{v / 1e6:.0f}" for v in d.salary],
            textfont={"size": 10, "color": "#ffffff" if typ != "salary unknown" else "#0b0b0b"},
            customdata=d[["player"]].to_numpy(),
            hovertemplate="%{y}, %{x}: " + typ + " %{text}M<extra></extra>",
        ))  # fmt: skip
    C.base_layout(fig, 34 * len(order) + 90,
                  xaxis={"categoryorder": "array", "categoryarray": [_season(s) for s in seasons],
                         "side": "top"},
                  yaxis={"categoryorder": "array", "categoryarray": order[::-1]},
                  legend={"orientation": "h", "y": -0.05})  # fmt: skip
    ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                         key="m_book")  # fmt: skip
    st.caption("Each square: that season's salary ($M), colored by what kind of money it is. "
               "Options are listed as if picked up. **Click a player** to open him.")  # fmt: skip
    if (pick := C.selected(ev)) is not None:
        goto("Player", clear="m_book", p_name=pick, p_season=season)
    tot = book[book.type.isin(["guaranteed", "player option", "team option", "length unknown",
                               "dead money"])].groupby(["season", "type"]).salary.sum()  # fmt: skip
    fig = go.Figure()
    for typ in ("guaranteed", "length unknown", "player option", "team option", "dead money"):
        vals = [tot.get((s, typ), 0) / 1e6 for s in seasons]
        fig.add_trace(go.Bar(x=[_season(s) for s in seasons], y=vals, name=typ,
                             marker_color=KIND_COLORS[typ]))  # fmt: skip
    caps = [p.salary_cap / 1e6 * (1.07**k) for k in range(5)]
    fig.add_trace(go.Scatter(x=[_season(s) for s in seasons], y=caps, mode="lines+markers",
                             name="cap (projected +7%/yr)",
                             line={"color": C.THEME[mode]["ink"], "dash": "dot"}))  # fmt: skip
    C.base_layout(fig, 320, barmode="stack", yaxis={"title": "$M committed"},
                  legend={"orientation": "h", "y": -0.2})  # fmt: skip
    st.plotly_chart(fig, width="stretch", key="m_commit")
    st.caption("Money committed by season. Deals signed in July have unknown length; they "
               "are shown for this season only.")  # fmt: skip


# ---- free agency -------------------------------------------------------------------------------


def free_agency_tab(con, t: pd.Series, p: pd.Series, mode: str, team_label, goto) -> None:
    season = int(p.season)
    nxt = _season(season + 1)
    teams = D.q(con, "select team_id, next_space, next_space_best, next_committed, "
                "n_unknown_length from market.teams")  # fmt: skip
    teams["name"] = [team_label(x, season).split()[-1] for x in teams.team_id]
    teams = teams.sort_values("next_space", ascending=False)
    st.subheader(f"Cap space next summer ({nxt})")
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=teams.name, y=teams.next_space_best / 1e6, name="if options are declined",
        marker_color=C.rgba(C.THEME[mode]["good"], 0.3),
        hovertemplate="%{x}: up to $%{y:.0f}M<extra></extra>"))  # fmt: skip
    fig.add_trace(go.Bar(
        x=teams.name, y=teams.next_space / 1e6, name="options picked up",
        marker_color=[C.THEME[mode]["good"] if v >= 0 else C.THEME[mode]["bad"]
                      for v in teams.next_space],
        customdata=teams[["team_id"]].to_numpy(),
        hovertemplate="%{x}: $%{y:.0f}M<extra></extra>"))  # fmt: skip
    C.base_layout(fig, 340, barmode="overlay", yaxis={"title": "$M under the projected cap"},
                  legend={"orientation": "h", "y": -0.3})  # fmt: skip
    st.plotly_chart(fig, width="stretch", key="m_space")
    st.caption(f"Projected cap {_m(p.next_cap)} (+7%). Deals signed in July of unknown length "
               "are assumed to run on; cap holds and exceptions aren't modelled, so space is "
               "an upper bound.")  # fmt: skip

    fa = D.q(con, "select * from market.fa_class")
    st.subheader(f"The {nxt} free-agent class")
    types = st.multiselect("Who", list(FA_COLORS), default=["UFA", "RFA", "player option"],
                           key="m_fa_types")  # fmt: skip
    f = fa[fa.fa_type.isin(types)]
    fig = go.Figure()
    for typ in types:
        d = f[f.fa_type == typ]
        fig.add_trace(go.Scatter(
            x=d.impact_next, y=d.ask / 1e6, mode="markers", name=typ,
            marker={"color": FA_COLORS[typ], "size": (d.proj_poss.fillna(2000) / 900).clip(5, 16),
                    "line": {"width": 1, "color": "rgba(0,0,0,0.3)"}},
            error_y={"type": "data", "symmetric": False,
                     "array": (d.ask_p90 - d.ask) / 1e6, "arrayminus": (d.ask - d.ask_p10) / 1e6,
                     "color": C.rgba(FA_COLORS[typ], 0.35), "thickness": 1, "width": 0},
            text=d.player, customdata=d[["player"]].to_numpy(),
            hovertemplate="%{text}<br>impact next season %{x:+.1f}, worth ~$%{y:.0f}M"
                          "<extra></extra>"))  # fmt: skip
    C.base_layout(fig, 440, xaxis={"title": "projected impact next season (per 100)"},
                  yaxis={"title": f"projected fair salary {nxt} ($M)"},
                  legend={"orientation": "h", "y": -0.2})  # fmt: skip
    ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                         key="m_fa")  # fmt: skip
    st.caption("Projected impact includes the aging curve; fair salary uses market comparables "
               "against the projected cap (whiskers: bad to good case). Bigger dots play more. "
               "**Click a player** to open him.")  # fmt: skip
    if (pick := C.selected(ev)) is not None:
        goto("Player", clear="m_fa", p_name=pick, p_season=season)

    fo = D.q(con, "select * from market.fa_options where team_id = ? order by need, rank",
             [int(t.team_id)])  # fmt: skip
    st.subheader(f"{team_label(int(t.team_id), season)}: free agents who fit a need")
    st.caption(f"Projected space {_m(t.next_space)} (up to {_m(t.next_space_best)} if options "
               "are declined).")  # fmt: skip
    if fo.empty:
        st.info("No upcoming free agent is both strong in a need and an upgrade here.")
    else:
        fo["fits"] = np.where(fo.fits_space, "fits", np.where(fo.fits_space_best,
                                                             "fits if options declined",
                                                             "needs room cleared"))  # fmt: skip
        fig = go.Figure()
        colors = {"fits": C.THEME[mode]["good"], "fits if options declined": "#eda100",
                  "needs room cleared": C.THEME[mode]["bad"]}  # fmt: skip
        for k, color in colors.items():
            d = fo[fo.fits == k]
            if d.empty:
                continue
            fig.add_trace(go.Scatter(
                x=d.ask / 1e6, y=d.wins_added, mode="markers+text", name=k,
                text=d.player.str.split().str[-1], textposition="top center",
                textfont={"size": 10, "color": C.THEME[mode]["ink2"]},
                marker={"color": color, "size": 11}, customdata=d[["player"]].to_numpy(),
                hovertemplate="%{text}: ~$%{x:.0f}M, %{y:.1f} wins<extra></extra>"))  # fmt: skip
        C.base_layout(fig, 380, xaxis={"title": "projected fair salary ($M)"},
                      yaxis={"title": "wins added next season"},
                      legend={"orientation": "h", "y": -0.25})  # fmt: skip
        ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                             key="m_fa_fit")  # fmt: skip
        if (pick := C.selected(ev)) is not None:
            goto("Player", clear="m_fa_fit", p_name=pick, p_season=season)

    now = D.q(
        con,
        """select player, age, impact, fair_salary, status from market.players
           where free_agent and impact is not null and poss_base >= 800
           order by impact desc limit 15""",
    )
    st.subheader("Unsigned right now")
    if now.empty:
        st.info("Nobody notable is unsigned.")
    else:
        lbl = [f"{n} ({a:.0f})" + ("  · may have retired" if a >= 37 else "")
               for n, a in zip(now.player, now.age.fillna(0), strict=True)]  # fmt: skip
        fig = go.Figure(go.Bar(
            y=lbl, x=now.impact, orientation="h", marker_cornerradius=4,
            marker_color=[C.THEME[mode]["good"] if v > 0 else C.THEME[mode]["bad"]
                          for v in now.impact],
            customdata=now[["player"]].to_numpy(),
            hovertemplate="%{y}: impact %{x:+.1f}<extra></extra>"))  # fmt: skip
        C.base_layout(fig, 26 * len(now) + 60, yaxis={"autorange": "reversed"},
                      xaxis={"title": "impact per 100 (last estimate)"})  # fmt: skip
        ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                             key="m_now")  # fmt: skip
        st.caption("Played last season or this one, no contract in the snapshots, not "
                   "playing now. They can sign at any time, usually near the minimum.")  # fmt: skip
        if (pick := C.selected(ev)) is not None:
            goto("Player", clear="m_now", p_name=pick, p_season=season - 1)


# ---- league pricing ----------------------------------------------------------------------------


def pricing_tab(con, teams: pd.DataFrame, p: pd.Series, mode: str, team_label, goto) -> None:
    season = int(p.season)
    df = D.q(con, "select * from market.players where salary_known and "
             "contract_kind = 'contract' and war is not null")  # fmt: skip
    focus = st.selectbox(
        "Highlight a team",
        [None] + teams.team_id.tolist(),
        key="m_focus",
        format_func=lambda x: "none" if x is None else team_label(x, season),
    )
    fig = go.Figure()
    lim = max(df.salary.max(), df.fair_salary.max()) / 1e6 * 1.05
    fig.add_trace(go.Scatter(x=[0, lim], y=[0, lim], mode="lines", hoverinfo="skip",
                             line={"color": C.THEME[mode]["muted"], "width": 1},
                             showlegend=False))  # fmt: skip
    for risk in ("low", "medium", "high"):
        d = df[df.risk == risk]
        hi = d.team_id == focus
        fig.add_trace(go.Scatter(
            x=d.fair_salary / 1e6, y=d.salary / 1e6, mode="markers", name=f"{risk} risk",
            marker={"color": C.RISK[risk], "size": [13 if h else 8 for h in hi],
                    "opacity": [1.0 if (h or focus is None) else 0.3 for h in hi],
                    "line": {"width": 1, "color": "rgba(0,0,0,0.3)"}},
            text=d.player, customdata=d[["player"]].to_numpy(),
            hovertemplate="%{text}<br>paid $%{y:.1f}M, worth $%{x:.1f}M<extra></extra>",
        ))  # fmt: skip
    C.base_layout(fig, 460, xaxis={"title": "estimated fair salary ($M)", "range": [0, lim]},
                  yaxis={"title": "actual salary ($M)", "range": [0, lim]},
                  legend={"orientation": "h", "y": -0.18})  # fmt: skip
    ev = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points",
                         key="m_pricing")  # fmt: skip
    st.caption("Above the line: paid more than the estimate; below: a bargain. "
               "**Click a player** to open him.")  # fmt: skip
    if (pick := C.selected(ev)) is not None:
        goto("Player", clear="m_pricing", p_name=pick, p_season=season)
    sub = df if focus is None else df[df.team_id == focus]
    with st.expander("Table"):
        st.dataframe(pd.DataFrame({
            "player": sub.player, "age": sub.age.round(0),
            "team": [team_label(x, season) if pd.notna(x) else "—" for x in sub.team_id],
            "impact /100": sub.impact.round(1), "± se": sub.impact_se.round(1),
            "wins above repl.": sub.war.round(1), "salary": sub.salary.map(_m),
            "fair salary": sub.fair_salary.map(_m), "box-score view": sub.fair_box.map(_m),
            "contract": sub.status, "overpay chance": (100 * sub.overpay_prob).round(0),
            "risk": sub.risk.map(RISK_ICON),
            "why": sub.risk_flags.map(lambda f: "; ".join(f) if f is not None else ""),
        }).sort_values("wins above repl.", ascending=False), hide_index=True)  # fmt: skip


def render(con, team_label, theme: str, goto) -> None:
    if not D.has_market(con):
        st.warning("No market data. Run `courtvision ingest salaries` then "
                   "`courtvision market build`.")  # fmt: skip
        return
    mode = theme
    p = D.q(con, "select * from market.params").iloc[0]
    season = int(p.season)
    teams = D.q(con, "select * from market.teams order by team_id")
    src = D.q(con, "select max(source) as s from salary.book "
              "where source like 'basketball-reference 20%'").s.iloc[0] or ""  # fmt: skip
    as_of = src.replace("basketball-reference ", "")
    st.caption(
        f"{_season(season)} contracts: Basketball-Reference's team contract pages as of "
        f"{as_of}, plus two-way deals and dead money from the HoopsHype snapshot of "
        "2026-07-07; your `data/salary_overrides.csv` wins over both (refresh with "
        "`courtvision ingest salaries --fetch-current`). Fair salary = market comparables: "
        "the Nth most valuable player (points above replacement, from per-possession impact) "
        "priced at the Nth-highest veteran salary, capped by his max for years of service. "
        f"{p.points_per_win:.0f} points = 1 win. Trade and tax rules are a simplified reading "
        "of the 2023 CBA: a planning aid."
    )
    ids = teams.team_id.tolist()
    if st.session_state.get("m_team") not in ids:
        st.session_state.pop("m_team", None)
    team = st.selectbox("Team", ids, format_func=lambda x: team_label(x, season), key="m_team")
    t = teams[teams.team_id == team].iloc[0]
    tabs = st.tabs(["Needs and trade options", "Contracts", "Free agency", "League pricing"])
    with tabs[0]:
        needs_tab(con, t, p, mode, team_label, goto)
    with tabs[1]:
        contracts_tab(con, t, p, mode, goto)
    with tabs[2]:
        free_agency_tab(con, t, p, mode, team_label, goto)
    with tabs[3]:
        pricing_tab(con, teams, p, mode, team_label, goto)
