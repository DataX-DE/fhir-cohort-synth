"""Reproducible draws from a hospital-local key, never a public numeric seed.

Each purpose has a separate HMAC input. Knowing a generated resource ID therefore
does not supply the keyed digest used for that resource's quantity or date draw.
Only the state database stores the key; callers must never put it in reports.
"""
import hmac
import secrets

from .jsonio import dumps


ALGORITHM = 'hmac-sha256-v1'
KEY_BYTES = 32


def new_key():
    """Use operating-system randomness; there is no deterministic fallback."""
    return secrets.token_bytes(KEY_BYTES)


def valid_key(key):
    return isinstance(key, bytes) and len(key) == KEY_BYTES


def _json_lists(value):
    # Traversal paths contain tuples; SQLite reads them back as JSON lists.
    # Normalize recursively so both representations have identical HMAC input.
    if isinstance(value, (tuple, list)):
        return [_json_lists(child) for child in value]
    if isinstance(value, dict):
        return {name: _json_lists(child) for name, child in value.items()}
    return value


def digest(key, purpose, value, counter=0):
    """Hash structured, purpose-separated input without ambiguous concatenation."""
    if not valid_key(key):
        raise ValueError('Run key must contain exactly 32 bytes.')
    message = dumps([ALGORITHM, purpose, _json_lists(value), counter]).encode('utf-8')
    return hmac.digest(key, message, 'sha256')


def label(key, purpose, value):
    return digest(key, purpose, value).hex()


def randbelow(key, purpose, value, upper):
    """Draw uniformly in [0, upper), reproducing the draw for the same input.

    Modulo alone biases ranges that do not divide 2**256. Reject the excess
    high values and use a new counter until a uniformly usable digest appears.
    """
    space = 1 << 256
    if type(upper) is not int or not 1 <= upper <= space:
        raise ValueError('Invalid bounded random range.')
    limit = space - space % upper
    counter = 0
    while True:
        candidate = int.from_bytes(digest(key, purpose, value, counter), 'big')
        if candidate < limit:
            return candidate % upper
        counter += 1
