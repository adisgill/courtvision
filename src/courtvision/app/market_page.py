"""Market view: each team's budget, where it is short, and the players who would fix it."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from courtvision.app import data as D

RISK_COLOR = {"low": "#0ca30c", "medium": "#fab219", "high": "#d03b3b"}
RISK_ICON = {"low": "● low", "medium": "▲ medium", "high": "■ high"}


def _m(x: float | None) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{'−' if x < 0 else ''}${abs(x) / 1e6:,.1f}M"


def budget_chart(payroll: float, p: pd.Series, mode: str) -> go.Figure:
    ink = "#f0efec" if mode == "dark" else "#0b0b0b"
    muted = "#898781"
    top = max(payroll, p.second_apron) * 1.08
    fig = go.Figure(go.Bar(
        x=[payroll / 1e6], y=["payroll"], orientation="h", marker_color="#6b6a64"
        if mode == "dark" else "#a8a69e", marker_cornerradius=4, width=0.45,
        hovertemplate="payroll $%{x:.1f}M<extra></extra>",
    ))  # fmt: skip
    for key, label in (("salary_cap", "cap"), ("luxury_tax_line", "tax"),
                       ("first_apron", "1st apron"), ("second_apron", "2nd apron")):  # fmt: skip
        x = p[key] / 1e6
        fig.add_vline(x=x, line_color=ink, line_width=2)
        fig.add_annotation(x=x, y=1.02, yref="paper", text=f"{label}<br>${x:.0f}M",
                           showarrow=False, font={"size": 11, "color": muted},
                           yanchor="bottom")  # fmt: skip
    fig.update_layout(
        xaxis={"range": [0, top / 1e6], "ticksuffix": "M", "tickprefix": "$",
               "showgrid": False, "fixedrange": True},
        yaxis={"visible": False, "fixedrange": True},
        height=150, margin={"l": 0, "r": 10, "t": 46, "b": 0}, showlegend=False,
    )  # fmt: skip
    return fig


def pricing_chart(df: pd.DataFrame, team_id: int | None) -> go.Figure:
    fig = go.Figure()
    lim = max(df.salary.max(), df.fair_salary.max()) / 1e6 * 1.05
    fig.add_trace(go.Scatter(x=[0, lim], y=[0, lim], mode="lines", hoverinfo="skip",
                             line={"color": "#898781", "width": 1}, showlegend=False))  # fmt: skip
    for risk in ("low", "medium", "high"):
        d = df[df.risk == risk]
        hi = d.team_id == team_id
        fig.add_trace(go.Scatter(
            x=d.fair_salary / 1e6, y=d.salary / 1e6, mode="markers", name=f"{risk} risk",
            marker={"color": RISK_COLOR[risk], "size": [12 if h else 7 for h in hi],
                    "opacity": [1.0 if (h or team_id is None) else 0.35 for h in hi],
                    "line": {"width": 1, "color": "rgba(0,0,0,0.3)"}},
            text=d.player, customdata=d[["surplus"]].to_numpy() / 1e6,
            hovertemplate="%{text}<br>paid $%{y:.1f}M, worth $%{x:.1f}M"
                          "<br>surplus $%{customdata[0]:+.1f}M<extra></extra>",
        ))  # fmt: skip
    fig.update_layout(
        xaxis={"title": "estimated fair salary ($M)", "range": [0, lim]},
        yaxis={"title": "actual salary ($M)", "range": [0, lim]},
        height=440, margin={"l": 0, "r": 0, "t": 10, "b": 0},
        legend={"orientation": "h", "y": -0.18},
    )  # fmt: skip
    return fig


def render(con, team_label, theme: str) -> None:
    if not D.has_market(con):
        st.warning("No market data. Run `courtvision ingest salaries` then "
                   "`courtvision market build`.")  # fmt: skip
        return
    p = D.q(con, "select * from market.params").iloc[0]
    season = int(p.season)
    teams = D.q(con, "select * from market.teams order by team_id")
    st.caption(
        f"{season}-{(season + 1) % 100:02d} salaries (HoopsHype snapshot of 2026-07-07, via "
        "the MrBridge dataset). Value = wins above replacement from per-possession impact "
        f"(adjusted plus-minus), at ${p.dollars_per_win / 1e6:.1f}M per win (the league's "
        f"{season - 1}-{season % 100:02d} rate); {p.points_per_win:.0f} points = 1 win. "
        "Trade and tax rules are a simplified reading of the 2023 CBA: a planning aid."
    )
    tab_team, tab_league = st.tabs(["Team needs and options", "League pricing"])
    with tab_team:
        team = st.selectbox("Team", teams.team_id.tolist(),
                            format_func=lambda t: team_label(t, season))  # fmt: skip
        t = teams[teams.team_id == team].iloc[0]
        c1, c2 = st.columns(2)
        c1.metric("Payroll", _m(t.payroll))
        c2.metric("Projected luxury tax", _m(t.tax_bill) if t.tax_bill > 0 else "none")
        st.markdown(f"**Status:** {t.status}")
        st.plotly_chart(budget_chart(t.payroll, p, theme), width="stretch", key="budget")
        room = {"cap": t.room_salary_cap, "tax": t.room_luxury_tax_line,
                "1st apron": t.room_first_apron, "2nd apron": t.room_second_apron}  # fmt: skip
        st.caption("Room: " + " · ".join(
            f"{k} {_m(v)}" if v >= 0 else f"{k} over by {_m(-v)}" for k, v in room.items()
        ))  # fmt: skip

        st.subheader("Where the team is short")
        dims = ["shooting", "defense", "ball_control", "backcourt", "frontcourt"]
        rated = pd.DataFrame({
            "dimension": [D.LABELS[d] for d in dims],
            "rating (opponent-adjusted)": [t.get(f"{d}_adj") for d in dims],
            "need": ["yes" if d in list(t.needs) else "" for d in dims],
        })  # fmt: skip
        st.dataframe(rated.round(1), hide_index=True)
        opts = D.q(con, "select * from market.options where team_id = ? order by need, rank",
                   [team])  # fmt: skip
        if opts.empty:
            st.info("No player in the league is both strong in a need and an upgrade on the "
                    "weakest rotation player at his position.")  # fmt: skip
            return
        for need in t.needs:
            o = opts[opts.need == need]
            st.markdown(f"**{D.LABELS[need]}**: {len(o)} options, cheapest per win first")
            if o.empty:
                st.caption("No upgrade found.")
                continue
            show = pd.DataFrame({
                "player": o.player + o.free_agent.map({True: " (unsigned)", False: ""}),
                "from": [team_label(x, season) if pd.notna(x) else "—" for x in o.from_team_id],
                f"{D.LABELS[need]} rating": o.need_rating.round(0),
                "impact /100": o.impact.round(1),
                "replaces": o.replaces,
                "wins added": o.wins_added.round(1),
                "wins (bad case)": o.wins_added_p10.round(1),
                "salary": o.salary.map(_m),
                "fair salary": o.fair_salary.map(_m),
                "salary to send out": o.outgoing_salary_needed.map(_m),
                "status after": o.status_after,
                "tax change": o.tax_change.map(lambda v: _m(v) if v else "—"),
                "risk": o.risk.map(RISK_ICON),
                "why": o.risk_flags.map(lambda f: "; ".join(f) if f is not None else ""),
            })  # fmt: skip
            st.dataframe(show, hide_index=True)
        st.caption(
            "Unsigned: no contract this season in the salary snapshot (a free agent, or "
            "retired; priced at the veteran minimum). Wins added: the candidate's impact "
            "minus that of the weakest rotation player in "
            "the same position group, over the minutes the candidate usually plays. Bad case: "
            "his estimate at its 10th percentile. Salary to send out: the least outgoing "
            "salary that makes the trade legal for this team's payroll."
        )
    with tab_league:
        df = D.q(con, "select * from market.players where salary > 0 and war is not null")
        focus = st.selectbox("Highlight a team", [None] + teams.team_id.tolist(),
                             format_func=lambda x: "none" if x is None
                             else team_label(x, season))  # fmt: skip
        st.plotly_chart(pricing_chart(df, focus), width="stretch", key="pricing")
        st.caption("Above the line: paid more than the estimate; below: a bargain.")
        sub = df if focus is None else df[df.team_id == focus]
        st.dataframe(
            pd.DataFrame({
                "player": sub.player, "age": sub.age.round(0),
                "team": [team_label(x, season) if pd.notna(x) else "—" for x in sub.team_id],
                "impact /100": sub.impact.round(1), "± se": sub.impact_se.round(1),
                "wins above repl.": sub.war.round(1), "salary": sub.salary.map(_m),
                "fair salary": sub.fair_salary.map(_m), "surplus": sub.surplus.map(_m),
                "overpay chance": (100 * sub.overpay_prob).round(0),
                "risk": sub.risk.map(RISK_ICON),
                "why": sub.risk_flags.map(lambda f: "; ".join(f) if f is not None else ""),
            }).sort_values("wins above repl.", ascending=False),
            hide_index=True,
        )  # fmt: skip
