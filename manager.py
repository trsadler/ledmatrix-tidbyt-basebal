"""
Tidbyt-Style Baseball Scoreboard plugin for ChuckBuilds/LEDMatrix.

Layout:
  - Left half: two team columns side by side, each full panel height.
    Logo fills nearly the whole column; a darkened bar across the
    bottom holds the bold "ABBR SCORE" text for contrast.
  - Right half (black background):
      - upper-left:  inning indicator (anti-aliased triangle + number)
      - upper-right: diamond of bases (anti-aliased, configurable colors)
      - lower-left:  ball-strike count
      - lower-right: outs indicator (configurable colors)

By default this cycles through every currently-live MLB game leaguewide
every `game_rotation_seconds`. Set `show_favorite_teams_only: true` to
restrict rotation to your favorite teams' live games. Falls back to
your favorite team's most recent/upcoming game if nothing is live.

FONT: rather than hardcoding a guessed filename, this scans
assets/fonts/ at startup and picks a real font shipped with your
LEDMatrix install (preferring anything that looks like a pixel/arcade
font, e.g. Press Start 2P, since that's the style the rest of the
project's plugins use). Falls back to a system font if that folder
isn't found. Team abbreviation/score text size is fit dynamically to
the column width so it can never overflow regardless of which font
gets picked up.

Data comes from ESPN's public scoreboard API:
    https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard

NOTE ON display_manager: different LEDMatrix versions have exposed the
PIL image slightly differently over time. This plugin builds its own
RGB PIL.Image internally and then tries, in order:
    1. display_manager.image.paste(...) + display_manager.update_display()
    2. display_manager.set_image(...)
"""

import logging
import os
import time
from io import BytesIO
from typing import Optional, Dict, Any, Tuple, List

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    from src.plugin_system.base_plugin import BasePlugin
except ImportError:
    class BasePlugin:  # type: ignore
        def __init__(self, plugin_id, config, display_manager, cache_manager, plugin_manager):
            self.plugin_id = plugin_id
            self.config = config
            self.display_manager = display_manager
            self.cache_manager = cache_manager
            self.plugin_manager = plugin_manager
            self.logger = logging.getLogger(plugin_id)


ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"

DEFAULT_AWAY_COLOR = (0, 142, 226)
DEFAULT_HOME_COLOR = (200, 16, 46)

# Fonts bundled directly with this plugin (in ./fonts/), pulled from the
# same assets/fonts/ folder the core LEDMatrix project ships with, so
# the look matches the rest of your display without depending on
# discovering files from the main install at runtime.
#
# Measured glyph widths at various sizes (see plugin README) show
# Press Start 2P is quite wide per character -- it doesn't comfortably
# fit "ABBR SCORE" in a 32px-wide column even at very small sizes
# without becoming unreadably tiny. 5by7 and 4x6 are much more compact
# pixel fonts and are the better fit for that particular row; Press
# Start 2P remains a selectable option since it may suit other layouts
# or wider panels.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_CHOICES = {
    "5by7": os.path.join(PLUGIN_DIR, "fonts", "5by7_regular.ttf"),
    "4x6": os.path.join(PLUGIN_DIR, "fonts", "4x6-font.ttf"),
    "press_start_2p": os.path.join(PLUGIN_DIR, "fonts", "PressStart2P-Regular.ttf"),
    "tom_thumb": os.path.join(PLUGIN_DIR, "fonts", "tom-thumb.bdf"),
    "system": None,
}

# Font choices backed by a real bitmap format (BDF) rather than a
# scalable TrueType outline. These render every pixel exactly as
# designed with zero anti-aliasing/rasterization softness -- no
# FreeType involved at all -- and are NOT resizable (BDF is a single
# fixed pixel size), so they skip the shrink-to-fit sizing logic used
# for the TTF options.
BDF_FONT_CHOICES = {"tom_thumb"}

# Preference order for auto-discovering a bundled font from the main
# LEDMatrix install, used only when font_choice is "system" or the
# selected bundled file is missing for some reason.
FONT_NAME_PREFERENCE = ["press", "pixel", "matrix", "arcade", "8x8", "4x6", "retro"]


class BDFFont:
    """Minimal BDF (Glyph Bitmap Distribution Format) parser and
    renderer. Pillow's ImageFont.truetype() can't load .bdf files at
    all, and BDF glyphs are exact per-pixel bitmaps rather than vector
    outlines -- so drawing them is just copying 1-bit pixel data
    directly, with no rasterization/anti-aliasing step to introduce any
    softness or halo. This is intentionally tiny: it only implements
    enough of BDF to render basic Latin text (letters, digits, and the
    handful of punctuation marks this plugin actually uses)."""

    def __init__(self, path: str):
        self.glyphs: Dict[int, Dict[str, Any]] = {}
        self.ascent = 0
        self.descent = 0
        self._parse(path)

    def _parse(self, path: str):
        with open(path, "r", errors="replace") as f:
            lines = f.read().splitlines()
        i, n = 0, len(lines)
        cur: Optional[Dict[str, Any]] = None
        while i < n:
            line = lines[i].strip()
            if line.startswith("FONT_ASCENT"):
                self.ascent = int(line.split()[1])
            elif line.startswith("FONT_DESCENT"):
                self.descent = int(line.split()[1])
            elif line.startswith("STARTCHAR"):
                cur = {}
            elif line.startswith("ENCODING") and cur is not None:
                cur["encoding"] = int(line.split()[1])
            elif line.startswith("DWIDTH") and cur is not None:
                cur["dwidth"] = int(line.split()[1])
            elif line.startswith("BBX") and cur is not None:
                p = line.split()
                cur["bbw"], cur["bbh"] = int(p[1]), int(p[2])
                cur["bbxoff"], cur["bbyoff"] = int(p[3]), int(p[4])
            elif line.startswith("BITMAP") and cur is not None:
                rows = []
                for _ in range(cur.get("bbh", 0)):
                    i += 1
                    hexrow = lines[i].strip()
                    nbits = len(hexrow) * 4
                    val = int(hexrow, 16) if hexrow else 0
                    bits = [(val >> (nbits - 1 - b)) & 1 for b in range(cur["bbw"])]
                    rows.append(bits)
                cur["rows"] = rows
            elif line.startswith("ENDCHAR") and cur is not None:
                if "encoding" in cur:
                    self.glyphs[cur["encoding"]] = cur
                cur = None
            i += 1

    def _glyph(self, ch: str) -> Optional[Dict[str, Any]]:
        return self.glyphs.get(ord(ch))

    def textbbox(self, text: str) -> Tuple[int, int, int, int]:
        """Mimics ImageDraw.textbbox((0,0), text, font=...) closely
        enough for this plugin's centering/width-fit math: returns
        (left, top, right, bottom) with (0,0) as the text origin."""
        cursor_x = 0
        min_top: Optional[int] = None
        max_bottom: Optional[int] = None
        for ch in text:
            g = self._glyph(ch)
            if g is None:
                cursor_x += 4
                continue
            glyph_top = self.ascent - (g["bbyoff"] + g["bbh"])
            glyph_bottom = glyph_top + g["bbh"]
            min_top = glyph_top if min_top is None else min(min_top, glyph_top)
            max_bottom = glyph_bottom if max_bottom is None else max(max_bottom, glyph_bottom)
            cursor_x += g.get("dwidth", 4)
        if min_top is None:
            min_top, max_bottom = 0, 0
        return (0, min_top, cursor_x, max_bottom)

    def draw(self, image: Image.Image, xy: Tuple[int, int], text: str, fill: Tuple[int, int, int]):
        x0, y0 = xy
        cursor_x = x0
        img_w, img_h = image.size
        for ch in text:
            g = self._glyph(ch)
            if g is None:
                cursor_x += 4
                continue
            glyph_top = self.ascent - (g["bbyoff"] + g["bbh"])
            for row_idx, row in enumerate(g.get("rows", [])):
                py = y0 + glyph_top + row_idx
                if py < 0 or py >= img_h:
                    continue
                for col_idx, bit in enumerate(row):
                    if not bit:
                        continue
                    px = cursor_x + g["bbxoff"] + col_idx
                    if 0 <= px < img_w:
                        image.putpixel((px, py), fill)
            cursor_x += g.get("dwidth", 4)




class TidbytBaseballPlugin(BasePlugin):
    def __init__(self, plugin_id, config, display_manager, cache_manager, plugin_manager):
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        self.logger = logging.getLogger(f"plugin.{plugin_id}")
        self._derive_settings()

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "LEDMatrix-TidbytBaseball/1.0"})

        self.live_games: List[Dict[str, Any]] = []
        self.fallback_game: Optional[Dict[str, Any]] = None
        self.current_index: int = 0
        self.last_switch_time: float = time.time()
        self.last_fetch_time: float = 0.0

        self._logo_cache: Dict[str, Optional[Image.Image]] = {}
        self._font_cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
        self._fit_font_cache: Dict[Tuple[str, int, bool], ImageFont.FreeTypeFont] = {}

        self._repo_font_path = self._discover_repo_font()

        # Verify the bundled font up front, not just when a render call
        # happens to fail into the fallback -- this makes it immediately
        # visible in the logs whether the plugin's own fonts/ folder
        # actually made it into the install correctly.
        fonts_dir = os.path.join(PLUGIN_DIR, "fonts")
        try:
            actual_files = os.listdir(fonts_dir) if os.path.isdir(fonts_dir) else None
        except Exception as e:
            actual_files = f"<could not list: {e}>"
        self.logger.info(f"Plugin fonts/ directory ({fonts_dir}) actually contains: {actual_files}")

        expected_bundled_path = FONT_CHOICES.get(self.font_choice)
        if expected_bundled_path and os.path.isfile(expected_bundled_path):
            self.logger.info(f"font_choice '{self.font_choice}' -> bundled file found OK at {expected_bundled_path}")
        elif self.font_choice != "system":
            self.logger.error(
                f"font_choice '{self.font_choice}' -> bundled file NOT FOUND at "
                f"{expected_bundled_path}. This means the plugin's fonts/ folder "
                f"didn't make it into your install correctly (see the directory "
                f"listing logged just above -- if it's missing entirely or empty, "
                f"that confirms it). Text will fall back to auto-discovery, other "
                f"bundled fonts, system fonts, or worst case PIL's crude default "
                f"bitmap font."
            )

        if self._repo_font_path:
            self.logger.info(f"Also found a font in the main LEDMatrix install: {self._repo_font_path}")

        self.font_small = self._load_font(9)
        self.font_tiny = self._load_font(7)
        self.font_count = self._load_font(6)

        # Log the ACTUAL resolved font type/size for the font_choice
        # selected -- this is the most direct way to confirm from logs
        # alone whether BDF loaded correctly, fell back to a TTF, or
        # fell all the way back to PIL's default bitmap font.
        resolved = self._load_font(9, bold=True)
        if isinstance(resolved, BDFFont):
            self.logger.info(f"font_choice '{self.font_choice}' resolved to: BDFFont (correct)")
        elif isinstance(resolved, ImageFont.FreeTypeFont):
            self.logger.info(
                f"font_choice '{self.font_choice}' resolved to a TrueType font "
                f"(path={getattr(resolved, 'path', '?')}, size={getattr(resolved, 'size', '?')}). "
                f"{'This is expected if you picked a TTF font_choice.' if self.font_choice not in BDF_FONT_CHOICES else 'WARNING: you picked tom_thumb but got a TTF font back -- BDF parsing failed, see errors above.'}"
            )
        else:
            self.logger.error(
                f"font_choice '{self.font_choice}' resolved to {type(resolved)} -- "
                f"this is almost certainly PIL's crude default bitmap font, meaning "
                f"EVERY candidate failed to load. Text will look wrong and ignore "
                f"requested sizes. Check all the errors logged above for why."
            )

    # ------------------------------------------------------------------
    # Config handling
    # ------------------------------------------------------------------
    def _derive_settings(self):
        cfg = self.config or {}
        self.favorite_teams = [t.upper() for t in cfg.get("favorite_teams", ["PHI"])]
        self.update_interval = cfg.get("update_interval_seconds", 300)
        self.live_update_interval = cfg.get("live_update_interval_seconds", 15)
        self.game_rotation_seconds = cfg.get("game_rotation_seconds", 8)
        self.show_favorite_teams_only = cfg.get("show_favorite_teams_only", False)
        self.display_duration = cfg.get("display_duration", 20)
        self.away_color_fallback = tuple(cfg.get("away_color", DEFAULT_AWAY_COLOR))
        self.home_color_fallback = tuple(cfg.get("home_color", DEFAULT_HOME_COLOR))
        self.use_team_colors = cfg.get("use_team_colors", True)
        self.show_logos = cfg.get("show_logos", True)
        self.logo_dir = cfg.get("logo_dir", "assets/sports/mlb_logos")
        self.base_fill_color = tuple(cfg.get("base_fill_color", [255, 255, 255]))
        self.base_empty_color = tuple(cfg.get("base_empty_color", [95, 95, 95]))
        self.out_fill_color = tuple(cfg.get("out_fill_color", [255, 140, 0]))
        self.out_empty_color = tuple(cfg.get("out_empty_color", [120, 120, 120]))
        self.font_choice = cfg.get("font_choice", "tom_thumb")
        self.show_batter_name = cfg.get("show_batter_name", True)
        self.test_mode = cfg.get("test_mode", False)

    def on_config_change(self, new_config):
        self.config = new_config
        old_font_choice = getattr(self, "font_choice", None)
        self._derive_settings()
        self.last_fetch_time = 0
        if self.font_choice != old_font_choice:
            # Selected font changed -- clear caches so _load_font picks
            # up the new one instead of returning a stale cached object.
            self._font_cache.clear()
            self._fit_font_cache.clear()
            self.font_small = self._load_font(9)
            self.font_tiny = self._load_font(7)
            self.font_count = self._load_font(6)

    def validate_config(self) -> bool:
        if not self.favorite_teams:
            self.logger.error("No favorite_teams configured")
            return False
        return True

    # ------------------------------------------------------------------
    # Fonts
    # ------------------------------------------------------------------
    def _discover_repo_font(self) -> Optional[str]:
        """Scans assets/fonts/ (relative to the LEDMatrix install root)
        for a real bundled font instead of guessing a filename. Prefers
        anything that looks like a pixel/arcade font so team text
        matches the aesthetic the rest of the project's plugins use."""
        fonts_dir = "assets/fonts"
        if not os.path.isdir(fonts_dir):
            return None
        try:
            files = [f for f in os.listdir(fonts_dir) if f.lower().endswith((".ttf", ".otf"))]
        except Exception as e:
            self.logger.warning(f"Could not list {fonts_dir}: {e}")
            return None
        if not files:
            return None

        for keyword in FONT_NAME_PREFERENCE:
            for f in files:
                if keyword in f.lower():
                    return os.path.join(fonts_dir, f)
        return os.path.join(fonts_dir, sorted(files)[0])

    def _load_font(self, size: int, bold: bool = False) -> Any:
        cache_key = (self.font_choice, size)
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]

        if self.font_choice in BDF_FONT_CHOICES:
            # BDF is a fixed-pixel bitmap format -- there's no "size" to
            # request, so every size maps to the same single instance.
            # Cache it once under a size-independent key too.
            bdf_key = (self.font_choice, "bdf")
            if bdf_key in self._font_cache:
                font = self._font_cache[bdf_key]
            else:
                bdf_path = FONT_CHOICES[self.font_choice]
                try:
                    font = BDFFont(bdf_path)
                except Exception as e:
                    self.logger.error(f"Failed to parse BDF font at {bdf_path}: {e}", exc_info=True)
                    font = None
                self._font_cache[bdf_key] = font
            if font is not None:
                self._font_cache[cache_key] = font
                return font
            # fall through to TTF/system candidates below if BDF parsing failed

        candidates = []

        bundled_path = FONT_CHOICES.get(self.font_choice)
        if bundled_path and os.path.isfile(bundled_path) and self.font_choice not in BDF_FONT_CHOICES:
            candidates.append(bundled_path)
        elif self.font_choice != "system" and self.font_choice not in BDF_FONT_CHOICES:
            self.logger.warning(
                f"font_choice '{self.font_choice}' bundled file not found at "
                f"expected path ({bundled_path}); falling back to auto-discovery / system font."
            )

        if self._repo_font_path:
            candidates.append(self._repo_font_path)

        # Try every OTHER bundled TTF this plugin ships with before
        # falling back to system fonts -- these are guaranteed to be
        # sitting right next to manager.py (assuming the plugin's own
        # fonts/ folder made it into the install at all), so they're
        # more likely to actually be there than OS-level font packages,
        # which a minimal Raspberry Pi OS Lite install may not include.
        for choice, path in FONT_CHOICES.items():
            if choice in BDF_FONT_CHOICES or choice == self.font_choice or path is None:
                continue
            if os.path.isfile(path):
                candidates.append(path)

        if bold:
            candidates += [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
            ]
        else:
            candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")

        font = None
        for path in candidates:
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
        if font is None:
            # IMPORTANT: PIL's load_default() renders a fixed, crude
            # bitmap font that IGNORES the requested `size` entirely on
            # a lot of Pillow versions. If you're seeing blocky,
            # oddly-large, or generic-looking text regardless of the
            # font_choice you picked, THIS is almost certainly why --
            # every candidate above failed to load. Check the plugin
            # logs for this exact error to confirm.
            self.logger.error(
                f"ALL font candidates failed to load for size={size}, bold={bold}: "
                f"{candidates}. Falling back to PIL's built-in default bitmap font, "
                f"which ignores the requested size -- this is very likely why text "
                f"looks wrong. Check that the plugin's fonts/ folder actually made it "
                f"into your install (should be at {os.path.join(PLUGIN_DIR, 'fonts')})."
            )
            font = ImageFont.load_default()

        self._font_cache[cache_key] = font
        return font

    def _measure(self, font: Any, text: str) -> Tuple[int, int, int, int]:
        """Unified text bounding-box measurement for either a BDFFont or
        a normal PIL font, so the rest of the code doesn't need to care
        which one is active."""
        if isinstance(font, BDFFont):
            return font.textbbox(text)
        tmp_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        return tmp_draw.textbbox((0, 0), text, font=font)

    def _render_text(self, image: Image.Image, xy: Tuple[int, int], text: str, font: Any, fill: Tuple[int, int, int]):
        """Unified text drawing for either a BDFFont (direct pixel
        writes, no anti-aliasing) or a normal PIL font (draw.text)."""
        if isinstance(font, BDFFont):
            font.draw(image, xy, text, fill)
        else:
            ImageDraw.Draw(image).text(xy, text, font=font, fill=fill)

    def _fit_font_for_width(self, draw, text: str, max_width: int, start_size: int, min_size: int = 4) -> Any:
        """Shrinks the font size until `text` fits within max_width.
        Works regardless of which font got auto-discovered, since
        different fonts have very different glyph widths (this is what
        caused double-digit scores to overflow before).

        BDF fonts are a fixed size, so this skips shrinking for them --
        but only after confirming _load_font() actually returned a
        BDFFont. If font_choice is "tom_thumb" but the BDF file failed
        to parse for some reason, _load_font() silently falls back to a
        full-size TTF font -- and skipping the shrink loop in that case
        would render that TTF at `start_size` with ZERO shrinking,
        which is exactly what caused oversized, overflowing text. Only
        the confirmed-BDF case skips the loop; any fallback still goes
        through normal shrink-to-fit."""
        candidate = self._load_font(start_size, bold=True)
        if isinstance(candidate, BDFFont):
            return candidate

        cache_key = (self.font_choice, text, max_width)
        if cache_key in self._fit_font_cache:
            return self._fit_font_cache[cache_key]

        size = start_size
        chosen = None
        while size >= min_size:
            font = self._load_font(size, bold=True)
            bbox = self._measure(font, text)
            if bbox[2] - bbox[0] <= max_width:
                chosen = font
                break
            size -= 1
        if chosen is None:
            chosen = self._load_font(min_size, bold=True)

        self._fit_font_cache[cache_key] = chosen
        return chosen

    def _fit_font_for_pair(self, draw, text_a: str, text_b: str, max_width: int, start_size: int, min_size: int = 4) -> Any:
        """Like _fit_font_for_width, but sizes for whichever of the two
        strings is wider, so both team columns render at the SAME font
        size rather than each shrinking independently based on its own
        text length (that mismatch was the original bug). See
        _fit_font_for_width's docstring for why this checks
        isinstance(..., BDFFont) rather than trusting font_choice."""
        candidate = self._load_font(start_size, bold=True)
        if isinstance(candidate, BDFFont):
            return candidate

        cache_key = (self.font_choice, text_a, text_b, max_width)
        if cache_key in self._fit_font_cache:
            return self._fit_font_cache[cache_key]

        size = start_size
        chosen = None
        while size >= min_size:
            font = self._load_font(size, bold=True)
            bbox_a = self._measure(font, text_a)
            bbox_b = self._measure(font, text_b)
            widest = max(bbox_a[2] - bbox_a[0], bbox_b[2] - bbox_b[0])
            if widest <= max_width:
                chosen = font
                break
            size -= 1
        if chosen is None:
            chosen = self._load_font(min_size, bold=True)

        self._fit_font_cache[cache_key] = chosen
        return chosen

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def update(self):
        now = time.time()
        has_data = bool(self.live_games) or self.fallback_game is not None
        interval = self.live_update_interval if self.live_games else self.update_interval

        if has_data and (now - self.last_fetch_time < interval):
            return

        self.last_fetch_time = now

        if self.test_mode:
            game = self._fake_game()
            self._resolve_logos(game)
            self.live_games = [game]
            self.fallback_game = None
            if self.current_index >= len(self.live_games):
                self.current_index = 0
            return

        try:
            resp = self.session.get(ESPN_SCOREBOARD_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self.logger.error(f"Failed to fetch MLB scoreboard: {e}", exc_info=True)
            return

        live_games, fallback_game = self._process_scoreboard(data)

        for g in live_games:
            self._resolve_logos(g)
        if fallback_game:
            self._resolve_logos(fallback_game)

        self.live_games = live_games
        self.fallback_game = fallback_game
        if self.current_index >= len(self.live_games):
            self.current_index = 0

    def _process_scoreboard(self, data: Dict[str, Any]):
        events = data.get("events", [])
        live_games = []

        for event in events:
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp = competitions[0]
            state = comp.get("status", {}).get("type", {}).get("state")
            if state != "in":
                continue
            game = self._parse_game(event, comp)
            if self.show_favorite_teams_only:
                if game["away_abbr"] in self.favorite_teams or game["home_abbr"] in self.favorite_teams:
                    live_games.append(game)
            else:
                live_games.append(game)

        fallback_game = None
        if not live_games:
            fallback_game = self._find_favorite_game(data)

        return live_games, fallback_game

    def _find_favorite_game(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        events = data.get("events", [])
        candidates = []
        for event in events:
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp = competitions[0]
            competitors = comp.get("competitors", [])
            abbrevs = [c.get("team", {}).get("abbreviation", "").upper() for c in competitors]
            if any(fav in abbrevs for fav in self.favorite_teams):
                candidates.append((event, comp))
        if not candidates:
            return None
        event, comp = candidates[0]
        return self._parse_game(event, comp)

    def _parse_game(self, event: Dict[str, Any], comp: Dict[str, Any]) -> Dict[str, Any]:
        competitors = comp.get("competitors", [])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[0])
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[-1])

        situation = comp.get("situation", {}) or {}
        status = comp.get("status", {}) or {}
        status_type = status.get("type", {}) or {}

        def team_color(competitor):
            color = competitor.get("team", {}).get("color")
            if self.use_team_colors and color:
                try:
                    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
                except Exception:
                    pass
            return None

        def team_logo_url(competitor):
            team = competitor.get("team", {})
            if team.get("logo"):
                return team["logo"]
            logos = team.get("logos") or []
            if logos:
                return logos[0].get("href")
            return None

        def extract_batter_name(situation_dict):
            """ESPN's scoreboard payload isn't officially documented, so
            this tries several plausible shapes for the current batter's
            name rather than assuming one exact path. Returns None (not
            a crash) if nothing matches -- the display just omits the
            batter line in that case. If the actual shape turns out to
            be something else entirely, check plugin logs for the raw
            situation keys logged the first time this comes up empty
            during a live game, and I can add the right path."""
            candidates = [
                lambda s: s.get("batter", {}).get("athlete", {}).get("displayName"),
                lambda s: s.get("batter", {}).get("athlete", {}).get("fullName"),
                lambda s: s.get("batter", {}).get("displayName"),
                lambda s: s.get("atBat", {}).get("athlete", {}).get("displayName"),
                lambda s: s.get("atBat", {}).get("displayName"),
            ]
            for getter in candidates:
                try:
                    name = getter(situation_dict)
                    if name:
                        return name
                except Exception:
                    continue
            return None

        return {
            "state": status_type.get("state", "pre"),
            "away_abbr": away.get("team", {}).get("abbreviation", "AWY")[:3].upper(),
            "home_abbr": home.get("team", {}).get("abbreviation", "HOM")[:3].upper(),
            "away_score": int(away.get("score", 0) or 0),
            "home_score": int(home.get("score", 0) or 0),
            "away_color": team_color(away) or self.away_color_fallback,
            "home_color": team_color(home) or self.home_color_fallback,
            "away_logo_url": team_logo_url(away),
            "home_logo_url": team_logo_url(home),
            "away_logo": None,
            "home_logo": None,
            "inning": status.get("period", 1),
            "inning_half": situation.get("isTopInning", True),
            "balls": situation.get("balls", 0),
            "strikes": situation.get("strikes", 0),
            "outs": situation.get("outs", 0),
            "batter_name": extract_batter_name(situation),
            "on_first": bool(situation.get("onFirst")),
            "on_second": bool(situation.get("onSecond")),
            "on_third": bool(situation.get("onThird")),
        }

    def _fake_game(self) -> Dict[str, Any]:
        return {
            "state": "in",
            "away_abbr": "ATH",
            "home_abbr": "DET",
            "away_score": 3,
            "home_score": 2,
            "away_color": self.away_color_fallback,
            "home_color": self.home_color_fallback,
            "away_logo_url": None,
            "home_logo_url": None,
            "away_logo": None,
            "home_logo": None,
            "inning": 3,
            "inning_half": True,
            "balls": 2,
            "strikes": 1,
            "outs": 1,
            "batter_name": "Riley Greene",
            "on_first": True,
            "on_second": False,
            "on_third": True,
        }

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------
    def _maybe_rotate(self):
        if len(self.live_games) <= 1:
            return
        now = time.time()
        if now - self.last_switch_time >= self.game_rotation_seconds:
            self.current_index = (self.current_index + 1) % len(self.live_games)
            self.last_switch_time = now

    def _current_game(self) -> Optional[Dict[str, Any]]:
        if self.live_games:
            return self.live_games[self.current_index]
        return self.fallback_game

    # ------------------------------------------------------------------
    # Logos
    # ------------------------------------------------------------------
    def _resolve_logos(self, game: Dict[str, Any], size: Optional[int] = None):
        if not self.show_logos:
            return
        if size is None:
            width, height = self._get_dimensions()
            col_w = (width // 2) // 2
            # Slightly larger than the column itself -- allowed to bleed
            # a small amount off the panel edges (and, since two columns
            # sit side by side, potentially a couple px into the
            # neighboring team's column too, though most team logos
            # taper to transparent near their outer edge so this is
            # rarely very visible in practice).
            size = max(min(col_w, height) + 4, 12)
        game["away_logo"] = self._get_team_logo(game["away_abbr"], game.get("away_logo_url"), size)
        game["home_logo"] = self._get_team_logo(game["home_abbr"], game.get("home_logo_url"), size)

    def _get_team_logo(self, abbr: str, url: Optional[str], size: int) -> Optional[Image.Image]:
        cache_key = f"{abbr}_{size}"
        if cache_key in self._logo_cache:
            return self._logo_cache[cache_key]

        logo = self._load_local_logo(abbr, size)

        if logo is None and url:
            try:
                resp = self.session.get(url, timeout=8)
                resp.raise_for_status()
                raw = Image.open(BytesIO(resp.content)).convert("RGBA")
                raw.thumbnail((size, size), Image.LANCZOS)
                logo = raw
            except Exception as e:
                self.logger.warning(f"Could not download logo for {abbr}: {e}")

        if logo is None:
            self.logger.info(f"No logo found for {abbr}; showing abbreviation only.")

        self._logo_cache[cache_key] = logo
        return logo

    def _load_local_logo(self, abbr: str, size: int) -> Optional[Image.Image]:
        candidates = [f"{abbr}.png", f"{abbr.lower()}.png", f"{abbr}.PNG"]
        for name in candidates:
            path = os.path.join(self.logo_dir, name)
            if os.path.isfile(path):
                try:
                    raw = Image.open(path).convert("RGBA")
                    raw.thumbnail((size, size), Image.LANCZOS)
                    return raw
                except Exception as e:
                    self.logger.warning(f"Found {path} but couldn't load it: {e}")
        return None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def display(self, force_clear: bool = False):
        self._maybe_rotate()

        width, height = self._get_dimensions()
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)

        game = self._current_game()
        if game is None:
            self._render_text(image, (4, height // 2 - 4), "No Game", self.font_small, (180, 180, 180))
            self._push_image(image, force_clear)
            return

        left_w = width // 2
        col_w = left_w // 2

        draw.rectangle([0, 0, col_w - 1, height - 1], fill=game["away_color"])
        draw.rectangle([col_w, 0, left_w - 1, height - 1], fill=game["home_color"])

        away_txt_color = self._text_color_for(game["away_color"])
        home_txt_color = self._text_color_for(game["home_color"])

        # Both columns must render at the SAME font size, or a team with
        # a longer score (e.g. "DET 12" vs "ATH 2") would shrink more to
        # fit and visibly look smaller than the other -- that's the bug
        # that made one team's text look bigger than the other's.
        away_text = f"{game['away_abbr']} {game['away_score']}"
        home_text = f"{game['home_abbr']} {game['home_score']}"
        available_text_width = col_w - 4
        shared_font = self._fit_font_for_pair(draw, away_text, home_text, available_text_width, start_size=10)

        self._draw_team_column(image, draw, 0, 0, col_w, height,
                                game["away_abbr"], game["away_score"], game.get("away_logo"),
                                away_txt_color, game["away_color"], shared_font)
        self._draw_team_column(image, draw, col_w, 0, left_w - col_w, height,
                                game["home_abbr"], game["home_score"], game.get("home_logo"),
                                home_txt_color, game["home_color"], shared_font)

        right_x0 = left_w + 2
        right_w = width - right_x0 - 1

        top_margin = 2  # keeps inning triangle/number and outs off the physical top edge
        self._draw_inning(image, right_x0 + 1, top_margin, game)
        self._draw_outs(draw, right_x0, top_margin, right_w, game)

        inning_tri_size = 6
        top_row_bottom = top_margin + inning_tri_size
        lower_y = height - 6  # bottom-row text measures ~5px tall, so this is measured, not padded
        diamond_y = top_row_bottom
        diamond_available_h = (lower_y - 2) - diamond_y  # leave a clear gap before the bottom row
        diamond_w = int(right_w * 0.5)
        diamond_x = right_x0 + (right_w - diamond_w) // 2
        self._draw_diamond(draw, diamond_x, diamond_y, diamond_w, diamond_available_h, game)

        count_text = f"{game['balls']}-{game['strikes']}"
        self._draw_count(image, right_x0 + 1, lower_y, game)

        if self.show_batter_name:
            count_bbox = self._measure(self.font_count, count_text)
            count_w = count_bbox[2] - count_bbox[0]
            batter_x = right_x0 + 1 + count_w + 4
            batter_max_w = (right_x0 + right_w) - batter_x
            self._draw_batter(image, draw, batter_x, lower_y, batter_max_w, game.get("batter_name"))

        self._push_image(image, force_clear)

    def _get_dimensions(self) -> Tuple[int, int]:
        dm = self.display_manager
        for attr_pair in (("width", "height"), ("matrix_width", "matrix_height")):
            w = getattr(dm, attr_pair[0], None)
            h = getattr(dm, attr_pair[1], None)
            if w and h:
                return int(w), int(h)
        matrix = getattr(dm, "matrix", None)
        if matrix is not None:
            w = getattr(matrix, "width", None)
            h = getattr(matrix, "height", None)
            if w and h:
                return int(w), int(h)
        return 128, 32

    def _push_image(self, image: Image.Image, force_clear: bool):
        dm = self.display_manager
        if hasattr(dm, "image") and hasattr(dm, "update_display"):
            dm.image.paste(image, (0, 0))
            dm.update_display()
            return
        if hasattr(dm, "set_image"):
            dm.set_image(image)
            return
        raise AttributeError(
            "display_manager has neither `.image`/`update_display()` nor "
            "`.set_image()`. Check your LEDMatrix DisplayManager API and "
            "adjust TidbytBaseballPlugin._push_image() to match."
        )

    @staticmethod
    def _text_color_for(bg: Tuple[int, int, int]) -> Tuple[int, int, int]:
        luminance = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        return (0, 0, 0) if luminance > 150 else (255, 255, 255)

    @staticmethod
    def _format_batter_name(full_name: Optional[str]) -> Optional[str]:
        """"Riley Greene" -> "R. Greene". Anything with a suffix like
        "Jazz Chisholm Jr." becomes "J. Chisholm Jr." -- reasonable
        enough for a tiny scoreboard row."""
        if not full_name:
            return None
        parts = full_name.strip().split()
        if len(parts) < 2:
            return full_name
        return f"{parts[0][0]}. {' '.join(parts[1:])}"

    # NOTE: this plugin previously had a faux-bold text helper and an
    # anti-aliased polygon helper (supersample + LANCZOS downsample).
    # Both turned out to hurt more than help at this pixel scale: the
    # faux-bold's extra offset copy read as a halo/ghost rather than
    # actual boldness, and anti-aliasing softened small shapes (the
    # inning triangle, the base diamonds) into blobs instead of crisp
    # shapes. Everything is now drawn plainly with PIL's normal
    # hard-edged polygon/text calls, matching the pixel-art aesthetic
    # the bundled fonts are designed for.



    def _draw_team_column(self, image, draw, x0, y0, w, h, abbr, score, logo, text_color, bg_color, font):
        """Logo fills nearly the whole column (as large as the panel
        allows); a darkened bar across the bottom holds the bold
        'ABBR SCORE' text so it stays legible over the logo. `font` is
        computed once by the caller from BOTH columns' text, so the two
        teams always render at the same size."""
        text_line = f"{abbr} {score}"
        line_bbox = self._measure(font, text_line)
        line_h = line_bbox[3] - line_bbox[1]
        line_w = line_bbox[2] - line_bbox[0]
        bar_h = line_h + 4

        if logo is not None:
            # No max(...,0) clamp: when the logo is bigger than the
            # column (allowed to bleed off the edges per request), this
            # keeps it centered with a symmetric negative offset instead
            # of snapping to the left/top edge.
            logo_x = x0 + (w - logo.width) // 2
            logo_y = y0 + (h - logo.height) // 2
            image.paste(logo, (logo_x, logo_y), logo)

        bar_y0 = y0 + h - bar_h
        bar_color = tuple(max(c // 2, 15) for c in bg_color)
        draw.rectangle([x0, bar_y0, x0 + w - 1, y0 + h - 1], fill=bar_color)

        tx = x0 + max((w - line_w) // 2, 0)
        tx = min(tx, x0 + w - line_w) if line_w < w else x0
        ty = bar_y0 + max((bar_h - line_h) // 2, 0) - line_bbox[1]
        self._render_text(image, (tx, ty), text_line, font, text_color)

    def _draw_diamond(self, draw, x, y, w, h, game):
        """`h` is the actual vertical space available for the whole
        diamond shape (from the caller's layout, not a scale factor) --
        `half` is derived so the diamond's total vertical span (3*half+2)
        and horizontal span (2*half+6) both fit inside h/w. This is
        what guarantees the diamond can never overlap the row above or
        below it even as the rest of the layout changes.

        Drawn with PIL's plain polygon (no anti-aliasing) so the
        unoccupied-base outline is an exact 1px line -- running it
        through the supersample/downsample anti-aliasing helper was
        blurring that 1px line into something that reads as thicker."""
        cx = x + w // 2
        max_half_by_height = max((h - 2) // 3, 3)
        max_half_by_width = max((w - 6) // 2, 3)
        half = max(min(max_half_by_height, max_half_by_width), 3)

        top_y = y + half + 1
        bottom_y = top_y + half + 2

        positions = {
            "second": (cx, top_y),
            "third": (cx - half - 3, bottom_y),
            "first": (cx + half + 3, bottom_y),
        }
        occupied = {
            "first": game["on_first"],
            "second": game["on_second"],
            "third": game["on_third"],
        }

        for base, (px, py) in positions.items():
            pts = [
                (px, py - half),
                (px + half, py),
                (px, py + half),
                (px - half, py),
            ]
            if occupied[base]:
                draw.polygon(pts, fill=self.base_fill_color)
            else:
                draw.polygon(pts, outline=self.base_empty_color, width=1)

    def _draw_inning(self, image, x, y, game):
        """Solid triangle -- point up for top of inning, point down for
        bottom -- plus the inning number, vertically centered on the
        triangle using its actual measured glyph height.

        Drawn with a HARD edge (no anti-aliasing): at only 6px tall,
        supersampling + downsampling was producing a soft/blobby shape
        that read as "not really a triangle" rather than a clean one."""
        tri_size = 6
        draw = ImageDraw.Draw(image)
        if game["inning_half"]:
            pts = [(x, y + tri_size), (x + tri_size / 2, y), (x + tri_size, y + tri_size)]
        else:
            pts = [(x, y), (x + tri_size, y), (x + tri_size / 2, y + tri_size)]
        draw.polygon(pts, fill=(255, 255, 255))

        number_text = str(game["inning"])
        bbox = self._measure(self.font_tiny, number_text)
        glyph_h = bbox[3] - bbox[1]
        # The triangle's polygon fill spans y to y+tri_size inclusive on
        # both ends (verified by rendering both orientations and
        # measuring actual white-pixel extent), so its true vertical
        # center is at y + tri_size//2. NOTE: don't use round() here --
        # Python's round() does banker's rounding (round(2.5) == 2, not
        # 3), which silently cancelled out this exact fix the first
        # time around. Plain integer division does the right thing.
        text_y = y + tri_size // 2 - glyph_h // 2 - bbox[1]
        self._render_text(image, (x + tri_size + 3, text_y), number_text, self.font_tiny, (255, 255, 255))

    def _draw_count(self, image, x, y, game):
        count_text = f"{game['balls']}-{game['strikes']}"
        self._render_text(image, (x, y), count_text, self.font_count, (255, 200, 0))

    def _draw_batter(self, image, draw, x, y, max_width, batter_name):
        """Draws 'F. Lastname' for whoever is currently at bat, shrunk
        to fit whatever width remains next to the count. Skips silently
        (no placeholder text) if ESPN didn't provide a batter name --
        see the comment in _parse_game.extract_batter_name for how to
        fix that if it turns out ESPN's field is named something else."""
        if not batter_name or max_width <= 0:
            return
        formatted = self._format_batter_name(batter_name)
        font = self._fit_font_for_width(draw, formatted, max_width, start_size=7, min_size=4)
        bbox = self._measure(font, formatted)
        if bbox[2] - bbox[0] > max_width:
            return  # still doesn't fit even at the smallest size -- skip rather than overflow
        self._render_text(image, (x, y), formatted, font, (200, 200, 200))

    def _draw_outs(self, draw, x, y, w, game):
        square = 3
        gap = 2
        edge_margin = 3
        base_x = x + w - edge_margin - (square + gap) * 3 + gap
        for i in range(3):
            sx = base_x + i * (square + gap)
            box = [sx, y + 1, sx + square, y + 1 + square]
            if i < game["outs"]:
                draw.rectangle(box, fill=self.out_fill_color)
            else:
                draw.rectangle(box, outline=self.out_empty_color)
