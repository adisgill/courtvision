// A team's lineups: the units it uses, why raw lineup plus-minus misleads, the best fives it
// could play, which pairs work, and who closes games.
import * as Plot from "@observablehq/plot";
import { useMemo } from "react";
import { Row, useQuery } from "../db";
import { css, fmt, seasonLabel } from "../lib/format";
import { grpOf, lineupKey, topFives } from "../lib/lineups";
import { useTeamNames } from "../lib/nba";
import { Figure } from "../ui/Figure";
import { Bars, DivBars, HeatGrid } from "../ui/grids";
import { Card, DataTable, PageHead, SeasonSelect, Select, useParam } from "../ui/kit";

const last = (s: string) => String(s ?? "").split(" ").slice(-1)[0];

export default function Lineups() {
  const ss = useQuery(`select season, sum(poss) poss from lineups.units group by 1 order by 1 desc`);
  const maxPoss = Math.max(0, ...ss.rows.map((r) => r.poss));
  const seasons = ss.rows.map((r) => Number(r.season));
  const def = ss.rows.find((r) => r.poss >= 0.5 * maxPoss)?.season;
  const [sp, setSeason] = useParam("season", "");
  const season = Number(sp) || Number(def) || seasons[0] || 0;
  const names = useTeamNames(season);
  const teams = useQuery(season ? `select distinct team_id from lineups.units where season = ${season}` : null);
  const opts = teams.rows.map((r) => [String(r.team_id), names.get(Number(r.team_id))?.team ?? String(r.team_id)] as [string, string]).sort((a, b) => a[1].localeCompare(b[1]));
  const [tp, setTeam] = useParam("team", "");
  const team = Number(tp) || Number(opts[0]?.[0]) || 0;
  return (
    <>
      <PageHead eyebrow={`Lineups · ${season ? seasonLabel(season) : ""}`} title="Lineups"
        lead="Five-man units and pairs, by net rating per 100 possessions. A lineup's own plus-minus is mostly noise; the projections lean on its players' impacts and shrink the rest." />
      <div className="filters">
        {seasons.length > 0 && <SeasonSelect value={season} onChange={(v) => setSeason(String(v))} seasons={seasons} />}
        {opts.length > 0 && <Select label="Team" value={String(team)} onChange={setTeam} options={opts} />}
      </div>
      {team > 0 && <TeamLineups season={season} team={team} name={names.get(team)?.name ?? ""} />}
    </>
  );
}

function TeamLineups({ season, team, name }: { season: number; team: number; name: string }) {
  const units = useQuery(`select * from lineups.units where season = ${season} and team_id = ${team} order by poss desc`);
  const tests = useQuery(`select * from lineups.tests`);
  const pairs = useQuery(`select * from lineups.pairs where season = ${season} and team_id = ${team}`);
  const roster = useQuery(`
    with g as (select person_id, minutes, game_id, dense_rank() over (order by game_id desc) rk
               from metrics.player_game_counts where season = ${season} and team_id = ${team} and season_type = 'regular'),
    m as (select person_id, sum(minutes) minutes, min(rk) <= 15 cur from g group by 1)
    select m.*, coalesce(i.impact, 0) impact, p.is_center, p.is_guard, p.is_forward, n.player
    from m left join metrics.player_impact i on i.person_id = m.person_id and i.season = ${season}
    left join stg.players p on p.person_id = m.person_id join web.player_names n on n.person_id = m.person_id`);
  const nm = new Map(roster.rows.map((r) => [Number(r.person_id), last(r.player)]));
  const label = (ids: number[]) => ids.map((x) => nm.get(Number(x)) ?? x).join(" · ");
  const sh = tests.rows.find((r) => r.test === "split-half");
  const halves = tests.rows.filter((r) => r.test === "second half from first");
  const pk = tests.rows.find((r) => r.test === "pairs split-half")?.k_poss;
  const top = units.rows.slice(0, 12);
  const scat = units.rows.filter((u) => u.poss >= 50);
  const scatter = useMemo(() => scat.length ? (width: number) => ({
    width, height: 380, x: { label: "possessions together (log)", type: "log" as const, grid: true }, y: { label: "net per 100 possessions", grid: true },
    marks: [Plot.ruleY([0], { stroke: css("--axis") }),
            Plot.link(scat, { x1: "poss", x2: "poss", y1: "net100", y2: "proj_net100", stroke: css("--grid"), strokeWidth: 1.5 }),
            Plot.dot(scat, { x: "poss", y: "net100", r: 3.5, stroke: css("--muted"), fill: css("--surface") }),
            Plot.dot(scat, { x: "poss", y: "proj_net100", r: 4, fill: (d: any) => (d.proj_net100 >= 0 ? css("--pos") : css("--neg")) }),
            Plot.tip(scat, Plot.pointer({ x: "poss", y: "proj_net100", title: (d: any) => `${label(d.players)}\n${fmt.n(d.poss)} possessions · raw ${fmt.signed(d.net100, 1)} → projected ${fmt.signed(d.proj_net100, 1)}` }))],
  }) : undefined, [scat, nm]);
  const pool = roster.rows.filter((r) => r.minutes >= 100 && r.cur).map((r) => ({ id: Number(r.person_id), impact: r.impact as number, grp: grpOf(r) }));
  const fives = topFives(pool, 5);
  const seen = new Map(units.rows.map((u) => [u.lineup, u]));
  const P9 = [...roster.rows].sort((a, b) => b.minutes - a.minutes).slice(0, 9).map((r) => Number(r.person_id));
  const pmap = new Map(pairs.rows.map((r) => [`${Math.min(r.person_a, r.person_b)}|${Math.max(r.person_a, r.person_b)}`, r]));
  const closers = [...units.rows].filter((u) => u.clutch_poss > 0).sort((a, b) => b.clutch_poss - a.clutch_poss).slice(0, 5);
  return (
    <>
      <section className="section">
        <h2>The lineups they use</h2>
        <Card title={`${name}: the 12 most-used units`} sub={sh ? `Raw net is the lineup's own plus-minus per 100; "from its players" adds up their impacts; projected shrinks the difference toward the players by ${fmt.n(sh.k_poss)} possessions (a lineup's synergy repeats from one half-season to the next at r ${fmt.n(sh.synergy_r, 2)}).` : undefined}>
          <HeatGrid rowHead="Lineup" rowWidth={320} cols={[{ key: "min", label: "Minutes" }, { key: "raw", label: "Raw net" }, { key: "exp", label: "From its players" },
            { key: "proj", label: "Projected" }, { key: "st", label: "Started" }]}
            rows={top.map((u: Row) => ({ key: u.lineup, label: label(u.players), cells: {
              min: { text: fmt.n(u.minutes), muted: true },
              raw: { text: fmt.signed(u.net100, 1), z: u.net100 / 15, tip: <>{fmt.n(u.poss)} possessions · offense {fmt.n(u.off100, 1)}, defense {fmt.n(u.def100, 1)} per 100</> },
              exp: { text: fmt.signed(u.exp_net100, 1), z: u.exp_net100 / 10 },
              proj: { text: fmt.signed(u.proj_net100, 1), z: u.proj_net100 / 10 },
              st: { text: fmt.n(u.starts), muted: true },
            } }))} />
        </Card>
      </section>
      <section className="section">
        <h2>Why a lineup's plus-minus misleads</h2>
        <div className="grid">
          <div className="c7">{scatter && <Figure title="Raw net against the projection" subtitle="Hollow: the lineup's own net per 100. Solid: its projection. Small samples swing wildly and get pulled in hardest." plot={scatter} />}</div>
          <div className="c5">
            <Card title="Predicting a lineup's second half from its first" sub="Average miss in net points per 100 possessions.">
              <DataTable rows={halves} cols={[{ key: "predictor", label: "Predictor" }, { key: "mae", label: "Miss", num: true, fmt: (v) => fmt.n(v, 2) }, { key: "lineups", label: "Lineups", num: true }]} />
            </Card>
          </div>
        </div>
      </section>
      <section className="section">
        <h2>The best fives they could play</h2>
        <Card title="By summed impact" sub="Every five of the 13 players with the most impact (100+ minutes, played in the last 15 games), with at least one guard and one or two bigs.">
          <DivBars rows={fives.map((f) => {
            const u = seen.get(lineupKey(f.ids));
            return { label: label(f.ids), v: f.sum, text: `${fmt.signed(f.sum, 1)} · ${u && u.poss >= 1 ? `${fmt.n(u.poss)} possessions together` : "untried"}` };
          })} labelWidth={320} />
        </Card>
      </section>
      <section className="section">
        <h2>Pairs</h2>
        <div className="grid">
          <div className="c7">
            <Card title="Net per 100 with both on court, vs the team" sub={pk ? `Shrunk by ${fmt.n(pk)} possessions toward zero. Diagonal: the player alone.` : undefined}>
              <HeatGrid rowHead="" rowWidth={110} compact cols={P9.map((p) => ({ key: String(p), label: nm.get(p) ?? String(p) }))}
                rows={P9.map((a) => ({ key: String(a), label: nm.get(a) ?? String(a), cells: Object.fromEntries(P9.map((b) => {
                  const r = pmap.get(`${Math.min(a, b)}|${Math.max(a, b)}`);
                  return [String(b), r ? { text: fmt.signed(r.rel_shrunk, 1), z: r.rel_shrunk / 6, tip: <>raw {fmt.signed(r.rel, 1)} · shrunk {fmt.signed(r.rel_shrunk, 1)} · {fmt.n(r.poss)} possessions</> } : { text: "–", muted: true }];
                })) }))} />
            </Card>
          </div>
          <div className="c5">
            <Card title="Who closes" sub="Lineups with the most clutch possessions (last 5 minutes, within 5 points).">
              <Bars rows={closers.map((u) => ({ label: label(u.players), v: u.clutch_poss, tip: <>{fmt.n(u.clutch_poss)} clutch possessions · raw clutch net {fmt.signed(u.clutch_net100, 1)} · projected {fmt.signed(u.proj_net100, 1)}</> }))}
                fmtV={(v) => `${fmt.n(v)} poss`} labelWidth={230} />
            </Card>
          </div>
        </div>
      </section>
    </>
  );
}
