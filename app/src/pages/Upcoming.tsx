// Every team's next game, and how far to trust the forecasts: the walk-forward test of the
// game model, its calibration and the player stat lines.
import * as Plot from "@observablehq/plot";
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "../db";
import { css, fmt } from "../lib/format";
import { CURRENT_SEASON, STATS, useTeamNames } from "../lib/nba";
import { Figure } from "../ui/Figure";
import { Card, DataTable, Loading, PageHead } from "../ui/kit";
import { marginRange, tipDay, tipTime, todayET } from "./Home";

const MODELS = ["home court only", "team form", "form + roster", "+ rest", "+ coach", "+ matchups"];

export default function Upcoming() {
  const names = useTeamNames(CURRENT_SEASON);
  const q = useQuery(`select * from forecast.games where tipoff::date >= '${todayET()}' order by tipoff, game_id`);
  const made = useQuery(`select (select max(predicted_at) from forecast.ledger_games) at,
                                (select max(game_date) from stg.games where home_score is not null) thru`);
  const m = made.rows[0];
  const rows = q.rows.map((g) => {
    const h = names.get(Number(g.home_team_id)), a = names.get(Number(g.away_team_id));
    return { ...g, home: h?.name ?? "", away: a?.name ?? "", ha: h?.abbrev ?? "", aa: a?.abbrev ?? "",
             fav: g.home_win_prob >= 0.5 ? h?.name : a?.name, p: Math.max(g.home_win_prob, 1 - g.home_win_prob) };
  });
  return (
    <>
      <PageHead eyebrow="Predictions" title="Every team's next game"
        lead={m ? <>Forecast made {fmt.dayYear(m.at)} at {tipTime(m.at)} ET, from games through {fmt.dayYear(m.thru)}. Updated every day: in the morning,
          after the nightly data refresh, and 40 minutes before the first tip-off. Every forecast is logged and <Link to="/scorecard">scored</Link>.</> : null} />
      <Loading state={q} />
      {!q.loading && !rows.length && <p className="muted">No forecast for today's games yet: the daily run posts one each morning and again 40 minutes before the first tip-off.</p>}
      {rows.length > 0 && (
        <section className="section">
          <h2>The slate</h2>
          <DataTable rows={rows} onRow={(r) => { window.location.hash = `#/game/${r.game_id}`; }}
            cols={[
              { key: "tipoff", label: "Tip-off (ET)", fmt: (v) => `${tipDay(v).split(",")[0]} ${tipTime(v)}` },
              { key: "away", label: "Game", fmt: (_, r) => <Link to={`/game/${r.game_id}`}><b>{r.away}</b> <span className="muted">at</span> <b>{r.home}</b></Link> },
              { key: "fav", label: "Favorite" },
              { key: "p", label: "Win chance", num: true, fmt: (v) => fmt.pct(v) },
              { key: "pred_margin", label: "Margin", num: true, fmt: (v) => fmt.n(Math.abs(v), 1) },
              { key: "margin_lo", label: "80% range", fmt: (_, r) => r.margin_lo != null ? marginRange(r.margin_lo, r.margin_hi, r.ha, r.aa) : "–", sortable: false },
              { key: "rest", label: "Back-to-back", fmt: (_, r) => [r.b2b_a ? r.aa : null, r.b2b_h ? r.ha : null].filter(Boolean).join(", ") || "–", sortable: false },
            ]} />
          <p className="xs muted">Click a game for the full preview: why the margin, who's out, the scouting report and every player's stat line.</p>
        </section>
      )}
      <Accuracy />
    </>
  );
}

export function Accuracy() {
  const ev = useQuery(`select * from forecast.wf_eval where season = 0`);
  const best = useQuery(`select any_value(model) m from forecast.margin_model where model <> 'player intervals'`);
  const cal = useQuery(`select * from forecast.wf_calibration order by bin`);
  const pe = useQuery(`select * from forecast.wf_player_eval where coverage_80 is not null`);
  const chosen = best.rows[0]?.m;
  const models = useMemo(() => MODELS.map((mm) => ev.rows.find((r) => r.model === mm)).filter(Boolean) as any[], [ev.rows]);
  const accPlot = useMemo(() => models.length ? (width: number) => ({
    width, height: 240, marginLeft: 110,
    x: { label: "winners picked", tickFormat: (v: number) => fmt.pct(v), domain: [0.5, 0.7] }, y: { label: null, domain: models.map((r) => r.model) },
    marks: [Plot.barX(models, { y: "model", x1: 0.5, x2: "accuracy", fill: (d: any) => (d.model === chosen ? css("--s1") : css("--axis")), rx: 3 }),
            Plot.text(models, { y: "model", x: "accuracy", text: (d: any) => fmt.pct(d.accuracy, 1), dx: 6, textAnchor: "start", fill: css("--text-2") })],
  }) : undefined, [models, chosen]);
  const llPlot = useMemo(() => models.length ? (width: number) => ({
    width, height: 240, marginLeft: 110,
    x: { label: "log loss (lower is better)", domain: [0.6, 0.7] }, y: { label: null, domain: models.map((r) => r.model) },
    marks: [Plot.barX(models, { y: "model", x1: 0.6, x2: "log_loss", fill: (d: any) => (d.model === chosen ? css("--s1") : css("--axis")), rx: 3 }),
            Plot.text(models, { y: "model", x: "log_loss", text: (d: any) => d.log_loss.toFixed(4), dx: 6, textAnchor: "start", fill: css("--text-2") })],
  }) : undefined, [models, chosen]);
  const calPlot = useMemo(() => cal.rows.length ? (width: number) => ({
    width, height: 300, x: { label: "predicted home win chance", domain: [0, 1], tickFormat: (v: number) => fmt.pct(v) },
    y: { label: "how often the home team won", domain: [0, 1], tickFormat: (v: number) => fmt.pct(v), grid: true },
    marks: [Plot.line([[0, 0], [1, 1]], { stroke: css("--axis"), strokeDasharray: "4 4" }),
            Plot.line(cal.rows, { x: "predicted", y: "actual", stroke: css("--s1"), strokeWidth: 2 }),
            Plot.dot(cal.rows, { x: "predicted", y: "actual", r: 5, fill: css("--s1"), stroke: css("--surface"), strokeWidth: 2 }),
            Plot.tip(cal.rows, Plot.pointer({ x: "predicted", y: "actual", title: (d: any) => `predicted ${fmt.pct(d.predicted)} · won ${fmt.pct(d.actual)} · ${fmt.n(d.games)} games` }))],
  }) : undefined, [cal.rows]);
  const n = models.find((r) => r.model === chosen)?.games;
  const cov = pe.rows.length ? pe.rows.reduce((a, r) => a + r.coverage_80, 0) / pe.rows.length : null;
  const below = pe.rows.length ? pe.rows.reduce((a, r) => a + r.below, 0) / pe.rows.length : null;
  const above = pe.rows.length ? pe.rows.reduce((a, r) => a + r.above, 0) / pe.rows.length : null;
  return (
    <section className="section">
      <h2>How good are these predictions?</h2>
      <p className="small ink2" style={{ marginTop: -6 }}>{fmt.n(n)} games, 2021-22 to 2025-26, each predicted from earlier games only, a week at a time; each step adds one
        ingredient. Blue: the model in use. Coaching history and style matchups made predictions worse, so they are left out.</p>
      <div className="grid">
        <div className="c6">{accPlot && <Figure title="Winners picked" plot={accPlot} source="walk-forward test" />}</div>
        <div className="c6">{llPlot && <Figure title="Log loss of the win chances" plot={llPlot} source="walk-forward test" />}</div>
        <div className="c6">{calPlot && <Figure title="Win chances mean what they say" subtitle="Predicted home win chance by decile against how often the home team won." plot={calPlot}
          table={{ rows: cal.rows, cols: [["predicted", "Predicted", (v) => fmt.pct(v)], ["actual", "Won", (v) => fmt.pct(v)], ["games", "Games"]] }} />}</div>
        <div className="c6">
          <Card title="Player stat lines" sub={cov != null ? `The 80% ranges held ${fmt.pct(cov)} of outcomes in 2023-24 to 2025-26, ${fmt.pct(below)} below and ${fmt.pct(above)} above (fitted on the two seasons before).` : undefined}>
            <DataTable rows={pe.rows.map((r) => ({ ...r, label: Object.fromEntries(STATS)[r.stat] ?? r.stat }))} cols={[
              { key: "label", label: "Stat" },
              { key: "model_mae", label: "Model miss", num: true, fmt: (v) => fmt.n(v, 3) },
              { key: "who_sat_known_mae", label: "Who sat known", num: true, fmt: (v) => fmt.n(v, 3), title: "With absences known and their minutes shared out, as live" },
              { key: "season_avg_mae", label: "Season average", num: true, fmt: (v) => fmt.n(v, 3) },
              { key: "coverage_80", label: "Inside 80%", num: true, fmt: (v) => fmt.pct(v, 1) },
            ]} />
            <p className="xs muted">Average miss per player-game played. Matchup adjustments for player lines (opponent's defense by position, pace) were tested and add nothing measurable.</p>
          </Card>
        </div>
      </div>
    </section>
  );
}
