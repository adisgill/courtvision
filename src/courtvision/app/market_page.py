"""Market view: each team's budget, where it is short, and the players who would fix it.

Everything is a chart: the budget against the cap lines, the needs as bars, every option
as a point (cost vs wins added, colored by risk) that opens a trade card when clicked, and
league-wide pricing where a click opens the player.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from courtvision.app import charts as C
from courtvision.app import data as D

RISK_ICON = {"low": "● low", "medium": "▲ medium", "high": "■ high"}
THRESH = {"cap": "salary_cap", "tax": "luxury_tax_line", "1st apron": "first_apron",
          "2nd apron": "second_apron"}  # fmt: skip


def _m(x: float | None) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{'−' if x < 0 else ''}${abs(x) / 1e6:,.1f}M"


def _marks(p: pd.Series) -> dict[str, float]:
    return {k: p[v] / 1e6 for k, v in THRESH.items()}


def _x0(team: pd.Series, p: pd.Series) -> float:
    """Axis start for payroll charts: below both the payroll and the cap, so the cap,
    tax and apron lines (within ~$60M of each other) get room."""
    return 0.8 * min(team.payroll, p.salary_cap) / 1e6


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
    st.caption(f"Replaces the minutes of **{row.replaces}** (impact {row.replaces_impact:+.1f} "
               f"→ {row.impact:+.1f} per 100). Team net rating {row.net_gain_per100:+.1f} "
               f"per 100 over the minutes he plays.")  # fmt: skip
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


def render(con, team_label, theme: str, goto) -> None:
    if not D.has_market(con):
        st.warning("No market data. Run `courtvision ingest salaries` then "
                   "`courtvision market build`.")  # fmt: skip
        return
    mode = theme
    p = D.q(con, "select * from market.params").iloc[0]
    season = int(p.season)
    teams = D.q(con, "select * from market.teams order by team_id")
    st.caption(
        f"{season}-{(season + 1) % 100:02d} salaries (HoopsHype snapshot of 2026-07-07, via "
        "the MrBridge dataset). Value = wins above replacement from per-possession impact, "
        f"at ${p.dollars_per_win / 1e6:.1f}M per win (the league's {season - 1}-"
        f"{season % 100:02d} rate); {p.points_per_win:.0f} points = 1 win. Trade and tax rules "
        "are a simplified reading of the 2023 CBA: a planning aid."
    )
    tab_team, tab_league = st.tabs(["Team needs and options", "League pricing"])
    with tab_team:
        ids = teams.team_id.tolist()
        if st.session_state.get("m_team") not in ids:
            st.session_state.pop("m_team", None)
        team = st.selectbox("Team", ids, format_func=lambda t: team_label(t, season),
                            key="m_team")  # fmt: skip
        t = teams[teams.team_id == team].iloc[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Payroll", _m(t.payroll))
        c2.metric("Projected luxury tax", _m(t.tax_bill) if t.tax_bill > 0 else "none")
        c3.metric("Room under the tax", _m(t.room_luxury_tax_line))
        st.plotly_chart(C.bullet(t.payroll / 1e6, _marks(p), mode, label="payroll", x0=_x0(t, p)),
                        width="stretch", key="m_budget")  # fmt: skip
        st.caption(f"**{t.status[:1].upper() + t.status[1:]}.** Lines: cap, tax, first and "
                   "second apron.")  # fmt: skip

        st.subheader("Where the team is short")
        dims = ["shooting", "defense", "ball_control", "backcourt", "frontcourt"]
        needs = list(t.needs) if t.needs is not None else []
        ratings = [t.get(f"{d}_adj") for d in dims]
        st.plotly_chart(
            C.diverging_bars([D.LABELS[d] + ("  ← need" if d in needs else "") for d in dims],
                             [None if pd.isna(r) else r - 50 for r in ratings], mode,
                             "rating minus league average (opponent-adjusted)",
                             ratings=ratings),
            width="stretch", key="m_needs",
        )  # fmt: skip
        st.caption("A need is a dimension rated below 47 this season after adjusting for "
                   "opponents (the weakest one if none is).")  # fmt: skip

        opts = D.q(con, "select * from market.options where team_id = ? order by need, rank",
                   [team])  # fmt: skip
        st.subheader("Options")
        if opts.empty or not needs:
            st.info("No player in the league is both strong in a need and an upgrade on the "
                    "weakest rotation player at his position.")  # fmt: skip
            return
        need = st.segmented_control("Need", needs, default=needs[0],
                                    format_func=lambda d: D.LABELS[d], key="m_need")  # fmt: skip
        o = opts[opts.need == (need or needs[0])].reset_index(drop=True)
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
                "fair salary": o.fair_salary.map(_m),
                "salary to send out": o.outgoing_salary_needed.map(_m),
                "status after": o.status_after, "risk": o.risk.map(RISK_ICON),
            }), hide_index=True)  # fmt: skip
        st.caption("Unsigned: no contract this season in the salary snapshot (a free agent, or "
                   "retired; priced at the veteran minimum).")  # fmt: skip
    with tab_league:
        df = D.q(con, "select * from market.players where salary > 0 and war is not null")
        focus = st.selectbox("Highlight a team", [None] + teams.team_id.tolist(), key="m_focus",
                             format_func=lambda x: "none" if x is None
                             else team_label(x, season))  # fmt: skip
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
                "fair salary": sub.fair_salary.map(_m), "surplus": sub.surplus.map(_m),
                "overpay chance": (100 * sub.overpay_prob).round(0),
                "risk": sub.risk.map(RISK_ICON),
                "why": sub.risk_flags.map(lambda f: "; ".join(f) if f is not None else ""),
            }).sort_values("wins above repl.", ascending=False), hide_index=True)  # fmt: skip
