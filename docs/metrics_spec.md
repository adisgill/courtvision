# Metrics spec

**Status: v1.8.** v1.0 approved 2026-11-12 with every recommended decision
(D1–D9); v1.1 (free throws, fouls valuation, role, per-possession impact, opponent
adjustment, market model) approved the same day, see [section 11](#11-v11-2026-11-12);
v1.2 (box-score prior for impact) also the same day, see [section 12](#12-v12-2026-11-12);
v1.3 (contracts, market-comparables pricing, free agency, NBA.com advanced stats) and
next-game forecasts in sections 13 and 14; v1.4 (coaching analytics, walk-forward
prediction engine) in sections 15 and 16; v1.5 (play types, hexagon shot maps,
scouting report) in sections 17 and 18; v1.6 (lineups) in section 19; v1.7 (official injury report) in section 20; v1.8 (prediction ledger, scorecard) in section 21.
Implemented in `src/courtvision/metrics/` and `src/courtvision/market.py`;
`uv run courtvision build` reproduces every number. Section 10 records what
implementation added or settled beyond the draft; section 8 has the
acceptance-test results. Grounded in [`data_audit.md`](data_audit.md).

Contents: [0 Decisions](#0-decisions) ·
[1 Conventions](#1-conventions) · [2 Scale](#2-the-0100-scale) ·
[3 Roll-up](#3-roll-up-player--game--team--season) ·
[4 Shrinkage](#4-small-sample-shrinkage) · [5 Dimensions](#5-the-six-dimensions) ·
[6 Availability](#6-availability-by-era) · [7 Outputs & CLI](#7-outputs-and-the-cli) ·
[8 Acceptance tests](#8-acceptance-tests) · [9 Out of scope](#9-out-of-scope-for-v1) ·
[10 Implementation notes](#10-implementation-notes-v10) · [11 v1.1](#11-v11-2026-11-12) ·
[12 v1.2](#12-v12-2026-11-12) · [13 v1.3](#13-v13-contracts-pricing-free-agency-nbacom-advanced-stats-2026-11-12) ·
[14 Forecasts](#14-forecasts-for-each-teams-next-game-2026-11-12) ·
[15 Coaching](#15-coaching-analytics-2026-11-12) ·
[16 Walk-forward](#16-walk-forward-prediction-engine-2026-11-12) ·
[17 Play types](#17-play-types-2026-11-12) ·
[18 Shot maps and scouting](#18-hexagon-shot-maps-and-the-scouting-report-2026-11-12) ·
[19 Lineups](#19-lineups-2026-11-12) ·
[20 Injury report](#20-the-official-injury-report-2026-11-12) ·
[21 Scorecard](#21-prediction-ledger-and-scorecard-2026-11-12)

---

## 0. Decisions

All nine were approved as recommended (2026-11-12). The table is kept as the
record of what was decided and why.

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | **0–100 mapping** | (a) `50 + 10·z`, clipped: 50 = league average, ±10 = ±1 SD; (b) percentile within season | **(a)**. Keeps magnitude, so "+8 tonight" means the same thing every season, and deltas are meaningful for the in-game use case. Percentiles flatten the tails and make deltas depend on where you start. |
| D2 | **Free throws in Shooting** | (a) Shooting is field goals only (decomposes eFG%); (b) include FT trips (decomposes TS%) | **(a)**. The brief defines Shooting by court zone; FTs have no zone. FT *allowed* is in Defense; offensive FT rate is reported as a supporting stat. **Superseded in v1.1 (section 11.1):** free throws join Shooting as two separate components, valued net of the shot they replace. |
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
| Defensive rebounding `DR` *(D3)* | `100 · ( DREB − lgDREB%_s · (DREB + oOREB) ) / oPOSS · lgPPP_s` |
| **Defense** | `DQ + DM + DT + DF (+ DR)` |

League constants per season: `lgTOVr` = TOV/POSS; `lgPPNT` = points per
non-turnover possession; `lgFTr` = FTA/FGA; `lgFT%`; `lgDREB%`; `lgPPP` =
points per possession. `oTOV` includes the opponent's team turnovers.

`DR` counts defensive rebounds above what the league rate would collect from the unit's rebound chances (its defensive rebounds plus the opponent's offensive rebounds) and values each at one possession. It's an
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
| Offensive rebounding `BO` *(D3, per 100 poss.)* | `100 · ( OREB − lgOREB%_s · (OREB + oDREB) ) / POSS · lgPPP_s` |
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

**v1.1:** the Presence rating is now per-possession impact (regularized adjusted
plus-minus, section 11.4); the on/off measures above remain as supporting detail.

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

One command rebuilds everything from the raw caches (about 7 minutes on an
8-core machine: 2 for play-by-play parsing, 5 for SQL):

```bash
uv run courtvision build                         # kaggle-load → nba-api load → metrics
uv run courtvision metrics build                 # metrics only, from the loaded sources
uv run courtvision metrics build --season 2025   # re-parse one season's play-by-play
```

League baselines, reference distributions and shrinkage constants span all
seasons, so `--season` limits only the play-by-play re-parse; the SQL stage
always runs in full.

Tables (schema `metrics`, also written to `data/parquet/metrics/`):

| Table | Grain | Contents |
|---|---|---|
| `league_baselines` | season | every league constant used above |
| `zone_baselines` | season × zone | FGA, share, FG%, xPTS |
| `shrinkage_constants` | tier × measure | *k*, split-half *r*, mean half exposure (§4) |
| `reference_stats`, `best_tier` | level × season × tier × measure | μ, σ and size of each reference population; the tier shown in wide tables |
| `ratings_long` | unit × tier × measure | every value (raw and shrunk), exposure, `low_sample`, rating, all tiers |
| `team_game`, `team_season`, `player_season` | unit | wide: `<measure>_value / _rating / _tier / _low_sample` for every measure, best tier |
| `player_game` | player × game | wide, headline dimensions only (components are in `ratings_long`) |
| `team_game_counts`, `player_game_counts` | unit | the additive counts every value is computed from |
| `presence_season`, `presence_rolling` | player × team × season / last 10 and 20 games | Δ per dimension, composite, rating (season), box net on/off |
| `lineup_qa_summary`, `stat_first_season` | season / stat | lineup and play-by-play QA rates; D9 availability |
| `build_info` | one row | git commit, Kaggle download time, spec version, tier start seasons |
| `player_impact`, `impact_lambda`, `impact_cv` | player × season / mode | per-possession impact (v1.1, 11.4) and its cross-validation |
| `team_season_adj` | team × season × tier × measure | schedule effect, opponent-adjusted value and rating (11.5) |
| `player_role` | player × team × season | usage, assist rate (11.3) |
| `market.players`, `market.teams`, `market.options`, `market.params` | player / team / option | value, price and risk; budgets and needs; options; parameters (11.6) |

`build_info` ties every rating to the exact code and data snapshot that
produced it.

## 8. Acceptance tests

Before any rating is trusted:

| # | Test | Threshold | Status |
|---|---|---|---|
| T1 | Zone rule vs official `SHOT_ZONE_BASIC` | ≥ 99.9% | **Pass.** 99.992% on 2025-26, 99.94% on 2005-06. 1996-97 is 89.6% because official zones use today's arc while 1994-95 to 1996-97 had a 22 ft line; the rule follows the recorded shot value, which is correct (see data audit) |
| T2 | PBP-counted `POSS` vs Kaggle's official `possessions` (team-game, 1996-97+) | MAE ≤ 0.5 | **Pass overall: MAE 0.28**, bias +0.14, 77% exact, 97% within 1 (76,328 team-games). 2002-03 on: 0.17–0.29 every season except 2017-18 (0.44). Over threshold: 1996-97 (0.55), 1997-98 (0.53), 1999-00 (0.50); 1998-99 to 2001-02 run 0.48–0.50, overcounting by ~0.4 from early-legacy event logging. The box formula was 2.5. |
| T3 | Reconstructed stints vs `gamerotation` (2025-26) | ≥ 99% of player-games within 1 min; on-court five exact for ≥ 98% of events | **Pass.** 99.53% of 28,698 player-games within 1 minute (MAE 0.16 min); 99.47% of 1.06M team-events have the exact official five. All seasons: lineups pass QA in 98.8% of games (`lineup_qa_summary`) |
| T4 | Roll-up identities (§3) | exact to 1e-9 | **Pass** (`tests/test_metrics_integration.py`): season = Σ games; team shots = Σ player shots; on-court = 5 × team; team Defense = exposure-weighted mean of players'; Backcourt players sum to team |
| T5 | Team-season ratings per season | mean 50 ± 0.5 | **Pass** for every season, tier and measure |
| T6 | Hand-checked games | 3 games of 2025-26 computed by hand | **Pass.** Shooting (SQ, SM) from nba_api's official shot zones and league rates, Ball control `BT` from the official box score, Defense `DT`/`DF` from the official box score and possessions; an independent source from the Kaggle play-by-play the pipeline uses |
| T7 | Tier A vs tier B correlation (1996-97+) | reported, not gated | [`validation.md`](validation.md) section 4 |

## 9. Out of scope for v1

(Opponent-strength adjustment and RAPM were added in v1.1, section 11.)
Shot-type-aware expected points
(e.g. dunk vs layup within the RA; `subType` is available, so this is an easy v2
for Shooting), shot-clock context, and anything needing player-tracking data.
**No metric here needs player (x, y) positions:** zones come from shot
locations, which are event data.

## 10. Implementation notes (v1.0)

What building it settled that the draft left open. Code references are to
`src/courtvision/metrics/`.

**Event stream (`stg.events`).** Both play-by-play schemas are normalized into one
vocabulary (`fga`, `ft`, `reb`, `tov`, `foul`, `sub`, ...). Legacy games are
ordered by (period, clock descending, `actionNumber`), because `actionNumber` is
out of clock order for about 0.4% of legacy events; live games use
`orderNumber`. Team ids are repaired from `teamId`, then `playerteamId` (the only
team column in 2026-27), then a team id stored in `personId` (legacy team
events), then the player's box-score team, then the live `possession` column
(team rebounds and turnovers in the 2025-26 playoffs and 2026-27).

**Possessions (`pbp.py`).** A possession is a run of events with the same
offensive team inside a period. Offense is named only by events the team with
the ball can produce: shot attempts, non-technical free throws, turnovers and
rebounds. Two period-end rules came out of acceptance test T2:

* after the period's last made basket, last free throw of a trip, or turnover,
  the other team is credited a possession even though nothing else is logged,
  because the official count includes it;
* in the legacy schema only, a period-ending run made of nothing but team
  rebounds (a missed heave rebounded at the buzzer) is not a possession. The
  official count for live-schema seasons does include it.

**Lineups.** Period starters are players whose first involvement in the period
is anything other than entering. Anyone still missing is filled from the
previous period's closing five, then by box minutes. Legacy substitutions name
the player coming in only by text, so the name is resolved in this order:

1. the name the same game's own substitution rows print for that player when he
   leaves (this also covers renamed players, e.g. Enes Kanter, now Freedom);
2. the play-by-play player name;
3. roster last name, then first name ("Nene");
4. abbreviated first name plus last name ("J. Smith", "Ja. Smith");
5. matching every word of the name.

210 early-2019-20 games use a third format ("X replaced by Y") that carries the
incoming player's id. A game's lineups pass QA when:

* both teams have exactly five players on the floor at every event;
* every substitution resolves;
* every player's minutes are within one minute of the box score.

The result is 98.8% of 38,183 games, at least 95.5% in every season
(`metrics.lineup_qa_summary`). On-court metrics (player Defense, `BO`,
Backcourt/Frontcourt defense parts, Presence) use passing games only.

**Play-by-play completeness.** A game is used for tier A only when both teams'
play-by-play points and field-goal attempts are within 3 of the box score. More
than 99% of team-games pass every season. The failures are a few 2019-20 games
truncated in the source.

**Credited defensive events.** Steals and fouls for Backcourt/Frontcourt and the
supporting Defense sub-ratings come from the box score, because legacy
play-by-play has no steal person.

**Player-level Backcourt/Frontcourt.** A player's value is his share of the
team's:

* his own position-weighted shooting, turnovers, steals and fouls;
* one fifth (`share = 0.2`) of the on-court zone-defense part and rebounding
  part.

Both are expressed per 100 on-court possessions, so the five players on the
floor sum exactly to the team. Box tier B player values are per 100 of the
player's own plays.

**Ball control constant.** `lgTOVp` includes team turnovers, so team values
average exactly zero. Player values, which have no team turnovers, average about
+0.6 (measured). Player ratings are unaffected because each level has its own reference.

**Presence.** Ball control on/off uses play-by-play turnovers and plays, so on and
off are measured the same way. Backcourt/Frontcourt on/off has:

* position-weighted shooting;
* turnovers;
* zone defense (and rebounding for Frontcourt).

It has no credited steals or fouls, which aren't in the legacy play-by-play.
Rolling windows (`presence_rolling`, last 10 and 20 games) are raw and unrated,
like single games. The box-score net on/off is in `presence_season.presence_net_box`.

**Season rows.** Season aggregates exist for the regular season and the playoffs.
Play-in and Cup-final games are rated at game level only. Player-season rows are
per team, plus a `team_id = 0` row for the whole season. Reference populations
use the whole-season rows, so traded players count once.

**Tiers in the outputs.** `metrics.ratings_long` keeps every tier, which gives
T7 its overlap seasons. The wide tables (`team_game`, `player_season`, ...) show,
for each measure, the best tier that exists in that season, with a `<measure>_tier`
column. Tier start seasons are computed from the data under D9(a), not
hard-coded, and they reproduce section 6:

| Tier | Start |
|---|---|
| Shooting C | 1974 |
| Ball control / Defense B− | 1983 |
| Shooting / Defense / Backcourt-Frontcourt B | 1986 |
| A | 1996 |

**Rebounding as a ratio of sums.** The draft wrote `DR` and `BO` as (rebound-rate gap) × (misses per possession), a product of two ratios. Players' on-court values then don't average to the team's, which breaks the T4 identity. The v1.0 formulas in 5.2 and 5.3 count extra rebounds against the rebound chances actually available, which is linear in counts and exact under roll-up. The two forms differ only by using rebound chances instead of missed field goals as the opportunity count.

**Shrinkage.** `k` is solved at the half-season exposure from `r = n_h / (n_h + k)`,
which is algebraically the Spearman-Brown route in section 4. A measure whose
halves don't correlate positively is shrunk fully to league average.

## 11. v1.1 (2026-11-12)

Approved by the user on 2026-11-12 ("add them all") after validation against
NBA.com's ratings ([`validation.md`](validation.md) section 6) found two valuation
gaps. v1.1 adds five things to the ratings and a market model on top of them.

### 11.1 Free throws in Shooting (supersedes D2)

D2 kept Shooting to field goals. Validation showed what that costs: the offense
parts explained 93% of NBA.com's offensive rating without free throws and 98%
with them. Shooting gains two components, from box-score counts so every tier has
them (per 100 FGA, like `SQ` and `SM`):

| Component | Formula |
|---|---|
| Drawing fouls `FD` | `100 · (FTA − lgFTr_s · FGA) / FGA · (lgFT%_s − ½ · lgPPS_s)` |
| Free-throw making `FM` | `100 · (FTM − lgFT%_s · FTA) / FGA` |
| **Shooting** | `SQ + SM + FD + FM` (tier B: two-class `SQ`/`SM`; tier C: FG gap) |

**Why `lgFT% − ½·lgPPS`.** A trip to the line mostly *replaces* a shot: a
two-shot foul ends the possession without the field-goal attempt that was fouled.
An extra FTA is therefore worth its make rate minus half a league shot (two FTA per
replaced attempt), about 0.26 points rather than the 0.77 of a plain make rate.
And-ones and three-shot fouls move this slightly. Backcourt / Frontcourt gain the
same parts on their position-weighted counts (`ft`, per 100 possessions), and the
box tier B of both includes them.

### 11.2 Fouls valued net of the replaced shot (Defense `DF`)

v1.0 valued every free throw allowed at `lgFT%`. Regressing NBA.com's net rating
on the components gave `DF` a weight of 0.27 while every other part sat between
0.83 and 1.10. `DF` now uses the same net value as `FD`:

`DF = 100 · r · (lgFTr − oFTA/oFGA) · (lgFT% − ½ · lgPPS)`

The player foul parts of Backcourt / Frontcourt (`df`) are scaled by the same
ratio, `(lgFT% − ½·lgPPS) / lgFT%`. Presence's on/off Shooting includes free throws
the same way.

### 11.3 Role context: usage and assist rate (not rated)

`metrics.player_role`, per player-season (per team and `team_id = 0`):
`usage = (FGA + 0.44·FTA + TOV) / team's (FGA + 0.44·FTA + TOV) while on court`,
`ast_pct = AST / teammates' FGM while on court`. Only games with rebuilt lineups,
so both sides cover the same minutes. Not rated: high usage is a role, not a skill.
It is shown next to the ratings because a rate stat at 30% usage and at 12% describe
different players (validation section 6: usage alone explains 45% of NBA.com's PIE,
the rate ratings 16%).

### 11.4 Per-possession impact (adjusted plus-minus) and Presence

`metrics.player_impact`, per player-season (regular season, 1996-97 on), built by
`courtvision.metrics.impact`:

* **Rows.** Every stint (a stretch with the same ten players, from the rebuilt
  lineups, in games passing lineup QA) gives two rows, one per offense:
  `y = 100 · points / possessions − league mean`, weight = possessions.
* **Model.** `y = Σ off(5 offensive players) + Σ def(5 defensive players) + h · site`.
  Ridge regression shrinks each coefficient toward a prior:
  * `season_only`: prior 0, this season alone;
  * `impact` (headline): prior = the player's estimate after the previous season,
    or the average newcomer for a player new to the league. Seasons are fitted in
    order, so each estimate carries every earlier season's evidence.
* **Ridge strength** `λ` (in possessions) is chosen per mode by 5-fold
  cross-validation over games on 2004, 2009, 2014, 2019 and 2024
  (`metrics.impact_cv`, `metrics.impact_lambda`).
* **Outputs** (points per 100 possessions, positive = good, defense negated):
  `impact_off`, `impact_def`, `impact`, `impact_se` (ridge posterior), `season_only`,
  `points_added = (off·poss_off + def·poss_def) / 100`, and `impact_rating`
  (50 + 10·z among players with ≥ 2,000 possessions × season fraction,
  possession-weighted).
* **Presence** (the sixth dimension) is now `impact_rating`. The v1.0 on/off
  measures stay in `presence_season` as supporting detail: on/off is what the team
  did with the player on the floor; impact is how much of that was him.

**Per possession, not per minute.** Impact is per 100 possessions so that players on
fast and slow teams compare fairly; `points_added` gives the season total, and the
app also shows it per 36 minutes.

### 11.5 Opponent-adjusted team ratings

`metrics.team_season_adj`, per team-season (regular season), every measure and tier
(`courtvision.metrics.adjust`). For each season × measure × tier:
`value(team, game) = μ + a[team] + b[opponent] + h·(±1 site)` by least squares
(ridge 1e-3 to pin the constant). A team's **schedule effect** is the mean of
`b[opponent] + h·site` over its games; `value_adj = value − schedule`, rated on the
same team-season reference as the raw value (`rating_adj`). Raw values remain the
default; the app has a toggle.

### 11.6 Market model

`courtvision ingest salaries` loads the salary dataset (README, data sources) into
`salary.*` and matches HoopsHype players to NBA ids by normalized name, using who
played for whom that season to break ties. `courtvision market build` writes
`market.*` for the latest salary season. All money is in that season's dollars.

* **Points per win**: from team-seasons 2000 on, `1 / slope(win% ~ point margin per
  game)`.
* **Replacement level**: possession-weighted impact of last season's players paid at
  most 1.05 × the veteran minimum (≥ 500 possessions).
* **Value**: `WAR = (impact − replacement) · projected possessions / 100 / points
  per win`. Projected possessions = 0.7 × last season + 0.3 × this season's pace
  (or whichever exists). Current impact = this season's prior-informed estimate.
* **$ per win**: last season's salary above the veteran minimum, summed, over last
  season's positive WAR, summed; inflated to this season by the cap ratio.
  **Fair salary** = veteran minimum + max(WAR, 0) × $/win, capped at 35% of the cap.
* **Risk** per player: `war_p10` / `war_p90` from the impact standard error;
  `overpay_prob = P(fair salary < salary)` under that error; flags for age (aging
  curve fitted on season-only estimates by the delta method; flagged at ≤ −0.75
  pts/100 expected next season), availability (< 70% of team games over two
  seasons), uncertainty (se ≥ 2.5), small role (< 2,000 projected possessions),
  player option, two-way contract. **High** if `overpay_prob ≥ 0.75` or two or more
  flags (a player option doesn't count); **low** if `overpay_prob < 0.35` and no
  flag; otherwise medium.
* **Budgets**: payroll (sum of the season's salaries) against cap, tax line, first
  and second apron; status; room to each; luxury tax at non-repeater rates (1.50,
  1.75, 2.50, 3.25, then +0.50 per step, the step scaled with the cap from $5.168M
  in 2023-24). Repeater rates, cap holds, exceptions and dead-money mechanics are
  not modelled.
* **Needs**: the team's dimensions rated below 47 opponent-adjusted this season
  (the weakest one if none is), up to three.
* **Options**: for each need, players on other teams (or unsigned with ≥ 800
  possessions last season, priced at the veteran minimum) rated ≥ 58 in that
  dimension whose impact beats the weakest of the team's nine most-used players in
  the same position group. `net_gain = (impact − replaced impact) × the share of
  team minutes the candidate usually plays` (capped at 80%); `wins_added = net_gain
  × team possessions / 100 / points per win`, plus a bad case at the candidate's
  10th percentile. Cost: salary; the least outgoing salary that makes the trade
  legal (2023 CBA matching, simplified: nothing within cap space; below the first
  apron the 200% + $250k / + $7.5M / 125% + $250k bands, scaled with the cap; above
  the first apron 100%); payroll and status after; and the change in tax. Ranked by
  (salary + added tax) per win, twelve per need.

The market is a planning aid built on public data, not a cap sheet: snapshot
salaries miss later signings, contract years beyond the current season are not in
the data, and the trade rules are simplified.

## 12. v1.2 (2026-11-12)

Approved by the user on 2026-11-12 ("yes, make it seamless") after v1.1's market
showed that pricing on plus-minus alone undervalues some high-usage scorers.

### 12.1 Box-score prior for impact

v1.1's prior was last season's estimate only. v1.2 adds what this season's box score
says. A **box model** (`metrics.impact_box_model`) regresses season-only offense and
defense on per-100-possession box rates relative to league (points, FGA, 3PA, FTA,
AST, TOV, OREB on offense; DREB, STL, BLK, PF on defense), true shooting and usage
relative to league, their product, and the frontcourt position weight. Weighted by
√possessions; player-seasons with ≥ 500 possessions, 1996-97 on, games with rebuilt
lineups only. Its predictions are `box_off`, `box_def`, `box` in `player_impact`.

Each player's prior for season *s* (per side):

* returning player: `last + (1 − α) · r · (box_s − last)`
* new to the league: `newcomer + r · (box_s − newcomer)`

with reliability `r = poss_s / (poss_s + 1500)`, so the box part counts for little a
few games into a season. `α` and `λ` are chosen jointly by 5-fold cross-validation over
games on the same five seasons (`metrics.impact_cv`). α = 1 is the v1.1 model.

**Result** ([`validation.md`](validation.md) section 7): CV picks α = 0.25 (mostly
the box model, a quarter last season) and λ = 5,000; held-out error 3,820.9 vs 3,823.0
at α = 1. Out of sample, last season's impact weighted by this season's minutes now
predicts this season's team net rating at r = 0.77 (r² 0.60), up from 0.71 (0.50).
The box model alone reaches 0.68 and raw on/off 0.38.

**Speed.** Each season's normal equations (and each CV fold's) are eigendecomposed
once, so every (λ, α) candidate costs two matrix-vector products per season; the
whole impact stage, including the 30-candidate search, runs in about two minutes.
The home-court term is now penalized like the player terms; with ~250k possessions
behind it the shrinkage is under 2%.

The market (11.6) needs no change: it reads `player_impact`, so prices, risk and
options update with every build.

## 13. v1.3: contracts, pricing, free agency, NBA.com advanced stats (2026-11-12)

Requested by the user after reviewing v1.2 ("the salary seems off"; "keep player
contracts, free agency and upcoming free agents in mind"; "something about Dyson
Daniels' fair salary looks wrong"; "are we using all the Kaggle data and tracking
metrics properly?"). Review findings, all fixed:

* 63 players playing in 2026-27 had no 2026-27 salary (signed after the 7 July
  snapshot) and were treated as unsigned free agents.
* 27 players traded since July were charged to their old teams ($248M of payroll).
* Two-way contracts ($36M) were counted in payroll; they don't count against the cap.
* Fair salary was capped at 35% of the cap for everyone; the maximum depends on years
  of service (25% / 30% / 35%). Daniels (4 years) can't earn more than 25%.
* The replacement level (about league average) and a linear $-per-win spread the whole
  salary pool over above-average players only, inflating every good player's price.

### 13.1 The contract book

`courtvision.ingest.contracts` builds `salary.book` (player × season, 2026-27 to
2030-31) and `salary.contract_summary` from three sources, newest winning for the
season it covers: Basketball-Reference contracts scraped 17–23 June 2026 (Kaggle
`deocheng/nba-data-1946-2026`, `public.player_contracts`: every year's salary and
option), the July snapshot, and the user's `data/salary_overrides.csv`
(`docs/salary_overrides.example.csv`). The module docstring lists the rules. Highlights:
a July salary within 10% of Basketball-Reference's for 2026-27 continues that contract's
later years; a new July deal has unknown length; an option year with no July row was
declined; this year's first-round picks follow the rookie scale (two guaranteed years,
team options on years 3–4); every contract is charged to the team the player plays for
now, waived money stays with the team that waived him; two-way deals are listed but
not counted; a player playing now with no contract anywhere is `unknown`, never a free
agent. Status next summer: UFA, RFA (rookie scale ending, or two-way), player / team
option, under contract, or length unknown. Basketball-Reference names arrive
double-encoded (UTF-8 read as Latin-1); `fix_mojibake` repairs them before matching.

**Current pages (user-approved, 2026-11-12).** `courtvision ingest salaries
--fetch-current` downloads Basketball-Reference's 30 team contract pages
(`/contracts/<TEAM>.html`, allowed by robots.txt; 4 s apart against a 3 s crawl delay;
cached per day under `data/raw/salaries/bbref_current/<date>/`;
`courtvision.ingest.bbref_contracts`). When cached, they decide every standard contract
and its later years and options (cell classes `salary-pl` / `salary-tm`); the July
snapshot then adds only two-way deals and dead money, which the pages don't list, and
the June table isn't used. Players playing now who are on no page are two-way or
short-term deals. First pull: 455 players, all matched (reversed name order and duplicate
ids handled); 63 unknown contracts fell to 32, all end-of-bench two-way or 10-day players.

### 13.2 Fair salary: market comparables by rank

`value = (impact − R) × projected possessions / 200` (points above replacement). The
market curve maps the percentile of a player's value among last season's veteran
contracts (rookie-scale and two-way excluded) to the same percentile of their salaries
as a share of the cap (quantile matching): the Nth most valuable player is priced at
the Nth-highest salary. Fair salary = that share × this season's cap, between the
veteran minimum and the player's service maximum. `R` is chosen by 5-fold
cross-validation of that mapping over {−1, −1.5, −2, −2.5, −3} pts/100 (the market pays
for minutes as well as per-possession impact; it picks −2.5). A second price from the
box-score model alone (`fair_box`) is shown beside it, and a risk flag fires when impact
exceeds the box score by 2.5 or more ("value rests on plus-minus"). The chance of being
overpaid compares the price across ±1.96 se of impact with the salary, a max player
judged against his max. Other new flags: a contract of 3+ seasons at 20%+ of the cap.

### 13.3 Free agency

* **Now:** players who played last season or this one, with no contract and not playing.
* **Next summer (`market.fa_class`):** UFA, RFA, player and team options. Next season's
  impact = impact + the aging curve's change; projected fair salary from the same
  market curve against next season's cap (this season's × 1.07; the 2025-26 → 2026-27
  rise was 6.7%; the dataset's 10% figure is a mechanical upper path).
* **Cap space next summer (`market.teams.next_*`):** projected cap minus committed money
  (guaranteed, options assumed picked up, July deals of unknown length assumed to run
  on); a best case with options declined. Cap holds and exceptions are not modelled.
* **`market.fa_options`:** for each team need, next summer's free agents who fill it,
  wins added over the weakest rotation player at the position, and whether his
  projected price fits the team's projected space.

### 13.4 NBA.com's advanced stats

`metrics.player_advanced` computes stats.nba.com's advanced player table (off/def/net
rating on court, AST%, AST/TO, AST ratio, OREB/DREB/REB%, TO ratio, eFG%, TS%, USG%,
pace) from our counts and lineups, only over games with rebuilt lineups. Validation
section 8 compares them with the official values in `kaggle.player_statistics_extended`
(r 0.986–1.000 for everything but rebound % — NBA.com counts team rebounds as available,
we don't — so lineups and possessions are tracked right in every season). PIE is taken
from NBA.com directly: its published values don't follow its published formula
consistently. Section 8 also lists what every Kaggle table feeds.

## 14. Forecasts for each team's next game (2026-11-12)

Requested by the user: predict what the game view shows, for the closest upcoming game
of every team, at team and player level, from trends, form, player form and opponent
strength. `courtvision forecast` (`courtvision.forecast`, schema `forecast`):

* **Upcoming games** from `kaggle.league_schedule_26_27`: each team's next game from
  today on (games between the data's last day and today were played but aren't in the
  data yet).
* **Team dimensions:** for each of 16 measures, `value(team, game) = μ + form[team] +
  opp[opponent] + h·site`, weighted least squares with ridge 4: this season's games plus
  last season's at ¼ weight, weights halving every 21 days. Prediction rated on the
  season's team-game reference; compared with the team's season-average game rating.
* **Margin and win chance:** a blend of the same model on net points per 100 (team
  form) and the roster view (each expected player's impact × expected minutes / 48,
  plus home court), × expected possessions / 100. Win chance = Φ(margin / σ).
* **Players:** expected minutes from recent games (weights halving every 6 games), for
  players in one of the team's last three games (there is no injury feed); each
  dimension's value = recent form (exposure-weighted, halving every 6 games) shrunk
  toward the season level with 120 exposure units of prior, plus the opponent's effect.
* **Backtest** (`forecast.backtest_*`): the last complete season re-predicted week by
  week from earlier games only (the roster view uses the previous season's impact, so
  nothing leaks). 2025-26: blend 75% form / 25% roster; margin error 11.5 points (13.4
  picking the home team), 68% of winners (55%), Brier 0.207 (0.25), σ 14.6. Team
  dimensions beat each team's season-to-date average on 15 of 16 measures.

The live forecast now uses the walk-forward model of section 16 for margin and win chance
(the blend above is kept as `blend_margin` / `blend_win_prob`), and adds each expected
player's stat line with an 80% range (`forecast.player_lines`).

## 15. Coaching analytics (2026-11-12)

Requested by the user: coaching form, plays, coach vs coach, and metrics the NBA doesn't
track. Everything is computed from play-by-play (1996-97 on), no tracking data.

**15.1 Who coached each game** (`courtvision ingest coaches` → `coach.coaches`,
`coach.assignments`). The Kaggle box scores carry no coach, so coaches come from
Basketball-Reference's coach records in Kaggle `deocheng/nba-data-1946-2026`
(`public.coach_stats`: coach, season, team, games, wins). A team-season with several
coaches is split at the game where each coach's record matches the team's results in
order (every order of the stints is tried). 71,404 team-games, 190 coaches; 12
team-seasons whose records don't add up give every game to the main coach. The current
season isn't in those records yet: it comes from Basketball-Reference's season coaches
page (`courtvision ingest coaches --fetch-current`; one request, approved by the user
2026-11-12; cached under `data/raw/coaches/bbref_current/<date>/`; plain `build` reuses
the cache and never fetches), split the same way, with the stint still running allowed to
extend past the last game in the data. `coach.current` holds each team's coach now. With
no page cached, each team keeps last season's final coach, flagged `assumed`;
`assignments.source` says which (`records`, `current page`, `assumed`). The 2026-11-12
page corrected four summer hires the fallback had wrong (Dallas, Milwaukee, Orlando,
Portland).

**15.2 Metrics** (`courtvision coaching` → `coach.possessions`, `coach.timeouts`,
`coach.team_games`, `coach.profiles` per coach-season and career (`season = 0`),
`coach.tempo`):

| Metric | Definition |
|---|---|
| After-timeout scoring | points on the calling team's next offensive action after its timeout, per 100 plays, minus league; shrunk with 250 plays of prior |
| After-timeout play mix | share of those plays ending in a three, free throws, a turnover, minus league |
| Timeouts that stop runs | opponent points per possession in the 3 possessions before minus the 3 after, minus league, per 100; shrunk (250 timeouts) |
| Halftime adjustment | third-quarter net per 100 minus first-half net per 100; shrunk (120 games) |
| Clutch net | net per 100 possessions with ≤ 5:00 left in the 4th or overtime and the margin ≤ 5; shrunk (600 possessions) |
| Transition share / scoring | share of possessions after a miss or steal with the first action within 8 s; points per 100 on them minus league |
| Tempo control | per game, \|game pace − opponent's usual pace\| − \|game pace − own usual pace\|; game pace = both teams' possessions averaged; usual = the team's other games that season. The two sides of a game sum to zero |
| Rotation, starters' minutes in blowouts | players with 10+ minutes vs league; top-five minutes in games decided by 20+ |
| Bounce-back | net per 100 after a loss minus after a win; shrunk (120 games) |
| Timeouts, challenges | per game (challenges from 2019-20) |

Possession start types (2025-26): after a make 1.10 points per possession, after a miss
1.16, others (mostly dead-ball and free-throw starts) 1.25.

**15.3 Value against expectation and coach vs coach** (after the walk-forward:
`coach.value`, `coach.value_reliability`, `coach.h2h`):

* *Beats roster expectation*: the team's margin minus the walk-forward roster view
  (last season's player impact over expected minutes, home court included), per game;
  *beats the prediction*: margin minus the walk-forward pre-game prediction. Shrunk by
  `games / (games + k)` with `k` from the split-half reliability (21 and 74 games).
* `coach.h2h`: per ordered coach pair, record, average margin, tempo control (every
  season) and margin against the prediction (2020-21 on).

**15.4 Which metrics are coaching?** A coach metric should repeat for the same coach and,
more tellingly, follow him to a new team with different players (`persistence`;
coach-seasons with 40+ games; about 40 team changes since 1996-97, so r below ~0.3 is not
distinguishable from zero):

| Follows the coach (new-team r) | Doesn't |
|---|---|
| tempo control 0.66, starters' minutes in blowouts 0.53, timeouts per game 0.48, timeouts that stop runs 0.48, pace 0.39, after-timeout three share 0.35, 3-point rate 0.29 | after-timeout scoring −0.13, after-timeout turnovers −0.11, halftime adjustment −0.40, clutch net 0.08, rotation depth 0.05, bounce-back 0.10, team net 0.12 |

Beats-roster-expectation is consistent within a season (split-half 0.70) but not from
one season to the next (−0.06): it captures what a team became that season (development,
rookies the roster view prices at a flat newcomer value, health), not a trait that
carries over. A coach pair's head-to-head history doesn't predict their next meeting
beyond the model (r = 0.003 over 10,258 games). The app says so wherever these appear.

## 16. Walk-forward prediction engine (2026-11-12)

Requested by the user: player and team prediction from stats and form, tested
walk-forward, weighing matchups (team and positional). `courtvision walkforward`
(`courtvision.walkforward` → `forecast.wf_*`, `forecast.margin_model`,
`forecast.wf_residuals`).

**Protocol.** Seasons 2020-21 (warm-up) and 2021-22 to 2025-26. Every week, every game of
that week is predicted from games before the week only; stage 2 is fitted on earlier
seasons' stage-1 outputs only (expanding window). No feature uses the predicted season's
results except through games already played.

**Stage 1 (per game):** team form (section 14's net model), the roster view (last
season's impact × expected minutes / 48 + home court; expected minutes: when he plays,
recent games weighted with a 5-game half-life, team total capped at 250), expected pace,
rest (days since the last game, capped at 3; back-to-back flags), the coach's running
margin against expectation (shrunk, 50 games), and style interactions (three-point
reliance × threes allowed, offensive × defensive rebounding, ball security × forced
turnovers, paint reliance × paint defense).

**Stage 2:** ridge regression of the home margin on feature groups added one at a time;
win chance Φ(margin / σ). 6,146 games:

| Model | Margin error | Winners | Brier | Log loss |
|---|---|---|---|---|
| home court only | 12.29 | 55.3% | 0.2475 | 0.6881 |
| + team form | 11.12 | 64.3% | 0.2190 | 0.6274 |
| + roster | 10.94 | 65.5% | 0.2143 | 0.6172 |
| **+ rest (chosen)** | **10.90** | **65.8%** | **0.2135** | **0.6152** |
| + coach | 10.99 | 65.2% | 0.2152 | 0.6195 |
| + style matchups | 10.98 | 65.4% | 0.2151 | 0.6193 |

Chosen model (fitted on all evaluation seasons, used live): margin = −0.36 + 0.735·form
+ 0.463·roster − 2.37·(home on a back-to-back) + 2.32·(away on a back-to-back) +
0.245·(rest difference), σ 13.9. Win chances are calibrated (deciles from 25% predicted /
21% won to 82% / 85%). Coach and style-matchup features made out-of-sample predictions
worse, so they are measured and shown but not used.

**Players:** for every expected player, points, rebounds, assists, threes, steals +
blocks and turnovers = per-minute rate (recent games, 25-game half-life, shrunk toward
last season's rate with 250 minutes of prior; newcomers at 80% of league) × expected
minutes × game pace / team pace × the opponent's allowance to his position group (guards,
wings, bigs; shrunk with 12 games). 118,415 player-games played:

| Stat | Model error | Season-average error | 80% range coverage (2023-24 on) |
|---|---|---|---|
| points | 4.737 | 4.733 | 80.4% |
| rebounds | 1.939 | 1.955 | 80.1% |
| assists | 1.345 | 1.356 | 79.5% |
| threes | 0.883 | 0.892 | 79.5% |
| steals + blocks | 0.915 | 0.937 | 78.9% |
| turnovers | 0.888 | 0.909 | 80.0% |

Ranges: sd = c·√(expected), with c set so 80% of the 2021-22 and 2022-23 misses fall
inside (an RMS-based width over-covers: counts are peaked with long tails), then checked
on later seasons. Positional matchups and pace change player errors by less than 0.002;
they are kept because they cost nothing, but they aren't claimed to help. Minutes when he
plays: error 5.5. A projection is conditional on playing: there is no injury feed, and
26% of players expected from recent games didn't play.

## 17. Play types (2026-11-12)

Requested by the user: "deeper analysis on plays that teams are using". `courtvision plays`
(`courtvision.plays` → schema `plays`), regular seasons 2015-16 on. No tracking data:
each field-goal attempt is classified from what the play-by-play records about the shot
(description, shot type, descriptor; the legacy text and the live format share one
vocabulary) and how its possession started (`coach.possessions`). First match wins:

| Play | Rule |
|---|---|
| Putback | putback, tip |
| Transition | possession after a miss or steal, first action within 8 s |
| Lob | alley-oop |
| Cut | cutting layup or dunk |
| Drive | driving layup, dunk, finger roll, reverse, hook |
| Floater | floating jumper (driving or not) |
| Post-up | hook, turnaround, fadeaway (not step-back) |
| Step-back | step-back jumper |
| Pull-up | pull-up jumper |
| Other rim | any other layup or dunk (rolls, dump-offs, running layups) |
| Spot-up three | any other three (98% of the made ones are assisted: catch-and-shoot) |
| Two-point jumper | any other two-point jumper |

Assists are recorded only on made shots, so they never classify a shot (a first draft that
used them gave "catch-and-shoot" a 100% FG%; caught by the outcome check and removed).
Tagging became more detailed over the years (two-point jumpers with no move recorded fall
from 22% of shots in 2015-16 to 1.6% in 2025-26 while pull-ups rise from 6% to 12%), so
every number is relative to its own season's league and teams are compared within a
season. 2025-26 league mix: spot-up threes 28%, drives 14%, pull-ups 12%, transition 8%,
floaters 8%, post-ups 7%, other rim 6%, step-backs 5%, putbacks 5%, cuts 4%, lobs 1%.

Per team, season and side (offense; defense = what opponents did): share of shots, share
against league, points per 100 shots against league on that play, assisted share of makes.
Efficiency is shrunk with `k = n_half (1 - r) / r` shots, from each play's own split-half
reliability (`plays.reliability`). Play mix is a team's fingerprint (split-half r 0.71 to
0.99 on offense); efficiency is moderately reliable on drives (0.46), pull-ups (0.44),
floaters (0.43) and post-ups (0.40), and mostly noise on cuts (0.13), lobs (0.14),
step-backs (0.18) and transition (0.21); `k` ranges from about 290 to 5,000 shots.

Also: per player (his mix and efficiency, shrunk with the team constants), passer →
scorer connections by play (`plays.assists`), turnovers by kind (bad pass, lost ball,
offensive foul, out of bounds, travel, shot clock) per 100 possessions against league.

**Matchup test** (`plays.matchup_test`): each season's second-half team-games predicted
from its first half. Against league average on that play, the offense's own shrunk number
cuts the shot-weighted squared error by 0.58% and the offense plus the opponent defense's
allowance by 0.91% (drives 2.3%, pull-ups 2.6%, other rim 1.6%). Single games are very
noisy, so gains are small in absolute terms, but the defense adds information: the
scouting report uses the sum.

## 18. Hexagon shot maps and the scouting report (2026-11-12)

Requested by the user: better-looking heat maps, and heat maps for upcoming games (danger
zones, players to watch, plays to watch).

**Shot maps** (Team and Player views; the five-zone court stays one click away):
hexagons of radius 1.7 ft. Size: share of the unit's shots from there (full size at 1.6%).
Color: points per 100 shots against league average *from the same hexagon* that season
(so a corner three is compared with corner threes), shrunk toward the unit's own zone
(60 shots of prior) and the zone toward league (150): `diff = 100 (Σ(pts − lg_pps) + 60 ·
zone_diff) / (n + 60)`. For a defense, the sign flips: blue = opponents scored less.

**Scouting report** (Upcoming, per game; this season plus last season's games at a quarter
weight):

* *Danger zones*: for each attacking team, per hexagon, its own shooting against league
  plus what the defense allows there (`edge`); size = how often the attack shoots there.
  Zone summary: the biggest edge and where the defense holds up best. Descriptive: the
  hexagon-level sum isn't separately validated.
* *Plays to watch*: per play, how often the attack uses it, its efficiency, the defense's
  allowance and their sum (validated in section 17), sorted by edge × frequency.
* *Players to watch*: expected points with the 80% range (section 16), form (points per
  minute in the last 5 games against the 40 before, pulled toward his usual rate with 60
  minutes of prior), matchup (his play mix against this defense's allowance per play) and
  his go-to play. Form and matchup are descriptive; section 16 found matchup adjustments
  add little to a player's expected line.

Grid heat maps share one style (`charts.heat_grid`): each column scaled on its own, a wide
gray band for "about average", blue good / red bad for the team or player looked at,
values printed in every cell.

## 19. Lineups (2026-11-12)

Requested by the user (the next gap after play types). `courtvision lineups`
(`courtvision.lineups` → schema `lineups`), every regular season from 1996-97, from the
rebuilt stints of games whose lineups check out (`metrics.game_pbp_qa.lineups_ok`).

* `team_stints`: per stint and team, the five, points and possessions both ways, the five
  faced, clutch possessions (from `coach.possessions`), and the expectation from the
  players: own five's summed per-possession impact minus the opponents' five (this
  season's impacts, and last season's for the tests). 2.2 million team-stints.
* `units`: per team, season and five: minutes, possessions, offense / defense / net per 100
  against league, the players' expectation, *synergy* (net − expectation), *projected net*
  = expectation + synergy · poss / (poss + k), games started, clutch.
* `pairs`: per team, season and two players (a = b: the player alone): possessions and net
  together relative to the team's net, shrunk with the pairs' own k.

**Is there lineup chemistry?** Synergy in odd vs even games of a season (7,170 lineups with
50+ possessions in each half, ~150 per half): r = −0.05 against this season's impacts
(biased low: they are fitted on the same stints) and +0.06 against last season's (biased
high: they miss this season's player changes). Both near zero: a lineup is about the sum of
its players. k = n_half (1 − r) / r from the upper bound = **2,354 possessions** (a lineup's
own result counts half only after about a season and a half of a starting five's minutes).
Raw lineup net repeats at r = 0.13.

**What predicts a lineup's later games** (each season's second half from its first, 2,865
lineups with 50+ possessions in both): league average misses by 10.4 points per 100; the
lineup's own first-half net by **13.2** (worse than knowing nothing); its players' impacts
from last season by 9.3 (r 0.25); the projection by **9.2** (r 0.27). Rank lineups by the
projection, never by raw plus-minus.

**Pairs:** net together relative to the team, odd vs even games, 33,057 pairs with 300+
possessions per half: r = 0.16, k = 3,991 possessions.

**Best possible fives** (`lineups.best_fives`): the five highest summed impacts among
players still on the roster (in one of the team's last 15 games, 100+ minutes; never two
players traded for each other), at least one guard, one or two centers; flagged *untried*
under 50 possessions together. For an upcoming game, from tonight's expected players.

**In the scouting report:** each team's likely starters (the five that started most this
season, if all are expected tonight), its most-used available fives, the best possible
five, and the opening fives' head-to-head by impacts. Descriptive: lineups for a specific
game aren't predicted beyond who is expected to play.

## 20. The official injury report (2026-11-12)

Approved by the user ("Yes, injury feed"). `courtvision ingest injuries --fetch` reads the
season's index page on official.nba.com and downloads the newest report PDF from
ak-static.cms.nba.com (two requests; robots.txt allows both, crawl delay 1 s), cached
under `data/raw/injuries/<date>/`. Plain `build` and `forecast` load the newest cached
report and never fetch.

**Reading the PDF without a library.** The report (GemBox; WinAnsi text) draws each table
cell as a clipping rectangle followed by its words. `ingest.injuries.parse_pdf`
decompresses each page's content stream (zlib), collects every word with the cell it was
drawn in, joins a cell's words in reading order (a word ending in "-" joins the next
line), orders pages by their "Page N of M" text, assigns cells to columns by the header
cells' positions (pages without a header reuse the last page's), and carries date, time,
matchup and team down to rows that leave them blank. Players are matched from "Last,
First" (accents, punctuation and Jr./II/III ignored), preferring a player who has played
for that team; 99% matched on the first report (the miss: a rookie with no games yet).
Teams that haven't filed are kept as "not yet submitted".

**In the forecast** (`forecast.availability`): for each upcoming game, the report's rows
for that date; for a later date the report doesn't reach yet, only players listed Out are
carried (`carried`; day-to-day statuses aren't); a report more than two days older than a
game is ignored. Out and Doubtful players leave the expected minutes, and their minutes go
to teammates in proportion to their own (the team total is unchanged), which moves the
roster view, the margin, the stat lines and the lineups. Questionable and Probable players
stay, tagged (Q) / (P) on their stat lines. Each absence's cost is shown: his expected
minutes had he played times his impact.

**What knowing who plays is worth** (`courtvision walkforward --availability-test` →
`forecast.availability_test`, ~8 min): the chosen game model re-run walk-forward with the
roster view built from the players who actually played each game (an oracle: the best any
injury report could do) instead of the live rule (seen in one of the last three games).
6,146 games: margin error 10.90 → 10.82, winners 65.8% → 66.5%, log loss 0.6153 → 0.6098 —
about 2.5 times what rest and back-to-backs add, and most of it in games where a key player
sits. The report recovers part of that ceiling: a player injured after his last game
(today's report listed Stephen Curry, Lauri Markkanen and Scottie Barnes, all in the
expected rotations) is now dropped. Historical reports aren't stored, so the report's own
share of the gain can't be measured until reports accumulate.

## 21. Prediction ledger and scorecard (2026-11-12)

Requested by the user: see how the predictions fare once the games are played.

**Ledger.** Every `courtvision forecast` run appends what it predicted to
`forecast.ledger_games`, `ledger_players` and `ledger_availability`, stamped with the time,
the last day of results it used (`data_through`), the injury report it used and the code
version (git commit). Rows are only ever appended, so a prediction is scored exactly as it
was made. The first entry (Nov 12, 2026) was copied from the forecast run at 8:26 PM ET,
before the ledger existed; its note says so.

**Scorecard.** `courtvision scorecard --date D` takes, for each of the day's games, the
latest logged prediction and compares it with the result: from the database when the
nightly Kaggle update has the game, otherwise from the NBA live feed's final box score
(the source live mode uses; cached, one request per game at most every 10 s). Per game:
predicted vs final margin, whether the predicted winner won, the win chance's Brier score,
whether the miss is inside the model's 80% band (±1.28 σ), and `after_tipoff` (the
prediction was made after the scheduled tip-off; forecasts read only completed games and
the injury report, never the game in progress, so this flags timing, not leakage). Per
player expected to play: each stat line against his box score, inside its 80% range or
not (ranges rounded outward to whole numbers, since stats are counts), and the error of
his season-to-date average as the baseline; players who didn't play are listed, not
scored. One night is a handful of games: compare with the walk-forward averages (section
16), not with zero.
