import { Link } from "react-router-dom";
import { useManifest } from "../db";
import { fmt } from "../lib/format";
import { Card, PageHead } from "../ui/kit";

const REPO = "https://github.com/adisgill/courtvision";

export default function About() {
  const m = useManifest();
  const tables = m ? Object.keys(m.files).length : 0;
  const rows = m ? Object.values(m.rows).reduce((a, b) => a + b, 0) : 0;
  return (
    <>
      <PageHead eyebrow="About" title="Sources, methods and limits"
        lead="courtvision is an open-source NBA analytics project built from public data. Everything on this site runs in your browser: DuckDB reads the published Parquet files directly, so every number here is a query you could run yourself." />
      <div className="grid">
        <div className="c6">
          <Card title="Data">
            <ul className="small ink2">
              {m && Object.values(m.attribution).map((a) => <li key={a}>{a}</li>)}
              <li>Data version <span className="mono">{m?.version ?? "–"}</span>, built {fmt.dayYear(m?.built_at)}, games through {fmt.dayYear(m?.data_through)}: {fmt.n(tables)} tables, {fmt.n(rows)} rows.</li>
              <li>Game-level detail and shot maps cover the current and last season; season ratings go back to 1974-75 and team box scores further.</li>
            </ul>
          </Card>
        </div>
        <div className="c6">
          <Card title="Methods">
            <ul className="small ink2">
              <li><b>Ratings</b>: each dimension is points per 100 possessions against league average, era-adjusted to a 0-100 scale (50 = league average that season, 10 = one standard deviation), from the best tier of data each season has.</li>
              <li><b>Impact</b>: adjusted plus-minus over play-by-play lineups, with last season's impact and a box-score model as starting points.</li>
              <li><b>Forecasts</b>: a two-stage model (team form, the roster's impact over expected minutes, rest), chosen in a walk-forward test over five seasons where every game was predicted from earlier games only.</li>
              <li><b>The ledger</b>: every forecast is appended, never rewritten, and each game is graded on the last prediction logged before tip-off. See the <Link to="/scorecard">scorecard</Link>.</li>
              <li><b>The draft</b>: every college player since 2007-08 linked to the NBA career that followed, a model of a player's first four NBA seasons from his college numbers,
                tested on past drafts against the order teams actually picked in. See the <Link to="/draft?tab=record">track record</Link>.</li>
            </ul>
          </Card>
        </div>
        <div className="c6">
          <Card title="Limits">
            <ul className="small ink2">
              <li>No player-tracking data: everything comes from box scores and the play-by-play, so defense away from the ball, spacing and screens are only seen through their results.</li>
              <li>One night is a handful of games. Compare the scorecard with the walk-forward averages, not with zero.</li>
              <li>Availability comes from the NBA's official injury report; players who sit without being listed are expected to play until the report or the box score says otherwise.</li>
            </ul>
          </Card>
        </div>
        <div className="c6">
          <Card title="More">
            <ul className="small ink2">
              <li>Source code and the full pipeline: <a href={REPO}>{REPO.replace("https://", "")}</a> (one command rebuilds everything from the raw caches).</li>
              <li>Every metric's definition and every change, with the evidence for it: <a href={`${REPO}/blob/main/docs/metrics_spec.md`}>docs/metrics_spec.md</a>.</li>
              <li>The validation report (stabilization, the walk-forward test, calibration): <a href={`${REPO}/blob/main/docs/validation.md`}>docs/validation.md</a>.</li>
              <li>A sibling project for football: <a href="https://adisgill.github.io/pitchvision/">pitchvision</a>.</li>
            </ul>
          </Card>
        </div>
      </div>
    </>
  );
}
