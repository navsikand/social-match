"""Runtime configuration. Every value is read at CALL time so tests can swap env vars."""
import os

_DB_DEFAULT = os.environ.get("SOCIAL_MATCH_DB", "./social_match.db")


def _int_env(name: str, default: int) -> int:
    """Env ints are trusted-but-sanitized: a bad value falls back, never crashes startup."""
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _str_env(name: str) -> str | None:
    """Unset or blank means "not configured": callers key features off None."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _bool_env(name: str, default: bool = False) -> bool:
    value = _str_env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


# --- Interest encoder (server/encoder.py). Every value read at call time.
def encoder_enabled() -> bool:
    """On by default. If the extra is not installed or the download fails, scoring falls back to
    the taxonomy and one log line says why, so leaving this on is always safe."""
    return _bool_env("ENCODER_ENABLED", True)


def encoder_model() -> str:
    return _str_env("ENCODER_MODEL") or "BAAI/bge-small-en-v1.5"


def encoder_cache_dir() -> str:
    return _str_env("ENCODER_CACHE_DIR") or "./.cache"


def encoder_device() -> str:
    """cpu by default: the pinned torch build is CPU-only. auto = cuda when torch sees a GPU."""
    return (_str_env("ENCODER_DEVICE") or "cpu").lower()


def encoder_template() -> str:
    """How a tag or word is wrapped before it is embedded; must contain one {}."""
    value = _str_env("ENCODER_TEMPLATE") or "hobby: {}"
    return value if value.count("{}") == 1 else "hobby: {}"


def encoder_min_auc() -> float:
    return max(0.5, min(1.0, _float_env("ENCODER_MIN_AUC", 0.80)))


def encoder_map_min() -> float:
    """How similar (0..1, calibrated) a free-text word must be to a tag before we map it onto it."""
    return max(0.5, min(0.99, _float_env("ENCODER_MAP_MIN", 0.85)))


def similarity_floor() -> float:
    """Minimum best-pair similarity for a card to exist at all. 0 (default) keeps the old behaviour
    where time alone makes a card; 0.5 means the two students must share at least a domain."""
    return max(0.0, min(1.0, _float_env("SIMILARITY_FLOOR", 0.0)))


def db_path() -> str:
    return os.environ.get("SOCIAL_MATCH_DB", _DB_DEFAULT)


def state_ttl_days() -> int:
    return _int_env("STATE_TTL_DAYS", 7)


def code_ttl_seconds() -> int:
    return _int_env("CODE_TTL_SECONDS", 300)


def poll_sweep_seconds() -> int:
    return _int_env("POLL_SWEEP_SECONDS", 3600)


def base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


def session_secret() -> str:
    return os.environ.get("SESSION_SECRET", "dev-insecure-secret-change-me")


def block_salt() -> str:
    return os.environ.get("BLOCK_SALT", "dev-insecure-salt-change-me")


def registration_code() -> str:
    return os.environ.get("REGISTRATION_CODE", "cubbie2026")


def rotate_grace_seconds() -> int:
    return 60


def grant_ttl_days() -> int:
    return 30


def closed_match_days() -> int:
    return _int_env("CLOSED_MATCH_DAYS", 30)


def audit_residue_days() -> int:
    return _int_env("AUDIT_RESIDUE_DAYS", 90)


def blocks_retention_months() -> int:
    return _int_env("BLOCKS_RETENTION_MONTHS", 12)


def upload_dir() -> str:
    """Where published photos live. Created on demand; verified writable at startup."""
    explicit = os.environ.get("UPLOAD_DIR")
    if explicit:
        return explicit
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "uploads")


# --- Chat app (CONTRACT-chat.md §1). Unset AI_API_URL means chat is DISABLED, not an error.


def ai_api_url() -> str | None:
    return _str_env("AI_API_URL")


def ai_api_key() -> str | None:
    return _str_env("AI_API_KEY")


def ai_model() -> str:
    return _str_env("AI_MODEL") or ""


def ai_max_tokens() -> int:
    return _int_env("AI_MAX_TOKENS", 800)


def ai_timeout_seconds() -> float:
    return _float_env("AI_TIMEOUT_SECONDS", 60.0)


def chat_ttl_days() -> int:
    return _int_env("AI_TTL_DAYS", 7)


def delegation_default_on() -> bool:
    """Demo-box only: seeds the delegation grant at signup. Absent/off by default."""
    return _bool_env("DELEGATION_DEFAULT_ON", False)


def matching_default_on() -> bool:
    """Demo-box only: signup seeds the two grants matchmaking actually requires.

    Chat writes are grant-preserving by design (a turn can never grant anything), so a
    student who only ever talks to the assistant holds no `matching` grant and no
    `publication/card` grant — and `matching.generate_for_user` then refuses every pair with
    `matching_grant_missing_me`, which is exactly "I see no matches" with a stored profile.
    On the demo box we seed both at signup so the flow is hands-free. Off by default: this is
    a consent decision, and the real product asks the human (Settings) to make it.
    """
    return _bool_env("MATCHING_DEFAULT_ON", False)
