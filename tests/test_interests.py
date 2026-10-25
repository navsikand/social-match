"""Interest canonicalisation: raw words -> TAG_VOCAB (the "matcha" problem)."""
import pytest

from server import interests
from server.schema import TAG_VOCAB


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("matcha", "coffee"),
        ("Matcha Latte", "coffee"),
        ("bouldering", "bouldering"),   # itself a vocabulary entry: exact match wins
        ("rock climbing", "climbing"),
        ("Valorant", "video_games"),
        ("sourdough", "baking"),
        ("concerts", "live_music"),
        ("ceramics", "pottery"),
        ("dnd", "board_games"),
        ("COFFEE", "coffee"),
        ("coffee", "coffee"),
        ("thai cooking class", "cooking"),
    ],
)
def test_synonyms_map_to_vocabulary(raw, expected):
    assert interests.normalize(raw) == expected
    assert interests.normalize(raw) in TAG_VOCAB


@pytest.mark.parametrize("raw", ["mycology", "uranium glass", "zzz", "", "   "])
def test_unknown_or_empty_is_not_scored(raw):
    assert interests.normalize(raw) is None


@pytest.mark.parametrize(
    "raw", ["live_music", "board_games", "video_games", "ultimate_frisbee", "language_exchange"]
)
def test_underscore_vocab_entries_survive_slugging(raw):
    """Regression: slug() turned live_music into 'live music' and silently dropped it."""
    assert interests.normalize(raw) == raw


def test_every_synonym_target_is_a_real_vocab_entry():
    """A typo in the table would silently make a whole interest unmatchable."""
    bad = {raw: tag for raw, tag in interests.SYNONYMS.items() if tag not in TAG_VOCAB}
    assert not bad, f"synonyms pointing outside TAG_VOCAB: {bad}"


def test_normalize_many_dedupes_and_orders():
    canonical, mapped, unscored = interests.normalize_many(
        ["matcha", "climbing", "espresso", "mycology", "climbing"]
    )
    assert canonical == ["coffee", "climbing"]          # deduped, first-seen order
    assert unscored == ["mycology"]
    assert {"raw": "espresso", "tag": "coffee"} in mapped


def test_slug_collapses_separators():
    assert interests.slug("  Matcha_Latte  ") == "matcha latte"
    assert interests.slug("board-games") == "board games"
