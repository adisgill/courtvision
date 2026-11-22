// The market: a team's needs and the players who fill them, its contracts, the free-agent class
// and how the league prices production.
import * as Plot from "@observablehq/plot";
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Row, useQuery } from "../db";
import { css, fmt, seasonLabel } from "../lib/format";
import { DIMENSIONS, useTeamNames } from "../lib/nba";
import { Figure } from "../ui/Figure";
import { DivBars, HeatGrid } from "../ui/grids";
import { Card, DataTable, PageHead, Select, Tabs, Tile, useParam } from "../ui/kit";

const RISK: Record<string, string> = { low: "--good", medium: "--warning", high: "--critical" };
const KIND_COLORS: Record<string, string> = { guaranteed: "--s1", "player option": "--s2", "team option": "--s7", "length unknown": "--axis",
  "dead money": "--s8", "two-way": "--s3", "salary unknown": "--grid" };
const M = (v: number) => (v == null ? "–" : `$${(v / 1e6).toFixed(1)}M`);

export default function Market() {
  const params = useQuery(`select * from market.params`);
  const P = params.rows[0];
  const season = Number(P?.season ?? 0);
  const names = useTeamNames(season || 2026);
  const teams = useQuery(`select * from market.teams order by team_id`);
  const opts = teams.rows.map((r) => [String(r.team_id), names.get(Number(r.team_id))?.team ?? String(r.team_id)] as [string, string]).sort((a, b) => a[1].localeCompare(b[1]));
  const [tp, setTeam] = useParam("team", "");
  const team = Number(tp) || Number(opts[0]?.[0]) || 0;
  const [tab, setTab] = useParam("tab", "needs");
  const T = teams.rows.find((r) => Number(r.team_id) === team);
  return (
    <>
      <PageHead eyebrow={`Market · ${season ? seasonLabel(season) : ""}`} title="Contracts against production"
        lead={P ? <>Salary cap {M(P.salary_cap)}, luxury tax line {M(P.luxury_tax_line)}, aprons {M(P.first_apron)} and {M(P.second_apron)} (a simplified reading of the 2023 CBA).
          Fair salary prices projected impact like comparable contracts; {fmt.n(P.points_per_win, 1)} points = 1 win. Contracts from Basketball-Reference.</> : null} />
      <div className="filters">{opts.length > 0 && <Select label="Team" value={String(team)} onChange={setTeam} options={opts} />}</div>
      <Tabs tabs={[["needs", "Needs and trade options"], ["contracts", "Contracts"], ["fa", "Free agency"], ["pricing", "League pricing"]]} value={tab} onChange={setTab} />
      {tab === "needs" && T && <Needs T={T} team={team} />}
      {tab === "contracts" && team > 0 && <Contracts team={team} season={season} cap={P?.salary_cap} />}
      {tab === "fa" && team > 0 && <FreeAgency team={team} names={names} />}
      {tab === "pricing" && <Pricing team={team} names={names} />}
    </>
  );
}

function Needs({ T, team }: { T: Row; team: number }) {
  const o = useQuery(`select * from market.options where team_id = ${team} order by need, rank`);
  const needs = new Set((T.needs ?? []) as string[]);
  const scatter = useMemo(() => o.rows.length ? (width: number) => ({
    width, height: 380, x: { label: "salary ($M)", grid: true }, y: { label: "wins added", grid: true },
    marks: [Plot.ruleY([0], { stroke: css("--axis") }),
            Plot.link(o.rows, { x1: (d: any) => d.salary / 1e6, x2: (d: any) => d.salary / 1e6, y1: "wins_added", y2: "wins_added_p10", stroke: css("--axis") }),
            Plot.dot(o.rows, { x: (d: any) => d.salary / 1e6, y: "wins_added", r: 6, symbol: (d: any) => (d.free_agent ? "diamond" : "circle"), fill: (d: any) => css(RISK[d.risk] ?? "--axis"), stroke: css("--surface") }),
            Plot.text(o.rows, { x: (d: any) => d.salary / 1e6, y: "wins_added", text: (d: any) => String(d.player).split(" ").slice(-1)[0], dy: -11, fontSize: 10, fill: css("--text-2") }),
            Plot.tip(o.rows, Plot.pointer({ x: (d: any) => d.salary / 1e6, y: "wins_added", title: (d: any) => `${d.player} (${d.need})\n+${d.wins_added.toFixed(1)} wins (bad case ${d.wins_added_p10?.toFixed(1)}) · ${M(d.salary)}\nreplaces ${d.replaces ?? "–"} · risk ${d.risk}` }))],
  }) : undefined, [o.rows]);
  return (
    <>
      <div className="tiles">
        <Tile label="Payroll" value={M(T.payroll)} delta={T.status} />
        <Tile label="Projected luxury tax" value={M(T.tax_bill)} />
        <Tile label="Room under the tax" value={M(T.room_luxury_tax_line)} delta={`${M(T.room_first_apron)} under the first apron`} />
        <Tile label="Contracts" value={fmt.n(T.contracts)} delta={T.unknown ? `${T.unknown} with salary unknown` : undefined} />
      </div>
      <div className="grid">
        <div className="c5">
          <Card title="Where the team is short" sub="Opponent-adjusted dimension ratings minus 50; a need is a dimension rated below 47.">
            <DivBars rows={DIMENSIONS.map(([k, l]) => ({ label: needs.has(k) ? `${l} ← need` : l, v: (T[`${k}_adj`] ?? 50) - 50, text: fmt.n(T[`${k}_adj`], 0) }))} labelWidth={150} />
          </Card>
        </div>
        <div className="c7">{scatter && <Figure title="Players who fill the needs" subtitle="Wins added against salary; whisker down to the bad case; colour: risk (green low, amber medium, red high); diamonds: free agents." plot={scatter} />}</div>
      </div>
      <Card title="All options" className="flat">
        <DataTable rows={o.rows} cols={[{ key: "need", label: "Need" }, { key: "player", label: "Player", fmt: (v, r) => <Link to={`/player/${r.person_id}`}>{v}</Link> },
          { key: "wins_added", label: "Wins", num: true, fmt: (v) => fmt.signed(v, 1) }, { key: "salary", label: "Salary", num: true, fmt: M },
          { key: "outgoing_salary_needed", label: "Send out", num: true, fmt: M }, { key: "tax_change", label: "Tax change", num: true, fmt: M },
          { key: "replaces", label: "Replaces" }, { key: "risk", label: "Risk" }]} />
      </Card>
    </>
  );
}

function Contracts({ team, season, cap }: { team: number; season: number; cap: number }) {
  const q = useQuery(`select b.*, n.player from salary.book b left join web.player_names n using (person_id)
                      where b.team_id = ${team} and b.season between ${season} and ${season + 4}`);
  const kindOf = (r: Row) => r.kind === "two-way" ? "two-way" : r.kind === "unknown" || r.salary == null ? "salary unknown"
    : r.option ? `${r.option} option` : r.length_known === false ? "length unknown" : "guaranteed";
  const seasons = Array.from({ length: 5 }, (_, i) => season + i);
  const byP = new Map<string, Row[]>();
  q.rows.forEach((r) => { const k = String(r.person_id ?? r.player); byP.set(k, [...(byP.get(k) ?? []), r]); });
  const players = [...byP.entries()].sort((a, b) => (b[1].find((x) => x.season === season)?.salary ?? 0) - (a[1].find((x) => x.season === season)?.salary ?? 0));
  const committed = seasons.map((s) => ({ s, total: q.rows.filter((r) => Number(r.season) === s && r.salary).reduce((a, r) => a + r.salary, 0) }));
  return (
    <div className="grid">
      <div className="c8">
        <Card title="Contract timeline" sub="Salary by season; colour: guaranteed, player option, team option, two-way, dead money, length or salary unknown.">
          <div className="table-wrap">
            <table className="data heatgrid">
              <thead><tr><th>Player</th>{seasons.map((s) => <th key={s} className="c">{seasonLabel(s)}</th>)}</tr></thead>
              <tbody>
                {players.map(([k, rs]) => (
                  <tr key={k}>
                    <td>{rs[0].person_id ? <Link to={`/player/${rs[0].person_id}`}>{rs[0].player}</Link> : rs[0].player}</td>
                    {seasons.map((s) => {
                      const r = rs.find((x) => Number(x.season) === s);
                      if (!r) return <td key={s} className="c muted">–</td>;
                      const kind = kindOf(r);
                      return <td key={s} className="c" title={kind}><span className="gcell" style={{ background: `color-mix(in srgb, var(${KIND_COLORS[kind] ?? "--axis"}) 55%, transparent)` }}>{r.salary ? M(r.salary) : "?"}</span></td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
      <div className="c4">
        <Card title="Committed by season" sub={`Against the cap, projected +7% a year from ${M(cap)}.`}>
          <HeatGrid rowHead="Season" rowWidth={90} cols={[{ key: "t", label: "Committed" }, { key: "c", label: "Cap" }]}
            rows={committed.map((c, i) => ({ key: String(c.s), label: seasonLabel(c.s), cells: { t: { text: M(c.total) }, c: { text: M(cap * 1.07 ** i), muted: true } } }))} />
        </Card>
      </div>
    </div>
  );
}

function FreeAgency({ team, names }: { team: number; names: ReturnType<typeof useTeamNames> }) {
  const cls = useQuery(`select * from market.fa_class`);
  const fo = useQuery(`select * from market.fa_options where team_id = ${team} order by need, rank`);
  const t = useQuery(`select next_space, next_space_best, next_committed from market.teams where team_id = ${team}`).rows[0];
  const scatter = useMemo(() => cls.rows.length ? (width: number) => ({
    width, height: 420, x: { label: "projected impact next season (per 100)", grid: true }, y: { label: "expected ask ($M)", grid: true },
    marks: [Plot.link(cls.rows, { x1: "impact_next", x2: "impact_next", y1: (d: any) => d.ask_p10 / 1e6, y2: (d: any) => d.ask_p90 / 1e6, stroke: css("--grid") }),
            Plot.dot(cls.rows, { x: "impact_next", y: (d: any) => d.ask / 1e6, r: (d: any) => Math.max(3, Math.min(9, (d.proj_poss ?? 0) / 900)),
              fill: (d: any) => css(d.fa_type === "UFA" ? "--s1" : d.fa_type === "RFA" ? "--s3" : d.fa_type?.startsWith("player") ? "--s2" : "--s7"), fillOpacity: 0.85 }),
            Plot.tip(cls.rows, Plot.pointer({ x: "impact_next", y: (d: any) => d.ask / 1e6, title: (d: any) => `${d.player} (${d.fa_type}, ${Math.round(d.age)})\nimpact ${fmt.signed(d.impact_next, 1)} · ask ${M(d.ask)} (${M(d.ask_p10)}–${M(d.ask_p90)})` }))],
  }) : undefined, [cls.rows]);
  return (
    <>
      {t && <div className="tiles">
        <Tile label="Cap space next summer" value={M(t.next_space)} delta={`${M(t.next_space_best)} if options are declined`} />
        <Tile label="Committed next season" value={M(t.next_committed)} />
      </div>}
      <div className="grid">
        <div className="c7">{scatter && <Figure title="The free-agent class" subtitle="Blue UFA, green RFA, orange player option, purple team option; size: projected possessions; whiskers: the ask's range." plot={scatter} />}</div>
        <div className="c5">
          <Card title={`Fits for the ${names.get(team)?.name ?? ""}`}>
            <DataTable rows={fo.rows} cols={[{ key: "need", label: "Need" }, { key: "player", label: "Player", fmt: (v, r) => <Link to={`/player/${r.person_id}`}>{v}</Link> },
              { key: "wins_added", label: "Wins", num: true, fmt: (v) => fmt.signed(v, 1) }, { key: "ask", label: "Ask", num: true, fmt: M },
              { key: "fits_space", label: "Fits", fmt: (v, r) => (v ? "yes" : r.fits_space_best ? "if options declined" : "needs room") }]} />
          </Card>
        </div>
      </div>
    </>
  );
}

function Pricing({ team, names }: { team: number; names: ReturnType<typeof useTeamNames> }) {
  const q = useQuery(`select * from market.players where salary_known and contract_kind = 'contract' and war is not null`);
  const rows = q.rows.map((r): Row => ({ ...r, mine: Number(r.team_id) === team }));
  const top = Math.max(1, ...rows.map((r) => Math.max(r.salary ?? 0, r.fair_salary ?? 0))) / 1e6;
  const scatter = useMemo(() => rows.length ? (width: number) => ({
    width, height: 460, x: { label: "fair salary ($M)", grid: true, domain: [0, top] }, y: { label: "salary ($M)", grid: true, domain: [0, top] },
    marks: [Plot.line([[0, 0], [top, top]], { stroke: css("--axis"), strokeDasharray: "4 4" }),
            Plot.dot(rows.filter((r) => !r.mine), { x: (d: any) => d.fair_salary / 1e6, y: (d: any) => d.salary / 1e6, r: 3.5, fill: (d: any) => css(RISK[d.risk] ?? "--axis"), fillOpacity: 0.3 }),
            Plot.dot(rows.filter((r) => r.mine), { x: (d: any) => d.fair_salary / 1e6, y: (d: any) => d.salary / 1e6, r: 6.5, fill: (d: any) => css(RISK[d.risk] ?? "--axis"), stroke: css("--text"), strokeWidth: 1.5 }),
            Plot.tip(rows, Plot.pointer({ x: (d: any) => d.fair_salary / 1e6, y: (d: any) => d.salary / 1e6, title: (d: any) => `${d.player} (${names.get(Number(d.team_id))?.abbrev ?? ""})\nsalary ${M(d.salary)} · fair ${M(d.fair_salary)}\nWAR ${d.war?.toFixed(1)} · risk ${d.risk}` }))],
  }) : undefined, [rows, top, names]);
  return (
    <>
      {scatter && <Figure title="Salary against fair salary" subtitle={`Above the line: paid more than the production is worth. Outlined: the ${names.get(team)?.name ?? "selected team"}; colour: risk.`} plot={scatter} />}
      <Card title="Biggest gaps" className="flat">
        <DataTable rows={[...rows].sort((a, b) => (b.salary - b.fair_salary) - (a.salary - a.fair_salary)).slice(0, 25)} cols={[
          { key: "player", label: "Player", fmt: (v, r) => <Link to={`/player/${r.person_id}`}>{v}</Link> }, { key: "age", label: "Age", num: true, fmt: (v) => fmt.n(v) },
          { key: "salary", label: "Salary", num: true, fmt: M }, { key: "fair_salary", label: "Fair", num: true, fmt: M },
          { key: "war", label: "WAR", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "overpay_prob", label: "Overpay chance", num: true, fmt: (v) => fmt.pct(v) }, { key: "risk", label: "Risk" }]} />
      </Card>
    </>
  );
}
