// NBA conventions and lookups shared by every page.
import { lit, useQuery } from "../db";

/** The season under way (named by its start year): October onward is the new season. */
export function seasonOf(d = new Date()): number {
  return d.getMonth() >= 8 ? d.getFullYear() : d.getFullYear() - 1;
}
export const CURRENT_SEASON = seasonOf();

/** The five headline dimensions of the 0-100 ratings, in display order. */
export const DIMENSIONS: [string, string][] = [
  ["shooting", "Shooting"], ["defense", "Defense"], ["ball_control", "Ball control"],
  ["backcourt", "Backcourt"], ["frontcourt", "Frontcourt"],
];
export const DIM_LABEL: Record<string, string> = Object.fromEntries(DIMENSIONS);

export const COMPONENT_LABELS: Record<string, string> = {
  shooting_sq: "Shot quality", shooting_sm: "Shot making", shooting_fd: "Drawing fouls", shooting_fm: "Free-throw making",
  defense_dq: "Shot quality allowed", defense_dm: "Shot making allowed", defense_dt: "Forced turnovers",
  defense_df: "Fouls (FT rate allowed)", defense_dr: "Defensive rebounding", defense_dqm: "FG points allowed",
  ball_bt: "Turnover avoidance", ball_bo: "Offensive rebounding",
  backcourt_sh: "Guard shooting", backcourt_ft: "Guard free throws", backcourt_bt: "Guard ball security",
  backcourt_dz: "Perimeter defense", backcourt_dt: "Guard steals", backcourt_df: "Guard fouls",
  frontcourt_sh: "Big shooting", frontcourt_ft: "Big free throws", frontcourt_bt: "Big ball security",
  frontcourt_dz: "Paint defense", frontcourt_dt: "Big steals", frontcourt_df: "Big fouls", frontcourt_dr: "Defensive rebounding",
  defense_stl_rate: "Steals / 100 def. poss.", defense_blk_rate: "Blocks / 100 def. poss.",
  defense_pf_rate: "Fouls / 100 def. poss. (neg.)", ball_shot_end_rate: "Plays ending in a shot",
  ball_ato: "A/TO vs league (log)", ball_ast_rate: "Assisted FG share vs league",
};
export const labelOf = (m: string) => DIM_LABEL[m] ?? (m === "presence" ? "Presence" : COMPONENT_LABELS[m] ?? m);
const ORDER = [...DIMENSIONS.map(([k]) => k), "presence", ...Object.keys(COMPONENT_LABELS)];
/** Headline dimensions first, then components in a fixed order. */
export const ordered = <T extends Record<string, any>>(rows: T[]) =>
  [...rows].sort((a, b) => (ORDER.indexOf(a.measure) + 1 || 999) - (ORDER.indexOf(b.measure) + 1 || 999));
/** Rate-style components left out of the component bar charts. */
export const isRateMeasure = (m: string) => ["defense_stl", "defense_blk", "defense_pf", "ball_ato", "ball_ast", "ball_shot"].some((p) => m.startsWith(p));

export const PLAYS = ["Transition", "Putback", "Lob", "Cut", "Drive", "Floater", "Post-up", "Step-back",
  "Pull-up", "Other rim", "Spot-up three", "Two-point jumper"];
export const DESCRIBE: Record<string, string> = {
  Transition: "shots in possessions after a miss or steal, within 8 seconds",
  Putback: "tip-ins and putbacks after an offensive rebound",
  Lob: "alley-oops", Cut: "cutting layups and dunks", Drive: "driving layups, dunks, finger rolls and hooks",
  Floater: "floaters", "Post-up": "hooks, turnarounds and fadeaways (post and mid-post)",
  "Step-back": "step-back jumpers", "Pull-up": "pull-up jumpers off the dribble",
  "Other rim": "layups and dunks with no move recorded (rolls, dump-offs, running)",
  "Spot-up three": "threes with no move recorded (mostly catch-and-shoot)",
  "Two-point jumper": "two-point jumpers with no move recorded",
};
export const TOV_KINDS = ["Bad pass", "Lost ball", "Offensive foul", "Out of bounds", "Travel", "Shot clock", "Other"];
export const STATS: [string, string][] = [["pts", "Points"], ["reb", "Rebounds"], ["ast", "Assists"], ["fg3m", "Threes"],
  ["stocks", "Steals + blocks"], ["tov", "Turnovers"]];

/** Whole numbers an 80% range covers (0.4-3.8 covers 1 to 3), never rounded outward. */
export const rangeText = (lo: number, hi: number) => `${Math.ceil(lo)}–${Math.floor(hi)}`;

/** Season list: seasons with team-game ratings (recent detail), newest first. */
export function useSeasons(sql = "select distinct season from metrics.team_season order by 1 desc") {
  const q = useQuery(sql);
  return q.rows.map((r) => Number(r.season));
}

/** Team names for a season: id -> { team, name, abbrev } (falls back to any season). */
export function useTeamNames(season: number) {
  const q = useQuery(`select team_id, team, name, abbrev, season from web.team_names
                      qualify row_number() over (partition by team_id order by (season = ${season}) desc, season desc) = 1`);
  const byId = new Map<number, { team: string; name: string; abbrev: string }>();
  for (const r of q.rows) byId.set(Number(r.team_id), { team: r.team, name: r.name, abbrev: r.abbrev ?? r.name });
  return byId;
}

/** Player names for a list of ids. */
export function usePlayerNames(ids: number[]) {
  const q = useQuery(ids.length ? `select person_id, player from web.player_names where person_id in (${ids.map(Number).join(",")})` : null);
  return new Map(q.rows.map((r) => [Number(r.person_id), String(r.player)]));
}

export const teamSql = (season: number | string) =>
  `(select team_id, team, name, abbrev from web.team_names where season = ${typeof season === "number" ? season : lit(season)})`;
