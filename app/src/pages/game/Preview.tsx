// The preview of an upcoming game: the forecast, why, who's out, the scouting report, every
// expected player's stat line with its 80% range, and where each side has the edge.
import * as Plot from "@observablehq/plot";
import { useMemo, useState } from "react";
import { lit, Row, useQuery } from "../../db";
import { css, fmt } from "../../lib/format";
import { CURRENT_SEASON, DIMENSIONS, labelOf, ordered, rangeText, STATS, useTeamNames } from "../../lib/nba";
import { Radar } from "../../ui/charts";
import { Figure } from "../../ui/Figure";
import { DivBars, RatingGrid } from "../../ui/grids";
import { Card, Loading, PageHead, Seg, Tile } from "../../ui/kit";
import { marginRange, tipDay, tipTime } from "../Home";
import Scout from "./Scout";

const STATUS_ORDER: Record<string, number> = { Out: 0, Doubtful: 1, Questionable: 2, Probable: 3, Available: 4 };
const flipName = (s: string) => s.split(",", 2).map((v) => v.trim()).reverse().join(" ");

export default function Preview({ id }: { id: string }) {
  const names = useTeamNames(CURRENT_SEASON);
  const gq = useQuery(`select * from forecast.games where game_id = ${lit(id)}`);
  const g = gq.rows[0];
  const mm = useQuery(`select feature, beta from forecast.margin_model where model <> 'player intervals'`);
  const made = useQuery(`select (select max(predicted_at) from forecast.ledger_games) at,
                                (select max(game_date) from stg.games where home_score is not null) thru`);
  if (!g) return <Loading state={gq} />;
  const home = Number(g.home_team_id), away = Number(g.away_team_id);
  const H = names.get(home), A = names.get(away);
  const hn = H?.name ?? "Home", an = A?.name ?? "Away";
  const b = Object.fromEntries(mm.rows.map((r) => [r.feature, r.beta]));
  const parts: [string, number][] = [
    ["Team form", (b.form ?? 0) * g.form], ["Roster", (b.roster ?? 0) * g.roster],
    ["Rest", ["b2b_h", "b2b_a", "rest_diff"].reduce((a, f) => a + (b[f] ?? 0) * (g[f] ?? 0), 0)], ["Constant", b.intercept ?? 0],
  ];
  const fav = g.pred_margin >= 0 ? hn : an;
  const m = made.rows[0];
  return (
    <>
      <PageHead eyebrow={`${tipDay(g.tipoff)} · ${tipTime(g.tipoff)} ET · game preview`} title={<>{A?.team} <span className="muted">at</span> {H?.team}</>}
        lead={m ? <>Forecast made {fmt.dayYear(m.at)} at {tipTime(m.at)} ET, from games through {fmt.dayYear(m.thru)}. Updated every day: in the morning,
          after the nightly data refresh, and 40 minutes before the first tip-off.</> : null} />
      <div className="tiles">
        <Tile label="Favorite" value={fav} delta={`by ${fmt.n(Math.abs(g.pred_margin), 1)} points`} />
        <Tile label={`${hn} win chance`} value={fmt.pct(g.home_win_prob)} delta={`${an} ${fmt.pct(1 - g.home_win_prob)}`} />
        <Tile label="80% range, final margin" value={g.margin_lo != null ? marginRange(g.margin_lo, g.margin_hi, H?.abbrev ?? hn, A?.abbrev ?? an) : "–"}
          delta={g.range_sigma != null ? `spread ${fmt.n(g.range_sigma, 1)} pts, from this season's misses` : undefined} />
        <Tile label="Expected possessions" value={fmt.n(g.pace, 0)} delta={`rest: ${A?.abbrev} ${fmt.n(g.rest_a)} · ${H?.abbrev} ${fmt.n(g.rest_h)} days`} />
      </div>
      <CoachesLine home={home} away={away} hn={hn} an={an} />

      <section className="section">
        <h2>Why this margin</h2>
        <div className="grid">
          <div className="c7">
            <Card title={`${fav} by ${fmt.n(Math.abs(g.pred_margin), 1)}: what each ingredient adds`}
              sub={`Points toward ${hn} (blue) or ${an} (red), weighted as the walk-forward test found best.`}>
              <DivBars rows={parts.map(([l, v]) => ({ label: l, v, tip: <><b>{fmt.signed(v, 1)} points</b> toward {v >= 0 ? hn : an}</> }))}
                unit={`points: blue toward ${hn}, red toward ${an}`} />
            </Card>
          </div>
          <div className="c5">
            <p className="small ink2">Team form is opponent-adjusted net rating with recent games weighted more; the roster is last season's per-possession
              impact of tonight's expected players. Both include home court; rest adds back-to-backs and the rest difference.
              {g.b2b_h ? ` ${hn} are on the second night of a back-to-back.` : ""}{g.b2b_a ? ` ${an} are on the second night of a back-to-back.` : ""}</p>
            <p className="small ink2">The 80% range follows how far this season's games have landed from the forecast so far (the walk-forward test set it to
              update with each season's own misses); the win chance uses the backtest's spread, which won on log loss.</p>
          </div>
        </div>
      </section>

      <Availability id={id} home={home} away={away} hn={hn} an={an} />

      <section className="section">
        <h2>Scouting report</h2>
        <Scout home={home} away={away} hn={hn} an={an} gameId={id} />
      </section>

      <StatLines id={id} home={home} away={away} hn={hn} an={an} />
      <TeamEdges id={id} home={home} away={away} hn={hn} an={an} />
      <Players id={id} home={home} away={away} hn={hn} an={an} />
    </>
  );
}

type Sides = { home: number; away: number; hn: string; an: string };

function CoachesLine({ home, away, hn, an }: Sides) {
  const cur = useQuery(`select team_id, coach_id, coach_name, assumed from coach.current where team_id in (${home}, ${away})`);
  const ch = cur.rows.find((r) => Number(r.team_id) === home), ca = cur.rows.find((r) => Number(r.team_id) === away);
  const h2h = useQuery(ch && ca ? `select * from coach.h2h where coach_id = ${lit(ca.coach_id)} and opp_coach_id = ${lit(ch.coach_id)}` : null);
  if (!ch || !ca) return null;
  const r = h2h.rows[0];
  const last = (s: string) => s.split(" ").slice(-1)[0];
  return (
    <p className="small ink2" style={{ marginTop: -4 }}>
      Coaches: <b>{ca.coach_name}</b> ({an}) vs <b>{ch.coach_name}</b> ({hn}).{" "}
      {r ? <>Head to head: {last(ca.coach_name)} {r.wins}-{r.games - r.wins}, average margin {fmt.signed(r.margin, 1)}, tempo control {fmt.signed(r.tempo_control, 1)} possessions
        (history alone hasn't predicted the next meeting).</> : "No regular-season meetings between them on record."}
    </p>
  );
}

function Availability({ id, home, away, hn, an }: { id: string } & Sides) {
  const a = useQuery(`select * from forecast.availability where game_id = ${lit(id)}`);
  const rep = useQuery(`select max(report_time) t from injury.reports`);
  const ab = useQuery(`select absorb from forecast.minutes_share`);
  const absorb = ab.rows[0]?.absorb;
  return (
    <section className="section">
      <h2>Availability</h2>
      <p className="small ink2" style={{ marginTop: -6 }}>NBA official injury report of {rep.rows[0] ? `${fmt.dayYear(rep.rows[0].t)}, ${tipTime(rep.rows[0].t)} ET` : "–"}.
        Each absence's cost: his expected minutes had he played, times his per-possession impact.</p>
      <div className="grid">
        {([[away, an], [home, hn]] as [number, string][]).map(([team, name]) => {
          const r = a.rows.filter((x) => Number(x.team_id) === team && x.status)
            .map((x): Row => ({ ...x, cost: x.impact != null && x.exp_minutes != null ? (x.impact * x.exp_minutes) / 48 : null }))
            .sort((p, q) => (STATUS_ORDER[p.status] ?? 9) - (STATUS_ORDER[q.status] ?? 9) || (q.cost ?? 0) - (p.cost ?? 0));
          const lost = r.filter((x) => x.status === "Out" || x.status === "Doubtful").reduce((s, x) => s + (x.cost ?? 0), 0);
          const q = r.find((x) => x.status === "Questionable");
          return (
            <div key={team} className="c6">
              <Card title={name}>
                {!r.length && <p className="muted small">Nobody listed.</p>}
                <ul className="avail">
                  {r.map((x, i) => (
                    <li key={i}>
                      <span className={`pill ${x.status === "Out" || x.status === "Doubtful" ? "neg" : x.status === "Questionable" ? "warn" : ""}`}>{x.status}</span>
                      <b>{flipName(String(x.player))}</b>
                      {x.exp_minutes != null && x.impact != null && <span className="muted"> · {fmt.n(x.exp_minutes, 0)} min, impact {fmt.signed(x.impact, 1)}</span>}
                      <span className="muted"> — {String(x.reason ?? "").replace("Injury/Illness - ", "")}{x.carried ? " (carried from an earlier game)" : ""}</span>
                    </li>
                  ))}
                </ul>
                {lost !== 0 && <p className="xs muted">Ruled out, these players were expected to add {fmt.signed(lost, 1)} points per 100 possessions.
                  The forecast drops them and gives {absorb != null ? fmt.pct(absorb) : "part"} of their minutes to the teammates expected to play,
                  more to those with room for more, as absences were shared in past seasons.</p>}
                {q && q.p_play != null && <p className="xs muted">Questionable players count as playing {fmt.pct(q.p_play)} of the time: how often
                  Questionable players have played in the games scored so far, pulled toward even while there are few.</p>}
              </Card>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function StatLines({ id, home, away, hn, an }: { id: string } & Sides) {
  const [stat, setStat] = useState("pts");
  const q = useQuery(`select l.*, p.player, a.status from forecast.player_lines l join web.player_names p using (person_id)
                      left join forecast.availability a on a.game_id = l.game_id and a.person_id = l.person_id
                      where l.game_id = ${lit(id)} and l.exp_minutes >= 8`);
  const label = Object.fromEntries(STATS)[stat];
  const tag = (s: string | null) => (s === "Questionable" ? " (Q)" : s === "Probable" ? " (P)" : s === "Doubtful" ? " (D)" : "");
  const plots = useMemo(() => ([[away, an, "--s2"], [home, hn, "--s1"]] as [number, string, string][]).map(([team, name, color]) => {
    const rows = q.rows.filter((r) => Number(r.team_id) === team).map((r) => ({ who: r.player + tag(r.status), v: r[stat], lo: r[`${stat}_lo`], hi: r[`${stat}_hi`], min: r.exp_minutes }))
      .sort((a, b) => b.v - a.v);
    return { team, name, rows, plot: (width: number) => ({
      width, height: 26 * rows.length + 60, marginLeft: 150,
      x: { label: `${label}, expected with 80% range`, grid: true }, y: { label: null, domain: rows.map((r) => r.who) },
      marks: [
        Plot.link(rows, { y: "who", x1: "lo", x2: "hi", stroke: css(color), strokeWidth: 6, strokeOpacity: 0.4, strokeLinecap: "round" }),
        Plot.dot(rows, { y: "who", x: "v", r: 5, fill: css(color), stroke: css("--surface"), strokeWidth: 1.5 }),
        Plot.tip(rows, Plot.pointerY({ y: "who", x: "v", title: (d: any) => `${d.who}: ${d.v.toFixed(1)} (80%: ${rangeText(d.lo, d.hi)}), ${Math.round(d.min)} min` })),
      ],
    }) };
  }), [q.rows, stat, home, away, hn, an, label]);
  if (!q.rows.length) return null;
  return (
    <section className="section">
      <h2>Stat lines</h2>
      <div className="filters"><Seg options={STATS as [string, string][]} value={stat} onChange={setStat} label="Stat" /></div>
      <div className="grid">
        {plots.map((p) => (
          <div key={p.team} className="c6">
            <Figure title={p.name} subtitle="Dot: expected; bar: the range that held 80% of real outcomes in seasons the model never saw. Players expected to play 8+ minutes."
              plot={p.plot} table={{ rows: p.rows, cols: [["who", "Player"], ["v", label, (v) => v.toFixed(1)], ["lo", "Range", (v) => String(Math.ceil(v))], ["hi", "to", (v) => String(Math.floor(v))], ["min", "Minutes", (v) => String(Math.round(v))]] }} />
          </div>
        ))}
      </div>
      <p className="xs muted">From recent per-minute production (shrunk toward last season), minutes when he plays (with absent teammates' minutes shared out),
        and the game's expected pace. (Q) Questionable · (P) Probable: their lines are for if they play.</p>
    </section>
  );
}

function TeamEdges({ id, home, away, hn, an }: { id: string } & Sides) {
  const q = useQuery(`select * from forecast.team where game_id = ${lit(id)}`);
  if (!q.rows.length) return null;
  const byTeam = (t: number) => new Map(q.rows.filter((r) => Number(r.team_id) === t).map((r) => [r.measure, r]));
  const H = byTeam(home), A = byTeam(away);
  const edges = ordered([...H.keys()].filter((mm) => A.has(mm)).map((mm) => ({ measure: mm, v: H.get(mm)!.pred_rating - A.get(mm)!.pred_rating })));
  const axes = DIMENSIONS.map(([k, l]) => ({ key: k, label: l }));
  return (
    <section className="section">
      <h2>Where each side has the edge</h2>
      <div className="grid">
        {([[away, an, A, "var(--s2)"], [home, hn, H, "var(--s1)"]] as [number, string, Map<string, Row>, string][]).map(([t, name, M, color]) => (
          <div key={t} className="c3">
            <Card title={name} sub="Predicted for tonight vs season average (0-100).">
              <Radar axes={axes} size={300} series={[
                { name: "tonight", color, values: Object.fromEntries(DIMENSIONS.map(([k]) => [k, M.get(k)?.pred_rating ?? null])) },
                { name: "season average", color: "var(--muted)", values: Object.fromEntries(DIMENSIONS.map(([k]) => [k, M.get(k)?.season_avg ?? null])) },
              ]} />
            </Card>
          </div>
        ))}
        <div className="c6">
          <Card title="Predicted rating, home minus away" sub={`Blue: ${hn} better; red: ${an} better. Each prediction combines a team's recent form with what this opponent does to teams on that measure, and home court.`}>
            <DivBars rows={edges.map((e) => ({ label: labelOf(e.measure), v: e.v, text: fmt.signed(e.v, 1) }))} height={22} />
          </Card>
        </div>
      </div>
    </section>
  );
}

function Players({ id, home, away, hn, an }: { id: string } & Sides) {
  const q = useQuery(`select f.*, p.player from forecast.players f join web.player_names p using (person_id)
                      where f.game_id = ${lit(id)} order by f.exp_minutes desc`);
  if (!q.rows.length) return null;
  return (
    <section className="section">
      <h2>Players</h2>
      <div className="grid">
        {([[away, an], [home, hn]] as [number, string][]).map(([t, name]) => {
          const rows = q.rows.filter((r) => Number(r.team_id) === t && r.exp_minutes >= 4);
          const contrib = rows.filter((r) => r.impact != null).map((r): Row => ({ ...r, c: (r.impact * r.exp_minutes) / 48 }))
            .sort((a, b) => b.c - a.c).slice(0, 10);
          return (
            <div key={t} className="c6">
              <Card title={`${name}: expected minutes and ratings`} sub="Ratings: recent form shrunk toward his season level, plus the opponent's effect.">
                <RatingGrid rowHead="Player" rows={rows.map((r) => ({ key: String(r.person_id), label: `${r.player} (${Math.round(r.exp_minutes)} min)`, to: `/player/${r.person_id}`,
                  values: Object.fromEntries(DIMENSIONS.map(([k]) => [k, r[`${k}_rating`]])) }))} cols={DIMENSIONS} />
                <p className="fig-title" style={{ marginTop: 14 }}>Expected contribution to team net per 100</p>
                <DivBars rows={contrib.map((r) => ({ label: r.player, v: r.c, to: `/player/${r.person_id}`, text: fmt.signed(r.c, 2),
                  tip: <>impact {fmt.signed(r.impact, 1)} per 100 × {Math.round(r.exp_minutes)}/48 minutes</> }))} height={22} />
              </Card>
            </div>
          );
        })}
      </div>
    </section>
  );
}
