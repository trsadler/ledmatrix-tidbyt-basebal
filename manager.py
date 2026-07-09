"""
Tidbyt-Style Baseball Scoreboard plugin for ChuckBuilds/LEDMatrix.

Layout:
  - Left half: two stacked team-color blocks (away on top, home on
    bottom). Each block shows, left to right: team logo, abbreviation,
    score.
  - Right half (black background):
      - upper-left:  inning indicator (up/down arrow + inning number)
      - upper-right: diamond of bases (lit when occupied)
      - lower-left:  ball-strike count
      - lower-right: outs indicator (small squares)

Data comes from ESPN's public scoreboard API, the same one used by
most community MLB scoreboard projects:
    https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard

NOTE ON display_manager: different LEDMatrix versions have exposed the
PIL image slightly differently over time. This plugin builds its own
RGB PIL.Image internally and then tries, in order:
    1. display_manager.image.paste(...) + display_manager.update_display()
    2. display_manager.set_image(...)
If neither exists, _push_image() raises a clear error telling you what
attribute to wire up -- check your installed BasePlugin/DisplayManager
version (the same class your own from-scratch scoreboard project
already draws to) and adjust _push_image() accordingly.
"""

import logging
import os
import time
from io import BytesIO
from typing import Optional, Dict, Any, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    from src.plugin_system.base_plugin import BasePlugin
except ImportError:
    # Fallback for local/dev-preview testing outside the full package tree.
    class BasePlugin:  # type: ignore
        def __init__(self, plugin_id, config, display_manager, cache_manager, plugin_manager):
            self.plugin_id = plugin_id
            self.config = config
            self.display_manager = display_manager
            self.cache_manager = cache_manager
            self.plugin_manager = plugin_manager
            self.logger = logging.getLogger(plugin_id)


ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"

# Fallback primary colors if use_team_colors is off or ESPN color is missing.
DEFAULT_AWAY_COLOR = (0, 142, 226)
DEFAULT_HOME_COLOR = (200, 16, 46)


class TidbytBaseballPlugin(BasePlugin):
    def __init__(self, plugin_id, config, display_manager, cache_manager, plugin_manager):
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        self.logger = logging.getLogger(f"plugin.{plugin_id}")
        self._derive_settings()

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "LEDMatrix-TidbytBaseball/1.0"})

        self.current_game: Optional[Dict[str, Any]] = None
        self.last_fetch_time: float = 0.0

        # abbreviation -> resized RGBA PIL.Image (or None if unavailable),
        # kept in memory so display() never blocks on network access.
        self._logo_cache: Dict[str, Optional[Image.Image]] = {}

        self.font_team = self._load_font(10, bold=True)
        self.font_score = self._load_font(10, bold=True)
        self.font_small = self._load_font(9)
        self.font_tiny = self._load_font(7)

    # ------------------------------------------------------------------
    # Config handling
    # ------------------------------------------------------------------
    def _derive_settings(self):
        cfg = self.config or {}
        self.favorite_teams = [t.upper() for t in cfg.get("favorite_teams", ["PHI"])]
        self.update_interval = cfg.get("update_interval_seconds", 300)
        self.live_update_interval = cfg.get("live_update_interval_seconds", 15)
        self.display_duration = cfg.get("display_duration", 20)
        self.away_color_fallback = tuple(cfg.get("away_color", DEFAULT_AWAY_COLOR))
        self.home_color_fallback = tuple(cfg.get("home_color", DEFAULT_HOME_COLOR))
        self.use_team_colors = cfg.get("use_team_colors", True)
        self.show_logos = cfg.get("show_logos", True)
        self.logo_dir = cfg.get("logo_dir", "assets/sports/mlb_logos")
        self.test_mode = cfg.get("test_mode", False)

    def on_config_change(self, new_config):
        """Called by the core when config.json changes for this plugin."""
        self.config = new_config
        self._derive_settings()
        self.last_fetch_time = 0  # force a re-fetch on the next update()

    def validate_config(self) -> bool:
        if not self.favorite_teams:
            self.logger.error("No favorite_teams configured")
            return False
        return True

    def _load_font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        # Reuses whatever bitmap/TTF fonts ship with the core project if
        # present; falls back to common system fonts for local preview
        # testing, and finally PIL's default bitmap font.
        candidates = [
            "assets/fonts/PressStart2P.ttf",
            "assets/fonts/4x6-font.ttf",
            "assets/fonts/PressStart2P-Regular.ttf",
        ]
        if bold:
            candidates += [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
            ]
        else:
            candidates += [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def update(self):
        now = time.time()
        interval = self.live_update_interval if self._is_live(self.current_game) else self.update_interval
        if now - self.last_fetch_time < interval and self.current_game is not None:
            return

        self.last_fetch_time = now

        if self.test_mode:
            self.current_game = self._fake_game()
            self._resolve_logos(self.current_game)
            return

        try:
            resp = self.session.get(ESPN_SCOREBOARD_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self.logger.error(f"Failed to fetch MLB scoreboard: {e}", exc_info=True)
            return

        game = self._find_favorite_game(data)
        if game is not None:
            self.current_game = game
            self._resolve_logos(game)

    def _find_favorite_game(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        events = data.get("events", [])
        # Prefer a live game among favorite teams; otherwise take the
        # soonest scheduled/most recent one.
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

        for event, comp in candidates:
            status = comp.get("status", {}).get("type", {}).get("state")
            if status == "in":
                return self._parse_game(event, comp)

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

        return {
            "state": status_type.get("state", "pre"),  # pre | in | post
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
            "on_first": bool(situation.get("onFirst")),
            "on_second": bool(situation.get("onSecond")),
            "on_third": bool(situation.get("onThird")),
        }

    def _is_live(self, game: Optional[Dict[str, Any]]) -> bool:
        return bool(game) and game.get("state") == "in"

    def _fake_game(self) -> Dict[str, Any]:
        return {
            "state": "in",
            "away_abbr": "PHI",
            "home_abbr": "ATL",
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
            "on_first": True,
            "on_second": False,
            "on_third": True,
        }

    # ------------------------------------------------------------------
    # Logos
    # ------------------------------------------------------------------
    def _resolve_logos(self, game: Dict[str, Any], size: Optional[int] = None):
        """Downloads and resizes each team's logo once, then reuses it
        from the in-memory cache on every later call/frame."""
        if not self.show_logos:
            return
        if size is None:
            _, height = self._get_dimensions()
            row_h = height // 2
            size = max(row_h - 4, 6)  # leave a little vertical padding
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
            self.logger.info(
                f"No logo found for {abbr} (checked {self.logo_dir}/{abbr}.png and ESPN URL); "
                f"showing abbreviation only."
            )

        self._logo_cache[cache_key] = logo
        return logo

    def _load_local_logo(self, abbr: str, size: int) -> Optional[Image.Image]:
        """Looks for a bundled logo shipped with the core LEDMatrix repo,
        e.g. assets/sports/mlb_logos/PHI.png. Checks a couple of common
        naming/extension variants since asset sets aren't always
        consistent about case or extension."""
        candidates = [
            f"{abbr}.png",
            f"{abbr.lower()}.png",
            f"{abbr}.PNG",
        ]
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
        width, height = self._get_dimensions()
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)

        if self.current_game is None:
            draw.text((4, height // 2 - 4), "No Game", font=self.font_small, fill=(180, 180, 180))
            self._push_image(image, force_clear)
            return

        game = self.current_game
        left_w = width // 2
        top_h = height // 2

        # --- Left half: two stacked team-color rows: logo | abbrev | score ---
        draw.rectangle([0, 0, left_w - 1, top_h - 1], fill=game["away_color"])
        draw.rectangle([0, top_h, left_w - 1, height - 1], fill=game["home_color"])

        away_txt_color = self._text_color_for(game["away_color"])
        home_txt_color = self._text_color_for(game["home_color"])

        self._draw_team_row(
            image, draw, 0, 0, left_w, top_h,
            game["away_abbr"], game["away_score"], game.get("away_logo"), away_txt_color,
        )
        self._draw_team_row(
            image, draw, 0, top_h, left_w, height - top_h,
            game["home_abbr"], game["home_score"], game.get("home_logo"), home_txt_color,
        )

        # --- Right half (black): inning upper-left, diamond centered,
        #     count lower-left, outs lower-right ---
        right_x0 = left_w + 2
        right_w = width - right_x0 - 1

        self._draw_inning(draw, right_x0 + 1, 1, game)

        diamond_w = int(right_w * 0.5)
        diamond_h = int(height * 0.62)
        diamond_x = right_x0 + (right_w - diamond_w) // 2
        diamond_y = (height - diamond_h) // 2 - 2
        self._draw_diamond(draw, diamond_x, diamond_y, diamond_w, diamond_h, game, scale=0.78)

        lower_y = height - 8
        self._draw_count(draw, right_x0 + 1, lower_y, game)
        self._draw_outs(draw, right_x0, lower_y, right_w, game)

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
        # Sensible default: 2x 64x32 panels chained horizontally.
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

    def _draw_team_row(self, image, draw, x0, y0, w, h, abbr, score, logo, text_color):
        """logo on the far left (vertically centered), team abbreviation
        vertically centered next to it, and the score right-aligned at
        the far edge of the block."""
        cursor_x = x0 + 2

        if logo is not None:
            logo_y = y0 + max((h - logo.height) // 2, 0)
            image.paste(logo, (cursor_x, logo_y), logo)
            cursor_x += logo.width + 4

        abbr_bbox = draw.textbbox((0, 0), abbr, font=self.font_team)
        abbr_h = abbr_bbox[3] - abbr_bbox[1]
        abbr_y = y0 + (h - abbr_h) // 2 - abbr_bbox[1]
        draw.text((cursor_x, abbr_y), abbr, font=self.font_team, fill=text_color)

        score_text = str(score)
        score_bbox = draw.textbbox((0, 0), score_text, font=self.font_score)
        score_w = score_bbox[2] - score_bbox[0]
        score_h = score_bbox[3] - score_bbox[1]
        score_x = x0 + w - score_w - 3
        score_y = y0 + (h - score_h) // 2 - score_bbox[1]
        draw.text((score_x, score_y), score_text, font=self.font_score, fill=text_color)

    def _draw_diamond(self, draw, x, y, w, h, game, scale=0.8):
        """Draws 3 diamonds (2nd top-center, 3rd/1st below on either side)
        lit up white when occupied, outlined grey when empty."""
        cx = x + w // 2
        size = int(min(w // 2, h) * scale)
        half = max(size // 2, 3)
        empty = (95, 95, 95)
        lit = (255, 255, 255)

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
            color = lit if occupied[base] else empty
            pts = [
                (px, py - half),
                (px + half, py),
                (px, py + half),
                (px - half, py),
            ]
            if occupied[base]:
                draw.polygon(pts, fill=color)
            else:
                draw.polygon(pts, outline=color)

    def _draw_inning(self, draw, x, y, game):
        arrow = "\u25b2" if game["inning_half"] else "\u25bc"  # ▲ top / ▼ bottom
        text = f"{arrow}{game['inning']}"
        draw.text((x, y), text, font=self.font_small, fill=(255, 255, 255))

    def _draw_count(self, draw, x, y, game):
        count_text = f"{game['balls']}-{game['strikes']}"
        draw.text((x, y), count_text, font=self.font_tiny, fill=(255, 200, 0))

    def _draw_outs(self, draw, x, y, w, game):
        # Outs: up to 3 small squares, right-aligned with a margin from
        # the panel edge, filled = recorded out.
        square = 3
        gap = 2
        edge_margin = 3
        base_x = x + w - edge_margin - (square + gap) * 3 + gap
        for i in range(3):
            sx = base_x + i * (square + gap)
            box = [sx, y + 1, sx + square, y + 1 + square]
            if i < game["outs"]:
                draw.rectangle(box, fill=(255, 140, 0))
            else:
                draw.rectangle(box, outline=(120, 120, 120))
