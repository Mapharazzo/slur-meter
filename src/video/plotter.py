"""Generate an animated 'Rage Graph' image sequence.

Strategy: render the complete graph once with matplotlib (all data, no progress
line), then stamp a PIL vertical line + time readout onto copies of that base
image for each frame.  This guarantees every frame has a pixel-identical graph —
only the progress indicator moves.

Portrait format: 540×640 px per frame (scaled to 1080×1280 by the compositor).
"""

from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from scipy.interpolate import make_interp_spline

matplotlib.use("Agg")
import warnings

warnings.filterwarnings("ignore", message="Glyph.*missing from font")


class RagePlotter:
    """Creates animated portrait line chart frames of slur intensity over time."""

    # Output size of each plotter frame
    W, H = 540, 640

    def __init__(self, config: dict):
        c = config.get("video", {}).get("colors", {})
        self.colors = {
            "hard":    c.get("hard_slur",  "#ff1744"),
            "soft":    c.get("soft_slur",  "#ffea00"),
            "f_bombs": c.get("f_bomb",     "#d500f9"),
            "bg":      c.get("background", "#0d0d0d"),
            "text":    c.get("text",       "#ffffff"),
            "accent":  c.get("accent",     "#76ff03"),
        }

    # ─────────────────────────────────────────────────────
    #  Public API
    # ─────────────────────────────────────────────────────

    def generate_frames(
        self,
        binned: list[dict],
        output_dir: str | Path,
        n_frames: int = 450,
        runtime_min: "float | None" = None,
    ) -> list[Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        base, max_x = self._render_base(binned, output_dir, runtime_min=runtime_min)
        if base is None:
            return self._blank_frames(output_dir, n_frames)

        display_max = runtime_min or max_x
        frames = []
        for i in range(n_frames):
            progress = i / max(n_frames - 1, 1)
            path = self._stamp_progress(base, max_x, display_max, progress,
                                        output_dir / f"frame_{i:05d}.png")
            frames.append(path)
        return frames

    def generate_specific_frames(
        self,
        binned: list[dict],
        output_dir: str | Path,
        frame_indices: list[int],
        total_frames: int = 450,
        runtime_min: "float | None" = None,
    ) -> list[Path]:
        """Render only specific frame indices as if from a total_frames sequence."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        base, max_x = self._render_base(binned, output_dir, runtime_min=runtime_min)
        if base is None:
            return self._blank_frames(output_dir, len(frame_indices))

        display_max = runtime_min or max_x
        paths = []
        for i in frame_indices:
            progress = i / max(total_frames - 1, 1)
            path = self._stamp_progress(base, max_x, display_max, progress,
                                        output_dir / f"frame_{i:05d}.png")
            paths.append(path)
        return paths

    # ─────────────────────────────────────────────────────
    #  Internals
    # ─────────────────────────────────────────────────────

    def _render_base(
        self, binned: list[dict], output_dir: Path,
        runtime_min: "float | None" = None,
    ) -> "tuple[Image.Image | None, float]":
        """Render the complete graph (all data, no progress line) → PIL Image."""
        if not binned:
            return None, 0.0

        df = pd.DataFrame(binned)
        if "minute" not in df.columns or df.empty:
            return None, 0.0

        x      = df["minute"].values.astype(float)
        y_hard = df.get("hard",    pd.Series(0, index=df.index)).astype(float).values
        y_soft = df.get("soft",    pd.Series(0, index=df.index)).astype(float).values
        y_f    = df.get("f_bombs", pd.Series(0, index=df.index)).astype(float).values

        cum_hard = np.cumsum(y_hard)
        cum_soft = np.cumsum(y_soft)
        cum_f    = np.cumsum(y_f)

        max_x = float(x.max()) if len(x) > 0 else 60.0
        axis_max = max(runtime_min or 0.0, max_x)
        max_y = max(cum_hard[-1], cum_soft[-1], cum_f[-1], 1.0) * 1.2

        # Smooth interpolation onto dense grid
        n_pts = max(len(x), 2)
        x_dense = np.linspace(x[0], x[-1], max(n_pts * 20, 400))

        def _smooth(xp, yp):
            # Deduplicate x (keep last value per minute)
            _, idx = np.unique(xp, return_index=True)
            xp, yp = xp[idx], yp[idx]
            if len(xp) < 4:
                return xp, yp   # too sparse for cubic — just use raw
            k = min(3, len(xp) - 1)
            spl = make_interp_spline(xp, yp, k=k)
            ys = spl(x_dense)
            # Clamp: cumulative must stay non-negative and non-decreasing
            ys = np.maximum(ys, 0)
            ys = np.maximum.accumulate(ys)
            return x_dense, ys

        plt.style.use("dark_background")
        fig, ax = plt.subplots(figsize=(self.W / 100, self.H / 100), dpi=100)
        fig.patch.set_alpha(0)
        ax.set_facecolor((0, 0, 0, 0.45))

        # Glow layers: (linewidth, alpha) — wide+faint → narrow+bright
        glow_layers = [
            (22, 0.02),
            (16, 0.04),
            (11, 0.07),
            (7,  0.12),
            (4,  0.22),
            (2.5, 0.50),
            (1.4, 0.95),
        ]

        for color, data, label in [
            (self.colors["hard"],    cum_hard, "Hard Slurs"),
            (self.colors["soft"],    cum_soft, "Soft Slurs"),
            (self.colors["f_bombs"], cum_f,    "F-Bombs"),
        ]:
            xs, ys = _smooth(x, data)
            for lw, alpha in glow_layers[:-1]:
                ax.plot(xs, ys, color=color, linewidth=lw, alpha=alpha)
            lw, alpha = glow_layers[-1]
            ax.plot(xs, ys, color=color, linewidth=lw, alpha=alpha, label=label)

        ax.set_xlim(0, axis_max)
        ax.set_ylim(0, max_y)
        ax.axis("off")

        fig.subplots_adjust(left=0.02, right=0.98, top=0.82, bottom=0.14)

        base_path = output_dir / "_base.png"
        fig.savefig(base_path, dpi=100, transparent=True)
        plt.close(fig)

        return Image.open(base_path).convert("RGBA"), max_x

    def _stamp_progress(
        self,
        base: Image.Image,
        max_x: float,
        display_max: float,
        progress: float,
        out_path: Path,
    ) -> Path:
        """Copy base image and draw progress line + time label at the right position."""
        img = base.copy()

        # Map data x → pixel x
        left_frac  = 0.02
        right_frac = 0.98
        plot_x0 = int(self.W * left_frac)
        plot_x1 = int(self.W * right_frac)
        px = int(plot_x0 + progress * (plot_x1 - plot_x0))
        px = max(plot_x0, min(plot_x1, px))

        # Hide future data (sharp clear — no visible edge)
        if px < self.W - 1:
            img.paste(Image.new("RGBA", (self.W - px, self.H), (0, 0, 0, 0)), (px, 0))

        # ── All PIL overlays rendered at 2× then scaled down for anti-aliasing ──
        S = 8  # noqa: N806  # supersampling factor for anti-aliasing
        ow, oh = self.W * S, self.H * S
        overlay = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
        odraw   = ImageDraw.Draw(overlay)

        accent = self._hex(self.colors["accent"])
        apx    = px * S

        # Dashed vertical progress line
        top_frac    = 0.80
        bottom_frac = 0.16
        y_top    = int(oh * (1 - top_frac))
        y_bottom = int(oh * (1 - bottom_frac))
        dash, gap_d = 12, 8
        y = y_top
        while y < y_bottom:
            odraw.line([(apx, y), (apx, min(y + dash, y_bottom))],
                       fill=accent + (90,), width=4)
            y += dash + gap_d

        # Load fonts at 2× size
        font_paths = [
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        def _font2x(size):
            for p in font_paths:
                try:
                    return ImageFont.truetype(p, size * S)
                except OSError:
                    continue
            return ImageFont.load_default()

        # White pill timer — bottom center
        cutoff_min = progress * display_max
        label  = f"{cutoff_min:.0f} / {display_max:.0f} min"
        pfont  = _font2x(18)
        tw     = int(odraw.textlength(label, font=pfont))
        th     = 18 * S
        pad_x, pad_y = 18 * S, 8 * S
        pill_w = tw + pad_x * 2
        pill_h = th + pad_y * 2
        pill_x = (ow - pill_w) // 2
        pill_y = int(oh * 0.865)
        odraw.rounded_rectangle(
            [(pill_x, pill_y), (pill_x + pill_w, pill_y + pill_h)],
            radius=pill_h // 2,
            fill=(255, 255, 255, 230),
        )
        odraw.text(
            (pill_x + pad_x, pill_y + pad_y),
            label,
            fill=(20, 20, 20, 255),
            font=pfont,
        )

        # Legend — top center
        lfont   = _font2x(14)
        line_w  = 18 * S
        gap_leg = 14 * S
        legend_items = [
            ("Hard Slurs", self._hex(self.colors["hard"])),
            ("Soft Slurs", self._hex(self.colors["soft"])),
            ("F-Bombs",    self._hex(self.colors["f_bombs"])),
        ]
        spacing     = 20 * S
        item_widths = [line_w + gap_leg + int(odraw.textlength(lbl, font=lfont))
                       for lbl, _ in legend_items]
        total_w = sum(item_widths) + spacing * (len(legend_items) - 1)
        lx = (ow - total_w) // 2
        ly = 10 * S

        for (lbl, rgb), iw in zip(legend_items, item_widths, strict=False):
            c    = rgb + (220,)
            my   = ly + 7 * S
            odraw.line([(lx, my), (lx + line_w, my)], fill=c, width=3 * S)
            odraw.text((lx + line_w + gap_leg, ly), lbl,
                       fill=(200, 200, 200, 200), font=lfont, anchor="lt")
            lx += iw + spacing

        # Scale overlay down → anti-aliased
        overlay_small = overlay.resize((self.W, self.H), Image.LANCZOS)
        img = Image.alpha_composite(img, overlay_small)

        img.save(out_path)
        return out_path

    def _blank_frames(self, output_dir: Path, n: int) -> list[Path]:
        base = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(base)
        draw.text((self.W // 2, self.H // 2), "No subtitle data",
                  fill=(255, 255, 255, 180), anchor="mm")
        frames = []
        for i in range(n):
            path = output_dir / f"frame_{i:05d}.png"
            base.save(path)
            frames.append(path)
        return frames

    @staticmethod
    def _hex(h: str) -> tuple:
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
