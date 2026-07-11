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

import datetime
import logging
import math
import os
import random
import re
import threading
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
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary"

DEFAULT_AWAY_COLOR = (255, 255, 255)
DEFAULT_HOME_COLOR = (255, 255, 255)

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

# Routine pitch-by-pitch play types to SKIP when last_play_filter is
# "significant" -- i.e., don't flash for these, only for actual outcomes
# (hits, walks, strikeouts, outs, runs, etc.). This is a denylist rather
# than an allowlist of "big moment" types on purpose: only two real
# samples of situation.lastPlay.type.type have been confirmed so far
# ("ball" and "start-batterpitcher"), so a denylist means an unknown
# type defaults to SHOWING (safer for not missing a real highlight)
# rather than defaulting to hidden. Add more here if routine updates
# still slip through once you see this against more live data.
NON_SIGNIFICANT_PLAY_TYPES = {
    "ball", "strike", "strike-looking", "strike-swinging",
    "foul", "foul-ball", "foul-tip",
    "start-batterpitcher", "pitch", "no-pitch",
    "automatic-ball", "automatic-strike", "warmup",
}

# Best-effort guess at ESPN's home-run type code -- confirmed WRONG in
# practice (real home runs were observed not triggering the animation).
# Kept as one signal, but no longer the only one -- see
# _is_home_run_play, which also checks the play's actual narrative text
# for "home run"/"homers", a far more reliable signal since that text
# is human-readable and ESPN's phrasing for a home run call
# ("Judge homers to right field") is predictable regardless of
# whatever internal type code they use.
HOME_RUN_PLAY_TYPES = {"home-run", "homerun", "home_run", "hr", "home run"}
HOME_RUN_TEXT_KEYWORDS = ("home run", "homers", "homered")

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
        self.past_games: List[Dict[str, Any]] = []
        self.upcoming_games: List[Dict[str, Any]] = []
        self.rotation_games: List[Dict[str, Any]] = []  # what _current_game()/_maybe_rotate() actually cycle through

        # ESPN's scoreboard endpoint defaults to TODAY's games only with
        # no date parameter -- a favorite team's actual next game is
        # almost always tomorrow or later (not today), so relying on the
        # main scoreboard fetch alone means upcoming_games is nearly
        # always empty. This cache holds results from explicitly querying
        # the next few days (see _fetch_future_upcoming_games), refreshed
        # on its own slower timer since schedules barely change.
        self._cached_future_upcoming_games: List[Dict[str, Any]] = []
        self._upcoming_last_fetch_time: float = 0.0

        # Same issue, opposite direction: ESPN's main scoreboard call
        # only covers TODAY, so a favorite team's most recent completed
        # game (if it was yesterday or earlier -- an off-day today, or
        # today's game hasn't finished) never shows up as a past game
        # without explicitly looking backward too.
        self._cached_past_lookback_games: List[Dict[str, Any]] = []

        # Tracks which final games have already had their hits/errors
        # enriched from the summary endpoint -- since a completed game's
        # stats can't change, this ensures we only ever fetch it once
        # per game rather than re-fetching every poll it stays in rotation.
        self._enriched_boxscore_event_ids: set = set()
        self._past_last_fetch_time: float = 0.0
        self.fallback_game: Optional[Dict[str, Any]] = None
        self.current_index: int = 0
        self.last_switch_time: float = time.time()
        self.last_fetch_time: float = 0.0

        # Per-game (keyed by event_id) last-play flash state. Games get
        # entirely new dicts each poll (live_games is rebuilt from
        # scratch), so this has to live on self, not on the game dict,
        # to persist across polls.
        #
        # IMPORTANT: this is a QUEUE + single "active" slot, not just a
        # per-game expiry timestamp. A plain "flash_until per event_id"
        # design (the original version) let a significant play's flash
        # window start counting down the moment it was DETECTED,
        # completely independent of whether that game was actually on
        # screen -- normal rotation runs on its own separate timer with
        # no awareness of pending flashes. With 2+ live games rotating,
        # a flash could easily expire before rotation ever got around to
        # showing that game, so the person never saw it -- exactly the
        # "works intermittently" symptom. The queue below is serviced by
        # _service_flash_queue(), called before rotation each frame, so
        # a pending flash can force-jump the display to the right game
        # (pausing normal rotation) and guarantee it's actually seen.
        self._last_shown_play_id: Dict[str, str] = {}
        self._pending_flash_event_ids: List[str] = []
        self._active_flash: Optional[Dict[str, Any]] = None

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

        # Guards concurrent access to live_games/fallback_game/current_index
        # between the background thread below and display()/update() being
        # called from whatever thread the core scheduler uses.
        self._data_lock = threading.Lock()

        # IMPORTANT: this plugin no longer relies solely on the core
        # calling update() often enough. Earlier debugging (score
        # staying frozen after the first successful load, even after
        # fixing an unrelated exception-swallowing bug) pointed at the
        # core scheduler likely only calling update() when this
        # plugin's rotation slot is active on screen -- not
        # continuously in the background. Rather than depend on that,
        # this background thread polls ESPN on its own schedule,
        # completely independent of how often update()/display() get
        # invoked externally. update() still exists and still works if
        # the core DOES call it regularly (both paths share the same
        # underlying _maybe_refresh() logic, gated by the same
        # last_fetch_time check, so there's no duplicate-fetch risk).
        self._stop_background_thread = threading.Event()
        self._background_thread = threading.Thread(
            target=self._background_update_loop, daemon=True, name=f"{plugin_id}-updater"
        )
        self._background_thread.start()

    def _background_update_loop(self):
        """Runs for the lifetime of the process, independent of
        whatever the core scheduler's calling pattern for update() is.
        Sleeps briefly between checks so it responds quickly once a
        game goes live, but the actual fetch still only happens as
        often as live_update_interval_seconds/update_interval_seconds
        allow (via _maybe_refresh's own interval check) -- this thread
        just guarantees SOMETHING is checking regularly."""
        while not self._stop_background_thread.is_set():
            try:
                self._maybe_refresh()
            except Exception as e:
                self.logger.error(f"Background updater thread hit an unexpected error: {e}", exc_info=True)
            self._stop_background_thread.wait(timeout=5)

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
        self.font_choice = "tom_thumb"  # no longer user-configurable -- see FONT_CHOICES for fallback chain if this fails to load
        self.show_batter_name = cfg.get("show_batter_name", True)
        self.show_last_play = cfg.get("show_last_play", True)
        self.last_play_display_seconds = cfg.get("last_play_display_seconds", 5)
        self.home_run_display_seconds = cfg.get("home_run_display_seconds", 10)
        self.last_play_filter = cfg.get("last_play_filter", "significant")
        self.last_play_favorites_only = cfg.get("last_play_favorites_only", False)
        self.show_past_games = cfg.get("show_past_games", False)
        self.show_upcoming_games = cfg.get("show_upcoming_games", False)
        self.max_past_games = cfg.get("max_past_games", 3)
        self.max_upcoming_games = cfg.get("max_upcoming_games", 3)
        self.past_upcoming_all_teams = cfg.get("past_upcoming_all_teams", False)
        self.upcoming_games_lookahead_days = cfg.get("upcoming_games_lookahead_days", 5)
        self.upcoming_games_refresh_seconds = cfg.get("upcoming_games_refresh_seconds", 1800)
        self.past_games_lookback_days = cfg.get("past_games_lookback_days", 3)
        self.past_games_refresh_seconds = cfg.get("past_games_refresh_seconds", 1800)
        self.test_mode = cfg.get("test_mode", False)

    def on_config_change(self, new_config):
        self.config = new_config
        self._derive_settings()
        with self._data_lock:
            self.last_fetch_time = 0

    def cleanup(self):
        """Called by the core on plugin unload/disable, if it supports
        that -- stops the background updater thread cleanly. Harmless
        no-op risk if the core never calls this (the thread is a daemon
        thread anyway, so it won't block process exit either way)."""
        self._stop_background_thread.set()

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

    def _ink_extent(self, font: Any, text: str) -> Tuple[int, int]:
        """Renders `text` to a small scratch image and returns the
        actual leftmost/rightmost columns containing ink (non-background
        pixels) -- as opposed to the font's nominal advance width, which
        for punctuation like ":" or "." often includes several columns
        of blank design space the font author left for normal spacing.
        Measuring real ink is what lets tightening work correctly
        regardless of which font is active, rather than guessing a
        fixed pixel offset tuned for one specific font."""
        bbox = self._measure(font, text)
        w = max(bbox[2] - bbox[0], 1) + 6
        h = max(bbox[3] - bbox[1], 1) + 6
        scratch = Image.new("RGB", (w, h), (0, 0, 0))
        self._render_text(scratch, (3, 3), text, font, (255, 255, 255))
        cols = [x for x in range(w) for y in range(h) if scratch.getpixel((x, y)) != (0, 0, 0)]
        if not cols:
            return (3, 3)
        return (min(cols), max(cols))

    def _draw_tight_join(self, image, x, y, font, fill, text_a: str, text_b: str, ink_gap: int = 1) -> int:
        """Draws text_a then text_b immediately after it, with only
        `ink_gap` background pixels between their actual rendered ink
        -- not their nominal advance widths. This is what actually
        tightens up spacing like "P:" or "T. Lastname": the blank space
        people see isn't extra spacing added between characters, it's
        blank design space baked into narrow glyphs (colons, periods)
        that a font author left for normal-width spacing. Returns the
        total pixel width used, for cursor advancement."""
        self._render_text(image, (x, y), text_a, font, fill)
        _, a_right_scratch = self._ink_extent(font, text_a)
        a_right_actual = x + (a_right_scratch - 3)

        b_left_scratch, b_right_scratch = self._ink_extent(font, text_b)
        # Target: B's first ink column should land at (A's last ink
        # column + 1 + ink_gap) -- the "+1" is because a_right_actual
        # IS the last ink pixel, so the very next column is already 0
        # gap; ink_gap blank columns after that is where B's ink starts.
        b_x = (a_right_actual + 1 + ink_gap) - (b_left_scratch - 3)
        self._render_text(image, (b_x, y), text_b, font, fill)

        b_bbox = self._measure(font, text_b)
        return (b_x + (b_bbox[2] - b_bbox[0])) - x

    def _draw_name_tightened(self, image, xy, font, fill, name: str, ink_gap: int = 1) -> int:
        """Draws a 'F. Lastname'-style string with the gap after the
        initial+period tightened to `ink_gap` real pixels instead of
        whatever blank space the space character/font design normally
        leaves (measured as 6px of pure blank for tom_thumb's "T. " --
        see the investigation that led to this). Falls back to a plain
        render if the string doesn't match that pattern. Returns the
        pixel width used."""
        x, y = xy
        m = re.match(r"^([A-Za-z]{1,2}\.) (.+)$", name)
        if not m:
            self._render_text(image, (x, y), name, font, fill)
            bbox = self._measure(font, name)
            return bbox[2] - bbox[0]
        prefix, rest = m.group(1), m.group(2)
        return self._draw_tight_join(image, x, y, font, fill, prefix, rest, ink_gap=ink_gap)

    def _measure_name_tightened(self, font, name: str, ink_gap: int = 1) -> int:
        """Width the tightened name would actually take up, WITHOUT
        drawing to the real image. Reuses _draw_name_tightened itself
        against a scratch canvas rather than reimplementing the
        positioning math separately -- that duplication is exactly what
        let measurement and final rendering drift apart before (fit
        checks used the untightened width, so text that only fit
        because of the tightening savings was truncating anyway, and
        then even the truncated fallback skipped tightening entirely)."""
        scratch = Image.new("RGB", (400, 30), (0, 0, 0))
        return self._draw_name_tightened(scratch, (2, 2), font, (255, 255, 255), name, ink_gap=ink_gap)

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
        """Called by the core scheduler, if/whenever it calls it. Just
        delegates to _maybe_refresh() -- the actual polling now also
        happens independently via the background thread started in
        __init__, so data refreshes either way regardless of the core's
        calling cadence for this method."""
        self._maybe_refresh()

    def _maybe_refresh(self):
        now = time.time()
        with self._data_lock:
            has_data = bool(self.live_games) or self.fallback_game is not None
            interval = self.live_update_interval if self.live_games else self.update_interval
            seconds_since_last = now - self.last_fetch_time
            should_skip = has_data and (seconds_since_last < interval)

        if should_skip:
            self.logger.debug(
                f"Skipping fetch -- only {seconds_since_last:.1f}s since last fetch "
                f"(interval is {interval}s)."
            )
            return

        with self._data_lock:
            self.last_fetch_time = now

        if self.test_mode:
            game = self._fake_game()
            self._resolve_logos(game)
            with self._data_lock:
                self.live_games = [game]
                self.fallback_game = None
                self.rotation_games = [game]
                if self.current_index >= len(self.rotation_games):
                    self.current_index = 0
            return

        try:
            resp = self.session.get(ESPN_SCOREBOARD_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self.logger.error(f"Failed to fetch MLB scoreboard: {e}", exc_info=True)
            return

        # IMPORTANT: this whole block used to be unprotected. If any
        # single game had an unusual shape ESPN sometimes sends
        # (pitching change, extra innings, a null field mid-play, etc.)
        # that our parsing code didn't handle, the exception would
        # propagate straight out uncaught. Wrapping this means a single
        # bad game/response degrades gracefully (keeps last-known-good
        # data, tries again next interval) instead of permanently
        # freezing everything.
        try:
            live_games, past_games, upcoming_games, fallback_game = self._process_scoreboard(data)

            # ESPN's main scoreboard call only covers today -- merge in
            # the separately-cached multi-day lookahead so upcoming_games
            # isn't nearly always empty (see _fetch_future_upcoming_games
            # for why). Only re-queries the future days on its own slower
            # timer, since schedules barely change within a day.
            now_ts = time.time()
            if self.show_upcoming_games and (now_ts - self._upcoming_last_fetch_time >= self.upcoming_games_refresh_seconds):
                try:
                    self._cached_future_upcoming_games = self._fetch_future_upcoming_games()
                    self._upcoming_last_fetch_time = now_ts
                except Exception as e:
                    self.logger.warning(f"Upcoming-games lookahead failed, keeping previous cache: {e}")

            if self.show_upcoming_games:
                combined_upcoming = {g["event_id"]: g for g in upcoming_games}
                for g in self._cached_future_upcoming_games:
                    combined_upcoming.setdefault(g["event_id"], g)
                upcoming_games = sorted(combined_upcoming.values(), key=lambda g: g.get("event_date_raw") or "")
                upcoming_games = upcoming_games[: self.max_upcoming_games]

            # Mirror image of the upcoming-games fix: ESPN's main call
            # only covers today, so a favorite team's most recent
            # completed game (yesterday or earlier) never shows up
            # without explicitly looking backward too.
            if self.show_past_games and (now_ts - self._past_last_fetch_time >= self.past_games_refresh_seconds):
                try:
                    self._cached_past_lookback_games = self._fetch_past_games_lookback()
                    self._past_last_fetch_time = now_ts
                except Exception as e:
                    self.logger.warning(f"Past-games lookback failed, keeping previous cache: {e}")

            if self.show_past_games:
                combined_past = {g["event_id"]: g for g in past_games}
                for g in self._cached_past_lookback_games:
                    combined_past.setdefault(g["event_id"], g)
                # Most recent first -- reverse chronological, unlike
                # upcoming games which sort soonest-first.
                past_games = sorted(combined_past.values(), key=lambda g: g.get("event_date_raw") or "", reverse=True)
                past_games = past_games[: self.max_past_games]

                for g in past_games:
                    try:
                        self._enrich_boxscore_stats(g)
                    except Exception as e:
                        self.logger.warning(f"Could not fetch box score stats for {g['away_abbr']}@{g['home_abbr']}: {e}")

            for g in live_games + past_games + upcoming_games:
                self._resolve_logos(g)
            if fallback_game:
                self._resolve_logos(fallback_game)

            # --- Favorite-team priority cascade ---
            # 1. If ANY favorite team has a live game right now, show
            #    ONLY that (those) live game(s) -- past/upcoming and
            #    every other team's live game are fully suppressed
            #    while a favorite is live.
            # 2. Otherwise, if show_favorite_teams_only is OFF: show
            #    every live game (any team) plus past/upcoming (scope
            #    controlled separately by past_upcoming_all_teams).
            # 2s. Otherwise (show_favorite_teams_only is ON, strict
            #     mode): show only favorites' past/upcoming -- no other
            #     team's live game ever appears.
            # 3. Falls out naturally: if the live portion is empty in
            #    either branch, rotation is just past/upcoming; if ALL
            #    of that is empty too, fall back to the single
            #    best-guess favorite game (preserves pre-this-feature
            #    behavior for anyone with everything else off).
            def _is_favorite_game(g):
                return g["away_abbr"] in self.favorite_teams or g["home_abbr"] in self.favorite_teams

            favorite_live_games = [g for g in live_games if _is_favorite_game(g)]

            if favorite_live_games:
                # Live favorite game(s) still get priority (listed
                # first, so they're what shows first each time rotation
                # cycles back around), but past/upcoming toggles are
                # still honored here too -- previously this branch
                # suppressed past/upcoming entirely whenever a favorite
                # was live, even for the SAME favorite team. Scoped
                # strictly to favorite teams regardless of
                # past_upcoming_all_teams -- mixing in some OTHER team's
                # past/upcoming game here would defeat the point of
                # favorite-team prioritization.
                rotation_games = list(favorite_live_games)
                if self.show_past_games:
                    rotation_games += [g for g in past_games if _is_favorite_game(g)]
                if self.show_upcoming_games:
                    rotation_games += [g for g in upcoming_games if _is_favorite_game(g)]
                cascade_state = "favorite team(s) live -- showing that + favorites' past/upcoming"
            elif self.show_favorite_teams_only:
                rotation_games = []
                if self.show_past_games:
                    rotation_games += past_games
                if self.show_upcoming_games:
                    rotation_games += upcoming_games
                cascade_state = "strict mode, no favorite live -- favorites' past/upcoming only"
            elif live_games:
                # NEW: some OTHER team is live (not a favorite). Explicit
                # request: past/upcoming games should be restricted to
                # favorites ONLY here, regardless of past_upcoming_all_teams
                # -- that setting only matters when NOTHING is live
                # anywhere (see the final branch below). Otherwise a
                # non-favorite team's past/upcoming game shows up right
                # alongside live games happening right now, which is
                # exactly the cluttered experience being avoided.
                rotation_games = list(live_games)
                if self.show_past_games:
                    rotation_games += [g for g in past_games if _is_favorite_game(g)]
                if self.show_upcoming_games:
                    rotation_games += [g for g in upcoming_games if _is_favorite_game(g)]
                cascade_state = "other team(s) live, no favorite live -- showing those + favorites' past/upcoming only"
            else:
                # Nothing live ANYWHERE -- past_upcoming_all_teams now
                # applies as designed (past_games/upcoming_games were
                # already scoped at fetch time per that setting).
                rotation_games = []
                if self.show_past_games:
                    rotation_games += past_games
                if self.show_upcoming_games:
                    rotation_games += upcoming_games
                cascade_state = "nothing live anywhere -- past/upcoming per past_upcoming_all_teams setting"

            if not rotation_games and fallback_game:
                rotation_games = [fallback_game]
                cascade_state += " (nothing available -- using single fallback game)"

            # Real pitch count isn't in the lightweight scoreboard
            # response (confirmed from actual captured data) -- fetch
            # it from ESPN's more detailed per-game summary endpoint
            # instead. Wrapped in its own try/except per game so one
            # game's summary failing (or ESPN changing that endpoint's
            # shape) can't take down the main scoreboard update.
            #
            # IMPORTANT: this only runs on the LIVE games actually
            # selected into rotation_games, not every live game
            # leaguewide -- "show all live games" mode could otherwise
            # mean fetching a summary for a dozen simultaneous MLB
            # games every poll, which is a lot of extra requests for
            # data that never even gets displayed.
            enrich_targets = [g for g in rotation_games if g.get("game_type") == "live"]
            for g in enrich_targets:
                try:
                    self._enrich_pitch_count(g)
                except Exception as e:
                    self.logger.warning(f"Could not fetch pitch count for {g['away_abbr']}@{g['home_abbr']}: {e}")

            for g in enrich_targets:
                try:
                    self._maybe_trigger_last_play_flash(g)
                except Exception as e:
                    self.logger.warning(f"Error checking last-play flash for {g['away_abbr']}@{g['home_abbr']}: {e}")

            self.logger.info(
                f"Fetched scoreboard OK: {len(live_games)} live game(s) leaguewide, "
                f"{len(favorite_live_games)} involving a favorite team, "
                f"{len(past_games)} past, {len(upcoming_games)} upcoming. "
                f"Cascade: {cascade_state}. Rotation has {len(rotation_games)} game(s)."
            )

            with self._data_lock:
                self.live_games = live_games
                self.past_games = past_games
                self.upcoming_games = upcoming_games
                self.fallback_game = fallback_game
                self.rotation_games = rotation_games
                if self.current_index >= len(self.rotation_games):
                    self.current_index = 0
        except Exception as e:
            self.logger.error(
                f"Fetched scoreboard successfully but failed to parse/process it: {e}. "
                f"Keeping last-known-good data instead of crashing -- will try again "
                f"next update cycle. If this repeats every time, something about the "
                f"CURRENT game state (extra innings, pitching change, etc.) is hitting "
                f"a parsing bug -- please share this traceback.",
                exc_info=True,
            )

    def _maybe_trigger_last_play_flash(self, game: Dict[str, Any]):
        """Detects when a game has a NEW play (by comparing lastPlay's
        id against what we last saw for this specific game, keyed by
        event_id since game dicts are rebuilt fresh every poll) and, if
        it's a "significant" play type, QUEUES it to be flashed. The
        actual timing/expiry is handled by _service_flash_queue(),
        called from display() before rotation -- this function only
        decides WHETHER something should flash, never when or for how
        long, since that's what guarantees it's actually shown (see the
        big comment on _pending_flash_event_ids in __init__).

        Deliberately does NOT flash the very first time we ever see a
        given game (i.e., when we have no previous play id to compare
        against) -- otherwise every game would flash immediately on
        first load / plugin startup for whatever play happened to be
        current already, which isn't really a "new" play from the
        person watching the display's perspective."""
        if not self.show_last_play:
            return
        event_id = game.get("event_id")
        play_id = game.get("last_play_id")
        if not event_id or not play_id:
            return

        with self._data_lock:
            previous_id = self._last_shown_play_id.get(event_id)
            self._last_shown_play_id[event_id] = play_id

            if previous_id is None or play_id == previous_id:
                return  # first time seeing this game, or no change

            play_type = (game.get("last_play_type") or "").lower()
            is_significant = (
                self.last_play_filter != "significant"
                or play_type not in NON_SIGNIFICANT_PLAY_TYPES
            )

            # Independent of rotation scope: even with all leaguewide
            # live games rotating normally, this restricts which games
            # are allowed to INTERRUPT that rotation with a flash. With
            # several simultaneous games, the aggregate rate of
            # significant plays across all of them can make the display
            # feel like it's jumping around constantly even though
            # rotation itself is calm -- this lets someone keep seeing
            # every live game in rotation while only getting interrupted
            # for their own team's moments.
            if self.last_play_favorites_only:
                involves_favorite = (
                    game.get("away_abbr") in self.favorite_teams
                    or game.get("home_abbr") in self.favorite_teams
                )
                if not involves_favorite:
                    is_significant = False

            if is_significant:
                already_active = self._active_flash and self._active_flash.get("event_id") == event_id
                already_pending = event_id in self._pending_flash_event_ids
                if not already_active and not already_pending:
                    self._pending_flash_event_ids.append(event_id)
                    self.logger.info(
                        f"Last-play flash QUEUED for {game['away_abbr']}@{game['home_abbr']}: "
                        f"\"{game.get('last_play_text')}\" (type={play_type})"
                    )

    def _service_flash_queue(self) -> Optional[Dict[str, Any]]:
        """Called from display(), before rotation. Returns a dict with
        the active flash's event_id/started_at/duration for this
        frame, or None if nothing's flashing. Also promotes the next
        queued flash (if any) into the active slot, force-jumping
        current_index to that game so it's guaranteed to actually be
        displayed rather than hoping normal rotation gets there before
        the flash would've expired -- that race condition was the
        actual bug behind the flash "only working intermittently"
        during multi-game nights."""
        with self._data_lock:
            now = time.time()
            if self._active_flash and now >= self._active_flash["expires_at"]:
                self._active_flash = None
                self.last_switch_time = now  # give normal rotation a fresh full window right after

            if self._active_flash is None and self._pending_flash_event_ids:
                next_event_id = self._pending_flash_event_ids.pop(0)

                # Look up the game BEFORE constructing the active-flash
                # dict, so home runs can get their own (longer) duration
                # instead of the general last-play duration -- the
                # animation needs more time to play out fully than a
                # plain text flash does.
                matched = False
                matched_game = None
                for idx, g in enumerate(self.rotation_games):
                    if g.get("event_id") == next_event_id:
                        self.current_index = idx
                        matched = True
                        matched_game = g
                        break

                duration = self.last_play_display_seconds
                if matched_game and self._is_home_run_play(
                    matched_game.get("last_play_type") or "", matched_game.get("last_play_text") or ""
                ):
                    duration = self.home_run_display_seconds

                self._active_flash = {
                    "event_id": next_event_id,
                    "started_at": now,
                    "duration": duration,
                    "expires_at": now + duration,
                }
                self.logger.info(
                    f"Last-play flash NOW SHOWING (event_id={next_event_id}, "
                    f"matched to a currently-live game: {matched}, duration={duration}s)"
                )

            return dict(self._active_flash) if self._active_flash else None

    def _enrich_boxscore_stats(self, game: Dict[str, Any]):
        """For FINAL games only: if hits/errors weren't in the main
        scoreboard response (unconfirmed field, unlike linescores which
        are documented), fetches ESPN's detailed summary endpoint to
        fill them in. Only ever needs to run ONCE per completed game
        (the data can't change after the fact), tracked via
        _enriched_boxscore_event_ids so this doesn't re-fetch on every
        poll for games already showing in past_games rotation.

        Defensive in the same way as pitch count: tries a specific
        plausible path (boxscore.teams[].statistics[]) first, and if
        that doesn't find hits/errors, logs the raw structure at DEBUG
        level so there's real data to fix this against instead of
        guessing again."""
        event_id = game.get("event_id")
        if not event_id or event_id in self._enriched_boxscore_event_ids:
            return
        if game.get("away_hits") is not None and game.get("home_hits") is not None:
            self._enriched_boxscore_event_ids.add(event_id)
            return

        resp = self.session.get(ESPN_SUMMARY_URL, params={"event": event_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        found_any = False
        try:
            for team_block in data.get("boxscore", {}).get("teams", []):
                abbr = team_block.get("team", {}).get("abbreviation", "").upper()
                stats = {}
                for s in team_block.get("statistics", []):
                    name = (s.get("name") or s.get("abbreviation") or "").lower()
                    stats[name] = s.get("displayValue", s.get("value"))
                hits = stats.get("hits") or stats.get("h")
                errors = stats.get("errors") or stats.get("e")
                if abbr == game.get("away_abbr"):
                    if hits is not None:
                        game["away_hits"] = hits
                        found_any = True
                    if errors is not None:
                        game["away_errors"] = errors
                        found_any = True
                elif abbr == game.get("home_abbr"):
                    if hits is not None:
                        game["home_hits"] = hits
                        found_any = True
                    if errors is not None:
                        game["home_errors"] = errors
                        found_any = True
        except Exception:
            pass

        if not found_any:
            self.logger.debug(
                f"Could not find hits/errors in the summary response for "
                f"{game.get('away_abbr')}@{game.get('home_abbr')}. "
                f"boxscore keys: {list(data.get('boxscore', {}).keys())}"
            )

        self._enriched_boxscore_event_ids.add(event_id)

    def _enrich_pitch_count(self, game: Dict[str, Any]):
        """Fetches ESPN's detailed per-game summary endpoint for the
        current pitcher's live pitch count. The lightweight scoreboard
        endpoint doesn't have this (confirmed from real captured data).

        CONFIRMED against real live-game data (2026-07-11, NYY@WSH):
        there is no simple pre-computed "total pitch count" field
        anywhere in this response -- the previous approach searching
        boxscore.players[].statistics[] was looking in the wrong place
        entirely (confirmed zero matches across 7 different live games).
        The real signal is the `plays` array: each individual pitch is
        its own entry with `type.type == "play-result"` and a
        `participants` list identifying who was pitching/batting via
        `{"athlete": {"id": ...}, "type": "pitcher"}`. Cumulative pitch
        count = counting how many such entries belong to the current
        pitcher across the whole game (see _count_pitches_for_pitcher).

        Falls back to the old boxscore-search approach afterward in
        case some other context does expose a direct field, but that's
        no longer the primary strategy since it's confirmed to not
        exist in the common case.

        IMPORTANT: prefers `game["pitcher_id"]` (extracted from the same
        scoreboard fetch that determined the pitcher NAME currently
        displayed) over re-deriving an id from this separately-fetched
        summary endpoint. Those two fetches happen at slightly
        different times -- if a pitching change occurs in between, the
        re-derived id could reference a DIFFERENT pitcher than the one
        whose name is on screen, showing an accurate-looking name next
        to a pitch count for someone else entirely. Using the same id
        throughout guarantees the name and count always refer to the
        same person."""
        event_id = game.get("event_id")
        if not event_id:
            return

        resp = self.session.get(ESPN_SUMMARY_URL, params={"event": event_id}, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        pitcher_id = game.get("pitcher_id")
        if not pitcher_id:
            try:
                for comp in data.get("header", {}).get("competitions", []):
                    situation = comp.get("situation", {})
                    pid = situation.get("pitcher", {}).get("playerId") or situation.get("pitcher", {}).get("athlete", {}).get("id")
                    if pid:
                        pitcher_id = str(pid)
                        break
            except Exception:
                pass

        # situation.pitcher can be empty during brief state transitions
        # (confirmed: happened in real diagnostic output caught between
        # innings) -- fall back to whoever the most recent play-result
        # in `plays` lists as the pitcher, which is far more consistently
        # populated than the situation snapshot.
        plays = data.get("plays", [])
        if not pitcher_id:
            for p in reversed(plays):
                if p.get("type", {}).get("type") != "play-result":
                    continue
                for part in p.get("participants", []):
                    if part.get("type") == "pitcher":
                        pid = part.get("athlete", {}).get("id")
                        if pid:
                            pitcher_id = str(pid)
                        break
                if pitcher_id:
                    break

        count = self._count_pitches_for_pitcher(plays, pitcher_id) if pitcher_id else None

        if count is None:
            # Fall back to the old search approach, kept only in case some
            # other context does expose a direct field -- not the primary
            # strategy anymore since it's confirmed absent in the common case.
            count = self._find_pitch_count(data, pitcher_id)

        if count is not None:
            game["pitch_count"] = count
            self.logger.info(
                f"Pitch count for {game['away_abbr']}@{game['home_abbr']} "
                f"(pitcher_id={pitcher_id}): counted {count} from play-by-play. "
                f"If this doesn't match a real broadcast/MLB app, please report it."
            )
        else:
            self.logger.debug(
                f"Could not determine a pitch count for "
                f"{game['away_abbr']}@{game['home_abbr']} (pitcher_id={pitcher_id}). "
                f"Plays in response: {len(plays)}."
            )

    def _count_pitches_for_pitcher(self, plays: List[Dict[str, Any]], pitcher_id: str) -> Optional[int]:
        """Counts real pitches thrown by `pitcher_id` across the whole
        game.

        REVISED after real data showed the first version was likely
        undercounting: that version filtered strictly to
        `type.type == "play-result"`, confirmed present on the FINAL
        pitch of an at-bat (a strikeout call) -- but never actually
        confirmed against an INTERMEDIATE pitch (a ball or a strike that
        doesn't end the at-bat). If those use a different type value
        (quite plausible -- ESPN's play-by-play often tags "Ball"/
        "Strike Looking" etc as their own distinct type rather than
        nested under a generic "play-result"), that filter would have
        only counted one pitch per at-bat faced, not the real total --
        producing a count far too low.

        Fixed by not depending on any type value at all: every pitch
        (confirmed from real data) carries an `atBatId` + a
        sequential `atBatPitchNumber` within that at-bat, whether it's
        an intermediate pitch or the final one. Counting DISTINCT
        (atBatId, atBatPitchNumber) pairs attributed to the pitcher
        naturally collapses any duplicate bookkeeping entries for the
        same pitch (like the confirmed "End Batter/Pitcher" duplicate
        of the final pitch) to a single count, without needing to know
        or guess which type values are "real" vs duplicates."""
        if not plays:
            return None
        seen_pitches = set()
        for p in plays:
            pitch_num = p.get("atBatPitchNumber")
            at_bat_id = p.get("atBatId")
            if pitch_num is None or at_bat_id is None:
                continue
            for part in p.get("participants", []):
                if part.get("type") == "pitcher" and str(part.get("athlete", {}).get("id")) == pitcher_id:
                    seen_pitches.add((at_bat_id, pitch_num))
                    break
        return len(seen_pitches) if seen_pitches else None

    def _find_pitch_count(self, data: Any, pitcher_id: Optional[str]) -> Optional[int]:
        """Tries a couple of specific plausible paths first (boxscore
        player stats keyed by name like "pitchesThrown" or "PC"), then
        falls back to a generic recursive scan of the whole response
        for any dict that has both a player/athlete id matching
        pitcher_id AND a key that looks like a pitch count. Best-effort:
        returns None rather than guessing wrong if nothing matches.

        NOTE: deliberately does NOT match a bare single-letter "P" label
        -- that was in an earlier version and is confirmed too
        ambiguous in practice (box scores commonly use "P" for
        "Position", not "Pitches", causing wrong values to be picked
        up for many real games). "PC", "PITCHES", "PITCHESTHROWN" are
        much less likely to collide with something unrelated."""
        # Specific attempt: ESPN boxscores commonly expose player stats
        # as parallel "labels"/"names" and "stats" (or "displayValue")
        # arrays under boxscore.players[].statistics[].
        try:
            for team_block in data.get("boxscore", {}).get("players", []):
                for stat_block in team_block.get("statistics", []):
                    labels = stat_block.get("labels") or stat_block.get("names") or []
                    pitch_idx = None
                    for i, label in enumerate(labels):
                        if str(label).strip().upper() in ("PC", "PITCHES", "PITCHESTHROWN"):
                            pitch_idx = i
                            break
                    if pitch_idx is None:
                        continue
                    for athlete_entry in stat_block.get("athletes", []):
                        athlete = athlete_entry.get("athlete", {})
                        if pitcher_id and str(athlete.get("id")) != pitcher_id:
                            continue
                        stats = athlete_entry.get("stats", [])
                        if pitch_idx < len(stats):
                            try:
                                return int(str(stats[pitch_idx]).split("-")[0])
                            except (ValueError, TypeError):
                                continue
        except Exception:
            pass

        # Generic fallback: recursively scan for any dict that has a
        # pitch-count-looking key AND whose NEARBY subtree (depth-limited,
        # not the entire response) also contains the pitcher's id.
        # Unbounded recursion here was a real bug: a large summary
        # response has rosters, standings, play-by-play, etc, and the
        # pitcher's id can coincidentally appear in unrelated sections
        # far from the actual current-game stat being looked for --
        # matching against a distant, unrelated field is exactly the
        # kind of thing that would produce a wrong-but-plausible-looking
        # number instead of failing loudly.
        pitch_key_names = {"pitchcount", "pitches", "pitchesthrown", "numberofpitches"}

        def contains_id(node, max_depth=3):
            if max_depth < 0:
                return False
            if isinstance(node, dict):
                if pitcher_id and str(node.get("id", "")) == pitcher_id:
                    return True
                return any(contains_id(v, max_depth - 1) for v in node.values())
            if isinstance(node, list):
                return any(contains_id(item, max_depth - 1) for item in node)
            return False

        def scan(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k.lower() in pitch_key_names and isinstance(v, (int, str)):
                        if pitcher_id is None or contains_id(node):
                            try:
                                return int(v)
                            except (ValueError, TypeError):
                                pass
                for v in node.values():
                    result = scan(v)
                    if result is not None:
                        return result
            elif isinstance(node, list):
                for item in node:
                    result = scan(item)
                    if result is not None:
                        return result
            return None

        return scan(data)

    def _fetch_past_games_lookback(self) -> List[Dict[str, Any]]:
        """Explicitly queries ESPN's scoreboard for each of the previous
        `past_games_lookback_days` days (via the `dates=YYYYMMDD`
        parameter) -- the mirror image of _fetch_future_upcoming_games's
        problem: the default no-date-parameter call only returns TODAY's
        games, so a favorite team's most recent COMPLETED game never
        shows up as a past game if it happened yesterday or earlier
        (an off-day today, or today's game hasn't finished yet). Same
        slower-cadence caching rationale as the upcoming lookahead --
        see past_games_refresh_seconds."""
        if not self.show_past_games:
            return []

        favorites_only = self.show_favorite_teams_only or not self.past_upcoming_all_teams
        results = []
        today = datetime.datetime.now()

        for offset in range(1, self.past_games_lookback_days + 1):
            day = today - datetime.timedelta(days=offset)
            date_param = day.strftime("%Y%m%d")
            try:
                resp = self.session.get(ESPN_SCOREBOARD_URL, params={"dates": date_param}, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                self.logger.warning(f"Could not fetch past-games lookback for {date_param}: {e}")
                continue

            for event in data.get("events", []):
                competitions = event.get("competitions", [])
                if not competitions:
                    continue
                comp = competitions[0]
                state = comp.get("status", {}).get("type", {}).get("state")
                if state != "post":
                    continue
                competitors = comp.get("competitors", [])
                abbrevs = [c.get("team", {}).get("abbreviation", "").upper() for c in competitors]
                involves_favorite = any(fav in abbrevs for fav in self.favorite_teams)
                if involves_favorite or not favorites_only:
                    results.append(self._parse_game(event, comp, game_type="final"))

        return results

    def _fetch_future_upcoming_games(self) -> List[Dict[str, Any]]:
        """Explicitly queries ESPN's scoreboard for each of the next
        `upcoming_games_lookahead_days` days (via the `dates=YYYYMMDD`
        parameter), since the default no-date-parameter call only
        returns TODAY's games -- a favorite team's actual next game is
        almost always tomorrow or later, not today, so relying on the
        main scoreboard fetch alone means upcoming_games is nearly
        always empty. This is a separate, slower-cadence fetch (see
        upcoming_games_refresh_seconds) since schedules barely change
        within a day, unlike scores/situations which need frequent
        polling. One request per day queried; failures on individual
        days are logged and skipped rather than aborting the whole
        lookahead."""
        if not self.show_upcoming_games:
            return []

        favorites_only = self.show_favorite_teams_only or not self.past_upcoming_all_teams
        results = []
        today = datetime.datetime.now()

        for offset in range(1, self.upcoming_games_lookahead_days + 1):
            day = today + datetime.timedelta(days=offset)
            date_param = day.strftime("%Y%m%d")
            try:
                resp = self.session.get(ESPN_SCOREBOARD_URL, params={"dates": date_param}, timeout=10)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                self.logger.warning(f"Could not fetch upcoming-games lookahead for {date_param}: {e}")
                continue

            for event in data.get("events", []):
                competitions = event.get("competitions", [])
                if not competitions:
                    continue
                comp = competitions[0]
                state = comp.get("status", {}).get("type", {}).get("state")
                if state != "pre":
                    continue
                competitors = comp.get("competitors", [])
                abbrevs = [c.get("team", {}).get("abbreviation", "").upper() for c in competitors]
                involves_favorite = any(fav in abbrevs for fav in self.favorite_teams)
                if involves_favorite or not favorites_only:
                    results.append(self._parse_game(event, comp, game_type="upcoming"))

        return results

    def _process_scoreboard(self, data: Dict[str, Any]):
        events = data.get("events", [])
        live_games = []
        past_games = []
        upcoming_games = []

        # Strict mode (show_favorite_teams_only) always restricts
        # past/upcoming to favorites regardless of past_upcoming_all_teams
        # -- that toggle only matters in non-strict mode.
        past_upcoming_favorites_only = self.show_favorite_teams_only or not self.past_upcoming_all_teams

        for event in events:
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp = competitions[0]
            state = comp.get("status", {}).get("type", {}).get("state")
            competitors = comp.get("competitors", [])
            abbrevs = [c.get("team", {}).get("abbreviation", "").upper() for c in competitors]
            involves_favorite = any(fav in abbrevs for fav in self.favorite_teams)

            if state == "in":
                # Collect ALL live games here, unfiltered -- the
                # favorite-vs-other cascade decision (which of these
                # actually get shown) happens in _maybe_refresh, since
                # it needs to know whether ANY favorite is live before
                # deciding whether to include everyone else.
                live_games.append(self._parse_game(event, comp, game_type="live"))
            elif state == "post" and self.show_past_games:
                if involves_favorite or not past_upcoming_favorites_only:
                    past_games.append(self._parse_game(event, comp, game_type="final"))
            elif state == "pre" and self.show_upcoming_games:
                if involves_favorite or not past_upcoming_favorites_only:
                    upcoming_games.append(self._parse_game(event, comp, game_type="upcoming"))

        past_games = past_games[: self.max_past_games]

        # Explicitly requested: upcoming games shown in start-time order.
        # Sorting by the raw ISO8601 UTC string (rather than the
        # formatted local date_str/time_str) sorts correctly across
        # date/month boundaries since ISO8601 sorts chronologically as
        # a plain string comparison.
        upcoming_games.sort(key=lambda g: g.get("event_date_raw") or "")
        upcoming_games = upcoming_games[: self.max_upcoming_games]

        fallback_game = None
        if not live_games and not past_games and not upcoming_games:
            fallback_game = self._find_favorite_game(data)

        return live_games, past_games, upcoming_games, fallback_game

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
        state = comp.get("status", {}).get("type", {}).get("state")
        game_type = {"in": "live", "post": "final", "pre": "upcoming"}.get(state, "upcoming")
        return self._parse_game(event, comp, game_type=game_type)

    def _format_game_datetime(self, event: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Parses ESPN's event.date (ISO8601 UTC, e.g.
        "2026-07-12T23:10Z") and formats it as separate (date, time)
        strings in the system's local timezone -- assumes the Pi's
        system clock/timezone is set correctly, which is the normal
        case for a home device. Returns (None, None) if the field is
        missing or unparseable rather than crashing."""
        raw = event.get("date")
        if not raw:
            return None, None
        try:
            iso = raw.replace("Z", "+00:00")
            dt_utc = datetime.datetime.fromisoformat(iso)
            dt_local = dt_utc.astimezone()
            date_str = f"{dt_local.month}/{dt_local.day}"
            hour_12 = dt_local.hour % 12 or 12
            ampm = "AM" if dt_local.hour < 12 else "PM"
            time_str = f"{hour_12}:{dt_local.minute:02d} {ampm}"
            return date_str, time_str
        except Exception:
            return None, None

    def _parse_game(self, event: Dict[str, Any], comp: Dict[str, Any], game_type: str = "live") -> Dict[str, Any]:
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

        def extract_linescores(competitor):
            """Confirmed via independent ESPN API documentation
            (community-maintained, not just my guess): each competitor
            object has a 'linescores' array, one entry per inning,
            each with a 'value' field for runs scored that inning, in
            order. Returns [] rather than crashing if missing/malformed."""
            try:
                linescores = competitor.get("linescores", [])
                return [int(ls.get("value", 0) or 0) for ls in linescores]
            except Exception:
                return []

        def extract_hits_errors(competitor):
            """UNLIKE linescores, hits/errors are NOT confirmed present
            in this lightweight scoreboard response -- there's a
            documented generic 'statistics' array, but not confirmed
            specifically for baseball H/E. Tries a few plausible keys
            here; returns (None, None) if not found, which signals the
            caller to fetch the detailed summary endpoint instead
            (see _enrich_boxscore_stats)."""
            hits, errors = None, None
            try:
                for stat in competitor.get("statistics", []):
                    name = (stat.get("name") or stat.get("abbreviation") or "").lower()
                    val = stat.get("displayValue", stat.get("value"))
                    if name in ("hits", "h"):
                        hits = val
                    elif name in ("errors", "e"):
                        errors = val
            except Exception:
                pass
            return hits, errors

        def extract_batter_info(situation_dict):
            """Confirmed against real live-game data (2026-07-09, ATH@DET
            and SEA@MIA): ESPN's actual structure is
            situation.batter.athlete.{displayName, fullName, shortName}.
            shortName comes pre-formatted as "F. Lastname" (e.g.
            "K. McGonigle") -- prefer that directly over reformatting
            displayName ourselves, since ESPN's own abbreviation handles
            edge cases (suffixes, multi-word names) more reliably than
            a naive "first letter + rest" split would.

            Falls back to a couple of alternate shapes in case ESPN
            changes this for other games/situations, and returns
            (full_name, short_name) with either possibly None rather
            than crashing if the structure is missing entirely."""
            candidates = [
                lambda s: s.get("batter", {}).get("athlete", {}),
                lambda s: s.get("atBat", {}).get("athlete", {}),
            ]
            for getter in candidates:
                try:
                    athlete = getter(situation_dict)
                    full = athlete.get("displayName") or athlete.get("fullName")
                    short = athlete.get("shortName")
                    if full or short:
                        return full, short
                except Exception:
                    continue
            return None, None

        def extract_pitcher_info(situation_dict):
            """Same shape as the batter extraction, confirmed against
            the same real live-game data: situation.pitcher.athlete.
            {displayName, fullName, shortName}. Also captures the
            pitcher's id -- needed so pitch-count enrichment can use
            the SAME pitcher this name was extracted for, rather than
            re-deriving an id from a separately-fetched endpoint that
            could reflect a pitching change that happened in between."""
            try:
                athlete = situation_dict.get("pitcher", {}).get("athlete", {})
                full = athlete.get("displayName") or athlete.get("fullName")
                short = athlete.get("shortName")
                pid = situation_dict.get("pitcher", {}).get("playerId") or athlete.get("id")
                return full, short, (str(pid) if pid else None)
            except Exception:
                return None, None, None

        def extract_pitch_count(situation_dict):
            """NOTE: the real live-game JSON already captured for this
            plugin shows situation.pitcher only has playerId/period/
            athlete/projections/summary (summary being a text string
            like "0.1 IP, 0 ER, 0 H, 0 BB") -- no explicit numeric pitch
            count field. These candidate paths are best-effort in case
            it appears under a different key or later in some games;
            if none match, this returns None and the display just
            shows the pitcher's name without a count number rather than
            a misleading placeholder."""
            candidates = [
                lambda s: s.get("pitcher", {}).get("pitchCount"),
                lambda s: s.get("pitcher", {}).get("pitches"),
                lambda s: s.get("pitchCount"),
            ]
            for getter in candidates:
                try:
                    val = getter(situation_dict)
                    if val is not None:
                        return val
                except Exception:
                    continue
            return None

        def extract_last_play(situation_dict):
            """Confirmed against the same real live-game data captured
            earlier (ATH@DET, SEA@MIA): situation.lastPlay.{id, text,
            type.type}. Unlike pitch count, this field IS present in
            the lightweight scoreboard endpoint -- no extra API call
            needed. `type.type` is a short code (seen so far: "ball",
            "start-batterpitcher") used to filter out routine
            pitch-by-pitch updates from actual play outcomes."""
            try:
                lp = situation_dict.get("lastPlay") or {}
                play_id = lp.get("id")
                play_text = lp.get("text")
                play_type = (lp.get("type") or {}).get("type", "")
                return play_id, play_text, play_type
            except Exception:
                return None, None, None

        batter_full, batter_short = extract_batter_info(situation)
        pitcher_full, pitcher_short, pitcher_id = extract_pitcher_info(situation)
        pitch_count = extract_pitch_count(situation)
        last_play_id, last_play_text, last_play_type = extract_last_play(situation)
        game_date_str, game_time_str = self._format_game_datetime(event)
        away_linescores = extract_linescores(away)
        home_linescores = extract_linescores(home)
        away_hits, away_errors = extract_hits_errors(away)
        home_hits, home_errors = extract_hits_errors(home)

        return {
            "game_type": game_type,
            "event_date_raw": event.get("date"),
            "game_date_str": game_date_str,
            "game_time_str": game_time_str,
            "away_linescores": away_linescores,
            "home_linescores": home_linescores,
            "away_hits": away_hits,
            "home_hits": home_hits,
            "away_errors": away_errors,
            "home_errors": home_errors,
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
            "batter_name": batter_full,
            "batter_short_name": batter_short,
            "pitcher_name": pitcher_full,
            "pitcher_short_name": pitcher_short,
            "pitcher_id": pitcher_id,
            "pitch_count": pitch_count,
            "event_id": event.get("id"),
            "last_play_id": last_play_id,
            "last_play_text": last_play_text,
            "last_play_type": last_play_type,
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
            "batter_short_name": "R. Greene",
            "pitcher_name": "Tarik Skubal",
            "pitcher_short_name": "T. Skubal",
            "pitch_count": 47,
            "event_id": "test-event-1",
            "last_play_id": "test-play-1",
            "last_play_text": "Riley Greene singles to left field.",
            "last_play_type": "single",
            "on_first": True,
            "on_second": False,
            "on_third": True,
        }

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------
    def _maybe_rotate(self):
        with self._data_lock:
            if len(self.rotation_games) <= 1:
                return
            now = time.time()
            if now - self.last_switch_time >= self.game_rotation_seconds:
                self.current_index = (self.current_index + 1) % len(self.rotation_games)
                self.last_switch_time = now

    def _current_game(self) -> Optional[Dict[str, Any]]:
        with self._data_lock:
            if self.rotation_games:
                # index is guarded above/in _maybe_refresh, but clamp
                # defensively in case rotation_games shrank between calls
                idx = min(self.current_index, len(self.rotation_games) - 1)
                return self.rotation_games[idx]
            return self.fallback_game

    # ------------------------------------------------------------------
    # Logos
    # ------------------------------------------------------------------
    def _resolve_logos(self, game: Dict[str, Any], size: Optional[int] = None):
        if not self.show_logos:
            return
        if size is None:
            width, height = self._get_dimensions()
            # IMPORTANT: final games use a narrower 40% left-half split
            # (_render_final_game), not the standard 50/50 the live/
            # upcoming layouts use -- sizing logos off the wrong split
            # is exactly what caused them to bleed into the box score:
            # a logo sized for the wider 50%-split column doesn't fit
            # the narrower one used for the box score layout.
            left_fraction = 0.4 if game.get("game_type") == "final" else 0.5
            left_w = int(width * left_fraction)
            col_w = left_w // 2
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
        active_flash = self._service_flash_queue()
        if active_flash is None:
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

        game_type = game.get("game_type", "live")

        try:
            if game_type == "final":
                self._render_final_game(image, draw, game, width, height)
                self._push_image(image, force_clear)
                return
            elif game_type == "upcoming":
                self._render_upcoming_game(image, draw, game, width, height)
                self._push_image(image, force_clear)
                return

            # Swapped per request: the darker shade now sits behind the
            # logo (better contrast so light/white logo elements don't
            # wash out against a bright saturated color), and the full
            # bright team color moves to the text bar instead.
            draw.rectangle([0, 0, col_w - 1, height - 1], fill=self._darken_color(game["away_color"]))
            draw.rectangle([col_w, 0, left_w - 1, height - 1], fill=self._darken_color(game["home_color"]))

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

            draw.rectangle([left_w, 0, left_w, height - 1], fill=(166, 166, 166))

            right_x0 = left_w + 2
            right_w = width - right_x0 - 1

            event_id = game.get("event_id")
            show_flash = active_flash is not None and event_id == active_flash["event_id"]

            if show_flash:
                play_type = (game.get("last_play_type") or "")
                play_text_for_detection = game.get("last_play_text") or ""
                if self._is_home_run_play(play_type, play_text_for_detection):
                    elapsed = time.time() - active_flash["started_at"]
                    batting_color = game["away_color"] if game["inning_half"] else game["home_color"]
                    self._draw_home_run_animation(
                        image, draw, right_x0, 0, right_w, height,
                        game.get("last_play_text") or "", elapsed, active_flash["duration"],
                        batting_color, seed=event_id or "hr",
                    )
                else:
                    self._draw_last_play(
                        image, draw, right_x0, 0, right_w, height,
                        game.get("last_play_text") or "", fill=(255, 255, 255),
                    )
            else:
                top_margin = 1
                lower_y = height - 6  # bottom-row (count/batter) text measures ~5px tall

                # --- Top row: pitch count + pitcher name, in the space
                #     freed up by moving inning/outs down next to the diamond ---
                # IMPORTANT: reserve a FIXED height here regardless of what
                # _draw_pitch_info actually returns. Using the real returned
                # height would make diamond_y (and therefore the diamond's
                # whole size) depend on whether THIS PARTICULAR game has
                # pitcher data -- which is exactly why the bases looked a
                # different size between games as the display cycled: some
                # games had a pitcher name (row ~6px tall) and others didn't
                # (row 0px tall), so the diamond got more or less vertical
                # room each time. A fixed reservation keeps geometry
                # identical across every game regardless of its data.
                pitch_row_reserved_h = 6
                has_batter = bool(game.get("batter_name") or game.get("batter_short_name"))
                has_pitcher = bool(game.get("pitcher_name") or game.get("pitcher_short_name"))
                if not has_batter and not has_pitcher:
                    # Mid-inning gap in ESPN's data (between at-bats, after
                    # a play, etc.) -- show who's due up next instead of
                    # leaving this row blank.
                    batting_team = game["away_abbr"] if game["inning_half"] else game["home_abbr"]
                    self._draw_due_up(image, draw, right_x0 + 1, top_margin, right_w - 2, batting_team)
                else:
                    self._draw_pitch_info(
                        image, draw, right_x0 + 1, top_margin, right_w - 2,
                        game.get("pitch_count"), game.get("pitcher_name"), game.get("pitcher_short_name"),
                    )

                # --- Middle: diamond centered, inning (left) and outs
                #     (right) vertically centered against it ---
                # Reserve real horizontal space for inning/outs first (measuring
                # actual glyph width, not guessing), THEN size the diamond to
                # fit exactly what's left -- this is what guarantees no overlap,
                # rather than assuming a fixed diamond width and hoping it fits.
                diamond_y = top_margin + pitch_row_reserved_h + 1
                diamond_available_h = (lower_y - 2) - diamond_y

                inning_tri_size = 6
                sample_bbox = self._measure(self.font_tiny, "12")  # worst-case 2-digit inning
                inning_number_w = sample_bbox[2] - sample_bbox[0]
                inning_reserved_w = inning_tri_size + 3 + inning_number_w + 2

                outs_size = 4
                outs_reserved_w = outs_size + 2 + 3

                available_diamond_w = right_w - inning_reserved_w - outs_reserved_w
                diamond_w = max(min(int(right_w * 0.5), available_diamond_w), 16)
                diamond_x = right_x0 + inning_reserved_w

                self._draw_diamond(draw, diamond_x, diamond_y, diamond_w, diamond_available_h, game)
                geo = self._diamond_geometry(diamond_x, diamond_y, diamond_w, diamond_available_h)

                inning_y = geo["center_y"] - inning_tri_size // 2
                self._draw_inning(image, right_x0 + 1, inning_y, game)

                outs_cx = right_x0 + right_w - 3 - outs_size // 2
                self._draw_outs(draw, outs_cx, geo["center_y"], game, size=outs_size, gap=1)

                # --- Bottom row: batter left-aligned (matching inning/
                #     pitch-count's left edge above), count right-aligned
                #     in the bottom-right corner ---
                count_text = f"{game['balls']}-{game['strikes']}"
                count_bbox = self._measure(self.font_count, count_text)
                count_w = count_bbox[2] - count_bbox[0]
                count_x = (right_x0 + right_w) - count_w - 2
                self._draw_count(image, count_x, lower_y, game)

                if self.show_batter_name:
                    batter_x = right_x0 + 1
                    batter_max_w = count_x - 2 - batter_x
                    self._draw_batter(image, draw, batter_x, lower_y, batter_max_w, game.get("batter_name"), game.get("batter_short_name"))
        except Exception as e:
            # Same reasoning as the try/except added around update()'s
            # parsing: a rendering bug triggered by one unusual game
            # state (e.g. a field temporarily missing mid-play) should
            # degrade to a blank/simple frame, not propagate an
            # exception out of display() -- which could have the same
            # "everything freezes" effect this whole investigation started from.
            self.logger.error(f"Error rendering game display, showing blank frame instead: {e}", exc_info=True)
            image = Image.new("RGB", (width, height), (0, 0, 0))
            self._render_text(image, (4, height // 2 - 4), "Render Err", self.font_small, (200, 60, 60))

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

    def _render_final_game(self, image, draw, game, width, height):
        """Left half: normal team columns (logo/abbreviation/score),
        same as the live layout, with the WINNING team's bar
        highlighted yellow. Right half: a compact inning-by-inning box
        score confined to just that space -- no team-abbreviation
        column in the grid itself, since the team names are already
        shown on the left; the top grid row is away, bottom is home,
        matching the left half's top/bottom team columns.

        Data notes: per-inning linescores are a documented ESPN field
        (confirmed via independent API documentation, not just
        assumed), so those should be reliable. Hits/errors are NOT
        confirmed in the lightweight scoreboard response the same way
        -- they're backfilled from ESPN's detailed summary endpoint via
        _enrich_boxscore_stats if missing, and simply left blank
        (rather than showing a misleading 0) if that still doesn't
        find them."""
        left_w = int(width * 0.4)  # squeezed from 50% to give the box score grid more room
        col_w = left_w // 2

        draw.rectangle([0, 0, col_w - 1, height - 1], fill=self._darken_color(game["away_color"]))
        draw.rectangle([col_w, 0, left_w - 1, height - 1], fill=self._darken_color(game["home_color"]))

        away_text = f"{game['away_abbr']} {game['away_score']}"
        home_text = f"{game['home_abbr']} {game['home_score']}"
        available_text_width = col_w - 4
        shared_font = self._fit_font_for_pair(draw, away_text, home_text, available_text_width, start_size=10)

        yellow = (255, 200, 0)
        away_won = game["away_score"] > game["home_score"]
        home_won = game["home_score"] > game["away_score"]

        away_txt_color = self._text_color_for(yellow) if away_won else self._text_color_for(game["away_color"])
        home_txt_color = self._text_color_for(yellow) if home_won else self._text_color_for(game["home_color"])

        self._draw_team_column(image, draw, 0, 0, col_w, height,
                                game["away_abbr"], game["away_score"], game.get("away_logo"),
                                away_txt_color, game["away_color"], shared_font,
                                bar_color_override=yellow if away_won else None)
        self._draw_team_column(image, draw, col_w, 0, left_w - col_w, height,
                                game["home_abbr"], game["home_score"], game.get("home_logo"),
                                home_txt_color, game["home_color"], shared_font,
                                bar_color_override=yellow if home_won else None)

        draw.rectangle([left_w, 0, left_w, height - 1], fill=(166, 166, 166))

        # Starts right after the separator (left_w+1), not left_w+2 --
        # that extra pixel was never filled by anything (not the
        # separator, not the green background), leaving a visible black
        # seam on the left edge of the box score specifically. On the
        # live/upcoming layouts that same gap is invisible since their
        # black half has no distinct fill color to seam against.
        right_x0 = left_w + 1
        # Unlike the live/upcoming layouts, this extends all the way to
        # the true panel edge (no "-1" reserve) -- that reserved pixel
        # was left unfilled/black, which is exactly the "dark border"
        # visible on the right edge of the box score.
        right_w = width - right_x0

        FENWAY_GREEN = (13, 46, 33)
        GRID_LINE = (70, 100, 85)
        TEXT_COLOR = (235, 235, 220)
        WIN_COLOR = (255, 200, 0)

        draw.rectangle([right_x0, 0, right_x0 + right_w - 1, height - 1], fill=FENWAY_GREEN)

        away_ls = game.get("away_linescores") or []
        home_ls = game.get("home_linescores") or []
        num_innings = max(9, len(away_ls), len(home_ls))

        # Investigate missing data: if the two teams' linescores arrays
        # are different lengths, log it so we can tell from real data
        # whether that's a genuine ESPN convention (traditionally, a
        # box score leaves a half-inning's box BLANK -- not "0" -- when
        # a team didn't bat there, e.g. the home team winning without
        # needing the bottom of the 9th) versus an actual extraction
        # bug losing a real data point.
        if away_ls and home_ls and len(away_ls) != len(home_ls):
            self.logger.info(
                f"Box score linescore length mismatch for "
                f"{game.get('away_abbr')}@{game.get('home_abbr')}: "
                f"away has {len(away_ls)} innings {away_ls}, home has "
                f"{len(home_ls)} innings {home_ls}. If a box shows blank "
                f"where you expected a real value, this is why -- ESPN's "
                f"own data has fewer entries for that team, most likely "
                f"because they didn't bat in that half-inning (traditional "
                f"box scores leave that blank, not zero) rather than a bug "
                f"here dropping a real value."
            )

        # Exact fixed widths based on measured ink + exactly 1px padding
        # on each side, per explicit spec -- inning columns and E only
        # ever need to fit a single digit (3px ink, confirmed uniform
        # across all digits after the earlier "1" glyph widening), so
        # 3 + 1 + 1 = 5px.
        single_digit_ink_w = 3
        double_digit_ink_w = 7
        pad = 1
        inning_col_w = single_digit_ink_w + pad * 2   # 5
        e_col_w = single_digit_ink_w + pad * 2         # 5, same as innings
        min_wide_col_w = double_digit_ink_w + pad * 2  # 9, minimum for R/H

        # Cap displayed innings using the MINIMUM wide-column width as a
        # conservative assumption -- ensures R/H never shrink below their
        # functional minimum even in a long extra-innings game, before
        # we know the final num_innings needed to compute their actual
        # (possibly larger) width below.
        fixed_extra_w_min = min_wide_col_w * 2 + e_col_w
        max_innings_that_fit = max((right_w - fixed_extra_w_min) // inning_col_w, 1)
        num_innings = min(num_innings, max_innings_that_fit, 12)

        # IMPORTANT: confirmed via a direct look at the raw rendered image
        # (bypassing any web UI rendering entirely) that fixing R/H at
        # exactly 9px left real, visible unused space after the E column
        # whenever the fixed columns didn't fully consume the available
        # width -- correct geometry, but wasted space that looked wrong.
        # R/H now dynamically absorb ALL leftover width instead of
        # leaving it unused, per explicit spec allowance that they can
        # be "slightly wider" -- never shrinking below the minimum
        # needed to comfortably fit double digits.
        remaining_for_wide = right_w - (inning_col_w * num_innings) - e_col_w
        wide_col_w = max(remaining_for_wide // 2, min_wide_col_w)

        col_widths = [inning_col_w] * num_innings + [wide_col_w, wide_col_w, e_col_w]
        total_grid_w = sum(col_widths)
        # REVERTED centering: confirmed via direct debug logging that the
        # column-width/padding math itself is correct (col_bounds matches
        # exactly between sandbox and real deployment), but centering the
        # grid introduced a NEW visible gap between the separator and the
        # start of the grid that wasn't there before, which made the
        # overall layout look worse. Back to flush-left against the
        # separator -- the right-edge border (drawn below, alongside
        # internal dividers) still fixes the original "E column looks
        # too wide" issue on its own, without needing centering too.
        grid_x0 = right_x0
        col_bounds = [grid_x0]
        for cw in col_widths:
            col_bounds.append(col_bounds[-1] + cw)
        row_bounds = [round(i * height / 3) for i in range(4)]

        self.logger.info(
            f"BOXSCORE DEBUG: width={width} left_w={left_w} right_x0={right_x0} "
            f"right_w={right_w} total_grid_w={total_grid_w} grid_x0={grid_x0} "
            f"col_bounds={col_bounds} row_bounds={row_bounds}"
        )

        header_y0, header_y1 = row_bounds[0], row_bounds[1]
        away_y0, away_y1 = row_bounds[1], row_bounds[2]
        home_y0, home_y1 = row_bounds[2], row_bounds[3]

        font = self.font_tiny

        # Draw grid lines ONCE as unified single-pixel lines spanning the
        # whole table, rather than having each cell independently draw
        # its own full border -- confirmed by direct pixel sampling that
        # doing it per-cell doubles every INTERNAL divider to 2px thick
        # (each of two adjacent cells draws its own separate 1px border
        # immediately next to, not on top of, the other's). Draws ALL
        # boundaries now, including the outer left/right edges -- with
        # the grid centered (leftover width split as margin on both
        # sides rather than dumped as one large gap after E), those
        # edges need their own visible border too, or the table's true
        # boundary is ambiguous against the surrounding green margin.
        grid_x1 = col_bounds[-1]
        for cx in col_bounds:
            draw.line([(cx, header_y0), (cx, home_y1 - 1)], fill=GRID_LINE)
        for cy in row_bounds[1:-1]:
            draw.line([(grid_x0, cy), (grid_x1 - 1, cy)], fill=GRID_LINE)

        def draw_cell(x0, x1, y0, y1, text, color=TEXT_COLOR):
            if text:
                w, h = x1 - x0, y1 - y0
                bbox = self._measure(font, text)
                th = bbox[3] - bbox[1]
                # IMPORTANT: centering horizontally using _measure's width
                # was confirmed wrong -- that width includes the glyph's
                # trailing advance spacing (DWIDTH), not just its ink
                # (BBX). Confirmed by direct pixel comparison: "0" measures
                # 4px wide but its actual ink is only 3px, so centering
                # against the measured width consistently drifted the ink
                # left by an amount that varied with cell width (since the
                # 1px of phantom trailing space shifts the centering math
                # differently depending on integer rounding at each
                # width) -- exactly the "scattered" look reported.
                # _ink_extent measures real rendered ink instead.
                ink_left, ink_right = self._ink_extent(font, text)
                ink_w = ink_right - ink_left + 1
                target_ink_x0 = x0 + max((w - ink_w) // 2, 0)
                tx = target_ink_x0 - ink_left + 3  # +3 undoes _ink_extent's internal scratch-render offset
                # Vertical centering: rounds UP (biases the text down)
                # rather than floor-rounding, specifically to fix row 2
                # (away row) -- confirmed its height comes out to 10px
                # vs 11px for header/home (32 doesn't divide evenly by
                # 3), leaving an ODD 5px of leftover space after fitting
                # the 5px-tall glyph, which floor-division split
                # asymmetrically (2px top, 3px bottom), placing every
                # digit 1px too high. Header/home have EVEN leftover
                # (6px) and already centered perfectly (3px/3px) --
                # rounding up instead of down doesn't change their
                # result at all, only row 2's.
                ty = y0 + max((h - th + 1) // 2, 0) - bbox[1]
                self._render_text(image, (tx, ty), text, font, color)

        # Header row: inning numbers, then R/H/E labels
        for i in range(num_innings):
            draw_cell(col_bounds[i], col_bounds[i + 1], header_y0, header_y1, str(i + 1))
        for j, label in enumerate(("R", "H", "E")):
            idx = num_innings + j
            draw_cell(col_bounds[idx], col_bounds[idx + 1], header_y0, header_y1, label)

        def draw_team_row(y0, y1, linescores, score, hits, errors, won):
            row_color = WIN_COLOR if won else TEXT_COLOR
            for i in range(num_innings):
                val = linescores[i] if i < len(linescores) else None
                draw_cell(col_bounds[i], col_bounds[i + 1], y0, y1, str(val) if val is not None else "")
            draw_cell(col_bounds[num_innings], col_bounds[num_innings + 1], y0, y1, str(score), color=row_color)
            draw_cell(col_bounds[num_innings + 1], col_bounds[num_innings + 2], y0, y1,
                      str(hits) if hits is not None else "", color=row_color)
            draw_cell(col_bounds[num_innings + 2], col_bounds[num_innings + 3], y0, y1,
                      str(errors) if errors is not None else "", color=row_color)

        draw_team_row(away_y0, away_y1, away_ls, game["away_score"], game.get("away_hits"), game.get("away_errors"), away_won)
        draw_team_row(home_y0, home_y1, home_ls, game["home_score"], game.get("home_hits"), game.get("home_errors"), home_won)

    def _render_upcoming_game(self, image, draw, game, width, height):
        """Scheduled game that hasn't started: team columns show only
        the abbreviation (no score -- there isn't one yet), and the
        black half shows "UPCOMING" with the game's date and start time
        (in local system time) below it."""
        left_w = width // 2
        col_w = left_w // 2

        draw.rectangle([0, 0, col_w - 1, height - 1], fill=self._darken_color(game["away_color"]))
        draw.rectangle([col_w, 0, left_w - 1, height - 1], fill=self._darken_color(game["home_color"]))

        away_txt_color = self._text_color_for(game["away_color"])
        home_txt_color = self._text_color_for(game["home_color"])

        available_text_width = col_w - 4
        shared_font = self._fit_font_for_pair(draw, game["away_abbr"], game["home_abbr"], available_text_width, start_size=10)

        self._draw_team_column(image, draw, 0, 0, col_w, height,
                                game["away_abbr"], game["away_score"], game.get("away_logo"),
                                away_txt_color, game["away_color"], shared_font, show_score=False)
        self._draw_team_column(image, draw, col_w, 0, left_w - col_w, height,
                                game["home_abbr"], game["home_score"], game.get("home_logo"),
                                home_txt_color, game["home_color"], shared_font, show_score=False)

        draw.rectangle([left_w, 0, left_w, height - 1], fill=(166, 166, 166))

        right_x0 = left_w + 2
        right_w = width - right_x0 - 1

        title_font = self._load_font(8, bold=True)
        title = "UPCOMING"
        tbbox = self._measure(title_font, title)
        tw = tbbox[2] - tbbox[0]
        tx = right_x0 + max((right_w - tw) // 2, 0)
        ty = 2 - tbbox[1]
        self._render_text(image, (tx, ty), title, title_font, (255, 255, 255))

        info_font = self._load_font(7, bold=False)
        date_str = game.get("game_date_str")
        time_str = game.get("game_time_str")
        cursor_y = ty + (tbbox[3] - tbbox[1]) + 4

        for line in filter(None, [date_str, time_str]):
            lbbox = self._measure(info_font, line)
            lw = lbbox[2] - lbbox[0]
            lx = right_x0 + max((right_w - lw) // 2, 0)
            ly = cursor_y - lbbox[1]
            self._render_text(image, (lx, ly), line, info_font, (200, 200, 200))
            cursor_y += (lbbox[3] - lbbox[1]) + 2

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
    def _darken_color(color: Tuple[int, int, int], min_channel: int = 15) -> Tuple[int, int, int]:
        return tuple(max(c // 2, min_channel) for c in color)

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



    def _draw_team_column(self, image, draw, x0, y0, w, h, abbr, score, logo, text_color, bg_color, font,
                          bar_color_override=None, show_score=True):
        """Logo fills nearly the whole column (as large as the panel
        allows); a darkened bar across the bottom holds the bold
        'ABBR SCORE' text so it stays legible over the logo. `font` is
        computed once by the caller from BOTH columns' text, so the two
        teams always render at the same size.

        `bar_color_override`: used for final (completed) games to
        highlight the winning team's bar in yellow instead of its
        normal team color. `show_score`: set False for upcoming games,
        which don't have a score yet -- shows just the abbreviation."""
        text_line = f"{abbr} {score}" if show_score else abbr
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
        bar_color = bar_color_override if bar_color_override is not None else bg_color
        draw.rectangle([x0, bar_y0, x0 + w - 1, y0 + h - 1], fill=bar_color)

        tx = x0 + max((w - line_w) // 2, 0)
        tx = min(tx, x0 + w - line_w) if line_w < w else x0
        ty = bar_y0 + max((bar_h - line_h) // 2, 0) - line_bbox[1]
        self._render_text(image, (tx, ty), text_line, font, text_color)

    def _diamond_geometry(self, x, y, w, h):
        """Single source of truth for the diamond's size/position math,
        shared between _draw_diamond (drawing it) and display() (which
        needs to know its actual center to align the inning/outs
        indicators next to it)."""
        cx = x + w // 2
        max_half_by_height = max((h - 2) // 3, 3)
        max_half_by_width = max((w - 6) // 2, 3)
        half = max(min(max_half_by_height, max_half_by_width), 3)
        top_y = y + half + 1
        bottom_y = top_y + half + 2
        left_x = cx - half - 3 - half   # leftmost point (third base tip)
        right_x = cx + half + 3 + half  # rightmost point (first base tip)
        center_y = (top_y + bottom_y) // 2
        return {
            "cx": cx, "half": half, "top_y": top_y, "bottom_y": bottom_y,
            "left_x": left_x, "right_x": right_x, "center_y": center_y,
        }

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
        geo = self._diamond_geometry(x, y, w, h)
        cx, half, top_y, bottom_y = geo["cx"], geo["half"], geo["top_y"], geo["bottom_y"]

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

    def _wrap_text_lines(self, font, text: str, max_width: int) -> List[str]:
        """Greedy word-wrap: fits as many words per line as possible
        within max_width, wrapping to a new line when the next word
        would overflow."""
        words = text.split()
        if not words:
            return []
        lines = []
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            bbox = self._measure(font, trial)
            if bbox[2] - bbox[0] <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _is_home_run_play(self, play_type: str, play_text: str) -> bool:
        """Two independent signals, either one triggers the animation:
        1. The guessed type code (HOME_RUN_PLAY_TYPES) -- confirmed
           wrong in practice, kept only as a cheap first check in case
           it's right for some other sport/context.
        2. The play's actual narrative text containing "home run" or
           "homers"/"homered" -- much more reliable, since ESPN's
           human-readable phrasing for a home run call is predictable
           regardless of whatever internal type code they actually use."""
        if (play_type or "").lower() in HOME_RUN_PLAY_TYPES:
            return True
        text_lower = (play_text or "").lower()
        return any(kw in text_lower for kw in HOME_RUN_TEXT_KEYWORDS)

    def _draw_home_run_animation(self, image, draw, x0, y0, w, h, play_text: str,
                                  elapsed: float, duration: float, team_color, seed: str):
        """Three-phase home run animation, sequenced within the total
        flash duration:
          1. Ball arc -- a small ball traces a parabolic path across
             the box and exits, representing the moment of contact.
          2. Strobing flash -- background alternates black/team-color
             with bold "HOME RUN!" text, an immediate high-contrast hit.
          3. Firework bursts + play text -- settles into a calmer black
             background with recurring particle bursts and the actual
             play description wrapped below "HOME RUN!".

        Phase lengths scale proportionally to `duration` (clamped to
        sensible min/max) so this still looks reasonable whether
        last_play_display_seconds is short or long."""
        phase1_len = max(min(duration * 0.25, 1.5), 0.6)
        phase2_len = max(min(duration * 0.20, 1.2), 0.5)
        phase3_start = phase1_len + phase2_len

        if elapsed < phase1_len:
            draw.rectangle([x0, y0, x0 + w - 1, y0 + h - 1], fill=(0, 0, 0))
            self._draw_ball_arc(image, x0, y0, w, h, elapsed / phase1_len)
        elif elapsed < phase3_start:
            self._draw_strobe_flash(image, draw, x0, y0, w, h, elapsed - phase1_len, team_color)
        else:
            self._draw_fireworks_with_text(image, draw, x0, y0, w, h, elapsed - phase3_start, play_text, seed)

    def _draw_ball_arc(self, image, x0, y0, w, h, progress: float):
        """Traces a small ball along a parabolic arc from the left
        edge, peaking in the middle, exiting past the right edge --
        with a short fading trail behind it."""
        progress = max(0.0, min(1.0, progress))
        trail_len = 5
        for i in range(trail_len, 0, -1):
            p = progress - i * 0.025
            if p < 0:
                continue
            bx = x0 + int(p * (w + 6)) - 3
            by = y0 + (h - 2) - int((h - 4) * (1 - (2 * p - 1) ** 2))
            fade = max(0, 200 - i * 45)
            if 0 <= bx < image.width and 0 <= by < image.height:
                image.putpixel((bx, by), (fade, fade, 0))

        bx = x0 + int(progress * (w + 6)) - 3
        by = y0 + (h - 2) - int((h - 4) * (1 - (2 * progress - 1) ** 2))
        draw = ImageDraw.Draw(image)
        if x0 - 2 <= bx <= x0 + w + 2 and y0 - 2 <= by <= y0 + h + 2:
            draw.ellipse([bx - 1, by - 1, bx + 1, by + 1], fill=(255, 255, 255))

    def _draw_strobe_flash(self, image, draw, x0, y0, w, h, t: float, team_color):
        """Alternates the whole box between black and the batting
        team's color a few times per second, with bold white/gold
        'HOME RUN!' text staying steady on top."""
        strobe_on = int(t * 4) % 2 == 0
        bg = team_color if strobe_on else (0, 0, 0)
        draw.rectangle([x0, y0, x0 + w - 1, y0 + h - 1], fill=bg)

        text_color = (255, 255, 255) if strobe_on else (255, 215, 0)
        font = self._load_font(9, bold=True)
        text = "HOME RUN!"
        bbox = self._measure(font, text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = x0 + max((w - tw) // 2, 0)
        ty = y0 + max((h - th) // 2, 0)
        self._render_text(image, (tx, ty), text, font, text_color)

    def _draw_fireworks(self, image, x0, y0, w, h, t: float, seed: str,
                         num_bursts: int = 2, particles_per_burst: int = 8, burst_period: float = 0.9):
        """A couple of recurring particle bursts radiating outward from
        random (but seed-fixed, so consistent across frames of the same
        flash) center points, fading out and looping every burst_period
        seconds for continued visual interest through the settled phase."""
        rng = random.Random(seed)
        colors = [(255, 90, 0), (255, 215, 0), (255, 255, 255), (80, 180, 255)]
        for b in range(num_bursts):
            center_x = x0 + rng.randint(int(w * 0.2), int(w * 0.8))
            center_y = y0 + rng.randint(int(h * 0.15), int(h * 0.6))
            color = colors[rng.randint(0, len(colors) - 1)]
            offset = rng.uniform(0, burst_period)
            cycle_t = (t + offset) % burst_period
            fade = max(0.0, 1.0 - cycle_t / (burst_period * 0.85))
            if fade <= 0:
                continue
            for p in range(particles_per_burst):
                angle = (2 * math.pi * p / particles_per_burst) + b
                speed = 5 + (p % 3) * 2
                dist = speed * cycle_t
                px = int(center_x + dist * math.cos(angle))
                py = int(center_y + dist * math.sin(angle) * 0.7)  # slightly flattened, more natural at this aspect ratio
                if x0 <= px < x0 + w and y0 <= py < y0 + h:
                    c = tuple(max(int(ch * fade), 0) for ch in color)
                    image.putpixel((px, py), c)

    def _draw_scrolling_text(self, image, x0, y0, w, h, text: str, font, fill, t: float,
                              speed_px_per_sec: float = 18.0, gap: int = 16):
        """Draws `text` on a single line, vertically centered. If it
        fits within `w` as-is, it's just centered and static -- no
        need to scroll short text. If it's wider than the box, it
        scrolls continuously leftward (looping) based on elapsed time
        `t`, so long play descriptions are never cut off, just take a
        few seconds to fully read.

        Renders onto a scratch canvas exactly the size of the box, then
        pastes that onto the real image -- this is what guarantees the
        scrolling text can never bleed outside its box (e.g. into the
        team panels on the left) regardless of how far negative the
        scroll offset gets or which font backend is active. PIL's
        draw.text() only clips to the full target image's bounds, not
        to an arbitrary sub-region, so without this a wide negative
        offset could draw well outside the intended box."""
        if not text or w <= 0 or h <= 0:
            return
        bbox = self._measure(font, text)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        scratch = Image.new("RGB", (w, h), (0, 0, 0))
        ty = max((h - th) // 2, 0) - bbox[1]

        if tw <= w:
            tx = max((w - tw) // 2, 0)
            self._render_text(scratch, (tx, ty), text, font, fill)
        else:
            loop_dist = w + tw + gap
            progress = (t * speed_px_per_sec) % loop_dist
            tx = int(round(w - progress))
            self._render_text(scratch, (tx, ty), text, font, fill)

        image.paste(scratch, (x0, y0))

    def _draw_fireworks_with_text(self, image, draw, x0, y0, w, h, t: float, play_text: str, seed: str):
        """Settled celebration phase: black background, recurring
        firework bursts confined to the upper portion, 'HOME RUN!'
        steady beneath them, and the actual play description scrolling
        along the bottom -- scrolling rather than the wrap/shrink/
        truncate approach used elsewhere, since this row only has
        room for a single line and a long description would otherwise
        get cut off or shrunk to near-illegibility."""
        draw.rectangle([x0, y0, x0 + w - 1, y0 + h - 1], fill=(0, 0, 0))

        fireworks_h = int(h * 0.55)
        self._draw_fireworks(image, x0, y0, w, fireworks_h, t, seed)

        font = self._load_font(7, bold=True)
        title = "HOME RUN!"
        tbbox = self._measure(font, title)
        tw = tbbox[2] - tbbox[0]
        tx = x0 + max((w - tw) // 2, 0)
        ty = y0 + fireworks_h
        self._render_text(image, (tx, ty), title, font, (255, 215, 0))

        if play_text:
            desc_y0 = ty + (tbbox[3] - tbbox[1]) + 2
            desc_h = max(h - (desc_y0 - y0), 0)
            if desc_h > 4:
                desc_font = self._load_font(7, bold=False)
                self._draw_scrolling_text(image, x0, desc_y0, w, desc_h, play_text, desc_font, (200, 200, 200), t)

    def _draw_last_play(self, image, draw, x0, y0, w, h, text: str, fill=(255, 255, 255)):
        """Word-wraps `text` (a full play description, e.g. "Aaron
        Judge homers to right field, 2 RBI") across as many lines as
        fit in the box, picking the largest font size that lets it fit
        both width- and height-wise. Falls back to the smallest size
        with truncated lines (ellipsis on the last visible line) if
        even that doesn't fully fit -- rare, but better than silently
        cutting off mid-sentence with no indication."""
        if not text:
            return

        max_text_width = w - 4
        best_font = None
        best_lines = None
        for size in range(9, 4, -1):
            font = self._load_font(size, bold=True)
            lines = self._wrap_text_lines(font, text, max_text_width)
            line_bbox = self._measure(font, "Ag")
            line_h = (line_bbox[3] - line_bbox[1]) + 2
            total_h = line_h * len(lines)
            all_fit_width = all(self._measure(font, ln)[2] - self._measure(font, ln)[0] <= max_text_width for ln in lines)
            if total_h <= h and all_fit_width:
                best_font, best_lines = font, lines
                break

        if best_font is None:
            font = self._load_font(5, bold=True)
            lines = self._wrap_text_lines(font, text, max_text_width)
            line_bbox = self._measure(font, "Ag")
            line_h = (line_bbox[3] - line_bbox[1]) + 2
            max_lines = max(h // line_h, 1)
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                lines[-1] = lines[-1].rstrip() + "..."
            best_font, best_lines = font, lines

        line_bbox = self._measure(best_font, "Ag")
        line_h = (line_bbox[3] - line_bbox[1]) + 2
        total_h = line_h * len(best_lines)
        start_y = y0 + max((h - total_h) // 2, 0)
        for i, line in enumerate(best_lines):
            lbbox = self._measure(best_font, line)
            line_w = lbbox[2] - lbbox[0]
            lx = x0 + max((w - line_w) // 2, 0)
            self._render_text(image, (lx, start_y + i * line_h), line, best_font, fill)

    def _draw_due_up(self, image, draw, x, y, max_width, team_abbr):
        """Shows "TEAM DUE UP" in red, in the same spot pitch count/
        pitcher name normally occupies, for when ESPN's data has a gap
        between at-bats (no current batter or pitcher listed).
        `team_abbr` is whichever team is currently batting, derived
        from inning_half by the caller."""
        text = f"{team_abbr} DUE UP"
        font = self._fit_font_for_width(draw, text, max_width, start_size=7, min_size=4)
        bbox = self._measure(font, text)
        text_to_draw = text
        if bbox[2] - bbox[0] > max_width:
            truncated = text
            text_to_draw = None
            while truncated:
                candidate = truncated + "."
                cbbox = self._measure(font, candidate)
                if cbbox[2] - cbbox[0] <= max_width:
                    text_to_draw = candidate
                    break
                truncated = truncated[:-1]
            if text_to_draw is None:
                return
        self._render_text(image, (x, y), text_to_draw, font, (255, 60, 0))

    def _draw_pitch_line(self, image, xy, font, fill, pitch_count, name, ink_gap: int = 1) -> int:
        """Draws 'P:<count> <name>' (or just name, if no count) with
        the P-colon gap and the name's initial-period gap both
        tightened. Single source of truth used for both measuring
        (via a scratch canvas) and final rendering, so they can never
        drift apart -- that drift (measuring untightened width, then
        only tightening if nothing needed truncating) was the actual
        bug: any name long enough to need truncation skipped tightening
        entirely, which is backwards since those are exactly the names
        that benefit from it most."""
        x, y = xy
        cursor = x
        if pitch_count is not None:
            w = self._draw_tight_join(image, cursor, y, font, fill, "P", ":", ink_gap=ink_gap)
            cursor += w + 2
            count_str = str(pitch_count)
            self._render_text(image, (cursor, y), count_str, font, fill)
            count_bbox = self._measure(font, count_str)
            cursor += (count_bbox[2] - count_bbox[0]) + 3
        name_w = self._draw_name_tightened(image, (cursor, y), font, fill, name, ink_gap=ink_gap)
        cursor += name_w
        return cursor - x

    def _measure_pitch_line(self, font, pitch_count, name, ink_gap: int = 1) -> int:
        """Width _draw_pitch_line would actually use, without touching
        the real image."""
        scratch = Image.new("RGB", (400, 30), (0, 0, 0))
        return self._draw_pitch_line(scratch, (2, 2), font, (255, 255, 255), pitch_count, name, ink_gap=ink_gap)

    def _draw_pitch_info(self, image, draw, x, y, max_width, pitch_count, pitcher_name, pitcher_short_name):
        """Draws 'P:<count> <Pitcher Name>' at the top of the black
        half. Returns the pixel height actually used, so the caller can
        position the diamond right below it regardless of font metrics.

        IMPORTANT CAVEAT: real live-game data already captured for this
        plugin shows ESPN's scoreboard situation.pitcher object doesn't
        appear to include a pitch-count field at all (see
        extract_pitch_count's docstring in _parse_game). If pitch_count
        never populates for you, that's most likely ESPN's lightweight
        scoreboard endpoint simply not exposing it.

        If the full name doesn't fit even at the smallest font size,
        truncates the NAME specifically (never the "P:<count>" prefix)
        character-by-character with a trailing "." -- using the same
        tightened-width measurement throughout, so tightening is never
        silently skipped the way it was before.

        If there's no pitcher name at all, draws nothing and returns 0
        so the diamond simply moves up to fill the freed space."""
        if not pitcher_name and not pitcher_short_name:
            return 0

        name = pitcher_short_name or self._format_batter_name(pitcher_name)
        color = (180, 180, 220)
        sizing_text = f"P:{pitch_count} {name}" if pitch_count is not None else name
        font = self._fit_font_for_width(draw, sizing_text, max_width, start_size=7, min_size=4)

        candidate_name = name
        final_name = None
        while candidate_name:
            trial = candidate_name if candidate_name == name else candidate_name + "."
            width = self._measure_pitch_line(font, pitch_count, trial, ink_gap=1)
            if width <= max_width:
                final_name = trial
                break
            candidate_name = candidate_name[:-1]

        if final_name is None:
            return 0

        used_w = self._draw_pitch_line(image, (x, y), font, color, pitch_count, final_name, ink_gap=1)
        bbox = self._measure(font, final_name)
        return bbox[3] - bbox[1]

    def _draw_batter(self, image, draw, x, y, max_width, batter_name, batter_short_name=None):
        """Draws whoever is currently at bat, shrunk to fit whatever
        width remains next to the count.

        Prefers ESPN's own pre-formatted `shortName` field (e.g.
        "K. McGonigle") over reformatting `displayName` ourselves --
        confirmed via real live-game data that ESPN already provides
        this, and it's more reliable for edge cases (suffixes,
        multi-word names) than a naive "first letter + rest" split.
        Falls back to our own `_format_batter_name` if shortName wasn't
        available for some reason.

        Truncation decisions and the final render both use
        `_measure_name_tightened`/`_draw_name_tightened` -- the SAME
        tightened-width calculation throughout. Previously, truncation
        was decided using the untightened width, then tightening was
        only applied if no truncation happened at all -- so any name
        long enough to need truncation (common, not rare) silently
        skipped tightening entirely, which is backwards: those are
        exactly the names that benefit from it most."""
        if (not batter_name and not batter_short_name) or max_width <= 0:
            return
        formatted = batter_short_name or self._format_batter_name(batter_name)
        font = self._fit_font_for_width(draw, formatted, max_width, start_size=7, min_size=4)

        candidate = formatted
        while candidate:
            trial = candidate if candidate == formatted else candidate + "."
            width = self._measure_name_tightened(font, trial, ink_gap=1)
            if width <= max_width:
                text_to_draw = trial
                break
            candidate = candidate[:-1]
        else:
            return  # nothing fits, not even a single truncated character

        self._draw_name_tightened(image, (x, y), font, (200, 200, 200), text_to_draw, ink_gap=1)

    def _draw_outs(self, draw, cx, center_y, game, size=3, gap=1):
        """Vertically stacked circles (top to bottom = out 1, 2, 3),
        centered on `center_y` -- filled when recorded, 1px outline
        when not. `cx` is the horizontal center to align all three on.

        `size` is the TRUE pixel width/height of each circle. NOTE:
        PIL's draw.ellipse([cx-r, cy-r, cx+r, cy+r]) actually renders
        (2*r + 1) pixels wide, not 2*r -- confirmed by direct pixel
        measurement (a "radius=2" box measured 5px wide, not 4). That
        off-by-one was why the circles looked like they were touching
        with zero gap despite the code nominally asking for a 1px gap.
        This version computes the box from the real desired `size`
        directly instead of doubling a radius, so the gap is accurate."""
        half = (size - 1) / 2
        spacing = size + gap
        total_h = size * 3 + gap * 2
        top = center_y - total_h // 2
        for i in range(3):
            cy = top + size // 2 + i * spacing
            box = [cx - half, cy - half, cx + half, cy + half]
            if i < game["outs"]:
                draw.ellipse(box, fill=self.out_fill_color)
            else:
                draw.ellipse(box, outline=self.out_empty_color, width=1)
