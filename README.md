# Tidbyt-Style Baseball Scoreboard (LEDMatrix plugin)

A custom MLB live-game scoreboard for [ChuckBuilds/LEDMatrix](https://github.com/ChuckBuilds/LEDMatrix),
styled after the Tidbyt baseball app: split team-color blocks on the left,
diamond/inning/score/count/outs on the right.

## Install

1. Push this folder to your own GitHub repo (repo root = this folder, or a
   subfolder — either works as long as `manifest.json` is at the root the
   installer points at).
2. On your Pi, open the web UI: `http://<pi-ip>:5000` → **Plugin Manager**.
3. Use **Install from GitHub URL**, paste your repo URL.
4. The installer copies the plugin into `plugin-repos/tidbyt-baseball-scoreboard/`
   (the folder name must match the `id` in `manifest.json` — it already does).
5. Restart the display service so the loader picks it up.
6. In the web UI config editor, add the block from `example_config.json`
   under your top-level config (or edit the auto-generated entry once the
   plugin is installed), setting `favorite_teams` to your team(s), e.g. `["PHI"]`.

## Testing without hardware

Set `"test_mode": true` in the config to render a fake in-progress game
(runners on 1st/3rd, top of the 3rd, 3-2) instead of calling ESPN — useful
for checking layout on your panels before wiring in live data. There's also
a standalone render check you can run locally:

```bash
pip install -r requirements.txt
python3 -c "
from manager import TidbytBaseballPlugin
from PIL import Image

class Stub:
    def __init__(self, w, h):
        self.width, self.height = w, h
        self.image = Image.new('RGB', (w, h), (0,0,0))
    def update_display(self):
        self.image.save('preview.png')

dm = Stub(128, 32)
p = TidbytBaseballPlugin('tidbyt-baseball-scoreboard', {'favorite_teams': ['PHI'], 'test_mode': True}, dm, None, None)
p.update(); p.display()
"
```

Open `preview.png` (it'll be tiny — scale it up to inspect).

## Important: verify `_push_image()` against your DisplayManager

I couldn't pull your exact installed `DisplayManager` class definition, so
`_push_image()` in `manager.py` tries the common pattern
(`display_manager.image.paste(...)` + `update_display()`) first, then
`display_manager.set_image(...)`. Since you've already built your own
from-scratch PIL scoreboard against this same hardware, you'll recognize
immediately which one your install actually uses — if neither matches,
the plugin raises a clear `AttributeError` telling you so instead of
silently failing.

## Layout notes / where to tweak things

All rendering lives in `manager.py::display()`:

- **Team rows** (`_draw_team_row`): each colored block reads left to
  right as **logo → abbreviation → score**, vertically centered/aligned.
  Logos are resolved in this order:
  1. **Bundled local logo** at `{logo_dir}/{ABBR}.png` — the same
     `assets/sports/mlb_logos/` folder the core LEDMatrix managers use,
     so if you've already got that installed you get logos with zero
     network calls and zero risk of ESPN changing its response shape.
  2. **ESPN download** (`team.logo` / `team.logos[0].href` from the
     scoreboard response) as a fallback if the local file isn't found.
  3. **No logo** — just abbreviation + score — if neither source has one.

  Resolved logos are cached in memory per team abbreviation, so this
  only runs once per team for the life of the process regardless of
  which source it came from. Set `"show_logos": false` to skip logos
  entirely.
- **Right half quadrants**:
  - upper-left: inning indicator (`_draw_inning`) — ▲ = top, ▼ = bottom
  - upper-right: diamond of bases (`_draw_diamond`) — filled white when
    occupied, outlined grey when empty
  - lower-left: ball-strike count (`_draw_count`), orange text
  - lower-right: outs indicator (`_draw_outs`) — up to 3 squares, filled
    = recorded out
- **Fonts**: tries `assets/fonts/PressStart2P.ttf` then
  `assets/fonts/4x6-font.ttf` (whatever ships with your LEDMatrix install),
  falls back to a bold system monospace font, then PIL's default bitmap
  font if neither exists — swap in whatever pixel font you're already
  using in your own scoreboard project for a closer match.
- **Logo caching**: logos are downloaded once per team abbreviation and
  kept in memory (`self._logo_cache`) for the life of the process, so
  `display()` (called every frame) never blocks on a network request —
  only `update()` triggers a fetch, and only on a cache miss.

## Config options

See `config_schema.json` for the full list. Key ones:

| Key | Default | Notes |
|---|---|---|
| `favorite_teams` | `["PHI"]` | ESPN 2-3 letter abbreviations |
| `update_interval_seconds` | `300` | Poll rate when no game is live |
| `live_update_interval_seconds` | `15` | Poll rate during a live game |
| `use_team_colors` | `true` | Pull real team colors from ESPN |
| `show_logos` | `true` | Show team logos in each color block |
| `logo_dir` | `assets/sports/mlb_logos` | Path to bundled local logo PNGs (checked before falling back to ESPN) |
| `test_mode` | `false` | Render a fake game for layout testing |

## Data source

Uses ESPN's public scoreboard endpoint — the same one your own scoreboard
project already pulls MLB data from:

```
https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard
```

No API key required.
