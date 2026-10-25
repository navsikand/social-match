"""Interest canonicalisation: raw student interests -> the closed TAG_VOCAB.

The payload accepts arbitrary short interests ("matcha", "sourdough", "synthwave").
Scoring must stay comparable between people, so the *matcher* only ever sees
canonical vocabulary entries. This module does the boiling down:

    "matcha latte"  -> "coffee"
    "bouldering"    -> "climbing"
    "valorant"      -> "video_games"
    "mycology"      -> None      (kept for the student, never scored, never on a card)

Why it exists: a student should not be told "the matcher doesn't have a tag for that".
They say what they like; we translate. Unknown interests are not an error — they are
simply not comparable, so they do not affect the score.

Privacy note: canonical tags are what appear on a card. Raw interests stay attached to
the student who typed them and are returned to them, but are never shown to anyone else.
"""
from server import encoder
from server.schema import TAG_VOCAB

_CANON = frozenset(TAG_VOCAB)

# Multi-word entries are checked before single tokens. Values MUST be TAG_VOCAB members.
SYNONYMS: dict[str, str] = {
    # drink
    "matcha": "coffee",
    "matcha latte": "coffee",
    "latte": "coffee",
    "espresso": "coffee",
    "cafe": "coffee",
    "coffee shop": "coffee",
    "tea": "coffee",
    "boba": "coffee",
    # climbing / gym
    "bouldering": "climbing",
    "rock climbing": "climbing",
    "climbing gym": "climbing",
    "crag": "climbing",
    "lifting": "gym",
    "weightlifting": "gym",
    "weights": "gym",
    "fitness": "gym",
    "workout": "gym",
    "gym rat": "gym",
    # cardio / outdoors
    "jogging": "running",
    "track": "running",
    "trail running": "running",
    "trekking": "hiking",
    "trails": "hiking",
    "backpacking": "hiking",
    "biking": "cycling",
    "bike": "cycling",
    "cycling ": "cycling",
    "road cycling": "cycling",
    "mtb": "cycling",
    "mountain biking": "cycling",
    "swim": "swimming",
    # sport
    "hoops": "basketball",
    "pickup basketball": "basketball",
    "football": "soccer",
    "futbol": "soccer",
    "pickup soccer": "soccer",
    "frisbee": "ultimate_frisbee",
    "ultimate": "ultimate_frisbee",
    # games
    "board games": "board_games",
    "boardgames": "board_games",
    "boardgame": "board_games",
    "tabletop": "board_games",
    "dnd": "board_games",
    "d&d": "board_games",
    "dungeons and dragons": "board_games",
    "video games": "video_games",
    "videogames": "video_games",
    "gaming": "video_games",
    "valorant": "video_games",
    "minecraft": "video_games",
    "league of legends": "video_games",
    "overwatch": "video_games",
    "smash": "video_games",
    "stardew": "video_games",
    # art
    "watercolor": "painting",
    "watercolors": "painting",
    "oil painting": "painting",
    "sketching": "drawing",
    "sketch": "drawing",
    "illustration": "drawing",
    "photos": "photography",
    "film photography": "photography",
    "photowalk": "photography",
    "ceramics": "pottery",
    "pottery wheel": "pottery",
    # music
    "acoustic guitar": "guitar",
    "electric guitar": "guitar",
    "bass": "guitar",
    "piano": "piano",
    "keyboard": "piano",
    "keys": "piano",
    "choir": "singing",
    "karaoke": "singing",
    "concerts": "live_music",
    "gigs": "live_music",
    "live shows": "live_music",
    "live music": "live_music",
    "electronic music": "edm",
    "house music": "edm",
    "dubstep": "edm",
    "synthwave": "edm",
    "punk": "rock",
    "metal": "rock",
    "indie rock": "rock",
    "shoegaze": "rock",
    # food
    "sourdough": "baking",
    "pastry": "baking",
    "pastries": "baking",
    "bread": "baking",
    "cookies": "baking",
    "cooking": "cooking",
    "recipes": "cooking",
    "food": "cooking",
    "ramen": "cooking",
    # mind / culture
    "languages": "language_exchange",
    "language learning": "language_exchange",
    "spanish": "language_exchange",
    "french": "language_exchange",
    "japanese": "language_exchange",
    "debating": "debate",
    "speech": "debate",
    "manga": "anime",
    "movies": "cinema",
    "film": "cinema",
    "movie nights": "cinema",
    "community service": "volunteering",
    "volunteer": "volunteering",
    "volunteering ": "volunteering",
}

MAX_RAW_TAG_CHARS = 32


def slug(raw: str) -> str:
    """Lowercase, trim, collapse separators. 'Matcha  Latte' -> 'matcha latte'."""
    cleaned = []
    for ch in raw.lower().strip():
        cleaned.append(" " if ch in "-_/|," else ch)
    return " ".join("".join(cleaned).split())


def normalize(raw: str) -> str | None:
    """Boil one raw interest down to a TAG_VOCAB entry, or None if nothing fits."""
    if not isinstance(raw, str):
        return None
    exact = raw.strip().lower()
    if exact in _CANON:          # live_music must not be slugged into "live music"
        return exact
    if exact in SYNONYMS:
        return SYNONYMS[exact]

    s = slug(raw)
    if not s:
        return None
    if s in _CANON:
        return s
    if s in SYNONYMS:
        return SYNONYMS[s]

    tokens = s.split()
    # a known multi-word phrase hiding inside a longer one: "thai cooking class"
    for size in (3, 2):
        for i in range(len(tokens) - size + 1):
            phrase = " ".join(tokens[i : i + size])
            if phrase in _CANON:
                return phrase
            if phrase in SYNONYMS:
                return SYNONYMS[phrase]
    # single tokens, in order of appearance
    for token in tokens:
        if token in _CANON:
            return token
        if token in SYNONYMS:
            return SYNONYMS[token]
    return None


def normalize_many(raws: list[str]) -> tuple[list[str], list[dict], list[str]]:
    """Map a raw interest list.

    Returns (canonical, mapped, unscored):
      canonical — deduped TAG_VOCAB entries, in first-seen order (what matching scores)
      mapped    — [{raw, tag}] every raw input that produced a canonical tag
      unscored  — raw inputs nothing matched (kept for the student, never on a card)
    """
    canonical: list[str] = []
    mapped: list[dict] = []
    unscored: list[str] = []
    seen: set[str] = set()
    for raw in raws:
        tag = normalize(raw)
        method = None
        if tag is None:
            # Nothing in the tables knew this word. If the learned encoder is up and the word is
            # close enough to one of our tags, map it there; otherwise it stays unscored.
            hit = encoder.nearest_tag(raw)
            if hit is not None:
                tag, method = hit[0], "encoder"
        if tag is None:
            if raw not in unscored:
                unscored.append(raw)
            continue
        entry = {"raw": raw, "tag": tag}
        if method:
            entry["method"] = method
        mapped.append(entry)
        if tag not in seen:
            seen.add(tag)
            canonical.append(tag)
    return canonical, mapped, unscored
