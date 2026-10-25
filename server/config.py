"""Runtime configuration. Every value is read at CALL time so tests can swap env vars."""
import os

_DB_DEFAULT = os.environ.get("SOCIAL_MATCH_DB", "./social_match.db")


def _int_env(name: str, default: int) -> int:
    """Env ints are trusted-but-sanitized: a bad value falls back, never crashes startup."""
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


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
