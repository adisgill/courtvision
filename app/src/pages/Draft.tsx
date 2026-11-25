// The draft: this season's college players rated on what college seasons have said about NBA
// careers, a mock first round matched to team needs (with a lottery you can re-draw), the
// projected order, and the model's record on past drafts against the order teams picked in.
import * as Plot from "@observablehq/plot";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Row, useQuery } from "../db";
import { css, fmt } from "../lib/format";
import { DIMS, applyTrades, bestFill, drawLottery, height, pct1, posGroup, runMock } from "../lib/draft";
import { useTeamNames } from "../lib/nba";
import { Figure } from "../ui/Figure";
import { HeatGrid } from "../ui/grids";
import { Card, DataTable, Loading, PageHead, ProbBar, Seg, Select, Tabs, Tile, useParam } from "../ui/kit";

const DIM_LABEL = Object.fromEntries(DIMS);

export default function Draft() {
  const info = useQuery(`select * from draft.info`).rows[0];
  const [tab, setTab] = useParam("tab", "board");
  const year = info ? Number(info.draft_year) : null;
  return (
    <>
      <PageHead eyebrow={year ? `Draft · ${year}` : "Draft"} title={year ? `The ${year} draft from college stats` : "The draft"}
        lead={info ? <>Every Division I player rated on what college seasons have said about NBA careers since 2007-08: expected WAR over his
          first four NBA seasons, and the chance he becomes a rotation player, a starter or a star. College numbers through {fmt.dayYear(info.stats_as_of)}
          {" "}(early in the season they lean on last year and on recruiting). College players only: international and G League prospects aren't covered.</> : null} />
      {info && <Headline info={info} />}
      <Tabs tabs={[["board", "Big board"], ["mock", "Mock draft"], ["order", "Draft order"], ["record", "Track record"]]} value={tab} onChange={setTab} />
      {tab === "board" && <Board />}
      {tab === "mock" && info && <Mock info={info} />}
      {tab === "order" && info && <Order info={info} />}
      {tab === "record" && info && <Record info={info} />}
    </>
  );
}

function Headline({ info }: { info: Row }) {
  const top = useQuery(`select player, team, ev from draft.prospects order by rank limit 1`).rows[0];
  const worst = useQuery(`select o.team_id, o.p_1, o.exp_wins from draft.order o order by seed limit 1`).rows[0];
  const names = useTeamNames(Number(info.nba_season));
  return (
    <div className="tiles">
      <Tile label="Top of the board" value={top ? top.player : "–"} delta={top ? `${top.team} · ${fmt.n(top.ev, 1)} expected WAR` : undefined} />
      <Tile label="Ranking past drafts" value={`${fmt.n(info.spearman_model, 2)} vs ${fmt.n(info.spearman_pick, 2)}`}
        delta={`rank correlation with careers, model vs the teams' order (${info.test_drafts})`} />
      <Tile label="Best odds at #1" value={worst ? names.get(Number(worst.team_id))?.team ?? "–" : "–"}
        delta={worst ? `${fmt.pct(worst.p_1, 0)} · projected ${fmt.n(worst.exp_wins, 0)} wins` : undefined} />
      <Tile label="Players linked to NBA careers" value={fmt.n(Number(info.linked_pick) + Number(info.linked_name))}
        delta={`${fmt.n(info.linked_pick)} by draft pick, ${fmt.n(info.linked_name)} by name`} />
    </div>
  );
}

// ------------------------------------------------------------------- big board

function Board() {
  const q = useQuery(`select * from draft.prospects order by rank`);
  const [pos, setPos] = useParam("pos", "All");
  const [cls, setCls] = useParam("class", "All");
  const rows = q.rows.filter((r) => (pos === "All" || posGroup(r.role) === pos) && (cls === "All" || r.class === cls));
  const shown = rows.slice(0, 100);
  const scatter = useMemo(() => shown.length ? (width: number) => {
    const top = new Set(shown.slice(0, 14).map((r) => r.pid));
    return {
      width, height: 420, x: { label: "age at the draft", grid: true, reverse: true }, y: { label: "expected WAR, first four seasons", grid: true },
      color: { domain: ["Guards", "Wings", "Bigs"], range: [css("--s1"), css("--s2"), css("--s3")], legend: true },
      marks: [
        Plot.dot(shown, { x: "age", y: "ev", r: (d: any) => 3 + 9 * d.p_star ** 0.5, fill: (d: any) => posGroup(d.role), fillOpacity: 0.85, stroke: css("--surface") }),
        Plot.text(shown.filter((r) => top.has(r.pid)), { x: "age", y: "ev", text: (d: any) => String(d.player).split(" ").slice(-1)[0], dy: -12, fontSize: 10.5, fill: css("--text-2") }),
        Plot.tip(shown, Plot.pointer({ x: "age", y: "ev", title: (d: any) => `#${d.rank} ${d.player} (${d.team}, ${d.class})\n${fmt.n(d.ev, 1)} expected WAR · rotation ${fmt.pct(d.p_rotation)} · star ${fmt.pct(d.p_star, 1)}\n${fmt.n(d.pts, 1)} pts · ${fmt.n(d.treb, 1)} reb · ${fmt.n(d.ast, 1)} ast in ${d.gp} games` })),
      ],
    };
  } : undefined, [shown]);
  return (
    <>
      <div className="filters">
        <Seg label="Position" options={["All", "Guards", "Wings", "Bigs"].map((v) => [v, v] as [string, string])} value={pos} onChange={setPos} />
        <Seg label="Class" options={[["All", "All"], ["Fr", "Freshmen"], ["So", "Sophomores"], ["Jr", "Juniors"], ["Sr", "Seniors"]]} value={cls} onChange={setCls} />
      </div>
      <Loading state={q} />
      {scatter && <Figure title="Value against age" subtitle="Younger is to the right. Dot size: the chance of a star (20+ WAR in four seasons). Hover for the line."
        plot={scatter} table={{ rows: shown, cols: [["rank", "#"], ["player", "Player"], ["team", "School"], ["age", "Age", (v) => fmt.n(v, 1)], ["ev", "Expected WAR", (v) => fmt.n(v, 2)]] }} />}
      <Card title="The board" sub="Expected WAR over the first four NBA seasons (0 for players who never get there), from the walk-forward-tested model. Click a player for his projection, comparables and fits." className="flat">
        <DataTable rows={shown} rank={false} cols={[
          { key: "rank", label: "#", num: true },
          { key: "player", label: "Player", fmt: (v, r) => <Link to={`/prospect/${r.pid}`}><b>{v}</b></Link> },
          { key: "team", label: "School" }, { key: "class", label: "Class", center: true }, { key: "role", label: "Role" },
          { key: "age", label: "Age", num: true, fmt: (v, r) => <span title={r.dob_known ? "" : "birth date estimated from class"}>{fmt.n(v, 1)}{r.dob_known ? "" : "*"}</span> },
          { key: "height_in", label: "Ht", num: true, fmt: height },
          { key: "rec_rank", label: "Rec", num: true, title: "Recruiting score out of high school (100 = the top recruit)", fmt: (v) => fmt.n(v, 1) },
          { key: "gp", label: "G", num: true },
          { key: "pts", label: "Pts", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "treb", label: "Reb", num: true, fmt: (v) => fmt.n(v, 1) },
          { key: "ast", label: "Ast", num: true, fmt: (v) => fmt.n(v, 1) },
          { key: "ts_raw", label: "TS%", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "fg3_pct_raw", label: "3P%", num: true, fmt: pct1 },
          { key: "bpm_raw", label: "BPM", num: true, fmt: (v) => fmt.signed(v, 1) },
          { key: "ev", label: "Exp. WAR", num: true, heat: "pos", fmt: (v) => fmt.n(v, 1) },
          { key: "p_rotation", label: "Rotation", num: true, fmt: (v) => <ProbBar p={v} width={44} /> },
          { key: "p_star", label: "Star", num: true, fmt: (v) => <ProbBar p={v} width={44} /> },
        ]} />
        <p className="xs muted">* birth date not on record; age estimated from class. Rec: recruiting score out of high school. Stats are this season's (raw); the model blends them with last season and recruiting.</p>
      </Card>
    </>
  );
}

// ------------------------------------------------------------------ mock draft

function Mock({ info }: { info: Row }) {
  const order = useQuery(`select * from draft.order order by seed`);
  const board = useQuery(`select * from draft.prospects order by rank limit 80`);
  const fitQ = useQuery(`select team_id, pid, fit from draft.fit`);
  const teams = useQuery(`select * from market.teams`);
  const odds = useQuery(`select per_1000 from draft.lottery order by seed`);
  const names = useTeamNames(Number(info.nba_season));
  const [draw, setDraw] = useState<number[] | null>(null);
  const [mode, setMode] = useParam("order", "Projected");
  const fit = useMemo(() => new Map(fitQ.rows.map((r) => [`${r.team_id}:${r.pid}`, Number(r.fit)])), [fitQ.rows]);
  const teamRow = useMemo(() => new Map(teams.rows.map((r) => [Number(r.team_id), r])), [teams.rows]);
  const seeded = order.rows.map((r) => Number(r.team_id));
  const slots = mode === "Lottery draw" && draw ? draw : seeded;
  const owners = applyTrades(slots, order.rows);
  const picks = useMemo(() => board.rows.length && fit.size ? runMock(owners, slots, board.rows, fit, Number(info.fit_weight)) : [],
    [board.rows, fit, owners.join(","), slots.join(","), info.fit_weight]);
  const redraw = () => { setDraw(drawLottery(seeded, odds.rows.map((r) => Number(r.per_1000)))); setMode("Lottery draw"); };
  const nm = (t: number) => names.get(t)?.team ?? String(t);
  return (
    <>
      <div className="filters">
        <Seg label="Order" options={[["Projected", "Projected order"], ["Lottery draw", "Lottery draw"]]} value={mode} onChange={(v) => { if (v === "Lottery draw" && !draw) redraw(); else setMode(v); }} />
        <button className="btn" onClick={redraw}>Draw the lottery{draw ? " again" : ""}</button>
        <span className="xs muted" style={{ maxWidth: 520 }}>Projected: the lottery goes to form (worst record picks first). A draw uses the NBA's odds (14% each for the three worst, down to 0.5%).
          {Number(info.trades) === 0 ? " No traded picks recorded yet: each pick sits with the team whose record earns it." : ` ${info.trades} traded picks applied.`}</span>
      </div>
      <Loading state={board} />
      <div className="mockgrid">
        {picks.map((m) => {
          const fills = bestFill(teamRow.get(m.team), m.p);
          const moved = draw && mode === "Lottery draw" ? seeded.indexOf(m.original) + 1 : null;
          return (
            <div key={m.pick} className="mockpick">
              <div className="num pickno">{m.pick}</div>
              <div style={{ minWidth: 0 }}>
                <div className="xs muted">
                  <Link to={`/team/${m.team}`}>{nm(m.team)}</Link>
                  {m.team !== m.original && <> · via {nm(m.original)}</>}
                  {moved && moved !== m.pick && <> · {moved > m.pick ? `up from ${moved}` : `down from ${moved}`}</>}
                </div>
                <div><Link to={`/prospect/${m.p.pid}`}><b>{m.p.player}</b></Link> <span className="muted small">{m.p.team} · {m.p.class} · {height(m.p.height_in)}</span></div>
                <div className="xs ink2">
                  {fmt.n(m.p.ev, 1)} exp. WAR
                  {m.bpa.pid === m.p.pid ? " · best available" : <> · fits {fills ? DIM_LABEL[fills].toLowerCase() : "the roster"}; best available was <Link to={`/prospect/${m.bpa.pid}`}>{m.bpa.player}</Link> ({fmt.n(m.bpa.ev, 1)})</>}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <p className="xs muted">The rule at every pick: the prospect with the most expected WAR after fit, where fit (his strengths on the dimensions the team rates below average)
        moves his value by up to {Math.round(250 * Number(info.fit_weight))}% either way. So the top picks go to the best players, and need decides between prospects close in value.</p>
    </>
  );
}

// ------------------------------------------------------------------ draft order

function Order({ info }: { info: Row }) {
  const order = useQuery(`select * from draft.order order by seed`);
  const probs = useQuery(`select * from draft.order_probs`);
  const names = useTeamNames(Number(info.nba_season));
  const P = new Map(probs.rows.map((r) => [`${r.team_id}:${r.slot}`, Number(r.prob)]));
  const slots = Array.from({ length: 14 }, (_, i) => i + 1);
  return (
    <>
      <Loading state={order} />
      <Card title="Where each team is likely to pick" sub={`${fmt.n(info.sims)} simulations of the rest of the season (team ratings from this season's margins, pulled toward last season's, with their uncertainty), the play-in, then the lottery.`}>
        <HeatGrid rowHead="Team" rowWidth={170} compact
          cols={[{ key: "w", label: "Wins now" }, { key: "ew", label: "Proj. wins" }, { key: "po", label: "Playoffs" }, { key: "ep", label: "Exp. pick" },
            ...slots.map((s) => ({ key: String(s), label: `#${s}` }))]}
          rows={order.rows.map((r) => ({
            key: String(r.team_id), label: names.get(Number(r.team_id))?.team ?? r.abbrev, to: `/team/${r.team_id}`,
            cells: {
              w: { text: fmt.n(r.wins_now) }, ew: { text: fmt.n(r.exp_wins, 0) }, po: { text: fmt.pct(r.p_playoffs) }, ep: { text: fmt.n(r.exp_pick, 1) },
              ...Object.fromEntries(slots.map((s) => {
                const p = P.get(`${r.team_id}:${s}`) ?? 0;
                return [String(s), { text: p >= 0.005 ? fmt.pct(p) : "", z: p > 0 ? Math.min(1, p / 0.35) : null, tip: <><b>{names.get(Number(r.team_id))?.team}</b><div>{fmt.pct(p, 1)} to pick #{s}</div></> }];
              })),
            },
          }))} />
        <p className="xs muted">Picks 15-30 follow the record of the playoff teams. Traded picks: {Number(info.trades) === 0 ? "none recorded yet, so every pick shows under the team whose record earns it" : `${info.trades} recorded`}.</p>
      </Card>
    </>
  );
}

// ---------------------------------------------------------------- track record

function Record({ info }: { info: Row }) {
  const ev = useQuery(`select * from draft.eval order by draft`);
  const years = ev.rows.map((r) => Number(r.draft));
  const [yp, setYear] = useParam("year", "");
  const year = Number(yp) || Number(ev.rows.filter((r) => r.complete).at(-1)?.draft ?? 0);
  const hist = useQuery(year ? `select * from draft.history where draft = ${year} order by pick` : null);
  const early = ev.rows.filter((r) => !r.complete).map((r) => Number(r.draft));
  const lines = useMemo(() => ev.rows.length ? (width: number) => {
    const long = ev.rows.flatMap((r) => [["Model", r.spearman_model], ["Teams' order", r.spearman_pick], ["Both (average rank)", r.spearman_blend]]
      .map(([k, v]) => ({ draft: Number(r.draft), k, v: Number(v), complete: r.complete })));
    return {
      width, height: 300, x: { label: "draft", tickFormat: "d", grid: false }, y: { label: "rank correlation with the outcome", grid: true, domain: [-0.1, 0.8] },
      color: { domain: ["Model", "Teams' order", "Both (average rank)"], range: [css("--s1"), css("--s2"), css("--s3")], legend: true },
      marks: [
        ...(early.length ? [Plot.rectX([{ a: Math.min(...early) - 0.5, b: Math.max(...years) + 0.5 }], { x1: "a", x2: "b", fill: css("--surface-2") })] : []),
        Plot.ruleY([0], { stroke: css("--axis") }),
        Plot.line(long, { x: "draft", y: "v", stroke: "k", strokeWidth: 2 }),
        Plot.dot(long, { x: "draft", y: "v", fill: "k", r: 3.5 }),
        Plot.tip(long, Plot.pointer({ x: "draft", y: "v", title: (d: any) => `${d.draft} · ${d.k}\n${fmt.n(d.v, 2)}${d.complete ? "" : " (careers still early)"}` })),
      ],
    };
  } : undefined, [ev.rows]);
  const scatter = useMemo(() => hist.rows.length ? (width: number) => ({
    width, height: 380, x: { label: "actual pick", grid: true }, y: { label: "model's rank among the drafted", grid: true, reverse: true },
    color: { type: "linear", scheme: "blues", domain: [0, 15], label: "WAR, first four seasons", legend: true },
    marks: [
      Plot.line([[1, 1], [60, 60]], { stroke: css("--axis"), strokeDasharray: "3,3" }),
      Plot.dot(hist.rows, { x: "pick", y: "model_rank_drafted", r: 5.5, fill: (d: any) => Math.max(0, d.war), stroke: css("--axis"), strokeWidth: 0.5 }),
      Plot.text(hist.rows.filter((r) => r.war >= 8 || r.pick <= 5), { x: "pick", y: "model_rank_drafted", text: (d: any) => String(d.player).split(" ").slice(-1)[0], dy: -10, fontSize: 10, fill: css("--text-2") }),
      Plot.tip(hist.rows, Plot.pointer({ x: "pick", y: "model_rank_drafted", title: (d: any) => `${d.player} (${d.team})\npick ${d.pick} · model #${d.model_rank_drafted}\n${fmt.n(d.war, 1)} WAR in ${d.complete ? "four seasons" : "his seasons so far"}` })),
    ],
  }) : undefined, [hist.rows]);
  const steals = hist.rows.filter((r) => r.pick - r.model_rank_drafted >= 10).sort((a, b) => b.war - a.war).slice(0, 6);
  return (
    <>
      <div className="tiles">
        <Tile label="Model" value={fmt.n(info.spearman_model, 2)} delta={`rank correlation with careers, drafts ${info.test_drafts}`} />
        <Tile label="Teams' actual order" value={fmt.n(info.spearman_pick, 2)} delta="the same players, ranked by pick" />
        <Tile label="Both together" value={fmt.n(info.spearman_blend, 2)} delta="average of the two ranks" />
        <Tile label="Rotation players in the top 10" value={`${fmt.n(info.top10_model_hits, 1)} vs ${fmt.n(info.top10_pick_hits, 1)}`} delta="per draft: the model's top 10 vs the first 10 picks" />
      </div>
      {lines && <Figure title="How well each ranking matched the careers that followed" subtitle="Each draft predicted only from classes whose four NBA seasons were over before it (walk-forward). Shaded: drafts whose careers are still early, so the outcome is partial."
        plot={lines} table={{ rows: ev.rows, cols: [["draft", "Draft"], ["spearman_model", "Model", (v) => fmt.n(v, 3)], ["spearman_pick", "Teams' order", (v) => fmt.n(v, 3)], ["spearman_blend", "Both", (v) => fmt.n(v, 3)], ["drafted", "Players"]] }} />}
      <Card title="The honest reading" className="flat">
        <p className="small ink2">NBA teams draft better than college numbers alone: their order matches careers more closely than the model in most drafts. Teams see what the box score can't
          (workouts, medicals, film, character), so a stats model is a second opinion, not a replacement. Where the model earns its place is the players teams pass on: it ranks the whole country,
          not just the 60 who get picked, and it flags the late picks and undrafted players whose numbers say more than their reputation.</p>
      </Card>
      <div className="filters"><Select label="Draft" value={String(year)} onChange={setYear} options={years.map((y) => [String(y), String(y)])} /></div>
      <div className="grid">
        <div className="c8">{scatter && <Figure title={`${year}: the model's order against the teams'`} subtitle="Below the dashed line: players the model liked more than the teams did. Colour: WAR over the first four seasons."
          plot={scatter} table={{ rows: hist.rows, cols: [["pick", "Pick"], ["player", "Player"], ["team", "School"], ["model_rank_drafted", "Model rank"], ["war", "WAR", (v) => fmt.n(v, 1)]] }} />}</div>
        <div className="c4">
          <Card title="Taken later than the model said" sub="10+ picks after the model's rank, by WAR.">
            {steals.length ? steals.map((r) => (
              <div key={r.pid} className="small" style={{ padding: "4px 0", borderBottom: "1px solid var(--rule)" }}>
                {r.person_id ? <Link to={`/player/${r.person_id}`}><b>{r.player}</b></Link> : <b>{r.player}</b>} <span className="muted">{r.team}</span>
                <div className="xs ink2">pick {r.pick} · model #{r.model_rank_drafted} · {fmt.n(r.war, 1)} WAR</div>
              </div>
            )) : <p className="small muted">None in this draft.</p>}
          </Card>
        </div>
      </div>
      <Card title={`Every ${year} pick from college`} className="flat">
        <DataTable rows={hist.rows} cols={[
          { key: "pick", label: "Pick", num: true },
          { key: "player", label: "Player", fmt: (v, r) => r.person_id ? <Link to={`/player/${r.person_id}`}>{v}</Link> : v },
          { key: "team", label: "School" }, { key: "class", label: "Class", center: true },
          { key: "model_rank_drafted", label: "Model rank", num: true }, { key: "ev", label: "Exp. WAR", num: true, fmt: (v) => fmt.n(v, 1) },
          { key: "war", label: "Actual WAR", num: true, heat: "pos", fmt: (v, r) => `${fmt.n(v, 1)}${r.complete ? "" : "*"}` },
        ]} />
        <p className="xs muted">* careers still under four seasons. WAR: wins above a replacement-level player (−2.0 per 100 possessions), from per-possession impact.</p>
      </Card>
    </>
  );
}
