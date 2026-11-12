# Metrics spec

**Status: DRAFT v0.1 for review. No metric code exists yet.** Everything below
is a proposal grounded in [`data_audit.md`](data_audit.md); section 0 lists
the decisions that need your call before implementation starts.

Contents: [0 Decisions](#0-decisions-needed) ·
[1 Conventions](#1-conventions) · [2 Scale](#2-the-0100-scale) ·
[3 Roll-up](#3-roll-up-player--game--team--season) ·
[4 Shrinkage](#4-small-sample-shrinkage) · [5 Dimensions](#5-the-six-dimensions) ·
[6 Availability](#6-availability-by-era) · [7 Outputs & CLI](#7-outputs-and-the-cli) ·
[8 Acceptance tests](#8-acceptance-tests) · [9 Out of scope](#9-out-of-scope-for-v1)

---

## 0. Decisions needed

Each has a recommendation; the rest of the spec is written assuming the
recommendation, and marks the places that change if you choose otherwise.

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | **0–100 mapping** | (a) `50 + 10·z`, clipped: 50 = league average, ±10 = ±1 SD; (b) percentile within season | **(a)**. Keeps magnitude, so "+8 tonight" means the same thing every season, and deltas are meaningful for the in-game use case. Percentiles flatten the tails and make deltas depend on where you start. |
| D2 | **Free throws in Shooting** | (a) Shooting is field goals only (decomposes eFG%); (b) include FT trips (decomposes TS%) | **(a)**. The brief defines Shooting by court zone; FTs have no zone. FT *allowed* is in Defense; offensive FT rate is reported as a supporting stat. |
| D3 | **Rebounding** | Not in any dimension as briefed. (a) leave out; (b) DREB% as a Defense component, OREB% as a Ball-control component | **(b)**, as separately reported components that can be switched off. Without it, Defense ignores ending the opponent's possession and the validation against Four Factors starts with a known hole. |
| D4 | **What "restricted to guards / perimeter zones" means** for Backcourt/Frontcourt | (a) position filter only; (b) zone filter only; (c) both at once; (d) **offense by position, defense by zone** | **(d)**. Offense events have a known actor, so a guard's shots and turnovers are backcourt offense (all zones, drives included). Defense has no known defender without tracking, so backcourt defense = opponent *perimeter* shots allowed, frontcourt defense = opponent *paint* shots allowed. |
| D5 | **Multi-position players** (G-F, F-C flags) | (a) fractional: weight = flag share; (b) first flag wins; (c) per-game `startingPosition` for starters | **(a)**. G-F counts ½ backcourt, ½ frontcourt; F-C counts fully frontcourt. Weights sum to 1, so roll-ups stay exact. Flags are career-level, a known limitation. |
| D6 | **Presence sample** | (a) on/off within games the player appeared in; (b) on/off over the whole season (includes games missed) | **(a)**. Keeps games the player missed out of the "off" sample, so injuries and a different mix of opponents and teammates don't leak into the comparison. |
| D7 | **Player-level Defense** | (a) on-court team defense (opponent events while the player is on the floor); (b) individually credited events (steals, blocks, charges, fouls); (c) blend | **(a)** as the rating, with (b) shown as supporting sub-ratings. (a) rolls up exactly to the team; (b) is attributable but covers a small part of defense. Without tracking, nothing better exists. |
| D8 | **Stint (lineup) source for 1996-97 → 2024-25** | (a) reconstruct from Kaggle PBP substitutions; (b) pull `gamerotation` for every game (~37k requests, ~20 h) | **(a)**, validated against `gamerotation` on 2025-26 (already pulled); pull `gamerotation` only for games that fail reconstruction checks. |
| D9 | **Era-gating rule** | (a) strict: a box-score input counts from the first season it is non-zero in ≥95% of team-games, sustained; (b) per-game gating with season coverage thresholds | **(a)** for v1. Simple and defensible; (b) can extend coverage into the 1970s later. |

## 1. Conventions

**Seasons.** `season` = start year (2025 = 2025-26), from the game id.
**Reference games** = regular season only (type digit 2, which includes NBA
Cup group and knockout games). Play-in, playoffs, and the Cup final are rated
against the *same season's* regular-season reference, so a playoff game reads
on the same scale as the season baseline.

**League baselines** (one row per season, regular season, all teams): zone
FG%, FT%, turnover rate, rebound rates, points per possession. For the season
in progress they are season-to-date and recomputed on every build, so the
current season's ratings drift until it ends.

**Exposure units.**

| Name | Definition | Used by |
|---|---|---|
| `FGA` | field-goal attempts in rated zones (backcourt heaves, 0.01% of shots, excluded) | Shooting |
| `PLAYS` | `FGA + c_FT·FTA + TOV` (TOV includes team turnovers at team level) | Ball control |
| `POSS` | tier A: **counted** from play-by-play (possession changes); box tiers: `FGA + c_FT·FTA − OREB + TOV` | Defense, Presence |

`c_FT = 0.40`, the share of free throws that end a trip, fitted below. One
constant is used everywhere.

**Possessions.** Kaggle's `possessions` (1996-97+) is an integer count, i.e.
the NBA's official play-by-play count, not a formula. Tested against it
(1996-97 → 2026-27, 71k team-games), the textbook estimate
`FGA + 0.44·FTA − OREB + TOV` is off by 2.5 possessions per team-game on
average. The best-fitting FT coefficient is 0.40 (0.39–0.41 in every era),
which gets to 1.7; no box formula gets below ~1.6. So:

* **Tier A (1996-97+)** counts possessions from play-by-play: a possession ends
  on a made FG (except and-ones), the last FT of a trip, a defensive rebound,
  a turnover, or period end. Counting events inside stints gives on-court
  possessions on the same basis, so team and player numbers roll up exactly.
  The live schema's `possession` column is a cross-check for 2019-20+.
* **Box tiers** use `FGA + 0.40·FTA − OREB + TOV`, with the coefficient fitted
  on 1996-97+ and documented as such.
* Team turnovers (`turnoversTeam`) are NULL, not 0, in 5% of 1996+
  team-games; treated as 0 and counted.

Ball control uses `PLAYS` instead of `POSS` because plays are attributable to
individual players, which keeps the player → team roll-up exact. Defense uses
`POSS` because it is measured against the opponent's possessions.

**Court zones** (from `x_legacy`, `y_legacy`: tenths of a foot, rim at the
origin, +y toward half court). The 2-vs-3 split uses the recorded shot
value; geometry splits within each class:

| Zone | Rule | 2025-26 share | FG% | xPTS/shot |
|---|---|---:|---:|---:|
| Restricted area (`RA`) | 2PT and √(x²+y²) < 40 | 28.4% | 67.1% | 1.342 |
| Paint, non-RA (`PAINT`) | 2PT and \|x\| ≤ 80 and y ≤ 138 | 20.0% | 44.6% | 0.891 |
| Mid-range (`MID`) | other 2PT | 10.0% | 41.7% | 0.833 |
| Corner 3 (`C3`) | 3PT and \|x\| ≥ 220 and y ≤ 87 | 10.7% | 38.7% | 1.161 |
| Above-the-break 3 (`ATB3`) | other 3PT with y ≤ 417.5 | 30.8% | 35.0% | 1.050 |
| Backcourt (excluded) | 3PT and y > 417.5 | 0.01% | | |

These are the court lines (4 ft arc, 16 ft lane, free-throw line 13.75 ft
from the rim, 22 ft corner three up to 14 ft from the baseline). The rule
reproduces the NBA's official `SHOT_ZONE_BASIC` on **99.992%** of 337,073
2025-26 shots (the 28 mismatches are shots whose official 2/3 flag contradicts
their official zone) and on 99.94% of 2005-06 shots. In **1996-97**, the last
season of the 22 ft three-point line, threes from 22–23.75 ft fall in `ATB3`
because the rule uses the recorded shot value. That's correct for the season,
but it makes 1996-97 zone *shares* not comparable with later seasons. Ratings
are unaffected because baselines are per season. Left/right corners are kept for the heat map (x < 0 is
the left corner) but rated as one zone. *Perimeter* = {MID, C3, ATB3};
*paint* = {RA, PAINT}.

Shots with no coordinates count toward box-score totals but are excluded from
zone metrics, and the coverage is reported per game. Nearly all of them (12.7k)
are in **2019-20**, where they are 6.8% of the season's FGA, so 2019-20 zone
metrics rest on 93% of its shots. Every other season is ≥ 99.9%.

**Positions.** For player *p* with flags *g, f, c* ∈ {0,1}:
`w_bc(p) = g / (g+f+c)`, `w_fc(p) = (f+c) / (g+f+c)`. Players with no flag
get no Backcourt/Frontcourt weight (≤0.45% of minutes in any season).

**Orientation.** Every component is signed so that **positive = good for the
unit being rated**, including defense (fewer points allowed → positive).

**Units.** Every component and composite is **points relative to league
average for that season**, per 100 of its exposure unit. Components inside a
dimension therefore add up, and the weighting is the points they're worth
rather than a chosen weight.

## 2. The 0–100 scale

For a raw composite (or component) value *v* of unit *u* at level *L* in
season *s*:

```
rating(u) = clip( 50 + 10 · (v_u − μ_{L,s}) / σ_{L,s} , 0, 100 )
```

* *L* ∈ {player-game, team-game, player-season, team-season}. **Each level has
  its own reference distribution** because noise differs by level: a single
  game varies far more than a season.
* μ, σ: mean and SD of *v* over the level's reference population in season
  *s*, weighted by exposure at player levels:

| Level | Reference population (regular season) |
|---|---|
| team-season | all teams (unweighted) |
| team-game | all team-games (unweighted) |
| player-season | players with exposure ≥ the dimension's qualifying minimum (§5), exposure-weighted |
| player-game | player-games with exposure ≥ the dimension's per-game minimum (§5), exposure-weighted |

* Units below the qualifying minimum still get a value and a rating, flagged
  `low_sample = true`. The minimums in §5 are starting values; the
  stabilization analysis in the validation notebook will tune them.
* Because raw values are already relative to league average, μ ≈ 0 at team
  levels; it is computed anyway.
* **Deltas for the in-game use case** are computed on the game scale:
  `Δ = rating(tonight) − mean team-game rating this season`, plus the raw
  points-per-100 delta. Never subtract a season-level rating from a
  game-level one, because their σ differ.

## 3. Roll-up: player → game → team → season

**One rule: ratios of sums, never averages of ratios or ratings.**

Every raw value is a function of additive counts (e.g. Σ points above
expected, Σ FGA). A higher level's counts are the sum of the lower level's
counts; the raw value is recomputed from those sums, then rated against
that level's reference. Expected values (xPTS) are computed per shot with
that season's league baselines before summing, so multi-season roll-ups
(careers) are also exact.

Two kinds of attribution:

* **Actor-credited** events (a player's own shots, turnovers, assists):
  team-game counts = Σ over the team's players. Team-only events (team
  turnovers, 0.7 per game) are added at the team level.
* **On-court** events (Defense, Presence): every opponent event is credited
  to each of the five players on the floor. Σ_players counts = 5 × team
  counts, so the ratio is unchanged and the team value equals the
  exposure-weighted mean of its players' on-court values exactly.

Unit tests assert these identities to 1e-9 (acceptance test T4).

## 4. Small-sample shrinkage

Player-season values are shrunk toward league average (0 on the raw scale)
before rating:

```
v̂ = v · n / (n + k_d)
```

*n* = the player's exposure; *k_d* = the exposure at which the dimension's
split-half reliability is 0.5. *k_d* is estimated, not chosen. On qualified
player-seasons pooled over 2013-14 → 2025-26, compute the odd/even-game
split-half correlation *r* at mean exposure *n̄*, apply Spearman-Brown to the
full sample, and solve `k_d = n̄ · (1 − r_full) / r_full`. The estimates are
written to `metrics.shrinkage_constants` by the build, so they're reproducible.

Game-level ratings are **not** shrunk: they describe what happened tonight.
Team-season samples are large enough that shrinkage is skipped.

## 5. The six dimensions

Notation for a unit's counts in season *s*: for each rated shot *i*: zone
*z_i*, value *v_i* ∈ {2, 3}, made *m_i* ∈ {0, 1}. League zone make rate
`p_{s,z}`; expected points `xP_i = v_i · p_{s,z_i}`. League points per FGA
`lgPPS_s = Σ v·m / FGA` (league-wide).

### 5.1 Shooting: shot quality vs shot making, by zone

**Question.** Is the unit getting good shots (where it shoots from), and is
it making more than a league-average shooter would from those spots?

| Component | Formula (per 100 FGA) | Meaning |
|---|---|---|
| Shot quality `SQ` | `100 · ( Σ xP_i / FGA − lgPPS_s )` | Points per 100 shots the zone mix is worth, vs league |
| Shot making `SM` | `100 · Σ (v_i·m_i − xP_i) / FGA` | Points per 100 shots above what league-average makes from those zones would give |
| **Shooting** | `SQ + SM` | = 100 · (FG points per FGA − league) — the eFG% gap, decomposed |

Per-zone profile (heat map; not rated in the composite): for each zone,
share of FGA − league share, FG% − `p_{s,z}`, and making
`Σ_{i∈z} (v·m − xP) / FGA_z`.

* **Inputs:** `stg.shots` (zone, value, made); league `p_{s,z}`.
* **Source:** Kaggle PBP (validated against nba_api shot charts for 2025-26).
* **Levels:** player-game, player-season, team-game, team-season. Actor-credited.
* **Qualifying minimum:** 200 FGA per player-season; 8 FGA per player-game.
* **Earliest:** **1996-97** (tier A). Before 2010-11 rim attempts carry no
  within-RA detail, which doesn't matter at zone resolution.

**Tier B: `Shooting (box)`.** Two classes instead of five zones: 2PT and 3PT.
`SQ = 100 · ( [2·(FGA−3PA)·p_{s,2} + 3·3PA·p_{s,3}] / FGA − lgPPS_s )`,
`SM = 100 · ( (PTS − FTM) / FGA − [same expected term] )`.
FG points are taken as `PTS − FTM` in all box tiers because points and FTM are
recorded in every era, while 3PM is unreliable before 1986-87.
Earliest **1986-87** (3PA recorded ≥95%). Computed for all seasons ≥ 1986-87
so tier A and tier B can be compared from 1996-97 on.

**Tier C: `Shooting (box, FG only)`.** `SM = 100 · ( (PTS − FTM)/FGA − lgPPS_s )`
only, with `SQ` undefined (no zone information; few or no threes). Earliest
**1974-75** (FGA recorded ≥95%). Not computable before 1974-75.

### 5.2 Defense: opponent shot quality and making, forced turnovers, fouls

**Question.** How many points per 100 opponent possessions does the defense
save vs league average, and through which channel?

For the unit's defensive possessions, opponent counts are prefixed `o`.
`r = oFGA / oPOSS` (opponent shots per possession).

| Component | Formula (per 100 opp. possessions) |
|---|---|
| Shot quality allowed `DQ` | `100 · r · ( lgPPS_s − Σ_opp xP / oFGA )` |
| Shot making allowed `DM` | `100 · r · ( − Σ_opp (v·m − xP) / oFGA )` |
| Forced turnovers `DT` | `100 · ( oTOV / oPOSS − lgTOVr_s ) · lgPPNT_s` |
| Fouls `DF` | `100 · r · ( lgFTr_s − oFTA / oFGA ) · lgFT%_s` |
| Defensive rebounding `DR` *(D3)* | `100 · ( DREB% − lgDREB%_s ) · ( oMissedFGA / oPOSS ) · lgPPP_s` |
| **Defense** | `DQ + DM + DT + DF (+ DR)` |

League constants per season: `lgTOVr` = TOV/POSS; `lgPPNT` = points per
non-turnover possession; `lgFTr` = FTA/FGA; `lgFT%`; `lgDREB%`; `lgPPP` =
points per possession. `oTOV` includes the opponent's team turnovers.

`DR` values a defensive rebound at roughly one possession. It's an
approximation, labelled as such, and the reason it is a switchable component.

* **Inputs:** `stg.shots` (opponent), box TOV / FTA / REB, stints for player level.
* **Levels:** team-game, team-season (opponent events), player-game and
  player-season (**on-court**, D7; exact roll-up). Supporting player
  sub-ratings, credited events per 100 on-court defensive possessions:
  steals, blocks (`blockPersonId`), charges drawn, personal fouls (negative).
* **Qualifying minimum:** 1,000 on-court defensive possessions per
  player-season; 20 per player-game.
* **Earliest:** **1996-97** (tier A, team and player).

**Tier B: `Defense (box)`.** Team level only. `DQ`/`DM` use the two-class
quality from 5.1 tier B applied to opponent shots; `DT`, `DF`, `DR` as
above from box scores. Earliest **1986-87** (3PA). **Tier B−**, from
**1983-84** (OREB/TOV recorded ≥95%), drops the quality/making split and uses
`DQ+DM = 100·r·(lgPPS − opp FG points per FGA)`. Not computable before
1983-84: no possessions without OREB and TOV. No player-level Defense before
1996-97: no stints.

Note for validation: opponent shot *making* (especially on threes) is widely
found to be mostly noise at small samples; this data will tell us how much. The stabilization analysis will show whether `DM` deserves its
full points weight; the spec keeps it, since it's real points allowed.

### 5.3 Ball control: turnovers, assists, possessions ending in a shot

**Question.** How often does the unit keep the ball long enough to get a
shot (or a trip to the line)?

The brief's three items overlap: in box-score data, "possessions ending in
a shot" = 1 − turnover rate exactly, so they're one component, not two. A/TO
mixes creation (assists) with security (turnovers) and isn't in points.
The composite is therefore the points-valued turnover component (+ OREB under
D3), and A/TO and assist rate are supporting sub-ratings.

| Component | Formula |
|---|---|
| Turnover avoidance `BT` (per 100 plays) | `100 · ( lgTOVp_s − TOV / PLAYS ) · lgPPNT_s` |
| Offensive rebounding `BO` *(D3, per 100 poss.)* | `100 · ( OREB% − lgOREB%_s ) · ( MissedFGA / POSS ) · lgPPP_s` |
| **Ball control** | `BT (+ BO)` |
| Supporting: shot-ending rate | `1 − TOV / PLAYS` (identical information to `BT`; reported because it's the brief's framing) |
| Supporting: A/TO | `ln( (AST + 0.5) / (TOV + 0.5) )` relative to league, rated separately |
| Supporting: assist rate | `AST / FGM` relative to league |

`lgTOVp` = league TOV / PLAYS. Under D3, OREB% is a team / on-court quantity,
so `BO` is computed for teams and on-court for players (not credited).

* **Inputs:** box FGA, FTA, TOV (+ team TOV), AST, OREB. No PBP needed.
* **Levels:** all four; actor-credited (`BT`), on-court (`BO`).
* **Qualifying minimum:** 500 plays per player-season; 10 per player-game.
* **Earliest:** **1983-84** (TOV and OREB recorded ≥95%). The box score *is*
  the full version, so there is no tier A/B split. Not computable before
  1983-84 under D9(a); under D9(b) `BT` could extend to 1977-78.

### 5.4 Backcourt and 5.5 Frontcourt

Under **D4(d)**, with position weights `w = w_bc` (Backcourt) or `w_fc`
(Frontcourt):

| Part | Backcourt | Frontcourt |
|---|---|---|
| Shooting | Σ over players of `w(p)` × the player's shot counts, all zones | same with `w_fc` |
| Ball control | Σ `w(p)` × the player's `BT` counts | same with `w_fc` |
| Defense | `DQ`, `DM` over opponent shots in **perimeter** zones; `DT`, `DF` from credited events (steals, charges drawn, fouls) of players weighted by `w` | `DQ`, `DM` over opponent shots in **paint** zones; credited `DT`, `DF` weighted by `w_fc`; `DR` (D3) |
| **Composite** | Shooting + Ball control + Defense parts, each per 100 team possessions so they add | same |

Converting to a common denominator: a part measured per 100 FGA is multiplied
by that part's FGA / team POSS, so the parts add as points per 100 team
possessions.

Notes:
* Backcourt + Frontcourt ≈ team Shooting + Ball control + Defense, but not
  exactly: turnovers forced and fouls with no credited player (e.g. an
  opponent's travel) belong to neither. The unattributed remainder is
  reported.
* **Player level:** a player's Backcourt value is the player's contribution
  weighted by `w_bc(p)`. Players with `w < 0.5` for a group get no rating in it; the
  reference population is players with `w ≥ 0.5`. So a guard's Backcourt
  rating is **rated among backcourt players**, which is the scouting view.
* **Earliest:** **1996-97** (tier A). **Tier B (`Backcourt/Frontcourt (box)`,
  offense only)**: position-weighted Shooting tier B + Ball control from
  **1986-87**. No defensive part before 1996-97, since zones need PBP.

Under D4(a), defense would use on-court opponent events weighted by
position; under D4(b) or (c), offense would also be filtered by shot zone. The
formulas above change accordingly.

### 5.6 Presence: on/off impact

**Question.** How much does each team dimension shift while this player is
on the floor?

For player *p*, team *T*, season *s*, using only games *p* appeared in
(D6):

```
Presence_d(p) = v_d(T, p on court) − v_d(T, p off court)
```

for each team-level dimension *d* ∈ {Shooting, Defense, Ball control,
Backcourt, Frontcourt}, each computed with its section-5 formulas over the
events in the on / off stints.

* **Composite Presence** = Δ Shooting + Δ Defense + Δ Ball control, each
  converted to points per 100 team possessions (Backcourt/Frontcourt are
  subsets, so they're excluded to avoid double counting). This is
  approximately on/off net rating decomposed by dimension; the residual
  (mostly free throws, plus rebounding if D3 is off) is reported.
* **Exposure:** min(on POSS, off POSS). On/off is notoriously noisy, so
  shrinkage `k` (§4) will be large. **Qualifying minimum:** 1,000 on and 300
  off possessions per player-season.
* **Levels:** player-season, plus rolling windows (last 10 / 20 games). A
  single game's on/off is reported raw and **unrated**, because it's
  dominated by noise. There's no team-level Presence (a team has no "off");
  the team view shows its roster's Presence.
* **Stints** (D8): reconstructed from Kaggle PBP. Live schema (2019-20+):
  substitution `in`/`out` rows. Legacy schema: `personId` is the player out;
  the player in is parsed from `SUB: <in> FOR <out>` and resolved by last name
  against that team's box-score roster for the game. Ties are broken by
  excluding players already on court. Period starters are inferred: players
  with an event in the period before any substitution involving them, filled
  from the previous period's closing five. Each team-game must pass two
  checks: 5 players on court at every event, and reconstructed minutes within
  1 minute of box minutes for every player. Games that fail fall back to
  `gamerotation`.
* **Earliest:** **1996-97** (tier A).

**Tier B: `Presence (box)`.** On/off **net only** (no per-dimension split)
from box plus/minus and minutes:
`on = 48·PM / MIN`, `off = 48·(team margin − PM) / (team MIN/5 − MIN)`.
Box plus/minus starts in 1996-97, so this covers the same era and serves only
as a fallback and cross-check. **Not computable before 1996-97.**

Out of scope for v1: regularized adjusted plus-minus (RAPM), which controls
for teammates and opponents. It's the natural v2 of Presence.

## 6. Availability by era

| Dimension | Tier A (PBP) | Tier B (box) | Tier C / B− | Nothing before |
|---|---|---|---|---|
| Shooting | 1996-97 | 1986-87 | 1974-75 (making only) | 1974-75 |
| Defense (team) | 1996-97 | 1986-87 | 1983-84 (no quality/making split) | 1983-84 |
| Defense (player) | 1996-97 | n/a | n/a | 1996-97 |
| Ball control | (box is full) | 1983-84 | n/a | 1983-84 |
| Backcourt / Frontcourt | 1996-97 | 1986-87 (offense only) | n/a | 1986-87 |
| Presence | 1996-97 | 1996-97 (net only) | n/a | 1996-97 |

Every output row carries a `tier` column (`A`, `B`, `B-`, `C`). **Tiers are
different constructs and are never mixed in one reference distribution**:
a 1990 tier-B rating is relative to 1990 tier-B values. For seasons where two
tiers overlap (1996-97 on), both are built and their correlation is reported,
so the degradation is quantified, not assumed.

Seasons before 1954-55 (incomplete game coverage) are excluded from league
baselines entirely.

## 7. Outputs and the CLI

Planned (not built yet). One command rebuilds everything from the raw caches:

```bash
uv run courtvision build              # kaggle-load → nba-api load → metrics, all seasons
uv run courtvision metrics build --season 2025   # one season
```

Tables (schema `metrics`, also written to `data/parquet/metrics/`):

| Table | Grain | Contents |
|---|---|---|
| `league_baselines` | season | every league constant used above |
| `zone_baselines` | season × zone | FGA, share, FG%, xPTS |
| `shrinkage_constants` | dimension × level | *k_d*, *r*, *n̄* (§4) |
| `team_game`, `team_season` | team × game / season | exposures, every component (raw), composites (raw), ratings, `tier` |
| `player_game`, `player_season` | player × game / season (× team) | same, plus `low_sample`, position weights |
| `presence_season`, `presence_rolling` | player × team × season / window | on, off, Δ per dimension, composite, rating |
| `build_info` | one row | git commit, Kaggle manifest timestamp, nba_api cache summary, spec version |

`build_info` ties every rating to the exact code and data snapshot that
produced it.

## 8. Acceptance tests

Before any rating is trusted:

| # | Test | Threshold | Status |
|---|---|---|---|
| T1 | Zone rule vs official `SHOT_ZONE_BASIC` | ≥ 99.9% | **Pass.** 99.992% on 2025-26, 99.94% on 2005-06. 1996-97 is 89.6% because official zones use today's arc while 1994-95 to 1996-97 had a 22 ft line; the rule follows the recorded shot value, which is correct (see data audit) |
| T2 | PBP-counted `POSS` vs Kaggle's official `possessions` (team-game, 1996-97+) | MAE ≤ 0.5 | to do. (Box formula already measured: MAE 2.5 with 0.44, 1.7 with 0.40, which is why tier A counts instead) |
| T3 | Reconstructed stints vs `gamerotation` (2025-26) | ≥ 99% of player-games within 1 min; on-court five exact for ≥ 98% of events | to do |
| T4 | Roll-up identities (§3) | exact to 1e-9 | unit tests |
| T5 | Team-season ratings per season | mean 50 ± 0.5 | unit test |
| T6 | Hand-checked games | 3 games of 2025-26 computed by hand in a fixture | next step |
| T7 | Tier A vs tier B correlation (1996-97+) | reported, not gated | validation notebook |

## 9. Out of scope for v1

Opponent-strength adjustment, RAPM, shot-type-aware expected points
(e.g. dunk vs layup within the RA; `subType` is available, so this is an easy v2
for Shooting), shot-clock context, and anything needing player-tracking data.
**No metric here needs player (x, y) positions:** zones come from shot
locations, which are event data.
