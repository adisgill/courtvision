// The broadcast ticker under the masthead: the latest scored results (with whether our pick
// won) and the next games with our favourite, scrolling continuously (paused on hover; static
// under reduced motion).
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "../db";
import { CURRENT_SEASON, teamSql } from "../lib/nba";

const time = (d: Date) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "UTC" });
const wday = (d: Date) => d.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });

export default function Ticker() {
  const q = useQuery(`
    with t as ${teamSql(CURRENT_SEASON)},
    res as (select 'res' kind, s.game_id, s.tipoff, s.home_team_id, s.away_team_id, s.home_score, s.away_score,
                   s.winner_right, null::double home_win_prob
            from forecast.scorecard_games s order by s.tipoff desc limit 14),
    fx as (select 'fx' kind, g.game_id, g.tipoff, g.home_team_id, g.away_team_id, null, null, null, g.home_win_prob
           from forecast.games g order by g.tipoff limit 14)
    select x.*, h.abbrev home, a.abbrev away from (select * from res union all select * from fx) x
    left join t h on h.team_id = x.home_team_id left join t a on a.team_id = x.away_team_id
    order by x.tipoff`);
  const items = useMemo(() => q.rows.map((g) => ({ g, d: new Date(g.tipoff) })), [q.rows]);
  if (!items.length) return <div className="ticker" aria-hidden="true" />;
  const one = items.map(({ g, d }, i) => {
    const body = g.kind === "res" ? (
      <>
        <span className={g.away_score > g.home_score ? "w" : ""}>{g.away}</span>
        <span className="sc">{g.away_score}–{g.home_score}</span>
        <span className={g.home_score > g.away_score ? "w" : ""}>{g.home}</span>
        <span className="fav" title={g.winner_right ? "Our pick won" : "Our pick lost"}>{g.winner_right ? "✓ pick" : "✗ pick"}</span>
      </>
    ) : (
      <>
        <span className="ko">{wday(d)} {time(d)}</span>
        <span>{g.away} @ {g.home}</span>
        {g.home_win_prob != null && (
          <span className="fav">{g.home_win_prob >= 0.5 ? g.home : g.away} {Math.round(100 * Math.max(g.home_win_prob, 1 - g.home_win_prob))}%</span>
        )}
      </>
    );
    return <Link key={i} className="it" to={`/game/${encodeURIComponent(g.game_id)}`} tabIndex={-1}>{body}</Link>;
  });
  return (
    <div className="ticker" role="region" aria-label="Latest results and upcoming games">
      <div className="tag"><i />Live board</div>
      <div className="lane">
        <div className="track" style={{ ["--dur" as any]: `${Math.max(40, items.length * 4)}s` }}>
          {one}
          <span aria-hidden="true" style={{ display: "contents" }}>{one}</span>
        </div>
      </div>
    </div>
  );
}
