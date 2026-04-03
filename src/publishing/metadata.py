"""Auto-generate title, description, tags, and hashtags for Shorts."""

import random

TITLES = [
    "The 📉 Daily Slur Meter — {title} is… {emoji}",
    "We counted every slur in {title}… the result? 🤯",
    "Is {title} the most toxic movie ever? 📉",
    "{title} — Slur Meter results will shock you 😬",
    "The Slur Meter just broke for {title} 💀",
    "How many times did {title} say the F-word? 💣",
    "Rating {title} on toxicity… it's bad 📉",
]

VERDICTS = {
    "clean": "⭐️ This movie is CLEAN!",
    "mild": "😄 Mildly spicy — nothing major!",
    "edgy": "😬 Edgy — you've been warned!",
    "tough": "🔥 TOUGH — bring a bib!",
    "toxic": "🚨 TOXIC — the Slur Meter exploded",
    "hazmat": "💀 HAZMAT — someone call the cleanup crew!",
}


def generate_metadata(title: str, summary: dict) -> dict:
    """Produce YouTube/TikTok metadata for a single video."""

    hard = summary.get("total_hard", 0)
    f = summary.get("total_f_bombs", 0)

    if hard == 0 and f == 0:
        tier = "clean"
    elif f < 10:
        tier = "mild"
    elif f < 50:
        tier = "edgy"
    elif hard < 50:
        tier = "tough"
    elif hard < 200:
        tier = "toxic"
    else:
        tier = "hazmat"

    emoji_map = {"clean": "⭐", "mild": "😄", "edgy": "😬",
                 "tough": "🔥", "toxic": "🚨", "hazmat": "💀"}

    video_title = random.choice(TITLES).format(
        title=title, emoji=emoji_map[tier]
    )

    description = (
        f"The Daily Slur Meter analysed {title}!\n\n"
        f"🔴 Hard Slurs: {hard}\n"
        f"💣 F-Bombs: {f}\n"
        f"🏆 Rating: {summary.get('rating', 'N/A')}\n\n"
        f"What movie should we rate next? Comment below! 👇\n\n"
    )

    tags = [
        "daily slur meter", "movie stats", "slur count",
        title.lower(), "movie analysis", "data viz",
        "shorts", "movie rating", "profanity", "toxic movies",
    ]

    hashtags = [
        "#Shorts", "#MovieStats", "#DailySlurMeter",
        f"#{title.replace(' ', '')}", "#DataViz",
    ]

    # Add tier hashtag
    hashtags.append(f"#{tier.upper()}" if tier != "clean" else "#Clean")

    return {
        "video_title": video_title,
        "description": description,
        "tags": tags,
        "hashtags": hashtags,
        "tier": tier,
        "verdict": VERDICTS[tier],
    }
