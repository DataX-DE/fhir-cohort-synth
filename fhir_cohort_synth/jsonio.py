"""Strict JSON with lossless decimal values (FHIR decimal precision matters)."""
import json
from json.encoder import encode_basestring
from decimal import Decimal


# Reuse the standard encoder for less common values such as tuples and floats.
# Its default spacing is part of existing path keys and must remain unchanged.
_encode = json.JSONEncoder(ensure_ascii=False, allow_nan=False).encode


def _invalid_constant(value):
    """Reject NaN/Infinity, which Python accepts but standard JSON does not."""
    raise ValueError("Non-finite JSON number")


def _unique_keys(pairs):
    """Build an object without silently overwriting repeated field names."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def loads(text):
    """Parse FHIR JSON without losing decimal digits or accepting duplicate keys.

    Decimal avoids binary floating-point rounding and preserves written
    precision such as 4.20. Integers continue to use Python's integer type.
    """
    return json.loads(text, parse_float=Decimal, parse_constant=_invalid_constant,
                      object_pairs_hook=_unique_keys)


def dumps(value):
    """Serialize parsed values consistently for storage and duplicate detection.

    The standard JSON encoder does not support Decimal. Recursively emit those
    values as numeric tokens, while using the standard JSON string escaper.
    Whitespace and object-key order are normalized;
    the original source bytes are not reconstructed.
    """
    # Strings dominate FHIR payloads. Avoid creating a JSONEncoder for every
    # key/value while retaining exactly the same Unicode and escaping behavior.
    if isinstance(value, str):
        return encode_basestring(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite decimal")
        return str(value)
    if isinstance(value, dict):
        # Sorting keys ensures that field order alone cannot change a digest.
        return "{" + ",".join(_encode(k) + ":" + dumps(value[k])
                              for k in sorted(value)) + "}"
    if isinstance(value, list):
        # Array order is part of the data and must remain unchanged.
        return "[" + ",".join(dumps(v) for v in value) + "]"
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if type(value) is int:
        return str(value)
    return _encode(value)


def pretty_json(value, level=0):
    """Indent report values without converting Decimal tokens to floating point."""
    """Indent a report fragment while preserving Decimal tokens as JSON numbers."""
    if isinstance(value, dict) and value:
        parts = ["  " * (level + 1) + dumps(key) + ": " + pretty_json(child, level + 1)
                 for key, child in value.items()]
        return "{\n" + ",\n".join(parts) + "\n" + "  " * level + "}"
    if isinstance(value, list) and value:
        return "[\n" + ",\n".join("  " * (level + 1) + pretty_json(child, level + 1) for child in value) + "\n" + "  " * level + "]"
    return dumps(value)
