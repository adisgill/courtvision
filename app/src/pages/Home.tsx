import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Row, useManifest, useQuery } from "../db";
import { fmt } from "../lib/format";
import { CURRENT_SEASON, useTeamNames } from "../lib/nba";
import { Loading, OutcomeBar, Tile } from "../ui/kit";

const TOOLS: [string, string, string][] = [
  ["/upcoming", "Predictions", "Every team's next game: margin, win chance, why, and each player's stat line with an 80% range."],
  ["/scorecard", "Scorecard", "Every logged prediction scored against the final: winners, Brier, margins, player lines."],
  ["/league", "League", "Standings, team ratings on five dimensions and the season's leaders."],
  ["/player", "Players", "0-100 ratings by game and season, per-possession impact and shot maps."],
  ["/lineups", "Lineups", "Five-man units and pairs by net rating per 100 possessions."],
  ["/plays", "Play types", "How every team scores and defends, from the play-by-play."],
  ["/coaches", "Coaches", "Which coaching habits follow a coach to a new team, and which don't."],
  ["/market", "Market", "Contracts against production: who's underpaid, who's not."],
];

/** Tip-offs are stored as Eastern wall-clock times: show them as such. */
export const tipTime = (v: any) => new Date(v).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "UTC" });
export const tipDay = (v: any) => new Date(v).toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric", timeZone: "UTC" });
/** Today's date in Eastern time, as YYYY-MM-DD (the schedule's calendar). */
export const todayET = () => new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });

/** A home-margin range in words: "BOS by 3 to 35", "NYK by 13 to BOS by 25". */
export function marginRange(lo: number, hi: number, home: string, away: string) {
  const l = Math.round(lo), h = Math.round(hi);
  if (l >= 0) return `${home} by ${l} to ${h}`;
  if (h <= 0) return `${away} by ${-h} to ${-l}`;
  return `${away} by ${-l} to ${home} by ${h}`;
}

function Board({ rows, names }: { rows: Row[]; names: ReturnType<typeof useTeamNames> }) {
  return (
    <div className="board">
      {rows.map((g) => {
        const h = names.get(Number(g.home_team_id)), a = names.get(Number(g.away_team_id));
        const hn = h?.name ?? "Home", an = a?.name ?? "Away";
        const ph = g.home_win_prob as number;
        return (
          <Link key={g.game_id} to={`/game/${encodeURIComponent(g.game_id)}`} className="fx spot" aria-label={`${an} at ${hn}`}>
            <div className="fx-top"><span>{tipTime(g.tipoff)} ET</span><span>{g.b2b_a ? `${a?.abbrev} on a back-to-back` : g.b2b_h ? `${h?.abbrev} on a back-to-back` : ""}</span></div>
            <div className={`fx-team ${ph < 0.5 ? "fav" : ""}`}><span className="n">{an}</span><span className="p">{fmt.pct(1 - ph)}</span></div>
            <div className={`fx-team ${ph >= 0.5 ? "fav" : ""}`}><span className="n">@ {hn}</span><span className="p">{fmt.pct(ph)}</span></div>
            <OutcomeBar thin h={1 - ph} d={0} a={ph} labels={[an, "", hn]} />
            <div className="fx-more">
              <span>{(g.pred_margin >= 0 ? hn : an)} by {fmt.n(Math.abs(g.pred_margin), 1)}</span>
              {g.margin_lo != null && <span title="80% range of the final margin">{marginRange(g.margin_lo, g.margin_hi, h?.abbrev ?? hn, a?.abbrev ?? an)}</span>}
            </div>
          </Link>
        );
      })}
    </div>
  );
}

export default function Home() {
  const manifest = useManifest();
  const names = useTeamNames(CURRENT_SEASON);
  const today = todayET();
  const next = useQuery(`
    select * from forecast.games
    where tipoff::date = (select min(tipoff::date) from forecast.games where tipoff::date >= '${today}')
    order by tipoff, game_id`);
  const last = useQuery(`
    select * from forecast.scorecard_games where day = (select max(day) from forecast.scorecard_games)
    order by tipoff, game_id`);
  const led = useQuery(`
    select count(*) n, avg(winner_right::int) acc, avg(brier) brier, avg(abs(error)) mae,
           avg(margin_inside_80::int) in80, min(day) first_day, count(distinct day) nights
    from forecast.scorecard_games`);
  const lines = useQuery(`
    select l.person_id, l.team_id, l.game_id, l.pts, l.pts_lo, l.pts_hi, l.exp_minutes, p.player
    from forecast.player_lines l join web.player_names p using (person_id)
    where l.game_id in (select game_id from forecast.games
                        where tipoff::date = (select min(tipoff::date) from forecast.games where tipoff::date >= '${today}'))
    order by l.pts desc limit 10`);
  const L = led.rows[0];
  const day = next.rows[0]?.tipoff;
  const lastRows = useMemo(() => last.rows, [last.rows]);
  return (
    <>
      <div className="home-hero">
        <div>
          <div className="eyebrow">Games through {fmt.dayYear(manifest?.data_through)} · {CURRENT_SEASON}-{String((CURRENT_SEASON + 1) % 100).padStart(2, "0")} season</div>
          <h1>Every game,<br /><em>forecast.</em></h1>
          <p className="lead">Ratings, forecasts, scouting and lineup analysis for the NBA, built from public data. Every prediction is
            logged before tip-off and scored after the final, in the open, wins and misses alike.</p>
          <div className="row" style={{ marginTop: 16 }}>
            <Link className="btn primary" to="/upcoming">All predictions</Link>
            <Link className="btn" to="/scorecard">The scorecard</Link>
          </div>
        </div>
        <div className="duel">
          <div className="row between">
            <span className="eyebrow">The ledger so far · since {fmt.day(L?.first_day)}</span>
            <Link className="xs" to="/scorecard">How we're scored →</Link>
          </div>
          {L && [
            ["Winners picked", fmt.pct(L.acc), L.acc, 0.658, "backtest 65.8%"],
            ["Final margin inside the 80% range", fmt.pct(L.in80), L.in80, 0.8, "target 80%"],
          ].map(([label, v, x, ref, note]) => (
            <div key={label as string} className="duel-row" style={{ gridTemplateColumns: "1fr 64px" }}>
              <div>
                <div className="row between"><span className="nm">{label as string}</span><span className="muted xs">{note as string}</span></div>
                <div style={{ position: "relative", height: 18 }}>
                  <div style={{ position: "absolute", left: 0, right: 0, top: 8, height: 2, background: "var(--surface-3)" }} />
                  <div style={{ position: "absolute", left: `${100 * (ref as number)}%`, top: 3, width: 2, height: 12, background: "var(--axis)" }} />
                  <div style={{ position: "absolute", left: `${100 * (x as number)}%`, top: 2, width: 14, height: 14, marginLeft: -7, borderRadius: "50%",
                                background: "var(--s1)", boxShadow: "0 0 0 2px var(--surface)", animation: "rise 0.6s var(--ease) both" }} />
                </div>
              </div>
              <span className="v">{v as string}</span>
            </div>
          ))}
          <p className="xs muted" style={{ margin: "10px 0 0" }}>{fmt.n(L?.n)} games over {fmt.n(L?.nights)} nights, each graded on the last prediction logged before tip-off.
            Brier {fmt.n(L?.brier, 3)} (a coin flip scores 0.250).</p>
        </div>
      </div>

      <div className="tiles">
        <Tile label={day ? `Games ${tipDay(day)}` : "Next games"} value={fmt.n(next.rows.length)} delta="each with a logged forecast" />
        <Tile label="Predictions scored" value={fmt.n(L?.n)} delta={<Link to="/scorecard">append-only ledger</Link>} />
        <Tile label="Average margin miss" value={L ? `${fmt.n(L.mae, 1)} pts` : "–"} delta="backtest 10.9 · a tie every game 13.4" />
        <Tile label="Brier score" value={fmt.n(L?.brier, 3)} delta="lower is better · coin flip 0.250" />
      </div>

      <section className="section">
        <h2>{day ? tipDay(day) : "Next games"}<Link className="btn" style={{ marginLeft: "auto", fontFamily: "var(--font)", textTransform: "none", fontSize: "0.84rem", letterSpacing: 0 }} to="/upcoming">Every prediction →</Link></h2>
        <p className="small ink2" style={{ marginTop: -6 }}>Win chance and predicted margin for each game (tip-off in Eastern time), with the 80% range of the final margin.
          The marked side is the favourite; click a game for the full preview.</p>
        <Loading state={next} />
        <Board rows={next.rows} names={names} />
      </section>

      {lines.rows.length > 0 && (
        <section className="section">
          <h2>Top projected scorers</h2>
          <p className="small ink2" style={{ marginTop: -6 }}>Expected points with the range that held 80% of outcomes in seasons the model never saw.</p>
          <div className="results">
            {lines.rows.map((r) => (
              <Link key={`${r.game_id}-${r.person_id}`} to={`/player/${r.person_id}`} className="res" style={{ borderRadius: 0 }}>
                <span className="h">{r.player}</span>
                <span className="s">{fmt.n(r.pts, 1)}</span>
                <span className="a muted">{Math.ceil(r.pts_lo)}–{Math.floor(r.pts_hi)} pts</span>
                <span className="meta"><span>{names.get(Number(r.team_id))?.abbrev}</span><span>{fmt.n(r.exp_minutes, 0)} min expected</span></span>
              </Link>
            ))}
          </div>
        </section>
      )}

      <section className="section">
        <h2>Last scored night{lastRows[0] ? ` · ${tipDay(lastRows[0].tipoff)}` : ""}</h2>
        <p className="small ink2" style={{ marginTop: -6 }}>Final scores against the prediction logged before tip-off.</p>
        <Loading state={last} />
        <div className="results">
          {lastRows.map((g) => {
            const h = names.get(Number(g.home_team_id)), a = names.get(Number(g.away_team_id));
            const pickHome = g.pred_margin >= 0;
            return (
              <Link key={g.game_id} to={`/game/${encodeURIComponent(g.game_id)}`} className={`res ${!g.winner_right && Math.max(g.home_win_prob, 1 - g.home_win_prob) >= 0.7 ? "upset" : ""}`} style={{ borderRadius: 0 }}>
                <span className="h" style={{ fontWeight: g.away_score > g.home_score ? 650 : 400 }}>{a?.name}</span>
                <span className="s">{g.away_score}–{g.home_score}</span>
                <span className="a" style={{ fontWeight: g.home_score > g.away_score ? 650 : 400 }}>{h?.name}</span>
                <span className="meta">
                  <span>{g.winner_right ? "✓" : "✗"} picked {pickHome ? h?.abbrev : a?.abbrev} ({fmt.pct(Math.max(g.home_win_prob, 1 - g.home_win_prob))})</span>
                  <span>predicted {pickHome ? h?.abbrev : a?.abbrev} by {fmt.n(Math.abs(g.pred_margin), 1)} · {g.margin_inside_80 ? "inside" : "outside"} the 80% range</span>
                </span>
              </Link>
            );
          })}
        </div>
      </section>

      <section className="section">
        <h2>Tools</h2>
        <div className="toolgrid">
          {TOOLS.map(([to, title, sub], i) => (
            <Link key={to} to={to} className="tool spot">
              <span className="ix">{String(i + 1).padStart(2, "0")}</span>
              <b>{title}</b>
              <span className="ar">→</span>
              <p>{sub}</p>
            </Link>
          ))}
        </div>
      </section>
    </>
  );
}
