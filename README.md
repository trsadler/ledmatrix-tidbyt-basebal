# Tidbyt-Style Baseball Scoreboard (LEDMatrix plugin)

A custom MLB scoreboard for [ChuckBuilds/LEDMatrix](https://github.com/ChuckBuilds/LEDMatrix),
built for a 128x32 panel: two team columns on the left (big logo, bold
abbreviation + score on a darkened bar), and a diamond/inning/count/outs
readout on the right. Cycles through every currently live MLB game by
default.

## Install

1. Push this folder to your own GitHub repo (repo root = this folder).
2. On your Pi, open the web UI: `http://<pi-ip>:5000` → **Plugin Manager**.
3. Use **Install from GitHub URL**, paste your repo URL.
4. Restart the display service so the loader picks it up.
5. Add the block from `example_config.json` in the web UI config editor,
   setting `favorite_teams` to your team(s).

## Testing without hardware

Set `"test_mode": true` to render a fake in-progress game (ATH @ DET)
instead of calling ESPN.

## What changed in this round

- **Anti-aliased triangle & diamond**: the inning indicator and bases
  are now drawn at 4x resolution and downsampled with LANCZOS
  resampling before compositing onto the panel image. This produces
  real partial-brightness edge pixels instead of a binary on/off fill,
  which is what was causing the jagged diagonal edges before. RGB LED
  panels can display those intermediate brightness levels, so this is
  a genuine visual improvement on real hardware, not just a software
  nicety. Verified by checking the rendered output actually contains a
  gradient of pixel values along the edges, not just pure black/white.
- **Much bigger logos**: logos now fill nearly the entire 32x32 team
  column (up to 30px) instead of sharing space with a separate text
  row. A darkened bar (half-brightness version of the team color) sits
  across the bottom of the column so the abbreviation/score text stays
  readable on top of the logo instead of competing with it.
- **Selectable bundled fonts**: three real pixel fonts pulled directly
  from ChuckBuilds/LEDMatrix's `assets/fonts/` folder are now bundled
  *with this plugin* (in `./fonts/`), so they're guaranteed available
  regardless of your install's layout. Pick one via `font_choice`:
  - `"5by7"` (default) -- best balance of legibility and compactness,
    comfortably fits `"ABBR SCORE"` including double-digit scores
  - `"4x6"` -- similarly compact, slightly different look
  - `"press_start_2p"` -- the classic arcade font, but it's quite wide
    per character; measured widths show it only fits this column's
    32px width at a very small 4-5px tall size, so it's available but
    not the best fit for this particular row
  - `"system"` -- falls back to a generic bold monospace font (and
    still tries auto-discovering a font from your main LEDMatrix
    install's `assets/fonts/` folder first, preferring anything with
    "press", "pixel", "matrix", etc. in the filename)
- **Text size fits dynamically regardless of font choice**: the
  abbreviation+score line shrinks its font size (down to a floor) until
  it actually fits the column width, since different fonts have very
  different glyph widths.

## Choosing a font

Set `font_choice` in config to `"5by7"` (default), `"4x6"`,
`"press_start_2p"`, or `"system"`. Measured glyph widths for
`"ATH 3"` / `"DET 12"` at the sizes this plugin will actually use:

| Font | Height at max fitting size | Fits `"DET 12"` in 28px? |
|---|---|---|
| 5by7 | 6-7px | Yes, comfortably |
| 4x6 | 6-8px | Yes, comfortably |
| press_start_2p | 4-5px | Only just -- quite cramped |
| system (DejaVu Sans Mono Bold) | 5-7px | Yes |

If you try `press_start_2p` and it looks too small to read, that's
expected given its per-character width -- switch back to `5by7` or
`4x6` for this particular layout.

**Licensing note**: these font files were pulled from ChuckBuilds/LEDMatrix's
`assets/fonts/` folder and are bundled directly in this plugin's `fonts/`
subfolder for portability. Press Start 2P is SIL Open Font License
licensed, so redistribution is fine. I don't know the license terms
for `5by7` and `4x6` specifically -- worth a quick check if you plan to
share this plugin publicly, though for personal use on your own display
it's not a concern.

## Layout notes / where to tweak things

All rendering lives in `manager.py::display()`:

- **Team columns** (`_draw_team_column`): logo sized to nearly fill the
  column, darkened team-color bar at the bottom holding bold
  `"ABBR SCORE"` text, dynamically sized to fit.
- **Anti-aliasing** (`_draw_smooth_polygon`): generic helper used by
  both the diamond and the inning triangle. Adjust `supersample`
  (default 4) if you want even smoother edges at the cost of a bit more
  render time per frame.
- **Right half layout** (updated):
  - upper-left: inning indicator
  - upper-right: outs indicator (colors via `out_fill_color`/`out_empty_color`)
  - middle: diamond of bases (colors via `base_fill_color`/`base_empty_color`).
    Its size is now *derived* from the actual vertical gap between the
    top row and bottom row (`_draw_diamond`'s `h` parameter is a real
    space budget, not a fixed scale factor) -- so if you adjust
    anything else in this layout, the diamond automatically resizes to
    fit without needing to hand-tune overlap margins again.
  - bottom-left: ball-strike count
  - bottom-right: current batter (`_draw_batter`), first initial + last
    name, next to the count. Shrinks to fit whatever width remains.
- **Rotation** (`_maybe_rotate`, `_current_game`): advances through
  `self.live_games` every `game_rotation_seconds`.

## About the batter name specifically

ESPN's scoreboard endpoint isn't officially documented, so I don't have
a confirmed field name for the current batter -- I couldn't verify this
against live data from this sandbox (no network access here). 
`_parse_game`'s `extract_batter_name()` tries several plausible paths
(`situation.batter.athlete.displayName`, `situation.atBat.athlete.displayName`,
etc.) and just omits the batter line if none of them match, rather than
crashing or showing a placeholder.

**If the batter name doesn't show up during a real live game**: check
your plugin logs during that game. If you're comfortable poking at it,
the quickest fix is to temporarily add `self.logger.info(f"situation
keys: {situation.keys()}")` right after `situation = comp.get("situation",
{})` in `_parse_game`, restart, and send me what it logs during a live
at-bat -- I'll wire up the correct path immediately.

## Config options

See `config_schema.json` for the full list.

| Key | Default | Notes |
|---|---|---|
| `favorite_teams` | `["PHI"]` | Fallback game + rotation filter if restricted |
| `show_favorite_teams_only` | `false` | STRICT: never show other teams' games at all, live or otherwise |
| `game_rotation_seconds` | `8` | How long each game shows before switching |
| `update_interval_seconds` | `300` | Poll rate when nothing is live |
| `live_update_interval_seconds` | `15` | Poll rate while games are live |
| `away_color` / `home_color` | white / white | Fallback colors if `use_team_colors` can't find one |
| `use_team_colors` | `true` | Pull real team colors from ESPN |
| `show_logos` | `true` | Show team logos |
| `logo_dir` | `assets/sports/mlb_logos` | Local logo folder, checked before ESPN |
| `base_fill_color` / `base_empty_color` | white / grey | Diamond colors |
| `out_fill_color` / `out_empty_color` | orange / grey | Outs indicator colors |
| `show_batter_name` | `true` | Show current batter next to the count |
| `show_last_play` | `true` | Flash the last play for significant moments |
| `last_play_display_seconds` | `5` | How long the last-play flash stays up |
| `last_play_filter` | `significant` | `significant` or `all` |
| `show_past_games` | `false` | Include recent final favorite-team games in rotation |
| `show_upcoming_games` | `false` | Include upcoming favorite-team games in rotation |
| `max_past_games` / `max_upcoming_games` | `3` / `3` | Caps on how many of each to include |
| `past_upcoming_all_teams` | `false` | Expand past/upcoming to all teams when no favorite is live (non-strict mode only) |
| `test_mode` | `false` | Render a fake game for layout testing |

## If text still looks wrong / blocky / generic on real hardware

This is very likely a font loading failure, not a design problem. PIL's
built-in fallback font (`ImageFont.load_default()`) **ignores whatever
size you ask for** and renders a fixed, crude bitmap font -- which
would explain blocky/unreadable text AND an oversized ball-strike count
at the same time, since neither can actually shrink to the size the
code is requesting.

**Check your plugin logs right after this loads.** You should see:
```
INFO: font_choice '5by7' -> bundled file found OK at .../fonts/5by7_regular.ttf
```
If instead you see:
```
ERROR: font_choice '5by7' -> bundled file NOT FOUND at ...
```
that confirms the `fonts/` folder didn't make it into your installed
copy of the plugin correctly. Most likely cause: if you used the GitHub
web upload method rather than `git`, double check the three `.ttf`
files actually show up in your repo's `fonts/` folder (binary files
sometimes don't survive a drag-and-drop upload cleanly). Re-upload them
if they're missing or 0 bytes, then reinstall/update the plugin.

I verified this diagnostic actually fires correctly by testing with the
`fonts/` folder deliberately removed -- it logs a clear, loud error
rather than failing silently into the generic fallback.

## Other fixes in this round

- **Faux-bold no longer smears text**: it previously offset the text
  both horizontally AND vertically to thicken strokes. At these very
  small glyph heights (5-7px), the vertical offset was blurring letters
  into the row above/below, which likely also contributed to
  readability complaints even with the correct font loading fine.
  Now horizontal-only.
- **Ball-strike count uses its own smaller font** (`font_count`, size 6)
  instead of reusing the same size as the inning number -- it was
  rendering noticeably larger than intended.
- **Inning triangle is no longer anti-aliased**: at only 6px, the
  supersample+downsample smoothing was producing a soft/blobby shape
  instead of a clean triangle -- ironically, anti-aliasing hurt more
  than it helped at that size. It's drawn with a hard edge now (the
  diamond, being bigger, still benefits from and keeps anti-aliasing).
- **Inning indicator now has a proper top margin** (2px) instead of
  sitting almost flush against the panel's physical top edge.

## Fixes: text halo and diamond outline thickness

- **The "halo/extra stroke" around text was the faux-bold trick**: text
  was being drawn twice, offset by 1px, to fake a bolder weight. At
  these tiny glyph sizes that reads as a double-struck ghost image
  rather than actual boldness. Removed entirely -- text is now drawn
  once, plainly, using the font's own weight.
- **The diamond's unoccupied-base outline is now an exact 1px line**:
  it was being run through the same anti-aliasing helper as the inning
  triangle (supersample 4x + LANCZOS downsample), which blurred a
  requested 1px outline into something visually thicker. Switched to
  PIL's plain `polygon(..., outline=..., width=1)` -- verified by
  checking the actual rendered pixel values contain no in-between
  (anti-aliased) shades, only pure background/outline/fill colors.
- The diamond ended up marginally bigger as a side effect of these
  layout numbers working out that way -- not a deliberate bump, just
  where the math landed.

## Three fixes: home run detection, box score left gap, pitch count accuracy

**1. Home run animation never triggering** -- root cause confirmed:
detection relied solely on a guessed ESPN type code
(`HOME_RUN_PLAY_TYPES`), which I'd explicitly flagged as unconfirmed
when I built it. Real-world testing showed it's simply wrong -- real
home runs never matched, always falling back to the plain text
overlay. Fixed with a second, more reliable signal: `_is_home_run_play`
now ALSO checks the play's actual narrative text for "home run" /
"homers" / "homered" -- ESPN's human-readable phrasing for a home run
call is predictable ("Judge homers to right field") regardless of
whatever internal type code they use. Tested against 7 scenarios
including the exact failure mode reported (unrecognized type code,
real home-run text) -- all pass. Verified through the full `display()`
pipeline at three different elapsed times, confirming all three
animation phases (ball arc, strobe, fireworks) actually render distinct
pixels, not just that the detection function returns the right boolean.

**2. Box score had a 1px black gap on the left edge** -- the separator
bar is 1px wide at `x=left_w`, but the box score's green fill started
at `left_w+2`, leaving `x=left_w+1` completely unfilled (stayed
whatever the original black background was). Invisible on the live/
upcoming layouts since their black half has no distinct fill color to
seam against, but very visible here. Fixed by starting the green fill
at `left_w+1`, immediately adjacent to the separator. Verified with
direct pixel sampling across the transition -- no black pixel remains
anywhere in it.

**3. Pitch count inaccurate in most games** -- found two real
weaknesses in the matching logic and tightened both, though I want to
be upfront that this reduces rather than eliminates the risk of
inaccuracy, since I still don't have confirmed real data for ESPN's
exact structure:
   - The specific-path matcher accepted a bare single-letter `"P"`
     label as meaning "Pitches" -- but box scores commonly use `"P"`
     for **Position**, not pitch count. Removed it, keeping only the
     much less ambiguous `"PC"`, `"PITCHES"`, `"PITCHESTHROWN"`.
   - The generic fallback's ID-matching recursed through the **entire**
     response with no depth limit, meaning the pitcher's ID
     coincidentally appearing anywhere else in a large response (rosters,
     standings, play-by-play) could match an unrelated field. Added a
     depth limit (3 levels) so it only trusts nearby matches.
   - Tested both fixes against the exact false-positive scenario (a
     `"P"` = Position column) and a deliberately distant/unrelated match
     -- both now correctly rejected, while legitimate close matches
     still work.
   - Added an INFO-level log line whenever a count IS found, so it's
     easy to spot-check against a real broadcast without needing debug
     logging enabled.
   - **Since games are live right now**, running `dump_summary.py`
     (shared earlier) during an actual game would give real ground-truth
     data to fix this with certainty instead of further tightening
     guesses -- happy to take another pass once there's real data to
     work from.

## Fixed: dark unfilled border around the box score

**Root cause wasn't the grid math** -- it was how `right_w` itself was
computed. The formula `width - right_x0 - 1` (used consistently across
all three game-type layouts) deliberately leaves the very last pixel
column unaccounted for; on the live/upcoming layouts that 1px never
gets touched by anything so it just stays background-colored and
unnoticed, but on the box score it meant that column was never filled
with the green background at all -- still plain black, which read as a
dark border along the right edge.

Also switched the grid from a fixed cell width (`right_w // total_cols`,
floor division) to exact cumulative column/row boundaries, so the grid
spans the *entire* available space with no leftover remainder pixels
either -- distributing any rounding slack across cells instead of
dumping it all into one unused strip. As a side effect, cells came out
very slightly larger on average (6-7px instead of a flat 6px).

Verified concretely, not just visually: sampled the actual pixel color
at the true right edge (x=127) before and after the fix -- was
unfilled black, now correctly grid-green -- and scanned the entire box
score area for any pixel that wasn't one of the four expected colors
(background, grid line, text, winner-highlight). Found 32 stray
pixels before the fix (exactly one column's worth, 32 rows tall --
matching the missing-column theory precisely), zero after.

## New: traditional green box score for final games

Kept the left-half team columns (logo/abbreviation/score, same as the
live layout, winning team's bar still highlighted yellow) and confined
the box score to the black half only, per follow-up request -- header
row (inning numbers + R/H/E labels), then one row per team showing
runs scored each inning plus final Runs/Hits/Errors, Fenway-green
style. No team-abbreviation column in the grid itself, since the team
names are already visible on the left; the box score's top row is
away, bottom is home, matching the left half's top/bottom team columns.

**Squeezed the team columns to make room**, per your explicit go-ahead:
went from a 50/50 split to 40/60 (left/right). Verified this was
actually necessary, not just assumed: at the original 50/50 split, box
score cells came out to 5px wide, and cropping/inspecting an actual
rendered digit at that size showed it bleeding right up against the
cell border with no clearance. After the squeeze, cells are 6px, and
the same check confirmed a full pixel of clearance from the border on
both sides -- a real, measured improvement, not just "looks a little
better."

**Data confidence, same honest breakdown as everything else in this
plugin**: per-inning `linescores` are a documented ESPN field
(confirmed via independent community API documentation, not just
assumed), so those should be reliable. Hits and errors are NOT
confirmed the same way -- there's a generic `statistics` array
documented but not confirmed specifically for baseball H/E, so those
get backfilled from ESPN's detailed summary endpoint
(`_enrich_boxscore_stats`) if missing from the lightweight scoreboard
response, and simply show blank (not a misleading `0`) if even that
doesn't find them. This only ever fetches once per completed game
(tracked via `_enriched_boxscore_event_ids`) since a finished game's
stats can't change -- verified this caching actually prevents a
second fetch on a repeat call, not just that the fetch itself works.

**Handles edge cases gracefully**: extra-innings games shown are
capped based on how many actually fit at a legible minimum cell width
(re-verified a 15-inning game still renders without crashing or
overflowing after the layout change), and missing hits/errors show
blank cells rather than crashing (re-verified after the squeeze too).

## Fixed: past games stopped showing up

**Same root cause, opposite direction**: this is the mirror image of
the upcoming-games bug -- the main scoreboard call still only returns
TODAY's games, and `past_games` was built exclusively from that
single-day fetch's "post"-state entries. So a favorite team's most
recent completed game only ever showed up if it happened to be
*today*. On an off-day, or before today's game finishes, that most
recent finished game was often yesterday or earlier -- which simply
never appeared, exactly matching "past games stopped working."

**Fixed the same way**: added `_fetch_past_games_lookback()`, which
explicitly queries the previous `past_games_lookback_days` days
(default 3) via the `dates=YYYYMMDD` parameter, merges those with
whatever same-day past games the main fetch found, dedupes by event
ID, and sorts most-recent-first (reverse chronological -- unlike
upcoming games, which sort soonest-first). Same slower refresh
cadence as the upcoming lookahead (`past_games_refresh_seconds`,
default 1800s), since completed games obviously don't change.

Verified end-to-end through the real code path: simulated a scenario
where today has no game at all for the favorite team (an off-day) but
a lookback day has their actual last completed game, and confirmed it
now correctly appears in `past_games` and rotation. Also confirmed the
slower refresh timer holds on repeated immediate calls, same as the
upcoming-games fix.



## Fixed: upcoming games never actually showing up

**Root cause**: ESPN's scoreboard endpoint returns only TODAY's games
when called with no date parameter -- which is what this plugin was
doing. A favorite team's actual next game is almost always tomorrow or
later, not today, so `upcoming_games` was nearly always empty; only a
same-day doubleheader's second game would ever show up. Meanwhile
`past_games` worked fine, since a team that already played today shows
up as "post" state in that same fetch -- which is exactly why it
looked like only final scores were ever cycling.

**Fixed**: added `_fetch_future_upcoming_games()`, which explicitly
queries ESPN's scoreboard for each of the next
`upcoming_games_lookahead_days` days (default 5) using the
`dates=YYYYMMDD` parameter, merges those results with whatever
same-day upcoming games the main fetch found, dedupes by event ID,
and sorts by actual start time. This runs on its own slower timer
(`upcoming_games_refresh_seconds`, default 1800s/30min) rather than
the normal live-polling cadence, since schedules don't change from one
15-second poll to the next -- no reason to re-issue several extra
requests that often.

Verified end-to-end through the real code path, not just by re-reading
the logic: simulated a scenario where today's fetch only contains a
completed game and a future day's fetch (queried via the `dates`
parameter) contains the actual next scheduled game, and confirmed it
now correctly appears in rotation alongside the final score. Also
confirmed the slower refresh timer actually holds -- ran two immediate
back-to-back refreshes and verified the lookahead only fired once, not
twice.

## Separator bar: now 1px gray, and fixed the "N looks like M" issue

- **Separator bar**: changed from 2px white to 1px, color `(166, 166, 166)`,
  on all three game types. Verified by sampling the exact pixel column --
  confirmed it's a single column wide and precisely that gray, with
  neighboring columns unaffected.
- **The "N" glyph really did look like "M"** -- confirmed this by
  decoding the actual bitmap data straight from the font file rather
  than guessing: at only 3 pixels wide, tom_thumb's "N" originally used
  three solid middle rows, differing from "M" by exactly one row --
  genuinely ambiguous, not a rendering bug. Iterated through several
  3px-wide designs (zigzag diagonal, dot at various rows) before
  settling on a real diagonal instead: **N is now 4px wide with an
  actual 2-step staircase stroke**, up from the 3px every other letter
  uses. Confirmed via code inspection that this font's renderer already
  advances the cursor using each glyph's own `DWIDTH` value (not a
  fixed column width), so N's advance was bumped to 5 (instead of the
  usual 4) to preserve the 1px gap before the next letter -- verified
  this holds by directly measuring N's rendered width (5px, as
  intended) and by stress-testing real team abbreviations containing N
  ("MIN", "CIN") to confirm they still fit the column width comfortably
  even with N's extra pixel. The trade-off: N now takes up marginally
  more horizontal space than other letters, so a word containing N
  isn't quite as tightly uniform-width as one without -- judged an
  acceptable cost for a diagonal that actually reads as a diagonal.
  Verified pixel-for-pixel through the actual BDF parser at every
  design iteration, not just that the source file's bytes look right.

## Favorite-team priority cascade

Reworked how `favorite_teams` interacts with rotation, since the old
`show_favorite_teams_only` boolean was too blunt -- it either always
restricted to favorites or never did, regardless of whether a favorite
was actually playing. Now:

1. **A favorite team live?** Rotation shows ONLY that game (or games,
   if multiple favorites are live simultaneously) -- past/upcoming and
   every other team's live game are fully suppressed while this is true.
2. **No favorite live, `show_favorite_teams_only` off (default)**: all
   live games leaguewide + past/upcoming games (scope controlled by
   the new `past_upcoming_all_teams`, favorites-only by default).
3. **No favorite live, `show_favorite_teams_only` on (strict mode)**:
   only favorites' past/upcoming games -- no other team's live game
   ever appears, full stop.
4. Falls out naturally: if the live portion is empty in either branch,
   rotation is just past/upcoming; if everything is empty/disabled,
   falls back to the single best-guess favorite game (the original
   pre-past/upcoming-feature behavior).

`show_favorite_teams_only` is kept specifically as that strict
override -- "never show any other team's games, period" -- rather
than being replaced by the cascade, per your call. The new
`past_upcoming_all_teams` setting only matters in non-strict mode.

**Performance note**: the extra per-game pitch-count API call and
last-play-flash checking now only run on whichever games actually end
up in rotation, not every live game leaguewide -- "show all live
games" mode could otherwise mean a dozen extra summary-endpoint
requests per poll for games that never even get displayed. Verified
directly: set up a favorite-team game alongside three other
leaguewide live games and confirmed enrichment was only attempted for
the favorite's game.

Verified all four cascade branches end-to-end (not just by re-reading
the logic) using synthetic scoreboard data run through the real
`_process_scoreboard`/`_maybe_refresh` code path: favorite-live
suppresses everything else; non-strict fallback includes other live
games plus favorites-only past/upcoming; strict mode excludes other
live games entirely; and `past_upcoming_all_teams` correctly expands
scope in non-strict mode while strict mode correctly overrides it back
to favorites-only.

## Config cleanup

- `away_color`/`home_color` fallback defaults changed to white
  (`[255,255,255]`) per request.
- `font_choice` removed entirely as a user setting -- `tom_thumb` is
  now hardcoded as the only font. The internal fallback chain (other
  bundled TTFs, system fonts, PIL's default) still exists for
  robustness if BDF somehow fails to load, it's just no longer
  something you pick.

## New: past games and upcoming games

Two new game types can now appear in rotation alongside live games:

- **Past (final) games** (`show_past_games`): recently completed
  favorite-team games. The WINNING team's bottom bar is highlighted
  yellow (`255, 200, 0`) instead of its normal team color, and the
  black half shows "FINAL" centered -- no diamond/inning/etc, since
  there's no live situation to show.
- **Upcoming games** (`show_upcoming_games`): scheduled favorite-team
  games, always shown in start-time order (sorted by ESPN's raw
  ISO8601 date string, which sorts correctly across date/month
  boundaries as a plain string comparison -- not by the formatted
  local date, which wouldn't). Team columns show only the abbreviation
  (no score number, since the game hasn't happened yet). The black
  half shows "UPCOMING" with the game's date and local start time
  below it.

Both are **filtered to your favorite teams only**, regardless of the
`show_favorite_teams_only` setting (which governs live-game rotation
scope) -- showing every past/upcoming MLB game league-wide would be
dozens of entries per day, so this only makes sense scoped to teams
you actually follow. Capped at `max_past_games`/`max_upcoming_games`
(default 3 each) to keep rotation length reasonable.

**How rotation works now**: live games always take priority and
appear first; past/upcoming games (if enabled) are appended after.
If none of those have anything (no live games, and past/upcoming are
off or empty), it falls back to the single best-guess favorite-team
game -- the same fallback behavior from before this feature existed.

**Local time assumption**: start times convert from ESPN's UTC
timestamp to your system's local timezone via Python's `astimezone()`
-- this assumes your Pi's system clock/timezone is configured
correctly, which is the normal case for a home device.

Verified with actual rendering and pixel checks, not just code review:
confirmed the winning team's bar is measurably yellow while the losing
team's stays its normal color; confirmed the upcoming-game team text
is measurably narrower than the same team with a score shown
(proving the score is genuinely omitted, not just visually similar);
confirmed past-game filtering excludes non-favorite-team games and
upcoming-game sorting/capping both work correctly against synthetic
multi-game scoreboard data; and confirmed existing live-game rendering
is completely unaffected by all this restructuring.



The bottom row of phase 3 (play description under "HOME RUN!") used
the same wrap/shrink/truncate logic as the plain last-play overlay --
fine there, but this row only has room for a single line, so a long
description would get aggressively shrunk or truncated with an
ellipsis. Replaced with `_draw_scrolling_text`: short text just
displays centered and static, but text wider than the box scrolls
continuously (looping) using the same time-based approach as the rest
of the animation, so nothing is ever cut off -- long descriptions just
take a few seconds longer to fully read.

Caught a real bug while building this: the scroll position is
naturally a float (from the time-based math), but `BDFFont.draw()`
needs integer pixel coordinates for `putpixel()` -- confirmed by
actually running it and hitting a `TypeError`, not just by inspection.
Fixed by rounding to the nearest int before rendering.

Also renders onto a scratch canvas sized exactly to the text's box,
then pastes that onto the real image -- this guarantees the scrolling
text can never bleed outside its intended area (e.g. into the team
panels) even at large negative offsets, since PIL's `draw.text()` only
clips to the full target image's bounds, not to an arbitrary
sub-region.

Verified directly: confirmed scroll position measurably changes across
time samples while staying within the box's pixel bounds throughout,
and confirmed short text (that fits without scrolling) renders
identically across different time values rather than jittering
unnecessarily.



When the last-play flash triggers for a home run specifically, it now
plays a three-phase animated sequence instead of the plain text
overlay, sequenced within the total `last_play_display_seconds` window
(phase lengths scale proportionally, so this still looks reasonable
whether that's set short or long):

1. **Ball arc** -- a small ball traces a parabolic path across the
   panel with a short fading trail, representing the moment of contact.
2. **Strobing flash** -- background alternates black/batting-team-color
   a few times per second, bold "HOME RUN!" text staying steady on top.
3. **Firework bursts + play text** -- settles into a black background
   with a couple of recurring particle bursts, "HOME RUN!" steady
   beneath them, and the actual play description word-wrapped at the
   bottom.

All three phases are pure functions of elapsed time (`time.time()`),
not persistent per-frame state, so they animate smoothly regardless of
however often `display()` happens to get called.

**Detection caveat, same pattern as pitch count/last-play**: I do NOT
have a confirmed real sample of ESPN's home-run type code (unlike
`NON_SIGNIFICANT_PLAY_TYPES`, where "ball" and "start-batterpitcher"
are both confirmed). `HOME_RUN_PLAY_TYPES` is a best-effort guess
(`"home-run"`, `"homerun"`, `"home_run"`, `"hr"`, `"home run"`). If a
real home run doesn't trigger the animation (falls back to the plain
text overlay instead), check the logs for the `"Last-play flash
QUEUED for ... (type=...)"` line during that play and add the actual
type string to `HOME_RUN_PLAY_TYPES` in `manager.py`.

Verified each phase renders distinctly and correctly via direct pixel
checks (not just visual inspection): phase 1's ball position measurably
moves rightward over time, phase 2's background measurably toggles
between team color and black, phase 3 shows both firework-colored
particles and the "HOME RUN!" text simultaneously. Also confirmed a
non-home-run significant play (e.g. a double) still correctly falls
back to the plain text overlay rather than triggering the animation.



**Root cause confirmed** (not a data/filter problem): the original
design set a per-game wall-clock expiry (`flash_until = now + 5s`) the
moment a significant play was DETECTED, completely independent of
whether that game happened to be on screen. Normal rotation runs on
its own separate timer with zero awareness of pending flashes. With 2+
live games rotating, a flash could easily expire before rotation ever
got around to actually showing that game -- so whether you saw it came
down to unlucky timing, not whether the play was detected correctly.
Even when the currently-displayed game got the play, rotation could
still switch away mid-flash and cut it short.

**Fixed**: replaced the per-game timer with a queue (`_pending_flash_event_ids`)
and a single "active" slot (`_active_flash`), serviced by
`_service_flash_queue()` -- called every frame, BEFORE rotation. When
a significant play is queued, it force-jumps `current_index` to that
specific game (interrupting whatever rotation was about to show) and
pauses normal rotation until the flash's duration naturally expires,
at which point rotation gets a fresh window and resumes normally. If
two games get significant plays close together, both queue up and get
shown one after another rather than one clobbering the other.

Verified with real scenarios, not just code review:
- A home run on a game that's NOT currently displayed correctly forces
  an immediate jump to that game (confirmed the display doesn't just
  keep showing whatever was already active and silently drop the play)
- Rotation stays locked on the flashing game for its full duration even
  with a very short rotation interval that would otherwise want to
  switch away
- Rotation resumes normally immediately after the flash expires
- Two simultaneous significant plays on different games both get shown
  in sequence, neither one lost



When something significant happens (hit, walk, strikeout, out, run
scored), the black half temporarily shows the play description instead
of the normal inning/diamond/count layout, then reverts automatically.
Team panels on the left stay untouched throughout.

- **Data source**: `situation.lastPlay` -- confirmed present in the
  same real live-game JSON already captured for this plugin (unlike
  pitch count, this field IS in the lightweight scoreboard endpoint,
  so no extra API call was needed).
- **Filtering**: `situation.lastPlay.type.type` is checked against a
  denylist (`NON_SIGNIFICANT_PLAY_TYPES`) of routine pitch-level
  updates -- confirmed from real data that ESPN sends a new `lastPlay`
  on *every single pitch*, not just outcomes (one sample was literally
  `"Pitch 2 : Ball 2"`). Only two real type codes have been confirmed
  so far (`"ball"` and `"start-batterpitcher"`) -- I only have two
  samples to go on, so this is a denylist rather than an allowlist on
  purpose: an unknown type defaults to showing rather than defaulting
  to hidden, so a real highlight is less likely to get silently
  filtered out by an unrecognized type code. If routine updates still
  slip through, add the offending type string to
  `NON_SIGNIFICANT_PLAY_TYPES` in `manager.py`.
- **Doesn't flash on first sighting**: a game's very first poll never
  triggers a flash (nothing to compare against yet) -- otherwise every
  game would flash immediately on plugin startup for whatever play
  happened to already be current.
- **Per-game tracking**: each game's last-seen play ID and flash
  deadline are tracked separately (keyed by ESPN's event ID), so
  switching between games in rotation can't show a stale or mismatched
  flash from a different game.
- **Text wrapping**: play descriptions are full sentences, so
  `_draw_last_play` word-wraps across multiple lines, picking the
  largest font size that fits both the width and the available height,
  falling back to the smallest size with a truncated+ellipsis line if
  even that doesn't fully fit.

**Config**: `show_last_play` (on/off), `last_play_display_seconds`
(default 5), `last_play_filter` (`"significant"` or `"all"` -- `"all"`
flashes for every single play update including routine pitches, per
your choice of a conservative denylist-based filter for the default).

Tested the trigger logic directly (first-sighting suppression, routine
plays correctly NOT triggering, significant plays correctly triggering,
and the flash correctly expiring after its configured duration) and
confirmed the overlay actually renders wrapped text while leaving the
left-side team panels untouched.



This was a real, systemic bug, not occasional -- confirmed by testing
several realistic pitcher name/pitch-count combinations against the
actual layout width: `"P:112 R. Zeferjahn"` and `"P:134 K. McGonigle"`
both needed truncation even at the smallest font size, while shorter
ones like `"P:47 T. Skubal"` didn't. The previous code only applied
tightening in an `if text_to_draw == text:` branch -- i.e., only when
NO truncation happened. Any name long enough to need truncation (which
turns out to be common, not rare) fell back to plain, untightened
rendering. That's backwards: those are exactly the names that benefit
from tightening the most, and the whole point of tightening was to fit
more of them in.

**Rebuilt both `_draw_pitch_info` and `_draw_batter`** around a single
draw/measure function each (`_draw_pitch_line`/`_measure_pitch_line`
and `_draw_name_tightened`/`_measure_name_tightened`), so measurement
and final rendering can never drift apart again -- the measurement
functions literally call the drawing functions against a scratch
canvas rather than reimplementing the position math separately, which
is what let this bug happen in the first place.

**Also changed what gets truncated**: instead of trimming characters
off the end of the whole combined string (which could theoretically
eat into the "P:47" prefix), truncation now only ever shortens the
pitcher/batter NAME, leaving the pitch count intact.

Verified by re-testing the exact names that triggered the bug --
confirmed tightened gaps (1px) now appear at both junctions even in
cases that need truncation, not just the cases that fit without it.



Batter name now starts at the same left x-position as the inning
indicator and pitch count/pitcher name above it, for a consistent left
edge down the whole column. Ball-strike count moved to the bottom-right
corner, right-aligned. Verified numerically that batter's available
width is correctly capped before the count's position (no overlap --
confirmed a clean 2px gap between them) and confirmed via rendered
pixel colors that each element lands in the expected corner.



Found the real cause: the "extra space" wasn't spacing added between
characters, it was blank design space baked into narrow glyphs
(colons, periods) that the font author left for normal-width spacing
against a space character. Measured actual rendered ink pixels to
confirm: a plain "P:" had a genuine 2px blank gap between P's ink and
the colon's ink, and "T. Skubal" had a full 6px blank gap between the
period and "S".

Added `_ink_extent()` (measures real rendered ink columns, not the
font's nominal advance width) and `_draw_tight_join()` (positions two
pieces of text so there's exactly N real background pixels between
their ink, regardless of which font is active). Used this to tighten:
- The gap between "P" and ":" in the pitch-count row (now 1px)
- The gap between the first-initial+period and the last name, for both
  batter and pitcher names (also now 1px)

Caught a real off-by-one bug while building this: my first version's
gap math was consistently 1px tighter than requested (a nominal
"ink_gap=1" was actually rendering as 0px, touching). Fixed and
re-verified by measuring the actual rendered gap directly -- confirmed
exactly 1px now, not just "looks about right."

This only changes the final rendering step, not the width-fitting/
truncation logic (which still measures the untruncated natural string
width, so it's a strictly safe direction of error -- actual rendered
width is now slightly less than what was measured, never more). If
truncation still kicks in for a very long name, it falls back to a
plain (untightened) render for that specific case, since re-deriving
tightened segment boundaries out of a string that's been cut mid-word
isn't worth the complexity.



- **Outs circles**: increased from 3px to 4px. Re-verified the gap is
  still exactly 1px (not touching) by directly measuring rendered
  pixels, same approach as the last fix -- tested `size=4, gap=1`
  against a few alternatives before picking this one specifically
  because it measured a clean 1px gap.
- **"(TEAM) DUE UP"**: when ESPN's data has a gap between at-bats (no
  current batter AND no current pitcher listed -- this happens
  sometimes between plays), the top row now shows which team is up
  next instead of sitting blank. Team is derived from `inning_half`
  (away team if top of the inning, home team if bottom). Same
  fit-to-width and truncation safety as the other text elements.
  Verified it actually renders (checked real pixel output, not just
  that the code runs) and confirmed the diamond/inning/outs geometry
  is unaffected since the row still reserves the same fixed height
  either way.



Since ESPN's lightweight scoreboard endpoint doesn't include pitch
count, this now makes a second request per live game per poll cycle to
ESPN's more detailed summary endpoint:
```
https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event=<id>
```
That endpoint isn't officially documented either, so `_find_pitch_count()`
tries a specific plausible shape first (boxscore stat tables with
`labels`/`athletes`/`stats` arrays, a common ESPN pattern), then falls
back to a generic recursive scan of the whole response for any
pitch-count-looking key whose subtree also contains the current
pitcher's ID (to avoid grabbing some other player's count). Tested
against synthetic versions of both shapes plus a false-positive check
(confirms it picks the right player when multiple pitch counts are
present) -- all pass. I could not test this against real ESPN summary
data from this environment (no network access), so real-world
accuracy is unverified until you try it live.

**If it still doesn't show up**: run `dump_summary.py` during a live
game (same idea as the earlier `dump_situation.py`/batter-name
diagnostic). It fetches the real summary JSON for whatever's live right
now and prints every field whose name contains "pitch" or "count"
anywhere in the response, plus the current pitcher's ID for
cross-referencing. If nothing relevant turns up in that list, ESPN's
summary endpoint may need a different query parameter or this data
might live under a completely different structure than what I could
reasonably guess -- paste me the output and I'll fix the path exactly
rather than guessing again.

**Performance note**: this doubles the number of ESPN requests while
games are live (one scoreboard + one summary per live game, each poll
interval). For a personal display checking every 15s with a handful of
live games, that's not meaningfully more load, but worth knowing it's
there if you ever have many favorite teams' games live simultaneously.



**Diamond size inconsistency** (bases looking a different size for some
games as the display cycled): confirmed root cause was that the
diamond's vertical space depended on `_draw_pitch_info`'s *actual*
returned height, which varies per game -- 0px for a game with no
pitcher data, ~6px for one with a pitcher name shown. That directly
changed `diamond_available_h` (and therefore the diamond's computed
size) game to game. Fixed by reserving a FIXED height for the pitch
row regardless of its actual content -- verified numerically that the
diamond's geometry (`half`, `center_y`) is now bit-for-bit identical
whether or not a game has pitcher data.

**Outs circles touching with no visible gap**: found a real off-by-one
bug -- a PIL ellipse box built from `radius=2` (nominally a "4px"
circle) actually renders 5 pixels wide, confirmed by direct pixel
measurement. My spacing math assumed 4px circles, so the real 1px gap
I intended became 0px in practice. Rewrote `_draw_outs` to build the
box from the true desired pixel size directly rather than doubling a
radius, reduced the size slightly (3px) as requested, and verified by
scanning actual rendered pixels: exactly 1px of background between
each circle now, confirmed programmatically, not just visually.

## Pitch count: confirmed not available from this ESPN endpoint

As flagged before touching this, the pitch count isn't showing because
it's very likely not present in ESPN's lightweight scoreboard endpoint
at all (confirmed from real captured JSON -- `situation.pitcher` only
has player info and a text summary, no numeric pitch count field). The
pitcher's *name* still shows since that data does exist. If you want a
real live pitch count, it would need an extra API call per live game
(ESPN's more detailed boxscore/summary endpoint) rather than a
one-line fix -- let me know if that's worth the added complexity/request
volume and I'll build it.



- **Color swap**: the darker shade now sits behind each team's logo
  (better contrast for light/white logo elements), and the full bright
  team color moved to the text bar behind the abbreviation/score.
- **Outs**: now vertically stacked circles (filled = recorded, 1px
  outline = not) instead of a horizontal row of squares, positioned to
  the right of the diamond.
- **Inning indicator**: moved down to sit vertically centered on the
  left side of the diamond, instead of pinned to the top corner.
- **New top row**: pitch count + pitcher name, in the space freed up by
  moving inning/outs down. Format is `"P:<count> <Pitcher Name>"` when
  a pitch count is available, or just the pitcher's name if not.

**Important caveat on pitch count**: the real live-game JSON already
captured for this plugin (during earlier batter-name debugging) shows
`situation.pitcher` only contains `playerId`/`period`/`athlete`/
`projections`/`summary` -- no explicit numeric pitch-count field.
`extract_pitch_count()` tries a few plausible alternate paths in case
it's available under a different key or in other game states, but
based on the data already seen, **it will likely just show the
pitcher's name without a count** rather than populating `P:47`. If you
want a guaranteed accurate live pitch count, that would need an extra
per-game API call to ESPN's more detailed boxscore/summary endpoint --
let me know if you want that added (it's a bigger change: one more
HTTP request per live game per poll, not a quick fix).

**Diamond size**: reworked to reserve exact horizontal space for
inning (left) and outs (right) based on their *actual measured widths*
(not a guessed proportion), then size the diamond to fill exactly
what's left -- verified numerically that nothing overlaps. This
happened to leave slightly more room than before, so the diamond is a
bit bigger than in the previous layout.



The exception-handling fix above didn't resolve it, which ruled out
that hypothesis and confirmed the other one: **the core LEDMatrix
scheduler most likely only calls this plugin's `update()` when its
rotation slot is actively on screen**, not continuously in the
background. This plugin's own `live_update_interval_seconds` logic is
useless if `update()` itself isn't being invoked often enough by
something external.

**Fix: this plugin now manages its own refresh cycle**, independent of
however often (or rarely) the core calls `update()`. A background
daemon thread (started in `__init__`) checks every 5 seconds whether
it's time to poll ESPN again, using the exact same interval logic and
exception-safety as before. If the core DOES call `update()`
regularly, that still works too -- both paths share the same
`_maybe_refresh()` method, gated by the same `last_fetch_time` check,
so there's no double-fetching risk either way.

Added a `threading.Lock` around all the shared game-state reads/writes
(`live_games`, `fallback_game`, `current_index`, `last_fetch_time`)
since now two different threads (the core's calling thread and this
plugin's own background thread) can touch that state concurrently.

**Verified concretely**: ran the plugin with zero external calls to
`update()`/`display()` at all -- just started it and slept -- and
confirmed via logging that it fetched fresh data on its own schedule
(3 fetches over 17 seconds at a short test interval, each ~5s apart,
matching the background thread's check cadence). This proves data now
refreshes regardless of the core's calling pattern.

Also added a `cleanup()` method that stops the background thread
cleanly if the core supports calling it on plugin disable/unload (a
no-op if it doesn't -- the thread is a daemon thread either way, so it
won't prevent the process from exiting).



Since you confirmed the **score** was frozen too (not just balls/
strikes/batter), that ruled out a narrow bug in one specific field and
pointed at something systemic: **an unhandled exception silently
killing all future updates after the first successful one.**

Both `update()` and `display()` had this same structural gap: only the
network request itself was wrapped in try/except. Anything that threw
*after* that -- parsing an unusual game state (extra innings, a
pitching change, a field that's temporarily null mid-play, etc.) --
would propagate straight out uncaught. If the core scheduler's plugin
loop doesn't itself guard against a plugin's `update()`/`display()`
throwing, one bad response at any point during a 3+ hour game could
permanently stop this plugin from ever refreshing again -- exactly
matching "shows latest info after restart, then nothing" (the restart
re-runs `__init__`, which works fine off a fresh state; it's some
*later* poll during the live game that likely hit the unhandled
exception).

**Fixed**: both `update()`'s parsing/processing and `display()`'s
rendering are now wrapped in their own broad exception handlers.
`update()` keeps the last-known-good game data and logs a full
traceback instead of losing everything; `display()` falls back to a
simple error frame instead of crashing. I verified this concretely
(not just by inspection): simulated a parsing exception mid-update and
confirmed `update()` survives it, preserves the previous data, logs
the full traceback, and `display()` continues working normally
afterward.

**If this was in fact the bug**, the traceback that gets logged next
time it happens will tell us exactly which game state triggered it --
please share it if you see the new `ERROR: Fetched scoreboard
successfully but failed to parse/process it: ...` line in your logs,
and I can add explicit handling for whatever specific case it turns
out to be.

## Diagnosing: live data seems to stop updating after restart

Two different things could cause this, and they need different fixes,
so `update()` now logs enough to tell them apart:

**Hypothesis A: the core LEDMatrix scheduler isn't calling `update()`
often enough.** This plugin's own polling logic (`live_update_interval_seconds`)
only works if the core actually calls `update()` at least that often.
If the scheduler only calls it when this plugin's rotation slot comes
up on screen (not continuously in the background while other plugins
are showing), the data would only ever refresh once per rotation cycle
-- which could be much slower than 15s if there are several plugins in
rotation. This is outside this plugin's control if it's what's
happening.

**Hypothesis B: `update()` is being called on schedule, but the fetch
itself is silently failing every time** (network hiccup, ESPN rate
limiting, etc.) after the first successful one at startup.

**What the new logging shows:**
```
DEBUG: update() called but skipping fetch -- only 3.2s since last fetch (interval is 15s)...
INFO: Fetched scoreboard OK: 1 live game(s). First: ATH@DET 3-2, count 2-1, outs 1, batter=K. McGonigle
```
or, if the fetch is failing:
```
ERROR: Failed to fetch MLB scoreboard: <error details>
```

**What to check**: during a live game, watch the logs for a minute or
two (past the first update). If you see repeated `Fetched scoreboard OK`
lines with the count/batter actually changing between them, the fetch
logic is fine and it's Hypothesis A (a core-scheduler question, not
something in this plugin). If you see `ERROR: Failed to fetch...`
repeating, that's Hypothesis B and tells us exactly what's failing. If
you see neither -- no log lines from this plugin at all after the
first one -- that's the strongest sign of Hypothesis A: `update()`
simply isn't being invoked again.

One more useful data point: does the **score** (not just balls/strikes/
batter) also stay frozen after the first update, or does it change
correctly while only balls/strikes/batter/count seem stuck? If score
updates fine but those three don't, that points to something more
specific worth digging into rather than a general polling problem.



Confirmed against real live-game data you pulled (ATH@DET and SEA@MIA,
2026-07-09) that ESPN's actual field structure is
`situation.batter.athlete.{displayName, fullName, shortName}` --
extraction itself was working correctly and pulling the right name.

**The actual bug was downstream**: `_draw_batter` had a safety check
that silently skipped drawing entirely if the formatted name didn't
fit even at the smallest allowed font size. Tested with real data:
"Kevin McGonigle" -> "K. McGonigle" measures 48px wide at the floor
size, but only ~44px of space is available next to the count. That's
not a rare edge case -- most real player names are long enough to hit
this, which is why it looked like the feature was completely broken
rather than just failing for a couple of long names.

**Fixed**: instead of skipping, it now truncates character-by-character
(adding a trailing ".") until something fits, so you always see as
much of the name as there's room for rather than nothing at all.
Verified against "K. McGonigle", "V. Guerrero Jr.", and "R. Acuna Jr." --
all now render.

**Also improved**: now prefers ESPN's own pre-formatted `shortName`
field (e.g. "K. McGonigle") instead of reformatting `displayName`
ourselves -- more reliable for suffixes and multi-word names than a
naive "first letter + rest" split, and it's real data ESPN already
provides.



**This was a real bug I introduced with the BDF support, not a tuning
issue.** `_fit_font_for_width`/`_fit_font_for_pair` were checking
`if self.font_choice in BDF_FONT_CHOICES` to decide whether to skip
the shrink-to-fit loop (since BDF is a fixed size and doesn't need
shrinking). But that check trusted the *config string*, not whether a
BDF font actually loaded. If `tom-thumb.bdf` failed to parse for any
reason, `_load_font()` silently falls back to a TTF font -- and the
shrink-loop-skip logic would still fire (because `font_choice` was
still `"tom_thumb"`), returning that TTF font at a fixed `start_size`
(10) with **zero shrinking ever applied**. That's exactly the "team
abbreviations far too big" symptom -- confirmed by deliberately
breaking BDF loading in a test and reproducing oversized, unshrunk
text, then re-running the same test after the fix and confirming text
stays compact (verified numerically: text height dropped from
overflowing most of the panel to a normal ~7px band).

**Fixed**: the skip-shrinking decision now checks
`isinstance(loaded_font, BDFFont)` -- the actual resolved object --
rather than trusting the config string. If BDF fails and falls back to
a TTF, that TTF now correctly goes through the normal shrink loop.

**Also hardened**: `_load_font()` now tries every *other* bundled TTF
this plugin ships with (5by7, 4x6, press_start_2p) before falling back
to OS-level system fonts, since a minimal Raspberry Pi OS install may
not have DejaVu/Liberation fonts at all. And startup now logs the
actual contents of the plugin's `fonts/` directory plus the concrete
resolved font type, so you can confirm from logs alone -- no SSH
needed -- whether BDF loaded correctly:
```
INFO: Plugin fonts/ directory (.../fonts) actually contains: ['PressStart2P-Regular.ttf', '5by7_regular.ttf', '4x6-font.ttf', 'tom-thumb.bdf']
INFO: font_choice 'tom_thumb' -> bundled file found OK at .../fonts/tom-thumb.bdf
INFO: font_choice 'tom_thumb' resolved to: BDFFont (correct)
```
If that last line instead says "resolved to a TrueType font" with a
WARNING, or "resolved to <class ...ImageFont.ImageFont>" (PIL's crude
default), that tells us definitively what's failing on your install --
please share that log output if things still look off.

**Inning-number centering**: I re-verified the centering math (from
the previous fix) against both a working BDF font AND a simulated TTF
fallback, and it measures an exact 0px difference between the
triangle's center and the number's center in both cases. My read is
that the "not centered" appearance in your screenshot was a symptom of
the oversized-font bug above (a much bigger number just looks
disproportionate next to a tiny triangle, even if technically
centered) rather than a separate centering bug. Should resolve once
you update.

**The stray "0" artifact I can't yet explain.** It's very likely
connected to the same font-loading cascade (an oversized or
mismeasured glyph from some other element bleeding into that area),
but I can't pin down the exact mechanism without seeing your actual
logs. Please check them after this update -- if it persists, the
specific log lines above (especially the "resolved to" line) will help
me find it fast rather than guessing again.

## Logo size and inning-number centering (latest tweaks)

- **Logos are bigger** and now allowed to bleed off the panel/column
  edges intentionally (removed the clamp that was snapping them to
  the top-left corner once they exceeded the column size). One
  trade-off worth knowing: since the two team columns sit side by
  side, a big enough logo can bleed a few px into the *neighboring*
  team's column too -- in practice this is rarely very visible since
  most team logos taper to transparent near their outer edge, but if
  it looks off with a particular team's logo shape, the fix is
  tuning the `+ 4` in `_resolve_logos()` back down.
- **Inning number centering was actually off by 1px**, not just a
  matter of preference -- I measured the triangle's real rendered
  pixel extent for both orientations (top/bottom of inning) and found
  its vertical center was consistently 1px lower than where the number
  was landing. Fixed and re-verified: both orientations now measure an
  exact matching center (not just "close").



`font_choice: "tom_thumb"` is now the default. It uses **Tom Thumb**, a
well-known 3x5 pixel BDF font (MIT licensed) purpose-built for tiny LED
displays -- the same one referenced in hzeller/rpi-rgb-led-matrix's own
font collection. Bundled at `fonts/tom-thumb.bdf`.

**Why this should be the most robust option**: Pillow's `ImageFont.truetype()`
can't load `.bdf` at all, and BDF glyphs are exact per-pixel bitmaps
rather than vector outlines FreeType has to rasterize. So this plugin
includes its own minimal BDF parser/renderer (`BDFFont` class in
`manager.py`) that reads the glyph bitmaps directly from the file and
writes each "on" pixel straight to the image with `putpixel()` --
there's no rasterization step at all, so there's nothing that *can*
introduce anti-aliasing, halos, or softness. I verified this by
checking the actual rendered pixel values contain only the exact
background/text colors with zero in-between shades, across every text
element (team names, inning number, count, batter name).

**One real limitation**: BDF is a fixed pixel size (Tom Thumb's glyphs
are only 3-4px wide), so it doesn't go through the shrink-to-fit sizing
logic the TTF options use -- there's only one size. In practice this
is fine since it's already extremely compact (comfortably fits
`"ABBR SCORE"` including double-digit scores without ever needing to
shrink), but if you ever see it overflow a row, that's the reason why,
and the fix would be different from the TTF options (abbreviating text
rather than picking a smaller size).

Other TTF options (`5by7`, `4x6`, `press_start_2p`) are still available
via `font_choice` if you'd rather compare looks side by side.

## Data source

ESPN's public scoreboard endpoint, no API key required:
```
https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard
```
