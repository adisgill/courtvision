// Every logged prediction scored against what happened: the whole ledger, and one night in
// detail (games, player stat lines, the biggest misses).
import * as Plot from "@observablehq/plot";
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Row, useQuery } from "../db";
import { css, fmt } from "../lib/format";
import { CURRENT_SEASON, rangeText, STATS, useTeamNames } from "../lib/nba";
import { Figure } from "../ui/Figure";
import { Card, DataTable, Loading, PageHead, Seg, Select, Tile, useParam } from "../ui/kit";
import { tipDay, tipTime } from "./Home";

export default function Scorecard() {
  const names = useTeamNames(CURRENT_SEASON);
  const days = useQuery(`select day, count(*) n, sum(winner_right::int) won, avg(brier) brier, avg(abs(error)) mae, avg(margin_inside_80::int) in80
                         from forecast.scorecard_games group by 1 order by 1 desc`);
  const tot = useQuery(`select count(*) n, avg(winner_right::int) acc, avg(brier) brier, avg(abs(error)) mae, avg(margin_inside_80::int) in80
                        from forecast.scorecard_games`);
  const ptot = useQuery(`select avg(inside::int) inside, avg((actual < lo)::int) below, avg((actual > hi)::int) above, count(distinct game_id || person_id) pg
                         from forecast.scorecard_players where played`);
  const dayList = days.rows.map((r) => new Date(r.day).toISOString().slice(0, 10));
  const [day, setDay] = useParam("day", dayList[0] ?? "");
  const [stat, setStat] = useParam("stat", "pts");
  const T = tot.rows[0], P = ptot.rows[0];
  const ledgerPlot = useMemo(() => days.rows.length ? (width: number) => {
    const rows = [...days.rows].reverse().map((r) => ({ ...r, acc: r.won / r.n }));
    return { width, height: 220, x: { label: null, type: "band" as const, tickFormat: (d: any) => fmt.day(d) },
      y: { label: "winners picked", domain: [0, 1], tickFormat: (v: number) => fmt.pct(v), grid: true },
      marks: [Plot.ruleY([0.658], { stroke: css("--axis"), strokeDasharray: "4 4" }),
              Plot.barY(rows, { x: "day", y: "acc", fill: css("--s1"), rx: 3, insetLeft: 4, insetRight: 4 }),
              Plot.text(rows, { x: "day", y: "acc", text: (d: any) => `${d.won}/${d.n}`, dy: -8, fill: css("--text-2") }),
              Plot.tip(rows, Plot.pointerX({ x: "day", y: "acc", title: (d: any) => `${fmt.dayYear(d.day)}: ${d.won} of ${d.n} winners · Brier ${d.brier.toFixed(3)} · margin miss ${d.mae.toFixed(1)} · ${fmt.pct(d.in80)} inside the range` }))] };
  } : undefined, [days.rows]);
  return (
    <>
      <PageHead eyebrow="Scorecard" title="Every prediction, scored"
        lead="Every forecast run is appended to a ledger that's never rewritten; each game is graded on the last prediction logged before it tipped off. One night is a handful of games: compare with the walk-forward averages, not with zero." />
      <div className="tiles">
        <Tile label="Winners picked" value={T ? fmt.pct(T.acc) : "–"} delta={`${fmt.n(T?.n)} games · backtest 65.8%`} />
        <Tile label="Brier score" value={fmt.n(T?.brier, 3)} delta="backtest 0.214 · coin flip 0.250" />
        <Tile label="Average margin miss" value={T ? `${fmt.n(T.mae, 1)} pts` : "–"} delta="backtest 10.9" />
        <Tile label="Final margin inside the 80% range" value={fmt.pct(T?.in80)} delta="target 80%" />
        <Tile label="Player stats inside the 80% range" value={fmt.pct(P?.inside)} delta={P ? `${fmt.pct(P.below)} below · ${fmt.pct(P.above)} above` : undefined} />
      </div>

      <section className="section">
        <h2>Night by night</h2>
        <div className="grid">
          <div className="c7">{ledgerPlot && <Figure title="Winners picked each night" subtitle="Dashed: the walk-forward backtest's 65.8%." plot={ledgerPlot} source="prediction ledger" />}</div>
          <div className="c5">
            <Card title="The ledger">
              <DataTable rows={days.rows} onRow={(r) => setDay(new Date(r.day).toISOString().slice(0, 10))} cols={[
                { key: "day", label: "Night", fmt: (v) => fmt.day(v) },
                { key: "won", label: "Winners", num: true, fmt: (v, r) => `${v}/${r.n}` },
                { key: "brier", label: "Brier", num: true, fmt: (v) => fmt.n(v, 3), heat: "neg" },
                { key: "mae", label: "Miss", num: true, fmt: (v) => fmt.n(v, 1), heat: "neg" },
                { key: "in80", label: "In range", num: true, fmt: (v) => fmt.pct(v) },
              ]} />
            </Card>
          </div>
        </div>
      </section>

      {day && <Night day={day} setDay={setDay} dayList={dayList} stat={stat} setStat={setStat} names={names} />}
    </>
  );
}

function Night({ day, setDay, dayList, stat, setStat, names }: { day: string; setDay: (v: string) => void; dayList: string[]; stat: string; setStat: (v: string) => void;
  names: ReturnType<typeof useTeamNames> }) {
  const g = useQuery(`select * from forecast.scorecard_games where day = '${day}' order by tipoff, game_id`);
  const p = useQuery(`select s.*, n.player from forecast.scorecard_players s join web.player_names n using (person_id) where s.day = '${day}'`);
  const label = Object.fromEntries(STATS)[stat];
  const games = g.rows.map((r): Row => ({ ...r, label: `${names.get(Number(r.away_team_id))?.abbrev} ${r.away_score}–${r.home_score} ${names.get(Number(r.home_team_id))?.abbrev}` }));
  const dumbbell = useMemo(() => games.length ? (width: number) => {
    const lim = Math.max(10, ...games.flatMap((r) => [Math.abs(r.pred_margin), Math.abs(r.actual_margin)])) * 1.15;
    return { width, height: 34 * games.length + 70, marginLeft: 120, x: { label: "home margin (right: home won)", domain: [-lim, lim], grid: true },
      y: { label: null, domain: games.map((r) => r.label) },
      marks: [Plot.ruleX([0], { stroke: css("--axis") }),
              Plot.link(games, { y: "label", x1: "pred_margin", x2: "actual_margin", stroke: css("--grid"), strokeWidth: 3 }),
              Plot.dot(games, { y: "label", x: "pred_margin", r: 6, stroke: css("--muted"), strokeWidth: 2, fill: css("--surface") }),
              Plot.dot(games, { y: "label", x: "actual_margin", r: 6, fill: (d: any) => (d.winner_right ? css("--pos") : css("--neg")) }),
              Plot.tip(games, Plot.pointerY({ y: "label", x: "actual_margin", title: (d: any) => `${d.label}\npredicted home ${fmt.signed(d.pred_margin, 1)} (${fmt.pct(d.home_win_prob)}) · final ${fmt.signed(d.actual_margin, 0)}\n${d.winner_right ? "pick right" : "pick wrong"} · ${d.margin_inside_80 ? "inside" : "outside"} the 80% range` }))] };
  } : undefined, [games]);
  const played = p.rows.filter((r) => r.played && r.stat === stat);
  const top = Math.max(1, ...played.map((r) => Math.max(r.hi, r.actual)));
  const scatter = useMemo(() => played.length ? (width: number) => ({
    width, height: 380, x: { label: `predicted ${label.toLowerCase()}`, grid: true }, y: { label: "actual", grid: true },
    marks: [Plot.line([[0, 0], [top, top]], { stroke: css("--axis"), strokeDasharray: "4 4" }),
            Plot.link(played, { x1: "lo", x2: "hi", y1: "actual", y2: "actual", stroke: (d: any) => (d.inside ? css("--pos") : css("--neg")), strokeOpacity: 0.25, strokeWidth: 2 }),
            Plot.dot(played, { x: "pred", y: "actual", r: 3.5, fill: (d: any) => (d.inside ? css("--pos") : css("--neg")), fillOpacity: 0.8 }),
            Plot.tip(played, Plot.pointer({ x: "pred", y: "actual", title: (d: any) => `${d.player}: predicted ${d.pred.toFixed(1)} (${rangeText(d.lo, d.hi)}), got ${d.actual} in ${Math.round(d.minutes)} min` }))],
  }) : undefined, [played, label]);
  const byStat = STATS.map(([k, l]) => {
    const x = p.rows.filter((r) => r.played && r.stat === k);
    return { stat: l, n: x.length, inside: x.reduce((a, r) => a + (r.inside ? 1 : 0), 0) / (x.length || 1),
             mae: x.reduce((a, r) => a + Math.abs(r.error), 0) / (x.length || 1), avg: x.filter((r) => r.avg_error != null).reduce((a, r) => a + Math.abs(r.avg_error), 0) / (x.filter((r) => r.avg_error != null).length || 1) };
  });
  const misses = [...played].sort((a, b) => Math.abs(b.error) - Math.abs(a.error)).slice(0, 8);
  const dnp = p.rows.filter((r) => !r.played && r.stat === "pts").sort((a, b) => b.exp_minutes - a.exp_minutes);
  const first = g.rows[0];
  return (
    <section className="section">
      <h2>{tipDay(day + "T00:00:00Z")}</h2>
      <div className="filters">
        <Select label="Night" value={day} onChange={setDay} options={dayList.map((d) => [d, tipDay(d + "T00:00:00Z")])} />
        <div className="field">Stat<Seg options={STATS as [string, string][]} value={stat} onChange={setStat} label="Stat" /></div>
      </div>
      <Loading state={g} />
      {first && <p className="small ink2">Predictions logged {fmt.dayYear(first.predicted_at)} at {tipTime(first.predicted_at)} ET, from games through {fmt.dayYear(first.data_through)}
        {first.injury_report ? `, with the injury report of ${tipTime(first.injury_report)} ET` : ""}.</p>}
      <div className="grid">
        <div className="c6">{dumbbell && <Figure title="Games: predicted vs final margin" subtitle="Hollow: predicted margin. Solid: final margin, blue when the predicted winner won, red when not. The gap is the miss."
          plot={dumbbell} table={{ rows: games, cols: [["label", "Game"], ["pred_margin", "Predicted", (v) => fmt.signed(v, 1)], ["actual_margin", "Final", (v) => fmt.signed(v, 0)], ["winner_right", "Pick", (v) => (v ? "right" : "wrong")]] }} />}</div>
        <div className="c6">{scatter && <Figure title={`${label}: predicted vs the box score`} subtitle="Blue: inside the 80% range; red: outside. Lines: the range."
          plot={scatter} table={{ rows: played, cols: [["player", "Player"], ["pred", "Predicted", (v) => v.toFixed(1)], ["actual", "Actual"], ["inside", "Inside", (v) => (v ? "yes" : "no")]] }} />}</div>
        <div className="c6">
          <Card title="Stat lines inside the 80% range, by stat" sub="Model miss against simply using each player's season average before the game.">
            <DataTable rows={byStat} cols={[{ key: "stat", label: "Stat" }, { key: "n", label: "Lines", num: true },
              { key: "inside", label: "Inside", num: true, fmt: (v) => fmt.pct(v) },
              { key: "mae", label: "Model miss", num: true, fmt: (v) => fmt.n(v, 2) }, { key: "avg", label: "Season avg miss", num: true, fmt: (v) => fmt.n(v, 2) }]} />
          </Card>
        </div>
        <div className="c6">
          <Card title="Biggest misses">
            <DataTable rows={misses} cols={[{ key: "player", label: "Player", fmt: (v, r) => <Link to={`/player/${r.person_id}`}>{v}</Link> },
              { key: "pred", label: "Predicted", num: true, fmt: (v) => fmt.n(v, 1) }, { key: "lo", label: "Range", fmt: (_, r) => rangeText(r.lo, r.hi), sortable: false },
              { key: "actual", label: "Got", num: true }, { key: "minutes", label: "Min", num: true, fmt: (v) => fmt.n(v) }, { key: "exp_minutes", label: "Expected min", num: true, fmt: (v) => fmt.n(v) }]} />
            {dnp.length > 0 && <p className="xs muted" style={{ marginTop: 8 }}>Expected to play but didn't: {dnp.map((r) => `${r.player} (${Math.round(r.exp_minutes)} min)`).join(", ")}.</p>}
          </Card>
        </div>
      </div>
    </section>
  );
}
