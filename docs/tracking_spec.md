# Film and tracking: a live game model from what the film shows

*Draft for approval, Nov 22 2026.* courtvision's standing rule "no tracking data; no metric may
need player (x, y) positions" is lifted **for this module only**. Everything else in courtvision
keeps working without tracking, and `courtvision build` stays offline and unchanged when no
tracking data is present.

## 1. Goal

A real-time live game model driven by what the film shows, producing four things during a game:

1. **Win probability** that updates every frame from the score, the clock and the state of the
   current possession on the floor, not just the score and clock;
2. **Shot quality**: the expected points of every shot from the position of the shooter and the
   defenders at release;
3. **Defense and schemes**: man or zone, pick-and-roll coverages (drop, switch, hedge, blitz,
   ice), help and rotations, and how often each works;
4. **Player movement**: speed, distance, spacing, off-ball movement and closeouts, per player
   and per lineup.

It is built in two layers so the models never depend on where the positions come from:

- **Tracking layer.** Ball (x, y, z) plus 10 players (x, y), 25 frames a second, in feet on a
  94 × 50 court. First from the public 2015-16 SportVU logs, later from computer vision on video.
  Both produce the same frame schema.
- **Model layer.** Streaming by design: each new frame updates the state, so the same code runs
  on a replayed 2016 game and on a live video feed.

## 2. The data, as profiled

**Source:** the SportVU logs NBA.com briefly published in 2016, collected in the GitHub repo
`linouk23/NBA-Player-Movements`:
- 636 regular-season games from the first half of 2015-16, one 7z archive each;
- 3.8 GB compressed, about 92 MB of JSON per game once extracted;
- the repo has **no license**.

Windows' built-in `tar` extracts the archives, so no dependency is needed for that.

**Sample game: 0021500493, Knicks at Bulls, Jan 1 2016.**

| What | Found |
|---|---|
| Structure | 455 events (one per play-by-play event), each with both rosters (name, NBA person id, jersey, position) and its frames |
| Frame | quarter, timestamp (ms), game clock, shot clock, and 11 rows: the ball (x, y, z) then 10 players (team, person id, x, y) |
| Rate | 25 per second (median gap 40 ms); 58% of gaps are exactly 40 ms; 89 gaps over a second (dead balls) |
| Duplication | events overlap: 188,280 frames listed, 74,636 unique (2.5×), so de-duplicate by timestamp |
| Coverage | the first ~15-20 s of each quarter are missing; 48 of 455 events have no frames; 0.2% of frames lack the ball or a player |
| Coordinates | feet; players −6 to 99 in x, −2 to 52 in y (out of bounds included); ball height up to 17.7 ft |
| Ids | player ids are NBA person ids, the same as courtvision's `person_id` |

**Size plan:**
- about 75,000 frames per game, about 48 million in all;
- keep the 7z archives (3.8 GB) plus compact Parquet (an estimated ~5 GB);
- never keep the JSON: process one game at a time and delete it after conversion.

**Linking to play-by-play.** SportVU numbers its events with the old `PlayByPlayV2` event
numbers. NBA has retired that endpoint (it now returns nothing), and courtvision's play-by-play
uses the newer V3 numbering, which matches only 59% of events. So alignment comes from the
tracking itself:
1. detect events in the frames (shots, rim arrivals, possession changes, passes, rebounds);
2. align them in sequence to the play-by-play by quarter, clock and type.

A first try on shots alone:
- 67% of field goals matched an arc within 3 s;
- the player nearest the ball at release was the logged shooter 71% of the time;
- tracked vs logged shot distance agreed at r 0.76 (median miss 1.5 ft).

Free throws and passes near the rim are still mixed in. The Phase T1 target is ≥95% matched,
≥95% shooter agreement and distance r ≥ 0.95.

## 3. Architecture (real-time first)

**`FrameSource`** yields frames:
`(t, quarter, game clock, shot clock, ball xyz, [team, person id, x, y] × 10)`.

Implementations:
- `SportVUReplay`: reads Parquet and emits frames at real time (25 per second) or as fast as
  possible;
- `VideoTracker` (Phase T5): computer vision on a video stream or file;
- any licensed live tracking feed, if one ever becomes available.

**An incremental engine** consumes the frames:
- possession tracker;
- event detector (shots, passes, dribbles, screens, rebounds);
- feature computers (spacing, defender distances, matchups);
- models that update per frame or per event, writing to a live state.

**Latency budgets:**
- tracking in → updated outputs: under 200 ms per frame;
- video in → updated outputs: under 1 s end to end at 30 frames a second on this machine
  (RTX 3070 Ti, 8 GB).

**Display.** Streamlit can't redraw at 25 frames a second. The live court (animated players and
ball, win probability, shot quality, scheme labels) is a small local web page (canvas plus a
WebSocket from the engine), linked from the app (decision D7).

## 4. Models

### 4.1 Player movement

Computed directly from the frames:
- speed, distance and acceleration; time in zones;
- offensive spacing (convex-hull area, mean pairwise distance, corner occupancy);
- off-ball distance and cuts;
- closeout speed and contests; load (distance at speed).

**Known-answer check:** NBA.com's 2015-16 tracking stats (distance and speed per game,
catch-and-shoot, shots by closest-defender distance) came from the same SportVU system. They're
available through `nba_api`, an approved source, so our numbers should match them.

### 4.2 Shot quality (expected points per shot)

**Features at the release frame:**
- shot location, distance and angle;
- closest defender's distance and angle, and how fast he's closing;
- defenders within 6 ft;
- the shooter's speed and direction;
- catch-and-shoot vs off the dribble (touch time, dribbles);
- shot clock and game clock;
- transition vs half court;
- the shooter's shrunk skill.

**Model:** start with a numpy logistic regression with spline terms (no new dependency), with
gradient boosting as an option (D5). Calibrate on held-out games.

**The honest test:** how much do the tracking features add over a location-only model, measured
in log loss and calibration? Shot-making (points − expected points) per player comes with a
reliability test.

### 4.3 Defense and schemes

- **Matchups.** Assign defenders to attackers every frame, smoothed over time (in the spirit of
  Franks et al. 2015). Stable assignments mean man; assignment by area means zone. Count
  switches.
- **Ball screens.** Detect them from positions: the screener comes within a few feet of the
  ball handler's defender, and the handler moves past (in the spirit of McQueen et al. 2014).
- **Coverage per screen,** from where the two defenders are relative to the screen in the frames
  around it:
  - drop;
  - hedge or show;
  - switch;
  - blitz;
  - ice;
  - under.
- **Help and rotations:** a second defender leaves his man toward the ball, and it is
  measured how long the rotation takes.

**Evaluation:** a hand-labeled set of about 300 screens from the replays, labeled to written
rules, with precision and recall reported honestly (D6).

**Outcomes:** points per possession and shot quality allowed, by coverage, team and lineup.

### 4.4 Film-aware live win probability

- **Base:** courtvision's live inputs (score difference, time left, possession) plus the
  pre-game strength from the forecast.
- **Film adds expected possession value (EPV)** (after Cervone et al. 2016): the expected
  points of the current possession given the floor state (ball location, shot clock, spacing,
  defender proximity), updated every frame.
- **Also:** lineup strength on the floor (courtvision's impact ratings), foul trouble, fatigue
  (minutes and distance run).
- **Formula:** WP = P(win | score difference + EPV of the current possession, time left,
  lineups, pre-game strength).
- **Validation on held-out games:** calibration by time bucket, and log loss vs a
  score-and-clock-only model.
- **The honest question:** how much does film add? Expect a modest gain, since most of win
  probability is score and time. It should show most in late, close games.

## 5. Video pipeline (Phase T5; needs approvals then)

**Input:** a video stream or file you have the right to process. Nothing captured is stored or
published beyond derived numbers.

**Steps:**
1. **Court homography.** Detect court-line keypoints and map pixels to feet, every frame,
   because the camera moves.
2. **Players.** Detect them; assign teams by clustering jersey colors.
3. **Tracking.** Multi-object tracking with re-identification.
4. **Identity.** Read jersey numbers and map them to the game's rosters.
5. **The ball.** Detect it, which is the hardest part. Estimate its height from shot-arc models.
6. **Output.** The same frame schema as SportVU, so every model above runs unchanged.

**Evaluation:** hand-annotated frames, plus public research datasets for detection and tracking
(candidates to verify for license and fit: SportsMOT's basketball clips, the DeepSportRadar
basketball datasets).

**Dependencies to approve at that point:**
- opencv-python (Apache-2.0) and torch/torchvision (BSD);
- a detector **without** an AGPL license, because Ultralytics YOLO is AGPL-3.0 and would
  conflict with this public MIT repo. For example torchvision's detectors, or RT-DETR through
  transformers (Apache-2.0);
- a tracker (ByteTrack or supervision, both MIT);
- an OCR model (PaddleOCR or EasyOCR, both Apache-2.0).

**Expected speed:** about 30 frames a second at 720p on the 3070 Ti with a small detector; the
ball is the bottleneck.

## 6. Phases

| Phase | Builds | Done when |
|---|---|---|
| **T1** | `courtvision ingest sportvu --fetch` (636 archives) → `tracking.frames` and `tracking.events` (Parquet, de-duplicated, aligned to play-by-play); QA per game | alignment targets met (§2); coverage report per game |
| **T2** | movement metrics; shot quality; a replay viewer (animated court) | movement matches NBA.com's 2015-16 tracking stats; shot quality calibrated, its gain over location-only measured |
| **T3** | matchups, man and zone, screens, pick-and-roll coverages, help and rotations | hand-labeled evaluation reported; coverage outcomes by team |
| **T4** | EPV and film-aware win probability, run as a real-time replay at 25 frames a second; a Live view | calibration and log loss vs score-and-clock; latency under 200 ms per frame |
| **T5** | video → frames (computer vision) on footage you provide, then a live stream | same frame schema; accuracy on annotated frames; real time on this GPU |

## 7. Decisions for approval

| # | Decision | Recommendation |
|---|---|---|
| D1 | The SportVU logs have no license: use them locally for research, publish only derived numbers (per-shot quality, movement summaries, scheme counts), never the raw frames, and credit the source in the README | yes |
| D2 | Download all 636 games (3.8 GB) with `courtvision ingest sportvu --fetch` from that GitHub repo; keep the archives and Parquet, delete the JSON after conversion | yes |
| D3 | Align with play-by-play through events detected in the tracking (V2 numbering is gone); targets ≥95% matched, ≥95% shooter agreement, distance r ≥ 0.95 | yes |
| D4 | Validate against NBA.com's 2015-16 tracking stats through `nba_api` (already an approved source) | yes |
| D5 | Shot quality as a numpy logistic regression first; add scikit-learn (BSD) or LightGBM (MIT) for gradient boosting only if it clearly helps out of sample, with your OK | numpy first |
| D6 | Screen coverages: I label ~300 screens from the replays to written rules; you spot-check 30 | yes |
| D7 | The live court is a small local web page (canvas + WebSocket) linked from the app. The hosted demo can't animate 2016 games (D1 keeps raw positions private), so it shows derived numbers only | yes |
| D8 | The video pipeline waits for T5; its dependency list is approved then; no AGPL components; it only processes streams or files you have the right to | yes |
| D9 | Code in `src/courtvision/tracking/`, tables in a `tracking` schema, spec here; the rest of courtvision unchanged | yes |
| D10 | 2015-16 is half a season from before the three-point boom: models trained on it are recalibrated on video-derived data before they're trusted on modern games | yes |

*Approved Nov 22 2026 ("Approve all").*

## 8. Phase T1 results (Nov 22 2026)

`courtvision ingest sportvu [--fetch]` (fetch and convert), then `courtvision tracking align`
(also `python -m courtvision.tracking ...`).

| Step | Result |
|---|---|
| Fetch | all 636 archives (3.6 GB); five have no tracking in the source (four empty archives, one with 466 events and no frames), so **631 games** |
| Convert | 631 Parquet files (6.4 GB, about 11 MB a game), de-duplicated by timestamp, home and away players in fixed slots; JSON deleted after each game |
| Align | all 631 games in 3.5 minutes; per-game QA in `data/tracking/align_qa.parquet`, matched shots in `data/tracking/shots/` |

**Against the targets (full season, 106,245 logged field goals):**

| Check | Target | Result |
|---|---|---|
| Field goals the tracking covers | (reported) | 97.0% |
| Covered field goals matched to a tracked shot | ≥95% | **93.1%** (2.3% of those through the guided pass) — missed |
| Shooter agreement, on matches made without the shooter cue | ≥95% | **96.1%** — met |
| Tracked vs logged distance, jump shots pinned down by time and shooter alone | r ≥ 0.95 | **r 0.928** on 55,937 shots — missed; median miss 0.97 ft; 1.9% miss by more than 5 ft; r 0.954 without the worst 2% |

The two misses are small, and partly a ceiling set by the logs (spots several feet off, whole
feet). Recommendation: go on to T2 with the matched 93% (about 96,000 shots); unmatched shots
are left out, never guessed.

*Change, Nov 22 (alignment, found while building it):*
- Shots are logged about **2.7 s after the release** (half within 2.1-3.3 s), sometimes up to
  8 s; the matching window is −9 to +1.5 s around that.
- Logged spots' **distance** from the hoop agrees with the tracking (r 0.96), but their
  **angle** can be 10+ ft off on long shots, so the location gate compares distances, not
  spots. Layups are logged at 0-2 ft wherever the ball left the hand (traced: a "1 ft
  layup" released 7 ft out), so a shot logged near the rim may be released up to 9 ft out.
- The play-by-play's `shotDistance` is sometimes **0** (on three-point misses); distances
  come from its x/y coordinates instead.
- The coverage target is measured over field goals the tracking covers (the first ~15-20 s of
  each quarter and some dead-ball stretches have no frames).
- The shooter is found team first: the team that had the ball over the last 2 s, then its
  last player to hold it for 2+ frames. Before this, a defender contesting at the rim took
  65% of the misattributed shots.
- A **guided second pass** finds the logged shooter's own release for shots still unmatched.
  Those rows are marked `guided` (the shooter came from the log) so analyses can leave them out.
- The distance check uses jump shots that time and shooter pin to a single tracked shot, so a
  miss and the same player's putback can't be confused, and so layups (logged at 0-2 ft) don't
  count against the tracking.

## 9. Phase T2 results (Nov 22 2026)

`courtvision tracking nbacom | movement | shots | replay GAME_ID`.

**The known answers.** NBA.com's 2015-16 tracking splits are season totals, but a date range of
one day is one game per team, so asking day by day gives player-game numbers: 85 dates × 8
splits (speed and distance, touches and possession, catch-and-shoot, pull-ups, and field goals
by the closest defender's four ranges), 680 cached requests.

### 9.1 Movement vs NBA.com

Settings were chosen on even game ids and scored on odd ones: 5,457 player-games with 10+
minutes. The tracked running-clock time is 96.6% of NBA.com's minutes (median), so rates are
compared, and counts are scaled to NBA.com's minutes.

| Check (per player-game, held-out games) | r | Ours vs NBA.com |
|---|---|---|
| Average speed | 0.938 | 4.24 vs 4.24 mph (bias 0.0%, mean miss 0.07 mph) |
| Average speed, summed per player (269 players) | **0.991** | mean miss 0.02 mph |
| Speed on offense / on defense | 0.888 / 0.854 | bias +0.2% / −0.4% |
| Distance (our speed × box-score minutes) | 0.998 | −1.7% (box-score minutes are whole numbers) |
| Touches | 0.962 | −5.1% |
| Time of possession | 0.996 | −1.6% |
| Seconds per touch | 0.977 | +3.2% |
| Dribbles per touch | 0.986 | +4.9% |

The settings the fit half chose:
- distance counted only while the clock runs;
- positions smoothed over 5 frames (0.2 s);
- a touch is at least 15 frames (0.6 s) in hand;
- possession time is the whole touch, gaps included.

Not checked against NBA.com, because it publishes no equivalent: top speed, distance above
10 mph, time in zones, and team spacing. As a face check of spacing, the five on offense
spread widest for Cleveland, Miami and the Clippers (convex hull 565-576 sq ft) and tightest
for Memphis, the Knicks and Orlando (472-503).

### 9.2 Shots vs NBA.com

We cover the 90.5% of NBA.com's field goals that matched, so shares are compared per
player-game (players with 8+ field goals by NBA.com's count and 6+ by ours).

| Closest defender | 0-2 ft | 2-4 ft | 4-6 ft | 6+ ft |
|---|---|---|---|---|
| NBA.com | 18.8% | 34.3% | 25.5% | 21.4% |
| Ours | 17.9% | 32.9% | 26.8% | 22.4% |

- Per player-game shares by range: r 0.847 (23,108 cells).
- Catch-and-shoot share: r 0.939; ours runs 2.6 points high (31.2% vs 28.6%). Counting
  logged jump shots only barely changes that, so the cause isn't pinned down.
- Pull-up share: r 0.936 (27.2% vs 27.6%).

### 9.3 Shot quality

Trained on Oct 27-Dec 31 (73,198 shots); judged on Jan 1-23 (22,859 shots it never saw).

| Model | Log loss | Brier | AUC |
|---|---|---|---|
| Base rate | 0.6890 | 0.2479 | 0.500 |
| Location only (distance, angle, three) | 0.6666 | 0.2368 | 0.615 |
| **Location + tracking** | **0.6502** | **0.2297** | **0.638** |
| + shooter skill (shrunk, from earlier games) | 0.6496 | 0.2294 | 0.640 |

- **The tracking adds 2.5%** in log loss beyond location (location itself adds 3.3% over the
  base rate). Most of it comes at the rim, where log loss falls from 0.660 to 0.606.
  Elsewhere the gain is smaller: 4-14 ft 0.682 → 0.670, long twos 0.677 → 0.674, threes
  0.650 → 0.647.
- **Calibrated on the held-out games:** recalibration slope 0.97, intercept 0.00, expected
  calibration error 0.019 (location only: 0.013).
- **Make rates by the closest defender move the way they should,** and the model follows
  them:
  - at the rim, from 53% (0-2 ft) to 95% (6+ ft);
  - on threes, from 25% to 39%.
- The shooter-skill term adds almost nothing (0.6502 → 0.6496).
- **Shot-making is about as noisy as raw efficiency over half a season.** In odd vs even
  games (253 players with 150+ shots), points over expected per shot repeat at r 0.27, raw
  points per shot at 0.28, and expected points per shot at 0.87. The shots a player gets
  are a stable trait; how far he beats them takes more than half a season to show.
- The leaders still read right (300+ shots, points over expected per shot): Curry +0.25,
  Redick +0.25, Durant +0.24, Leonard +0.20, Nowitzki +0.16, Middleton +0.15.
- Gradient boosting (D5) isn't needed for now: numpy logistic regression is enough for this
  test.

### 9.4 Replay viewer

`courtvision tracking replay GAME_ID [--period N] [--open]` writes one self-contained page
(about 5 MB a game) to `data/tracking/replays/`.

What it shows:
- players with their jersey numbers, and the ball drawn bigger the higher it is;
- the game and shot clocks, and the score;
- every matched shot in a list with its chance of going in; clicking one jumps to it;
- at each release, the shooter ringed and a line to the closest defender.

Playback runs at 0.5-8×, and the page follows the system's light or dark theme. It holds raw
positions, so it stays local (D1).

*Change, Nov 22 (movement and shots, found while building them):*
- Speed matches NBA.com as distance **while the clock runs** over running-clock time.
  NBA.com divides distance by minutes played; counting movement during stoppages too reads
  0.41 mph high.
- Raw positions jitter by a few inches, which unsmoothed would add distance nobody ran. A
  0.2 s moving average matches NBA.com with no bias.
- Steps implying more than 22 mph are tracking glitches and are dropped. Top speed is held
  over a full second and divided by at least a frame's time, because a few frames arrive
  2 ms apart; before that fix, Kevin Love "reached" 75 mph. Top speed is still rough: the
  highest values (about 20 mph, Kevin Garnett among them) look like glitches just under the
  cut, and NBA.com publishes nothing to check it against.
- NBA.com's touches leave out short holds: at least 0.6 s in hand matched best.
- Ten archives in the source carry a folder prefix in their names, so dates are read with a
  pattern.
- The shot model uses nothing after the release: a shot's kind (rim, tip, launch), its
  flight and its peak are outcomes. A putback is defined from before the release instead
  (the ball was at the rim within the last 3 s).

## 10. Phase T3 results (Nov 22 2026)

`courtvision tracking defense` (matchups, man or zone, screens, coverages, help, outcomes), then
`courtvision tracking labels sample | draw | evaluate | spot` (the hand-labeled check).

### 10.1 Matchups

Who guards whom is a hidden Markov model over each half-court possession (after Franks et al.
2015). A defender stands near a mix of his man, the ball and the hoop, and how far he is from
each attacker's version of that point is the evidence.

- **The weights fit to this data are 0.612 man, 0.115 ball, 0.273 hoop.** Franks et al.
  found 0.62, 0.11, 0.27 on SportVU data from another season, with different code.
- A defender's spread around his point is 3.5 ft. That error takes about 2 s to
  decorrelate, so evidence counts once per 45 frames, not 25 times a second.
- Defenders change their man about 0.5 times a minute.

### 10.2 Man or zone

A possession is called zone when two things hold:
- defenders don't go with cutters: when an attacker moves 10+ ft in 2 s, the defender who was
  on him moves less than 30% as far along his path (in man it's typically 0.5-1);
- a "spots plus ball shift" story fits the defenders better than the matchups do.

Possessions where the attackers barely move, or nobody cuts, are left unread. Both thresholds
were set before any labels.

- **Zone on 3.9% of the 109,169 readable half-court possessions.** The most zone: the Lakers,
  Milwaukee, Houston and Sacramento (5.3-5.6%); the least: San Antonio, New Orleans and Indiana
  (2.6-2.8%).
- **Against the labels** (60 possessions, half of them called zone):
  - of 30 called zone, 15 were labeled zone, 4 man and 11 unclear;
  - of 30 called man, 18 were labeled man, 3 zone and 9 unclear;
  - where the labelers could tell, zone calls were right 79% of the time and man calls 86%.

### 10.3 Ball screens and coverages

A screen: a teammate stops within 5 ft of the ball handler, the handler's defender is within
8 ft of him, the handler is 15+ ft from the hoop, and it isn't a handoff. Its coverage comes from
where the two defenders go (`defense._screen_row` gives the rules), and the route from the
handler's defender's path.

- **About 67 screens a game, 42,610 in all.**
- **Coverage:** drop 73.9%, show 12.0%, switch 7.2%, blitz 4.5%, ice 2.4%.
- **The screener:** rolls 61%, stays 31%, pops 9%.
- **The handler's defender:** goes over 71%, under 20%.
- **The team mix reads right:**
  - Milwaukee, Jason Kidd's trapping defense, blitzes most (10.3%), then Brooklyn, Houston
    and Memphis;
  - Portland, Detroit, Charlotte and the Knicks drop most (80-83%);
  - San Antonio, Golden State, Boston and Chicago switch most (9-11%).

**The labels.** 300 candidate screens from a looser pool (screener within 8 ft, defender within
12 ft), stratified by the detector's call (drop 60, show 40, switch 40, blitz 35, ice 35) plus
90 the detector rejected, and 60 possessions.
- Labeled blind to written rules (`docs/tracking_labeling_rules.md`) from schematic strips of
  court panels, by six Claude subagents, 60 events each. The labeler sees only movement, never
  the detector's call.
- A drawing bug (bars across panels when a player was off the half court) spoiled 65 strips;
  they were redrawn and relabeled.
- The labelers marked half their answers low confidence (180 of 360), so the labels are noisy.
- You spot-check 30 (D6): `courtvision tracking labels spot` opens them as looping replays with
  their labels.
- Rates below are reweighted to the whole pool. Labels are in `docs/tracking_labels.csv`.

| Check (vs the labels) | Result |
|---|---|
| Screen detection precision | **0.82** (of what the detector calls a screen, labeled a screen) |
| Recall within the loose pool | 0.62 (some screens it misses are wider than 5 ft or with the defender 8+ ft off) |
| Coverage accuracy, detected screens with a clear label | **0.78** |
| Route (over or under) agreement | 0.93 (61 screens) |

| Coverage | Called | Precision | Labeled | Recall |
|---|---|---|---|---|
| drop | 49 | **0.86** | 70 | **0.93** |
| switch | 35 | 0.49 | 28 | 0.40 |
| show | 15 | 0.47 | 15 | 0.50 |
| ice | 18 | 0.56 | 19 | 0.10 |
| blitz | 20 | 0.25 | 5 | 1.00 |

**What this means:**
- Drop, the common coverage, is read well.
- The rarer coverages are coin flips against these labels:
  - blitz over-fires on a show with a brief moment of two on the ball;
  - "switch" catches drops where the defenders end up exchanged late;
  - the labelers saw ice far more often (19% of screens) than the detector calls it (2%).

**A revision that didn't hold up (kept as a negative result).** The labels were split by event
number:
- On the odd half (dev), three changes raised coverage accuracy from 0.75 to 0.80:
  - a blitz needs 1 s of two on the ball;
  - a switch is judged by who's nearer whom;
  - a big 8+ ft back at the screen is drop.
- On the even half (test), never looked at while revising, accuracy fell from 0.81 to 0.76.
  Switch improved, show and some drops were lost.

So the original rules stand. The variants stay in the code as switches, set off
(`BLITZ_HOLD`, `SWITCH_RELATIVE`, `DROP_DEEP`). Firmer labels (yours, or video) would be needed
to tune the rare coverages honestly.

### 10.4 Help and rotations

A drive: the handler, with the ball, gets from beyond 16 ft to inside 10 ft of the hoop within
2 s (44,550 drives, 71 a game). A help: another defender leaves his man by 4+ ft to end within
6 ft of the driver.

- 8.8% of drives draw help by this strict definition. Boston, Atlanta, Brooklyn and
  Sacramento help most (10-11%); Dallas, New Orleans and Portland least (7%).
- On a kick-out to the man the helper left (642 drives), the receiver is open by a median
  11 ft at the catch, and a defender closes to 6 ft in a median 0.72 s.
- Help wasn't hand-labeled; these are geometry, not yet checked.

### 10.5 Outcomes by coverage

Points scored after the screen, until the possession ends (the other team holds the ball 12
frames, the period ends, or 24 s pass), come from the play-by-play; expected points come from
the shot model (§9.3).

| Coverage | Screens | Points | Expected points | Shot taken |
|---|---|---|---|---|
| switch | 3,055 | 0.93 | 0.80 | 75% |
| show | 5,121 | 0.91 | 0.75 | 73% |
| drop | 31,504 | 0.91 | 0.77 | 75% |
| blitz | 1,910 | 0.86 | 0.73 | 68% |
| ice | 1,020 | 0.74 | 0.59 | 57% |

- These are descriptive, not causal: defenses choose coverages by matchup and situation (ice
  mostly on side screens), and a quarter of blitz and switch calls are wrong (§10.3).
- After a screen the screener rolls for 0.92 points, stays for 0.89, and pops for 0.82.
- Drives: unhelped 1.05 points; helped without a kick-out 0.93; helped with a kick-out 1.09.
  The help stops the drive, and the kick-out gives it back.
- Per team and coverage: `data/tracking/defense/coverage_by_team.parquet`.
- Lineup-level coverage results aren't reported: half a season leaves too few screens per
  five.

*Change, Nov 22 (defense, found while building it):*
- Matchup evidence is tempered by its 2 s decorrelation. Counting all 25 frames a second made
  defenders flip men six times a minute.
- The first zone test (spots vs matchups alone) called 25% of possessions zone, because giving
  each defender his own spot flatters the zone story. Requiring that defenders also not
  follow cutters brought it to 3.9%.
- Possession, for outcomes, ends only when the other team holds the ball 12 frames. 173 of
  590 possession runs in a sample game lasted under 0.5 s: a hand on the ball, a deflection.
- Help "rotation time" from the help itself was capped (3 s) for most drives, since most help
  never leads to a pass. It's now measured on kick-outs only.

## 11. Phase T4 results (Nov 22 2026)

`courtvision tracking winprob` (training rows, EPV, win probability, the held-out report),
then `courtvision tracking live GAME_ID` (the Live view) or `courtvision tracking live
GAME_ID --check` (a whole game through the engine, timed and compared with the batch path).

### 11.1 The streaming engine

- **One code path.** The training tables and the live engine call the same functions:
  - `state.py`: the causal trackers (who has the ball, which team is on offense, which hoop
    it attacks);
  - `epv.raw_features`: the EPV inputs, for one frame or many.

  The batch path runs them over a whole game at once; the engine runs them one frame at a
  time.
- **Nothing looks ahead.**
  - The ball holder: T1-T3's detector used a frame or two of the future. The engine uses its
    causal twin, which agrees on 99.4% of frames.
  - Possession changes when the other team has held the ball 12 frames (as in T3).
  - Each team's hoop comes from where the ball has been held this period; before that, last
    period's, switched at halftime. It agrees with T3's whole-period rule on 99.8% of frames;
    the misses are the first seconds of the game.
- **Checked.** A whole game run frame by frame matches the batch tables at every compared
  frame (one a second): EPV inputs to 1e-14, offense and score exactly, win probability to
  1e-15.
- **Speed** (the budget is 200 ms a frame):

| Game | Frames | Engine, p50 / p99 / worst | Plus the page's message | Faster than real time |
|---|---|---|---|---|
| NYK at CHI (0021500493) | 74,636 | 1.1 / 2.4 / 5.3 ms | 1.2 / 2.5 / 5.6 ms | 30× |
| OKC at POR (0021500564) | 83,170 | 1.1 / 2.4 / 6.6 ms | 1.2 / 2.5 / 6.8 ms | 30× |

In the Live view the replay holds 24.9 frames a second at 1× (25 is real time), and a frame
reaches the page in under 1 ms on this machine.

### 11.2 The score feed

Win probability needs the score the way a live feed shows it. Each scoring play goes up:
- **a made field goal:** 12 frames (0.5 s) after the ball reaches the rim (T1's release and
  rim frames);
- **a free throw, or a basket the tracking didn't match:** when the possession ends, or half a
  second of game clock after its play-by-play time, whichever comes first.

So every point is counted exactly once: on the scoreboard, or still to come in the possession
it belongs to (EPV's target). In a sample game, 97% of scoring plays tie to a tracked
possession, and the scoreboard ends at the final score.

### 11.3 Expected possession value (EPV)

EPV is the points the team with the ball will still add this possession, from the floor right
now (after Cervone et al. 2016). Its inputs:
- the shot clock, and how long the team has had the ball;
- who holds the ball: an attacker, a defender, or nobody;
- the holder's distance to the hoop, his nearest defender, the value of a shot from his spot
  (the T2 location model), and the defenders between him and the hoop (transition);
- the best open teammate, spacing, and defenders in the paint;
- when nobody holds the ball, its height and speed toward the hoop (a shot or pass in the air);
- dead balls: free throws and inbounds.

The model is least squares on a spline basis, numpy only, with one row a second (2.03 million
in all). It's trained on games before 2016-01-01 (480 games, out of fold within them) and
tested on the 151 after.

| Test (478,721 moments) | Error (RMSE, points) | Variance explained |
|---|---|---|
| A constant | 1.120 | 0 |
| Clock only (shot clock, possession age, game clock) | 1.118 | 0.4% |
| **EPV (the floor)** | **1.107** | **2.4%** |

- **The share is small** because most of what a possession scores is whether its shot goes in.
  Ranked, though, EPV separates possessions: its top tenth of moments led to 1.14 points, its
  bottom tenth to 0.45.
- **No overfitting:** 2.5% out of fold on the training games, 2.4% on test.
- **Situations, on the test games:**

| Moment | EPV | Points that followed |
|---|---|---|
| Free-throw setup | 1.23 | 1.24 |
| Transition (0-1 defenders between the holder and the hoop) | 0.98 | 1.01 |
| Half court, shot clock 4 s or less | 0.68 | 0.64 |
| The defense has the ball (the possession is about to change) | 0.35 | 0.35 |
| Holder open (6+ ft) inside 8 ft (671 moments) | 1.03 | 1.22 |

- **The middle deciles run 0.03-0.09 low on test.** January's offenses scored more than
  October-December's (0.89 vs 0.86 points still to come per moment), and a model trained on
  the earlier months can't see that.
- **Not modeled: end-game strategy.** Score and time aren't EPV inputs. In the last minute:
  - an offense up 4-6 points, fouled on purpose, scored 0.99 points per possession against
    0.83 EPV;
  - an offense up 7+, running out the clock, scored 0.67 against 0.77.

### 11.4 Win probability, with and without the film

**Baseline (score and clock).** 3.47 million states, one every 10 s, from 11,969 games: ten
seasons of courtvision's play-by-play (2008-09 to 2018-19, leaving out 2015-16). Its inputs,
after Stern (1994):
- the margin over the root of the time left;
- the pre-game margin times it;
- possession over it.

**Pre-game margin.** courtvision's team model, refitted weekly on earlier games. It picked the
winner 67% of the time in 2014-15.

**Film terms.** Fitted on the 480 training tracking games, with the baseline kept as an
offset:
- EPV beyond an average possession, signed for home, over the root of the time left;
- lineup strength: the ten players' courtvision impact ratings from 2014-15.

The Live view uses EPV plus lineups, chosen out of fold on the training games.

**Test:** 151 games, one moment a second. Log loss (lower is better):

| Situation | Moments | Score and clock | Recalibrated | + EPV | + EPV + lineups |
|---|---|---|---|---|---|
| All | 479,465 | 0.4343 | 0.4345 | 0.4344 | **0.4245** |
| 1st quarter | 116,216 | 0.5617 | 0.5638 | 0.5637 | **0.5452** |
| 2nd quarter | 118,127 | 0.4896 | 0.4922 | 0.4922 | **0.4826** |
| 3rd quarter | 119,886 | 0.4233 | 0.4237 | 0.4236 | **0.4161** |
| 4th, before the last 5 min | 66,940 | 0.3202 | 0.3178 | 0.3178 | **0.3121** |
| Last 5 min of the 4th | 52,093 | 0.1794 | 0.1769 | **0.1769** | 0.1769 |
| Last 5 min, within 5 points | 15,908 | 0.4906 | 0.4797 | 0.4796 | **0.4743** |
| Last 2 min, within 5 points | 7,378 | 0.3859 | 0.3791 | 0.3790 | **0.3789** |
| Overtime | 6,203 | 0.5750 | 0.5429 | 0.5424 | **0.5137** |

**What this shows:**
- **EPV doesn't measurably sharpen win probability moment by moment.**
  - Log loss moves from 0.43447 to 0.43444 overall. The 95% interval of the gain, resampling
    games, is −0.00004 to 0.00009.
  - In the last five minutes within five points, the interval is −0.0009 to 0.0011.
  - Most of win probability is score and time, and a possession's value is a fraction of a
    point.
- **EPV does tell you where win probability is going.** During a possession, add EPV to the
  baseline and it predicts the baseline's own number after the possession (once its points
  are on the board) with less squared error:
  - 1.7% less overall (95% interval 1.2-2.1%);
  - 1.6% in the last five minutes of close games (0.2-2.7%);
  - the last two minutes are too few to tell (1.5%, −0.8 to 3.4%).

  This is the in-possession information the film adds: it moves the number before the
  scoreboard does. In the fitted model, an expected point of possession value is worth 0.74
  of a point on the scoreboard.
- **Lineup strength helps more,** mostly early in games (first quarter 0.564 → 0.545):
  - knowing who's on the floor sharpens a pre-game margin built on a few weeks of games;
  - by games, its interval over EPV alone is wide (−0.006 to 0.025), but it was also better
    out of fold on the training games (0.482 → 0.469);
  - it isn't film: the play-by-play knows who's on the floor.

**Calibration** (the live model, test moments):

| Predicted (home) | Happened | Moments |
|---|---|---|
| 0.04 | 0.01 | 61,174 |
| 0.15 | 0.14 | 34,026 |
| 0.25 | 0.32 | 38,448 |
| 0.35 | 0.41 | 34,501 |
| 0.45 | 0.43 | 34,154 |
| 0.55 | 0.57 | 38,542 |
| 0.65 | 0.67 | 45,216 |
| 0.75 | 0.71 | 48,409 |
| 0.85 | 0.86 | 57,761 |
| 0.96 | 0.98 | 87,234 |

The misses in the 0.25-0.35 bins come from a few games: 151 games is a small test for
whole-game outcomes.

### 11.5 The Live view

`courtvision tracking live GAME_ID` serves http://127.0.0.1:8770/. It needs only the standard
library: asyncio and a minimal WebSocket.

**The page shows:**
- the court, the score and both clocks;
- win probability, film and score-and-clock, with a chart over the game;
- the possession's expected points;
- a feed of plays: shot chances (T2) at the release, and screen coverages (T3) 3 s after the
  screen. Both are read from the batch tables at the moment they would be known.

**Controls:**
- play, pause, and speeds from 0.5× to 16×;
- jump to any quarter or the last two minutes. The engine resumes from the trackers' recorded
  state, exactly.

It listens on 127.0.0.1 only and answers only pages from that address, so raw positions never
leave the machine (D1). The hosted app can't show it (D7).

*Change, Nov 22 (found while building it):*
- The live ball holder is causal: backward differences, and a high touch counts once it has
  lasted 3 frames.
- Hoops come from votes pooled over both teams. One team's backcourt dribbling early in a
  period had flipped its hoop (97.6% → 99.8% agreement).
- EPV is floored at zero. The linear fit dipped below zero in a period's final second, which
  swung win probability by more than 20 points on a possession with no time to score.
- The replay clock follows the wall clock. Windows' timer ticks run long, and a fixed tick
  played the game about 15% slow.
