"""SRT parser + profanity analysis engine.

Takes a raw .srt file and the config dictionary, then outputs:
  • A pandas DataFrame with per-minute binned slur counts
  • A summary dict (totals, f-bomb count, peak minute, rating)
"""

import re
from pathlib import Path

import pandas as pd
import pysrt


class ProfanityEngine:
    """Categorises and counts profanity in an SRT file over time."""

    def __init__(self, config: dict):
        cats = config.get("categories", {})
        self.categories = {
            "hard": [w.lower() for w in cats.get("hard", [])],
            "soft": [w.lower() for w in cats.get("soft", [])],
            "f_bombs": [w.lower() for w in cats.get("f_bombs", [])],
        }
        self._compiled = self._compile_patterns()

    # ─────────────────────────────────────────────

    def analyse_srt(self, srt_path: str | Path) -> dict:
        """Full pipeline: parse SRT → scan → bin → summarise."""

        subs = pysrt.open(str(srt_path))
        events: list[dict] = []

        for sub in subs:
            text = sub.text.lower()
            start_sec = sub.start.ordinal / 1000.0
            matches = self._scan_line(text)
            for word, tier in matches:
                events.append(
                    {"time": start_sec, "word": word, "tier": tier}
                )

        df = pd.DataFrame(events, columns=["time", "word", "tier"])

        if df.empty:
            return _empty_summary()

        binned = self._bin_by_minute(df)
        summary = self._summarise(df, binned)

        return {
            "events": df.to_dict(orient="records"),
            "binned": binned.to_dict(orient="records"),
            "summary": summary,
        }

    # ──────────────── Internals ────────────────

    def _compile_patterns(self) -> dict[str, list[re.Pattern]]:
        """Convert word lists into compiled regexes."""
        out = {}
        for tier, words in self.categories.items():
            patterns = []
            for w in words:
                # Replace * with wildcard so "f*ck" catches variations
                escaped = re.escape(w).replace(r"\*", r"\S*")
                patterns.append(re.compile(rf"\b{escaped}\b", re.IGNORECASE))
            out[tier] = patterns
        return out

    def _scan_line(self, text: str) -> list[tuple[str, str]]:
        """Return list of (matched_word, tier) found in the line."""
        matches: list[tuple[str, str]] = []
        for tier, patterns in self._compiled.items():
            for pat in patterns:
                for m in pat.finditer(text):
                    matches.append((m.group(0), tier))
        return matches

    def _bin_by_minute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Aggregate slur counts into 1-minute windows."""
        df = df.copy()
        df["minute"] = (df["time"] / 60).astype(int)
        pivot = pd.crosstab(df["minute"], df["tier"])

        # Ensure all three tier columns exist
        for col in ["hard", "soft", "f_bombs"]:
            if col not in pivot.columns:
                pivot[col] = 0
        pivot = pivot[["hard", "soft", "f_bombs"]]
        return pivot.reset_index().rename(columns={"minute": "minute"})

    def _summarise(self, df: pd.DataFrame, binned: pd.DataFrame) -> dict:
        total_hard = int(df["tier"].eq("hard").sum())
        total_soft = int(df["tier"].eq("soft").sum())
        total_f = int(df["tier"].eq("f_bombs").sum())

        # Weighted score: hard slurs hit hardest
        tier_weights = {"hard": 5, "soft": 1, "f_bombs": 3}
        binned["score"] = sum(
            binned.get(tier, 0) * weight
            for tier, weight in tier_weights.items()
        )

        peak_idx = binned["score"].idxmax()
        peak_minute = int(binned.loc[peak_idx, "minute"])
        score_at_peak = int(binned["score"].max())
        runtime_minutes = int(df["time"].max() / 60)

        return {
            "total_hard": total_hard,
            "total_soft": total_soft,
            "total_f_bombs": total_f,
            "peak_minute": peak_minute,
            "peak_score": score_at_peak,
            "runtime_minutes": runtime_minutes,
            "total_words_counted": len(df),
            "rating": _generate_rating(total_hard, total_soft, total_f),
        }


def _generate_rating(hard: int, soft: int, f: int) -> str:
    """Generate a funny 'rating' for the movie."""
    if hard == 0 and f == 0:
        return "⭐ G-RATED ANGEL"
    if hard == 0 and f < 10:
        return "🌸 Wholesome-ish"
    if hard == 0 and f < 50:
        return "😄 Mildly Spicy"
    if hard == 0 and f >= 50:
        return "🔥 F-Bomb Olympics"
    if hard < 50:
        return "😬 EDGY"
    if hard < 200:
        return "🚨 TOXIC AF"
    return "💀 CALL THE HAZMAT TEAM"


def _empty_summary() -> dict:
    return {
        "events": [],
        "binned": [],
        "summary": {
            "total_hard": 0,
            "total_soft": 0,
            "total_f_bombs": 0,
            "peak_minute": 0,
            "peak_score": 0,
            "runtime_minutes": 0,
            "total_words_counted": 0,
            "rating": "⭐ NO DATA — MOVIE IS TOO QUIET",
        },
    }
