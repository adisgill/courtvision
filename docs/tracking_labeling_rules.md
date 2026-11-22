# Labeling rules: ball screens and man or zone

Written before any labels were made (Phase T3, decision D6). The labels test the tracking's
defense detectors, so they're made **blind**: the labeler sees only the players' movement,
never what the detector decided.

## The pictures

Each event is a strip of panels, left to right in time. Every panel is the half court the
offense attacks, drawn from above with the **hoop at the bottom** and the half-court line at
the top.

- **Blue circles** are the offense and **red circles** the defense, each with the player's
  jersey number.
- The **small orange dot** is the ball.
- A thin line behind each player is his **trail**: where he came from since the previous panel.
- Ball screens only:
  - the ball handler at the moment in question has an **orange ring**;
  - the teammate who comes close to him (the possible screener) is a **blue square**.

**Ball screens: 4 panels.** 1.5 s before the moment, the moment (the possible screener at his
closest to the handler), 1.5 s after and 3 s after.

**Possessions: 3 panels.** The start, the middle and the end of one half-court possession.

## A. Ball screens

### A1. Is it a ball screen? (`yes` / `no` / `unclear`)

**Yes:** the square player stops (roughly set) next to the ball handler, in the way of the
handler's defender, while the handler has the ball, so the handler can use him to get free.
It's still a screen if the handler then refuses it and goes the other way.

**No:**
- a **handoff** (the square player hands or passes the ball to the handler);
- a teammate merely **cutting past** or standing near the handler with no defender to screen;
- an **off-ball** screen (the handler isn't the one being freed);
- **transition** traffic, a post entry, a scramble.

### A2. Coverage, if it's a screen

First find the two defenders before the screen: the red player guarding the ball handler (the
**handler's defender**) and the red player guarding the square (the **screener's defender**).
Then pick the one description that fits:

- **switch:** the two defenders exchange men. After the screen, the screener's defender is
  guarding the ball handler and the handler's defender has the screener.
- **blitz** (trap): both defenders go to the ball handler together and stay on him for a
  moment (two red players on the ball), at or above the screen.
- **ice:** before the screen, the handler's defender jumps to the screen side, between the
  handler and the screener, so the handler can't use it. The handler goes away from the
  screen, usually toward the sideline or baseline.
- **show** (hedge or "up to touch"): the screener's defender steps up to the level of the
  screen or higher, into the handler's path, to slow him. Then he goes back to his own man.
  Nobody exchanges men.
- **drop:** the screener's defender stays back, below the screen toward the hoop, while the
  handler's defender gets through or around the screen.
- **unclear:** the panels don't show enough to tell.

### A3. The handler's defender's route, unless switch or ice

- **over:** he goes over the top of the screen (on the side away from the hoop) or chases
  behind the handler;
- **under:** he goes beneath the screen, between the screener and the hoop;
- **unclear.**

## B. Man or zone (one possession)

- **man:** each defender stays with a particular attacker, and follows him when he cuts
  across the floor; a switch on a screen is still man;
- **zone:** defenders keep to areas and pass a cutter on to the next defender instead of
  following him; the whole defense shifts as the ball moves;
- **unclear:** not enough movement to tell, a scramble, or both at once.

## The labels file

One line per event, comma separated:

```
event,screen,coverage,route,confidence,note
s017,yes,drop,over,high,
s018,no,,,high,handoff
p04,,,,medium,zone
```

- For a possession (`p..`), the man/zone answer goes in the `note` column.
- `confidence` is `high`, `medium` or `low`.
