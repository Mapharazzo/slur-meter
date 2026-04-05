"""PIL-based video compositor for 9:16 Shorts.

Layout (1080 × 1920):
  ┌──────────────────┐
  │  POSTER  (640px) │  ← movie poster + info overlay
  ├──────────────────┤
  │  CONTENT (1280px)│  ← animated graph / intro text / verdict
  └──────────────────┘
"""

import math
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter


GRAPH_H  = 1280   # top — animated plot
POSTER_H = 640    # bottom — movie info + thumbnail


class VideoCompositor:

    def __init__(self, config: dict):
        v = config.get("video", {})
        self.width  = v.get("resolution", [1080, 1920])[0]
        self.height = v.get("resolution", [1080, 1920])[1]
        self.fps    = v.get("fps", 30)
        c = v.get("colors", {})
        self.colors = {
            "bg":     c.get("background", "#0d0d0d"),
            "text":   c.get("text",       "#ffffff"),
            "accent": c.get("accent",     "#76ff03"),
            "hard":   c.get("hard_slur",  "#ff1744"),
            "soft":   c.get("soft_slur",  "#ffea00"),
            "f_bomb": c.get("f_bomb",     "#d500f9"),
            "dim":    "#aaaaaa",
        }

    # ─────────────────────────────────────────────
    #  Poster area (top 640px) — shared across all segments
    # ─────────────────────────────────────────────

    def _load_poster(self, poster_path: "Path | None") -> "Image.Image | None":
        if poster_path and Path(poster_path).exists():
            try:
                return Image.open(str(poster_path)).convert("RGB")
            except Exception:
                pass
        return None

    def render_poster_area(
        self,
        title: str,
        year: str,
        poster_path: "Path | None" = None,
        movie_info: "dict | None" = None,
        day_number: "int | None" = None,
    ) -> Image.Image:
        """Return a 1080×640 PIL image: brand banner + movie info card."""
        img = Image.new("RGB", (self.width, POSTER_H), self._hex(self.colors["bg"]))
        draw = ImageDraw.Draw(img)

        info     = movie_info or {}
        director = (info.get("Director") or "").split(",")[0].strip()
        actors   = (info.get("Actors") or "").strip()
        imdb_r   = (info.get("imdbRating") or "").strip()
        runtime  = (info.get("Runtime") or "").strip()
        awards   = (info.get("Awards") or "").strip()

        cx      = self.width // 2
        accent  = self.colors["accent"]

        # ── Brand banner ──
        draw.text((cx, 36), "DAILY SLUR METER",
                  fill=accent, font=self._font(46), anchor="mt")
        if day_number is not None:
            draw.text((cx, 92), f"DAY  #{day_number}",
                      fill=self.colors["dim"], font=self._font(28), anchor="mt")
        draw.line([(60, 130), (self.width - 60, 130)],
                  fill=self._hex(accent), width=1)

        # ── Movie details — vertically centered in remaining space (y: 140 – 640) ──
        DETAIL_TOP = 140
        detail_h   = POSTER_H - DETAIL_TOP

        has_meta    = bool(year or (runtime and runtime != "N/A") or (imdb_r and imdb_r != "N/A"))
        has_dir     = bool(director and director != "N/A")
        has_actors  = bool(actors and actors != "N/A")
        has_awards  = bool(awards and awards not in ("N/A", ""))

        block_h = 74   # title
        if has_meta:    block_h += 50
        if has_dir:     block_h += 54
        if has_actors:  block_h += 52
        block_h += 28   # divider
        if has_awards:  block_h += 40

        y = DETAIL_TOP + max(10, (detail_h - block_h) // 2)

        # ── Title ──
        draw.text((cx, y), title,
                  fill=self.colors["text"], font=self._font(58), anchor="mt")
        y += 74

        # ── Year · Runtime · ★ IMDb ──
        meta_parts = []
        if year:                            meta_parts.append(year)
        if runtime and runtime != "N/A":    meta_parts.append(runtime)
        if imdb_r  and imdb_r  != "N/A":   meta_parts.append(f"★ {imdb_r}")
        if meta_parts:
            draw.text((cx, y), "   ·   ".join(meta_parts),
                      fill=self.colors["dim"], font=self._font(30), anchor="mt")
            y += 50

        # ── Director ──
        if has_dir:
            draw.text((cx, y), f"Directed by  {director}",
                      fill=self.colors["text"], font=self._font(34), anchor="mt")
            y += 54

        # ── Cast ──
        if has_actors:
            actor_list = [a.strip() for a in actors.split(",")][:3]
            draw.text((cx, y), "With  " + ",   ".join(actor_list),
                      fill=self.colors["dim"], font=self._font(28), anchor="mt")
            y += 52

        # ── Accent divider ──
        accent_rgb = self._hex(self.colors["accent"])
        draw.line([(80, y), (self.width - 80, y)], fill=accent_rgb, width=1)
        y += 20

        # ── Awards ──
        if has_awards:
            if len(awards) > 62:
                awards = awards[:59] + "…"
            draw.text((cx, y), awards, fill="#c8960c", font=self._font(26), anchor="mt")

        return img

    # ─────────────────────────────────────────────
    #  Content areas (bottom 1280px)
    # ─────────────────────────────────────────────

    def _make_frame(self, graph_content: Image.Image,
                    poster_area: Image.Image) -> np.ndarray:
        """Combine poster info (top) and graph (bottom) into a full 1080×1920 frame."""
        frame = Image.new("RGB", (self.width, self.height),
                          self._hex(self.colors["bg"]))
        frame.paste(poster_area, (0, 0))
        frame.paste(graph_content, (0, POSTER_H))
        # Thick accent separator bar at the boundary
        draw = ImageDraw.Draw(frame)
        bar_h = 8
        draw.rectangle(
            [(0, POSTER_H - bar_h // 2), (self.width, POSTER_H + bar_h // 2)],
            fill=self._hex(self.colors["accent"]),
        )
        return np.array(frame)

    # ─────────────────────────────────────────────
    #  Shared background helper
    # ─────────────────────────────────────────────

    def _make_graph_bg(self, poster_path: "Path | None") -> Image.Image:
        """Blurred + darkened poster background for the graph area (1080×1280)."""
        raw_poster = self._load_poster(poster_path)
        if raw_poster:
            ratio = min(self.width / raw_poster.width, GRAPH_H / raw_poster.height)
            fit_w = int(raw_poster.width  * ratio)
            fit_h = int(raw_poster.height * ratio)
            scaled = raw_poster.resize((fit_w, fit_h), Image.LANCZOS)
            bg = Image.new("RGB", (self.width, GRAPH_H), (0, 0, 0))
            bg.paste(scaled, ((self.width - fit_w) // 2, (GRAPH_H - fit_h) // 2))
            bg = bg.filter(ImageFilter.GaussianBlur(radius=14))
            bg = Image.blend(bg, Image.new("RGB", (self.width, GRAPH_H), (0, 0, 0)), alpha=0.68)
        else:
            bg = Image.new("RGB", (self.width, GRAPH_H), self._hex(self.colors["bg"]))
        return bg

    def render_intro_hold(
        self,
        title: str,
        poster_path: "Path | None",
        day_number: "int | None" = None,
        duration: float = 2.0,
    ) -> list[np.ndarray]:
        """Full-canvas poster reveal with day banner overlay. No split layout yet."""
        n = int(duration * self.fps)

        raw_poster = self._load_poster(poster_path)
        canvas_w, canvas_h = self.width, self.height

        # Build the static frame once, repeat it
        frame_img = Image.new("RGB", (canvas_w, canvas_h), self._hex(self.colors["bg"]))

        if raw_poster:
            x, y, w, h = self._fit_rect(raw_poster.width, raw_poster.height, canvas_w, canvas_h)
            fitted = raw_poster.resize((w, h), Image.LANCZOS)
            frame_img.paste(fitted, (x, y))

        # Gradient veil — dark at top (brand) and bottom (title)
        veil = self._gradient_veil(canvas_w, canvas_h)
        frame_img = Image.alpha_composite(frame_img.convert("RGBA"), veil).convert("RGB")

        draw = ImageDraw.Draw(frame_img)
        cx = canvas_w // 2
        accent = self.colors["accent"]

        # Brand at top
        draw.text((cx, 52), "DAILY SLUR METER",
                  fill=accent, font=self._font(52), anchor="mt")
        if day_number is not None:
            draw.text((cx, 116), f"DAY  #{day_number}",
                      fill=self.colors["dim"], font=self._font(30), anchor="mt")

        # Movie title near bottom
        draw.text((cx, canvas_h - 260), title,
                  fill=self.colors["text"], font=self._font(72), anchor="mt")

        frame_arr = np.array(frame_img)
        return [frame_arr] * n

    def render_intro_transition(
        self,
        poster_path: "Path | None",
        poster_area: Image.Image,
        plotter_frames: "list[Path]",
        duration: float = 2.0,
    ) -> list[np.ndarray]:
        """Animate poster from full-canvas into the top 640px banner position.

        Simultaneously: poster blurs + darkens, brand banner fades in,
        graph content fades in below.
        """
        n = int(duration * self.fps)
        raw_poster = self._load_poster(poster_path)
        graph_bg = self._make_graph_bg(poster_path)

        # First plotter frame (graph at t=0) for fade-in
        first_graph: "Image.Image | None" = None
        if plotter_frames:
            first_graph = Image.open(str(plotter_frames[0])).convert("RGBA")
            first_graph = first_graph.resize((self.width, GRAPH_H), Image.LANCZOS)

        # Source rect: poster fitted to full canvas (same as hold phase)
        if raw_poster:
            sx, sy, sw, sh = self._fit_rect(
                raw_poster.width, raw_poster.height, self.width, self.height
            )
        else:
            sx, sy, sw, sh = 0, 0, self.width, self.height

        # Destination rect: poster fitted into top 640px area
        if raw_poster:
            dx, dy, dw, dh = self._fit_rect(
                raw_poster.width, raw_poster.height, self.width, POSTER_H
            )
        else:
            dx, dy, dw, dh = 0, 0, self.width, POSTER_H

        frames = []
        for i in range(n):
            t = i / max(n - 1, 1)
            te = t * t * (3 - 2 * t)   # smoothstep

            canvas = Image.new("RGB", (self.width, self.height), self._hex(self.colors["bg"]))

            # ── Animated poster ──
            if raw_poster:
                px = int(sx + (dx - sx) * te)
                py = int(sy + (dy - sy) * te)
                pw = max(1, int(sw + (dw - sw) * te))
                ph = max(1, int(sh + (dh - sh) * te))
                poster_resized = raw_poster.resize((pw, ph), Image.LANCZOS)

                blur_r = 14 * te
                if blur_r > 0.5:
                    poster_resized = poster_resized.filter(
                        ImageFilter.GaussianBlur(radius=blur_r)
                    )
                # Darken toward graph-bg darkness (68 % black blend)
                black = Image.new("RGB", (pw, ph), (0, 0, 0))
                poster_resized = Image.blend(poster_resized, black, alpha=0.68 * te)

                canvas.paste(poster_resized, (px, py))

            # ── Graph content fades in (bottom 1280px, y=640) ──
            graph_alpha = int(te * 255)
            if graph_alpha > 0:
                content = graph_bg.copy()
                if first_graph:
                    content_rgba = content.convert("RGBA")
                    content_rgba = Image.alpha_composite(content_rgba, first_graph)
                    content = content_rgba.convert("RGB")
                content_rgba = content.convert("RGBA")
                content_rgba.putalpha(graph_alpha)
                canvas.paste(content_rgba, (0, POSTER_H), mask=content_rgba.split()[3])

            # ── Brand banner fades in (top 640px) ──
            banner_alpha = int(te * 255)
            if banner_alpha > 0:
                banner_rgba = poster_area.convert("RGBA")
                banner_rgba.putalpha(banner_alpha)
                canvas.paste(banner_rgba, (0, 0), mask=banner_rgba.split()[3])

            # ── Accent separator bar fades in ──
            if banner_alpha > 0:
                bar = Image.new("RGBA", (self.width, 8),
                                self._hex(self.colors["accent"]) + (banner_alpha,))
                bar_y = POSTER_H - 4
                canvas.paste(bar, (0, bar_y), mask=bar.split()[3])

            frames.append(np.array(canvas))

        return frames

    def render_graph_segment(
        self,
        plotter_frames: list[Path],
        poster_area: Image.Image,
        poster_path: "Path | None" = None,
        duration: float = 47.0,
    ) -> list[np.ndarray]:
        n  = int(duration * self.fps)
        bg = self._make_graph_bg(poster_path)

        if not plotter_frames:
            return [self._make_frame(bg, poster_area)] * n

        frames = []
        for i in range(n):
            content = bg.copy()
            graph_png = Image.open(str(plotter_frames[i % len(plotter_frames)])).convert("RGBA")
            graph_png = graph_png.resize((self.width, GRAPH_H), Image.LANCZOS)
            content.paste(graph_png, (0, 0), mask=graph_png.split()[3])
            frames.append(self._make_frame(content, poster_area))
        return frames

    def render_verdict(
        self,
        title: str,
        summary: dict,
        poster_area: Image.Image,
        duration: float = 9.0,
    ) -> list[np.ndarray]:
        n  = int(duration * self.fps)
        s  = summary or {}
        rating = s.get("rating", "RATED")
        hard   = s.get("total_hard", 0)
        soft   = s.get("total_soft", 0)
        f      = s.get("total_f_bombs", 0)
        peak   = s.get("peak_minute", 0)
        peak_s = s.get("peak_score", 0)

        cx = self.width // 2

        # Items: (text, color, font_size)
        # Header is drawn separately (always present); stats slam in one by one.
        HEADER_Y = GRAPH_H // 2 - 330
        stats = [
            (f"Hard Slurs:  {hard}",              self.colors["hard"],   34),
            (f"Soft Slurs:  {soft}",              self.colors["soft"],   34),
            (f"F-Bombs:     {f}",                 self.colors["f_bomb"], 34),
            (f"Peak:        Min {peak}  (score {peak_s})", self.colors["dim"], 30),
            (rating,                               self.colors["accent"], 52),
        ]

        # Compute final y positions
        y = GRAPH_H // 2 - 190
        final_ys = []
        for _, _, fsize in stats:
            final_ys.append(y)
            gap = 50 if fsize > 40 else 0   # extra gap before rating
            y += fsize + 22 + gap

        # Animation timing: each item starts slamming SLAM_EVERY frames apart
        SLAM_DUR   = 18   # frames for one item's slam animation
        SLAM_EVERY = 22   # frames between each item's start
        RATING_DELAY = 15  # extra pause before the rating slams in

        start_frames = []
        t = 8  # first item starts at frame 8
        for i, (_, _, _) in enumerate(stats):
            start_frames.append(t)
            extra = RATING_DELAY if i == len(stats) - 2 else 0
            t += SLAM_EVERY + extra

        DROP = 90  # pixels to drop from

        def slam_y(frame_idx, item_start, y_final):
            elapsed = frame_idx - item_start
            if elapsed < 0:
                return None          # not yet visible
            p = min(elapsed / SLAM_DUR, 1.0)
            if p < 0.65:            # accelerating drop (gravity feel)
                t2 = p / 0.65
                offset = DROP * (1 - t2 * t2)
            else:                   # overshoot + settle
                t2 = (p - 0.65) / 0.35
                offset = -12 * math.sin(t2 * math.pi) * (1 - t2)
            return int(y_final - offset)

        frames = []
        for frame_idx in range(n):
            content = Image.new("RGB", (self.width, GRAPH_H),
                                self._hex(self.colors["bg"]))
            draw = ImageDraw.Draw(content)

            # Header — always visible
            draw.text((cx, HEADER_Y), "THE VERDICT",
                      fill=self.colors["accent"], font=self._font(56), anchor="mt")

            # Stats — slam in one by one
            for i, (text, color, fsize) in enumerate(stats):
                y_pos = slam_y(frame_idx, start_frames[i], final_ys[i])
                if y_pos is None:
                    continue
                self._draw_with_emoji(draw, (cx, y_pos), text,
                                      fill=color, size=fsize, anchor="mt")

            frames.append(self._make_frame(content, poster_area))

        return frames

    # ─────────────────────────────────────────────
    #  Verdict timing (for SFX sync)
    # ─────────────────────────────────────────────

    @staticmethod
    def verdict_impact_times(
        fps: int = 30,
        verdict_start_offset: float = 0.0,
    ) -> list[dict]:
        """Return the timestamp of each verdict stat's 'slam' impact.

        Each entry: {"index": int, "label": str, "impact_frame": int, "impact_time": float}
        *verdict_start_offset* is the time (seconds) when the verdict segment
        begins within the full video — used to get absolute timestamps.
        """
        SLAM_DUR = 18
        SLAM_EVERY = 22
        RATING_DELAY = 15

        labels = ["hard_slurs", "soft_slurs", "f_bombs", "peak", "rating"]

        start_frames: list[int] = []
        t = 8
        for i in range(len(labels)):
            start_frames.append(t)
            extra = RATING_DELAY if i == len(labels) - 2 else 0
            t += SLAM_EVERY + extra

        impacts = []
        for i, label in enumerate(labels):
            # Impact = end of the gravity-drop phase (p=0.65)
            impact_frame = start_frames[i] + int(SLAM_DUR * 0.65)
            impact_time = verdict_start_offset + impact_frame / fps
            impacts.append({
                "index": i,
                "label": label,
                "impact_frame": impact_frame,
                "impact_time": impact_time,
            })
        return impacts

    # ─────────────────────────────────────────────
    #  Master render
    # ─────────────────────────────────────────────

    def render_all(
        self,
        output_dir: "str | Path",
        title: str = "Unknown",
        year: str = "",
        plotter_frames: "list[Path] | None" = None,
        summary: "dict | None" = None,
        poster_path: "Path | None" = None,
        movie_info: "dict | None" = None,
        day_number: "int | None" = None,
    ) -> dict:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        poster_area = self.render_poster_area(title, year, poster_path, movie_info, day_number)

        intro_hold       = self.render_intro_hold(title, poster_path, day_number)
        intro_transition = self.render_intro_transition(
            poster_path, poster_area, plotter_frames or []
        )
        graph   = self.render_graph_segment(plotter_frames or [], poster_area, poster_path)
        verdict = self.render_verdict(title, summary or {}, poster_area)

        segments = {
            "intro_hold":       intro_hold,
            "intro_transition": intro_transition,
            "graph":            graph,
            "verdict":          verdict,
        }

        all_dir = output_dir / "concat"
        all_dir.mkdir(parents=True, exist_ok=True)
        global_idx = 0
        segment_timing: dict[str, dict] = {}
        for seg_name in ["intro_hold", "intro_transition", "graph", "verdict"]:
            seg_dir = output_dir / seg_name
            seg_dir.mkdir(parents=True, exist_ok=True)
            start_frame = global_idx
            for idx, frame in enumerate(segments[seg_name]):
                img = Image.fromarray(frame)
                img.save(seg_dir / f"{idx:05d}.png")
                img.save(all_dir / f"{global_idx:05d}.png")
                global_idx += 1
            segment_timing[seg_name] = {
                "start_frame": start_frame,
                "end_frame": global_idx - 1,
                "start_time": start_frame / self.fps,
                "end_time": global_idx / self.fps,
                "num_frames": global_idx - start_frame,
            }

        return {
            "segments": segments,
            "timing": segment_timing,
            "total_frames": global_idx,
            "total_duration": global_idx / self.fps,
        }

    # ─────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────

    # Regex that matches a single leading emoji (including ZWJ sequences / variation selectors)
    _EMOJI_RE = re.compile(
        r'^([\U00010000-\U0010ffff][\ufe0f\u20d0-\u20ff]?(?:\u200d[\U00010000-\U0010ffff][\ufe0f\u20d0-\u20ff]?)*'
        r'|[\u2600-\u27bf][\ufe0f]?)'
    )
    _EMOJI_NATIVE_SIZE = 109   # NotoColorEmoji is a bitmap font — only loads at this size
    _EMOJI_FONT_PATH   = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
    _emoji_font_cache: "ImageFont.FreeTypeFont | None | bool" = False  # False = unchecked

    def _get_emoji_font(self) -> "ImageFont.FreeTypeFont | None":
        if self._emoji_font_cache is False:
            try:
                VideoCompositor._emoji_font_cache = ImageFont.truetype(
                    self._EMOJI_FONT_PATH, self._EMOJI_NATIVE_SIZE
                )
            except (IOError, OSError):
                VideoCompositor._emoji_font_cache = None
        return self._emoji_font_cache  # type: ignore[return-value]

    def _render_emoji(self, emoji_str: str, target_size: int) -> "Image.Image | None":
        """Render a single emoji at native size, scale to target_size, return RGBA patch."""
        efont = self._get_emoji_font()
        if efont is None:
            return None
        native = self._EMOJI_NATIVE_SIZE
        scratch = Image.new("RGBA", (native + 20, native + 20), (0, 0, 0, 0))
        ImageDraw.Draw(scratch).text((0, 0), emoji_str, font=efont, embedded_color=True)
        # Crop to tight bounding box
        bbox = scratch.getbbox()
        if not bbox:
            return None
        scratch = scratch.crop(bbox)
        # Scale so height matches target_size
        scale  = target_size / scratch.height
        new_w  = max(1, int(scratch.width * scale))
        return scratch.resize((new_w, target_size), Image.LANCZOS)

    def _draw_with_emoji(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        fill: str,
        size: int,
        anchor: str = "mt",
    ) -> None:
        """Draw text, rendering a leading emoji with NotoColorEmoji when present.

        NotoColorEmoji is bitmap-only so we render at native size, scale, and paste.
        The caller's draw object must belong to an RGBA or RGB image stored on
        draw._image — we access it to composite the emoji patch.
        """
        m = self._EMOJI_RE.match(text)
        if not m:
            draw.text(xy, text, fill=fill, font=self._font(size), anchor=anchor)
            return

        emoji_str  = m.group(0)
        rest       = text[m.end():].lstrip()
        emoji_img  = self._render_emoji(emoji_str, size)
        tfont      = self._font(size)

        if emoji_img is None:
            # Emoji font unavailable — draw text only
            draw.text(xy, rest or text, fill=fill, font=tfont, anchor=anchor)
            return

        e_w = emoji_img.width
        t_w = int(draw.textlength(rest, font=tfont)) if rest else 0
        gap = int(size * 0.15)
        total = e_w + (gap + t_w if rest else 0)

        x, y = xy
        x_start = (x - total // 2) if anchor == "mt" else x

        # Paste emoji onto the underlying image
        canvas: Image.Image = draw._image  # type: ignore[attr-defined]
        canvas.paste(emoji_img, (int(x_start), int(y)), mask=emoji_img.split()[3])

        if rest:
            draw.text((x_start + e_w + gap, y), rest, fill=fill, font=tfont)

    @staticmethod
    def _fit_rect(
        img_w: int, img_h: int, canvas_w: int, canvas_h: int
    ) -> tuple[int, int, int, int]:
        """Return (x, y, w, h) for image fitted inside canvas, centered."""
        ratio = min(canvas_w / img_w, canvas_h / img_h)
        w = int(img_w * ratio)
        h = int(img_h * ratio)
        x = (canvas_w - w) // 2
        y = (canvas_h - h) // 2
        return x, y, w, h

    def _gradient_veil(self, width: int, height: int) -> Image.Image:
        """RGBA gradient veil: dark band at top (~200px) and bottom (~400px)."""
        veil = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pixels = veil.load()

        top_band = 200      # px — covers brand text area
        bottom_band = 400   # px — covers movie title area

        for y in range(height):
            if y < top_band:
                alpha = int(180 * (1 - y / top_band))
            elif y >= height - bottom_band:
                progress = (y - (height - bottom_band)) / bottom_band
                alpha = int(210 * progress)
            else:
                alpha = 0
            for x in range(width):
                pixels[x, y] = (0, 0, 0, alpha)

        return veil

    def _font(self, size: int = 32) -> ImageFont.FreeTypeFont:
        for path in [
            "assets/fonts/Montserrat-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]:
            try:
                return ImageFont.truetype(path, size)
            except IOError:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _hex(h: str) -> tuple:
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
