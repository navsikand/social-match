"""Password hashing: pbkdf2 sha256, 200k iterations, format 'salt$hex'."""
import hashlib
import secrets

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _ITERATIONS
    ).hex()
    return f"{salt}${digest}"


def verify(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    calc = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _ITERATIONS
    ).hex()
    return secrets.compare_digest(calc, digest)
