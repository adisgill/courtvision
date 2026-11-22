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

`python -m courtvision.tracking fetch | convert | align` (it moves to `courtvision ingest
sportvu` once the main CLI is free to change).

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
