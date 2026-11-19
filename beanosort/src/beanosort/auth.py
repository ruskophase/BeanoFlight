import hashlib
import hmac
import secrets

from .quantities import DomainError


def hash_password(password):
    if len(password) < 12:
        raise DomainError("Passwords must have at least 12 characters")
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${key}"


def verify_password(password, stored):
    try:
        _, salt, key = stored.split("$")
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, key)
    except (ValueError, TypeError):
        return False


def require_role(user, *roles):
    if not user.active or user.role not in roles:
        raise DomainError("This action requires " + "/".join(roles))
