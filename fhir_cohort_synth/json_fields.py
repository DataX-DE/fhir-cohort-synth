"""Walk arbitrary JSON without knowing any FHIR field names.

Every yielded node describes one object, array or scalar. Parent ordinals keep
sibling values together; typed path segments distinguish a literal key such as
``a.b`` from two nested keys. Only statistical paths replace array positions
with a wildcard. Concrete paths and array positions are always retained.
"""
from dataclasses import dataclass
from decimal import Decimal
import json

from .jsonio import dumps


# Examples: (("key", "component"), ("index", 0), ("key", "value"))
# and its statistical equivalent using ("item", None) instead of ("index", 0).
Path = tuple[tuple[str, str | int | None], ...]


def path_json(path: Path) -> str:
    """Use an unambiguous, serializable path as the database lookup key."""
    return json.dumps(path, ensure_ascii=False, separators=(",", ":"))


def display_path(path: Path) -> str:
    """Render paths for people; database identity uses path_json(), not this."""
    result = "$"
    for kind, value in path:
        if kind == "key":
            result += "[" + json.dumps(value, ensure_ascii=False) + "]"
        else:
            result += "[*]" if kind == "item" else f"[{value}]"
    return result


def json_kind(value) -> str:
    """Classify by JSON type, checking boolean before Python's integer type."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, Decimal, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    raise ValueError("Unsupported non-JSON value")


@dataclass(frozen=True)
class Node:
    """One occurrence in a resource tree; ordinals are local to that resource.

    Containers have no scalar_json. Their children and shape reconstruct their
    value without storing a second full payload at every nested level.
    Explicit null has scalar_json='null'; an absent key creates no node at all.
    Empty objects/arrays still have nodes, so these cases remain distinguishable.
    """
    ordinal: int
    parent_ordinal: int | None
    path: Path
    statistical_path: Path
    kind: str
    scalar_json: str | None = None
    number_format: str | None = None
    string_length: int | None = None
    array_index: int | None = None
    array_length: int | None = None
    object_keys_json: str | None = None
    array_types_json: str | None = None


def walk_json(value):
    """Yield every node in parent-before-child order, including empty containers.

    The stack avoids recursive Python calls. A containing resource's entire
    tree, including contained resources, is traversed here; the caller avoids
    separately traversing the ingestion index's duplicate contained rows.
    """
    # Each stack entry is (value, parent ordinal, concrete path, statistical path).
    stack = [(value, None, (), ())]
    ordinal = 0
    while stack:
        current, parent_ordinal, path, statistical = stack.pop()
        kind = json_kind(current)
        if kind == "object" and any(not isinstance(key, str) for key in current):
            raise ValueError("JSON object keys must be strings")
        scalar = None if kind in {"object", "array"} else dumps(current)
        yield Node(
            ordinal=ordinal, parent_ordinal=parent_ordinal, path=path,
            statistical_path=statistical, kind=kind, scalar_json=scalar,
            number_format=("integer" if isinstance(current, int) else "decimal") if kind == "number" else None,
            string_length=len(current) if kind == "string" else None,
            array_index=path[-1][1] if path and path[-1][0] == "index" else None,
            array_length=len(current) if kind == "array" else None,
            object_keys_json=dumps(sorted(current)) if kind == "object" else None,
            array_types_json=dumps([json_kind(item) for item in current]) if kind == "array" else None,
        )
        # Children point to this node's ordinal. Reverse pushes preserve source
        # order on a last-in-first-out stack, including heterogeneous arrays.
        if kind == "object":
            for key, child in reversed(list(current.items())):
                segment = (("key", key),)
                stack.append((child, ordinal, path + segment, statistical + segment))
        elif kind == "array":
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], ordinal, path + (("index", index),),
                              statistical + (("item", None),)))
        ordinal += 1
