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
| `show_favorite_teams_only` | `false` | Restrict rotation to favorite teams' live games |
| `game_rotation_seconds` | `8` | How long each live game shows before switching |
| `update_interval_seconds` | `300` | Poll rate when nothing is live |
| `live_update_interval_seconds` | `15` | Poll rate while games are live |
| `use_team_colors` | `true` | Pull real team colors from ESPN |
| `show_logos` | `true` | Show team logos |
| `logo_dir` | `assets/sports/mlb_logos` | Local logo folder, checked before ESPN |
| `base_fill_color` / `base_empty_color` | white / grey | Diamond colors |
| `out_fill_color` / `out_empty_color` | orange / grey | Outs indicator colors |
| `font_choice` | `5by7` | `5by7`, `4x6`, `press_start_2p`, or `system` |
| `show_batter_name` | `true` | Show current batter next to the count |
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

## Critical fix: oversized/overflowing team text

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
