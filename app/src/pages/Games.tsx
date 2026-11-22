// Every result of the season (and the last), with our pick where the game was in the ledger.
import { Link } from "react-router-dom";
import { useQuery } from "../db";
import { fmt, seasonLabel } from "../lib/format";
import { useSeasons, useTeamNames } from "../lib/nba";
import { DataTable, Loading, PageHead, SeasonSelect, Select, useParam } from "../ui/kit";

export default function Games() {
  const seasons = useSeasons();
  const [sp, setSeason] = useParam("season", "");
  const season = Number(sp) || seasons[0] || 0;
  const [team, setTeam] = useParam("team", "");
  const names = useTeamNames(season);
  const q = useQuery(season ? `
    select g.game_id, g.game_date, g.home_team_id, g.away_team_id, g.home_score, g.away_score, g.season_type,
           s.winner_right, s.home_win_prob, s.pred_margin
    from stg.games g left join forecast.scorecard_games s using (game_id)
    where g.season = ${season} and g.home_score is not null and g.season_type in ('regular', 'playoffs', 'play_in', 'cup_final')
      ${team ? `and (g.home_team_id = ${Number(team)} or g.away_team_id = ${Number(team)})` : ""}
    order by g.game_date desc, g.game_id desc limit 400` : null);
  const teamOpts = [...names.entries()].map(([id, t]) => [String(id), t.team] as [string, string]).sort((a, b) => a[1].localeCompare(b[1]));
  const rows = q.rows.map((r) => ({ ...r, home: names.get(Number(r.home_team_id))?.name ?? "", away: names.get(Number(r.away_team_id))?.name ?? "" }));
  return (
    <>
      <PageHead eyebrow={`Games · ${season ? seasonLabel(season) : ""}`} title="Every result"
        lead="Click a game for its recap: each team's ratings that night against its season average, the players, and, for games in the ledger, our prediction against the final." />
      <div className="filters">
        {seasons.length > 0 && <SeasonSelect value={season} onChange={(v) => setSeason(String(v))} seasons={seasons} />}
        <Select label="Team" value={team} onChange={(v) => setTeam(v || null)} options={[["", "All teams"], ...teamOpts]} />
      </div>
      <Loading state={q} />
      <DataTable rows={rows} onRow={(r) => { window.location.hash = `#/game/${r.game_id}`; }} cols={[
        { key: "game_date", label: "Date", fmt: (v) => fmt.dayYear(v) },
        { key: "away", label: "Away", fmt: (v, r) => <span style={{ fontWeight: r.away_score > r.home_score ? 650 : 400 }}>{v}</span> },
        { key: "away_score", label: "", num: true, sortable: false },
        { key: "home_score", label: "", num: true, sortable: false },
        { key: "home", label: "Home", fmt: (v, r) => <Link to={`/game/${r.game_id}`} style={{ fontWeight: r.home_score > r.away_score ? 650 : 400 }}>{v}</Link> },
        { key: "season_type", label: "Type", fmt: (v) => (v === "regular" ? "" : String(v).replace("_", "-")) },
        { key: "winner_right", label: "Our pick", fmt: (v, r) => (v == null ? <span className="muted">–</span> : `${v ? "✓" : "✗"} ${fmt.pct(Math.max(r.home_win_prob, 1 - r.home_win_prob))}`) },
      ]} />
    </>
  );
}
