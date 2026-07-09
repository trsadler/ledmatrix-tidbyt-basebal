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

## Data source

ESPN's public scoreboard endpoint, no API key required:
```
https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard
```
