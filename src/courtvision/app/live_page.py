"""Live view: a game in progress rated against its teams' seasons."""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from courtvision import live
from courtvision.app import court
from courtvision.app import data as D


def render(con) -> None:
    st.subheader("Live game")
    st.caption(
        "Polls the NBA live feed (at most every 10 seconds per game; every response is "
        "cached under data/raw/live/). Tonight's numbers are on the team-game scale of the "
        "current season, and each delta is tonight minus the team's average game this season."
    )
    try:
        board = live.scoreboard()
    except Exception as e:  # network, feed down
        st.warning(f"Live scoreboard unavailable: {e}")
        board = pd.DataFrame()
    options = []
    if not board.empty:
        options = [
            f"{r.game_id}  {r.away} {r.away_score} @ {r.home} {r.home_score}  ({r.status})"
            for r in board.itertuples()
        ]
    c1, c2 = st.columns([3, 2])
    pick = c1.selectbox("Today's games", options, index=0 if options else None)
    manual = c2.text_input("...or a game id", placeholder="0022600123")
    game_id = manual.strip() or (pick.split()[0] if pick else None)
    if not game_id:
        st.info("No games on today's scoreboard. Enter a game id (finished games work too).")
        return
    auto = st.toggle("Refresh every 30 s", value=False)
    try:
        res = live.rate_game(con, game_id)
    except Exception as e:
        st.error(f"Could not rate {game_id}: {e}")
        return

    st.markdown(
        f"### {res['away']} {res['away_score']} @ {res['home']} {res['home_score']}"
        f" — {res['status'] or ''}"
    )
    if res["baseline_season"] != res["season"]:
        st.caption(f"League baselines from {res['baseline_season']} (current season not built).")
    qa = res["lineup_qa"]
    if qa is not None and not qa.ok:
        st.caption(
            "Lineups don't fully reconstruct yet (common mid-game); on-court parts may be off."
        )
    r = res["ratings"]
    cols = st.columns(2)
    for col, (team_id, abbr) in zip(
        cols, [(res["away_id"], res["away"]), (res["home_id"], res["home"])], strict=True
    ):
        with col:
            st.markdown(f"**{abbr}**")
            t = r[r.team_id == team_id].set_index("measure")
            axes = [m for m in D.HEADLINE if m in t.index]
            st.plotly_chart(
                court.radar(
                    [D.LABELS[m] for m in axes],
                    {
                        "tonight": [t.rating.get(m) for m in axes],
                        "season avg": [t.season_rating.get(m) for m in axes],
                    },
                ),
                width="stretch",
                key=f"live_radar_{team_id}",
            )
            for s in live.signals(r, team_id):
                st.markdown(f"- {s}")
            show = t.loc[D.ordered(t.index)].reset_index()
            show["measure"] = show.measure.map(
                lambda m: D.LABELS.get(m) or D.COMPONENT_LABELS.get(m, m)
            )
            st.dataframe(
                show[["measure", "value", "rating", "season_value", "delta_value"]].round(1),
                hide_index=True,
                height=320,
            )
    if auto:
        time.sleep(30)
        st.rerun()
