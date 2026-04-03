"""PIL-based video compositor for 9:16 Shorts.

Layout (1080 × 1920):
  ┌──────────────────┐
  │  POSTER  (640px) │  ← movie poster + info overlay
  ├──────────────────┤
  │  CONTENT (1280px)│  ← animated graph / intro text / verdict
  └──────────────────┘
"""

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

    def render_intro(
        self,
        title: str,
        year: str,
        poster_area: Image.Image,
        plotter_frames: "list[Path] | None" = None,
        poster_path: "Path | None" = None,
        movie_info: "dict | None" = None,
        duration: float = 4.0,
    ) -> list[np.ndarray]:
        n    = int(duration * self.fps)
        bg   = self._make_graph_bg(poster_path)
        info = movie_info or {}
        runtime = (info.get("Runtime") or "").strip()
        imdb_r  = (info.get("imdbRating") or "").strip()

        frames = []
        for i in range(n):
            content = bg.copy()

            # Animate graph building 0 → 80 % of plotter frames behind the text
            if plotter_frames:
                t         = i / max(n - 1, 1)
                frame_idx = min(int(t * len(plotter_frames) * 0.8), len(plotter_frames) - 1)
                graph_png = Image.open(str(plotter_frames[frame_idx])).convert("RGBA")
                graph_png = graph_png.resize((self.width, GRAPH_H), Image.LANCZOS)
                content_rgba = content.convert("RGBA")
                content_rgba = Image.alpha_composite(content_rgba, graph_png)
                # Extra dark veil so text stays readable
                veil = Image.new("RGBA", (self.width, GRAPH_H), (0, 0, 0, 140))
                content_rgba = Image.alpha_composite(content_rgba, veil)
                content = content_rgba.convert("RGB")

            draw = ImageDraw.Draw(content)
            cx   = self.width // 2
            cy   = GRAPH_H // 2

            # Movie title
            draw.text((cx, cy - 180), title,
                      fill=self.colors["text"], font=self._font(62), anchor="mt")

            # Year · Runtime · IMDb
            meta_parts = []
            if year:                           meta_parts.append(year)
            if runtime and runtime != "N/A":   meta_parts.append(runtime)
            if imdb_r  and imdb_r  != "N/A":   meta_parts.append(f"★ {imdb_r}")
            if meta_parts:
                draw.text((cx, cy - 90), "  ·  ".join(meta_parts),
                          fill=self.colors["dim"], font=self._font(30), anchor="mt")

            # Accent divider
            draw.line([(200, cy - 30), (self.width - 200, cy - 30)],
                      fill=self._hex(self.colors["accent"]), width=2)

            # Hook line
            draw.text((cx, cy), "How toxic is it?",
                      fill=self.colors["accent"], font=self._font(48), anchor="mt")

            frames.append(self._make_frame(content, poster_area))

        return frames

    def render_graph_segment(
        self,
        plotter_frames: list[Path],
        poster_area: Image.Image,
        poster_path: "Path | None" = None,
        duration: float = 45.0,
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
        n     = int(duration * self.fps)
        s     = summary or {}
        rating = s.get("rating", "RATED")
        hard  = s.get("total_hard", 0)
        soft  = s.get("total_soft", 0)
        f     = s.get("total_f_bombs", 0)
        peak  = s.get("peak_minute", 0)
        peak_s = s.get("peak_score", 0)

        content = Image.new("RGB", (self.width, GRAPH_H),
                            self._hex(self.colors["bg"]))
        draw = ImageDraw.Draw(content)

        draw.text((self.width // 2, GRAPH_H // 2 - 320), "THE VERDICT",
                  fill=self.colors["accent"], font=self._font(56), anchor="mt")

        stats = [
            (f"Hard Slurs:  {hard}",    self.colors["hard"]),
            (f"Soft Slurs:  {soft}",    self.colors["soft"]),
            (f"F-Bombs:     {f}",       self.colors["f_bomb"]),
            (f"Peak:        Min {peak} (score {peak_s})", self.colors["dim"]),
            ("", None),
            (rating,                    self.colors["accent"]),
        ]

        y = GRAPH_H // 2 - 200
        for text, color in stats:
            if not text:
                y += 30
                continue
            fsize = 46 if text == rating else 34
            draw.text((self.width // 2, y), text,
                      fill=color or self.colors["text"],
                      font=self._font(fsize), anchor="mt")
            y += fsize + 20

        frame = self._make_frame(content, poster_area)
        return [frame] * n

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

        intro   = self.render_intro(title, year, poster_area,
                                    plotter_frames=plotter_frames or [],
                                    poster_path=poster_path,
                                    movie_info=movie_info)
        graph   = self.render_graph_segment(plotter_frames or [], poster_area, poster_path)
        verdict = self.render_verdict(title, summary or {}, poster_area)

        segments = {"intro": intro, "graph": graph, "verdict": verdict}

        all_dir = output_dir / "concat"
        all_dir.mkdir(parents=True, exist_ok=True)
        global_idx = 0
        for seg_name in ["intro", "graph", "verdict"]:
            seg_dir = output_dir / seg_name
            seg_dir.mkdir(parents=True, exist_ok=True)
            for idx, frame in enumerate(segments[seg_name]):
                img = Image.fromarray(frame)
                img.save(seg_dir / f"{idx:05d}.png")
                img.save(all_dir / f"{global_idx:05d}.png")
                global_idx += 1

        return segments

    # ─────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────

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
