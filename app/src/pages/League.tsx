// The league: standings with ratings on five dimensions, the offense-defense team map, and the
// players with the most impact per possession.
import * as Plot from "@observablehq/plot";
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Row, useQuery } from "../db";
import { css, div, fmt, seasonLabel } from "../lib/format";
import { DIMENSIONS, useSeasons, useTeamNames } from "../lib/nba";
import { Figure } from "../ui/Figure";
import { RatingGrid } from "../ui/grids";
import { Card, DataTable, PageHead, Seg, SeasonSelect, useParam } from "../ui/kit";

export default function League() {
  const seasons = useSeasons();
  const [sp, setSeason] = useParam("season", "");
  const season = Number(sp) || seasons[0] || 0;
  const [adj, setAdj] = useParam("adj", "1");
  const [pos, setPos] = useParam("pos", "all");
  const [side, setSide] = useParam("side", "total");
  const names = useTeamNames(season);
  const map = useQuery(season ? `
    with t as (select team_id, sum(b_pts) pts, sum(o_b_pts) opp, sum(b_poss) poss, sum(o_b_poss) o_poss,
                      count(*) filter (where b_pts > o_b_pts) wins, count(*) filter (where b_pts < o_b_pts) losses
               from metrics.team_game_counts where season = ${season} and season_type = 'regular' group by team_id),
    lg as (select sum(pts) / sum(poss) ppp from t)
    select team_id, wins, losses, 100 * (pts / poss - ppp) offense, 100 * (ppp - opp / o_poss) defense, 100 * (pts / poss - opp / o_poss) net
    from t, lg order by net desc` : null);
  const dims = useQuery(season ? `
    select a.team_id, a.measure, ${adj === "1" ? "a.rating_adj" : "a.rating"} rating from metrics.team_season_adj a
    join metrics.best_tier b on b.level = 'team_season' and b.season = a.season and b.measure = a.measure and b.tier = a.tier
    where a.season = ${season} and a.measure in (${DIMENSIONS.map(([k]) => `'${k}'`).join(",")})` : null);
  const lead = useQuery(season ? `
    select i.person_id, i.impact, i.impact_off, i.impact_def, i.impact_se, i.poss, i.points_added, i.qualified, coalesce(pos.w_bc, 0) w_bc, n.player
    from metrics.player_impact i join web.player_names n using (person_id) left join metrics.positions pos using (person_id)
    where i.season = ${season} and i.qualified` : null);
  const D = new Map<number, Record<string, number>>();
  dims.rows.forEach((r) => { const t = Number(r.team_id); D.set(t, { ...(D.get(t) ?? {}), [r.measure]: r.rating }); });
  const teams = map.rows.map((r): Row => ({ ...r, name: names.get(Number(r.team_id))?.name ?? String(r.team_id), abbrev: names.get(Number(r.team_id))?.abbrev ?? "",
    pct: r.wins / Math.max(1, r.wins + r.losses), ...D.get(Number(r.team_id)) }));
  const top = Math.max(4, ...teams.flatMap((t) => [Math.abs(t.offense), Math.abs(t.defense)])) * 1.2;
  const mapPlot = useMemo(() => teams.length ? (width: number) => ({
    width, height: Math.min(560, width * 0.85), x: { label: "offense: points per 100 vs league", domain: [-top, top] },
    y: { label: "defense: points saved per 100 vs league", domain: [-top, top] },
    marks: [
      ...Array.from({ length: 13 }, (_, i) => -24 + 4 * i).map((c) => Plot.line([[-top, c + top], [top, c - top]], { stroke: css("--grid"), clip: true })),
      Plot.ruleX([0], { stroke: css("--axis") }), Plot.ruleY([0], { stroke: css("--axis") }),
      Plot.dot(teams, { x: "offense", y: "defense", r: 7, fill: (d: any) => div(d.net / 10), stroke: css("--surface"), strokeWidth: 1.5 }),
      Plot.text(teams, { x: "offense", y: "defense", text: "abbrev", dy: -12, fontSize: 10.5, fill: css("--text-2") }),
      Plot.tip(teams, Plot.pointer({ x: "offense", y: "defense", title: (d: any) => `${d.name} ${d.wins}-${d.losses}\noffense ${fmt.signed(d.offense, 1)}, defense ${fmt.signed(d.defense, 1)} per 100\nnet ${fmt.signed(d.net, 1)}` })),
    ],
  }) : undefined, [teams, top]);
  const leaders = lead.rows.filter((r) => pos === "all" || (pos === "bc" ? r.w_bc >= 0.5 : r.w_bc < 0.5))
    .map((r): Row => ({ ...r, v: side === "off" ? r.impact_off : side === "def" ? r.impact_def : r.impact }))
    .sort((a, b) => b.v - a.v).slice(0, 20);
  const leadPlot = useMemo(() => leaders.length ? (width: number) => ({
    width, height: 24 * leaders.length + 50, marginLeft: 150, x: { label: "points per 100 possessions vs league", grid: true },
    y: { label: null, domain: leaders.map((r) => r.player) },
    marks: [
      ...(side === "total" ? [Plot.link(leaders, { y: "player", x1: (d: any) => d.v - d.impact_se, x2: (d: any) => d.v + d.impact_se, stroke: css("--text-2"), strokeWidth: 1.5 })] : []),
      Plot.barX(leaders, { y: "player", x: "v", fill: css("--pos"), fillOpacity: 0.85, rx: 3, insetTop: 4, insetBottom: 4 }),
      Plot.tip(leaders, Plot.pointerY({ y: "player", x: "v", title: (d: any) => `${d.player}: ${fmt.signed(d.v, 1)} per 100 (± ${fmt.n(d.impact_se, 1)})\n${fmt.n(d.poss)} possessions · ${fmt.signed(d.points_added, 0)} points added` })),
    ],
  }) : undefined, [leaders, side]);
  return (
    <>
      <PageHead eyebrow={`League · ${season ? seasonLabel(season) : ""}`} title="The league"
        lead="Records, net rating per 100 possessions and 0-100 ratings on five dimensions (50 = league average, 10 = one standard deviation), and the players worth the most per possession." />
      <div className="filters">
        {seasons.length > 0 && <SeasonSelect value={season} onChange={(v) => setSeason(String(v))} seasons={seasons} />}
        <div className="field">Ratings<Seg options={[["1", "Opponent-adjusted"], ["0", "Raw"]]} value={adj} onChange={setAdj} /></div>
      </div>
      <section className="section">
        <h2>Standings and ratings</h2>
        <div className="grid">
          <div className="c5">
            <Card title="By net rating">
              <DataTable rows={teams} rank cols={[
                { key: "name", label: "Team", fmt: (v, r) => <Link to={`/team/${r.team_id}?season=${season}`}>{v}</Link> },
                { key: "wins", label: "W-L", num: true, fmt: (v, r) => `${v}-${r.losses}` },
                { key: "offense", label: "Off", num: true, fmt: (v) => fmt.signed(v, 1), heat: "pos" },
                { key: "defense", label: "Def", num: true, fmt: (v) => fmt.signed(v, 1), heat: "pos" },
                { key: "net", label: "Net", num: true, fmt: (v) => fmt.signed(v, 1), heat: "pos" },
              ]} />
            </Card>
          </div>
          <div className="c7">
            <Card title="Five dimensions" sub={`0-100 ratings${adj === "1" ? ", adjusted for opponents and home court" : ""}; teams by net rating.`}>
              <RatingGrid rowHead="Team" cols={DIMENSIONS} rows={teams.map((t) => ({ key: String(t.team_id), label: t.name, to: `/team/${t.team_id}?season=${season}`,
                values: Object.fromEntries(DIMENSIONS.map(([k]) => [k, (t as any)[k]])) }))} />
            </Card>
          </div>
        </div>
      </section>
      <section className="section">
        <h2>Team map</h2>
        <div className="grid">
          <div className="c7">{mapPlot && <Figure title="Offense against defense" subtitle="Top right is good at both ends; diagonal lines are equal net rating; colour is net rating."
            plot={mapPlot} table={{ rows: teams, cols: [["name", "Team"], ["offense", "Offense", (v) => fmt.signed(v, 1)], ["defense", "Defense", (v) => fmt.signed(v, 1)], ["net", "Net", (v) => fmt.signed(v, 1)]] }} />}</div>
          <div className="c5">
            <p className="small ink2">Offense: points scored per 100 possessions against the league rate; defense: points saved per 100 against it, so up is good on both axes.
              Net rating is the distance from the diagonal through the origin.</p>
          </div>
        </div>
      </section>
      <section className="section">
        <h2>Impact per possession</h2>
        <div className="filters">
          <div className="field">Position<Seg options={[["all", "All"], ["bc", "Backcourt"], ["fc", "Frontcourt"]]} value={pos} onChange={setPos} /></div>
          <div className="field">Side<Seg options={[["total", "Total"], ["off", "Offense"], ["def", "Defense"]]} value={side} onChange={setSide} /></div>
        </div>
        {leadPlot ? <Figure title="The leaders" subtitle="Adjusted plus-minus with a box-score prior; whiskers: ± 1 standard error. Qualified players only."
          plot={leadPlot} table={{ rows: leaders, cols: [["player", "Player"], ["v", "Per 100", (v) => fmt.signed(v, 1)], ["impact_se", "±", (v) => fmt.n(v, 1)], ["poss", "Possessions", (v) => fmt.n(v)]] }} />
          : <p className="muted">Per-possession impact needs play-by-play lineups.</p>}
        <p className="xs muted">Players: <Link to="/player">search any player</Link> for his ratings, impact history and shot map.</p>
      </section>
    </>
  );
}
