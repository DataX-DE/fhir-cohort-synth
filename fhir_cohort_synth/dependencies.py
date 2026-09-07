"""Group existing field nodes according to explicit, local JSON rules.

This module resolves associations, not probabilities. One group represents one
object (the anchor); selecting several array elements never multiplies groups.
See docs/dependencies.md for the rule format and the returned dictionaries.
"""
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from itertools import zip_longest
from pathlib import Path
import sqlite3

from .ingest import InputError
from .json_fields import Path as JsonPath
from .jsonio import dumps, loads
from .profiling import open_source


class DependencyError(InputError):
    """Configuration/input errors without source field values in messages."""


def _object(value, required, optional, where):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise DependencyError(f"{where}: expected keys {', '.join(sorted(required))}; check missing or unknown keys.")


def _name(value, where):
    if not isinstance(value, str) or not value.strip():
        raise DependencyError(f"{where}: expected a nonempty string.")
    return value


def _path(value, where):
    if not isinstance(value, list):
        raise DependencyError(f"{where}: expected a typed path array.")
    for part in value:
        if not (isinstance(part, list) and len(part) == 2 and
                ((part[0] == "key" and isinstance(part[1], str)) or
                 (part[0] == "item" and part[1] is None))):
            raise DependencyError(f"{where}: use ['key', name] or ['item', null] segments.")
    return tuple(tuple(part) for part in value)


def _selector(value, where):
    _object(value, {"path"}, {"from"}, where)
    origin = value.get("from", "anchor")
    if origin not in ("anchor", "root"):
        raise DependencyError(f"{where}: 'from' must be 'anchor' or 'root'.")
    return {"from": origin, "path": _path(value["path"], where)}


def _named(values, parse, where):
    if not isinstance(values, dict):
        raise DependencyError(f"{where}: expected an object of named entries.")
    result = {}
    for number, (name, value) in enumerate(values.items(), 1):
        location = f"{where} entry {number}"
        result[_name(name, location)] = parse(value, location)
    return result


def _link(value, where):
    _object(value, {"reference", "resource_type", "fields"}, set(), where)
    return {"reference": _selector(value["reference"], where + " reference"),
            "resource_type": _name(value["resource_type"], where + " resource_type"),
            "fields": _named(value["fields"], _path, where + " fields")}


def _statistics(value, rule, where):
    """Statistics refer to existing field names, not a second set of paths."""
    _object(value, {"given", "targets"}, set(), where)
    result = {}
    for role in ("given", "targets"):
        entries = value[role]
        if not isinstance(entries, list) or (role == "targets" and not entries):
            raise DependencyError(f"{where}: given must be an array and targets a nonempty array.")
        result[role] = []
        for entry in entries:
            _object(entry, {"field"}, {"link"}, where + " " + role)
            name = _name(entry["field"], where + " field")
            fields = rule["fields"]
            if "link" in entry:
                link_name = _name(entry["link"], where + " link")
                if link_name not in rule["links"]:
                    raise DependencyError(f"{where}: statistics refer to an unknown link.")
                fields = rule["links"][link_name]["fields"]
            if name not in fields:
                raise DependencyError(f"{where}: statistics refer to an unknown field.")
            result[role].append(dict(entry))
    return result


def _compile(document):
    """Validate before opening databases; compile typed paths once per call."""
    _object(document, {"schema_version", "rules"}, set(), "Rules document")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise DependencyError("Unsupported rules schema; expected version 1.")
    if not isinstance(document["rules"], list):
        raise DependencyError("Rules must be an array.")
    rules, names = [], set()
    for number, rule in enumerate(document["rules"], 1):
        where = f"Rule {number}"
        _object(rule, {"name", "resource_type", "anchor", "fields"}, {"links", "statistics"}, where)
        name = _name(rule["name"], where + " name")
        if name in names:
            raise DependencyError(f"{where}: rule names must be unique.")
        names.add(name)
        rules.append({"name": name,
                      "resource_type": _name(rule["resource_type"], where + " resource_type"),
                      "anchor": _path(rule["anchor"], where + " anchor"),
                      "fields": _named(rule["fields"], _selector, where + " fields"),
                      "links": _named(rule.get("links", {}), _link, where + " links")})
        if "statistics" in rule:
            rules[-1]["statistics"] = _statistics(rule["statistics"], rules[-1], where + " statistics")
    return rules


def load_rules(path):
    """Read and validate a schema-1 rules document; return its JSON dictionary.

    The same dictionary can be supplied directly to iter_groups(), which also
    validates it. Duplicate JSON keys and nonstandard numeric constants fail.
    """
    try:
        document = loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise DependencyError("Cannot read rules as valid UTF-8 JSON with unique keys.") from None
    _compile(document)
    return document


@dataclass(frozen=True)
class NodeRef:
    """A field-index node, including its original parent and parsed JSON token.

    resource_id is the profiled, non-contained root's SQLite key. A contained
    target's own ingestion key is separately returned as target_resource_id.
    Immutable nodes can safely be shared between groups and the bounded cache.
    """
    node_id: int
    resource_id: int
    parent_id: int | None
    path: JsonPath
    kind: str
    scalar_json: str | None
    array_index: int | None
    array_length: int | None
    object_keys_json: str | None


@contextmanager
def _open_fields(path, source, source_run):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise DependencyError("Input must be an existing field database, not a symlink.")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        runs = db.execute("SELECT status,schema_version,source_schema_version,source_status,source_fhir_version FROM run").fetchall()
        if (len(runs) != 1 or runs[0]["schema_version"] != 1 or
                runs[0]["source_schema_version"] != 1):
            raise DependencyError("Unsupported field database schema; expected version 1.")
        run = runs[0]
        if run["status"] not in {"completed", "completed_with_warnings"}:
            raise DependencyError("The field profiling run is incomplete.")
        if (run["source_status"] != source_run["status"] or
                run["source_fhir_version"] != source_run["fhir_version"]):
            raise DependencyError("The field and ingestion databases do not match.")
        # File paths can change when a hospital copies a completed run. Match
        # resource keys/digests and occurrence lookup contexts, not file names.
        # Stream these comparisons rather than holding the cohort in memory.
        comparisons = [
            ("SELECT resource_id,resource_type,identity,digest FROM source_resources ORDER BY resource_id",
             "SELECT id,resource_type,identity,digest FROM resources WHERE contained=0 ORDER BY id"),
            ("SELECT occurrence_id,resource_id,locator,context,full_url FROM source_occurrences ORDER BY occurrence_id",
             "SELECT o.id,o.resource_id,o.locator,o.context,o.full_url FROM occurrences o "
             "JOIN resources r ON r.id=o.resource_id WHERE r.contained=0 ORDER BY o.id"),
        ]
        for field_sql, source_sql in comparisons:
            for left, right in zip_longest(db.execute(field_sql), source.execute(source_sql)):
                if left is None or right is None or tuple(left) != tuple(right):
                    raise DependencyError("The field and ingestion databases do not match.")
        db.execute("SELECT id,resource_id,parent_id,concrete_path,kind,scalar_json,array_index,array_length,object_keys_json FROM nodes LIMIT 0")
        source.execute("SELECT source_resource_id,path,literal,kind,status,target_resource_id FROM resource_references LIMIT 0")
        yield db
    finally:
        db.close()


class _Tree:
    """Index one resource's nodes, keeping containers instead of flattening rows."""

    def __init__(self, source, fields, resource_id):
        self.nodes, self.children, self.keys = {}, defaultdict(list), {}
        self.root = None
        for row in fields.execute("SELECT * FROM nodes WHERE resource_id=? ORDER BY ordinal", (resource_id,)):
            path = tuple(tuple(segment) for segment in loads(row["concrete_path"]))
            node = NodeRef(row["id"], resource_id, row["parent_id"], path, row["kind"],
                           row["scalar_json"], row["array_index"], row["array_length"], row["object_keys_json"])
            self.nodes[node.node_id] = node
            if node.parent_id is None:
                self.root = node
            else:
                self.children[node.parent_id].append(node)
                if path[-1][0] == "key":
                    self.keys[node.parent_id, path[-1][1]] = node
        if self.root is None or self.root.kind != "object":
            raise DependencyError("The field database has an invalid resource tree.")
        self.owners = {(): (resource_id, self.root)}
        self.resource_nodes = {resource_id: self.root}
        # Contained resources already exist inside this tree. Locate their
        # ingestion keys by root-scoped identity; never construct a second tree
        # or match a same-named child belonging to another containing resource.
        contained = self.keys.get((self.root.node_id, "contained"))
        if contained is not None and contained.kind == "array":
            for child in self.children[contained.node_id]:
                identity = self.keys.get((child.node_id, "id"))
                if identity is None or identity.kind != "string":
                    raise DependencyError("Cannot associate a contained resource with its field nodes.")
                rows = source.execute("SELECT id FROM resources WHERE identity=? AND contained=1",
                                      (f"contained:{resource_id}#{loads(identity.scalar_json)}",)).fetchall()
                if len(rows) != 1:
                    raise DependencyError("Cannot associate a contained resource with its field nodes.")
                owner = rows[0][0]
                self.owners[child.path] = (owner, child)
                self.resource_nodes[owner] = child

    def owner(self, node):
        return self.owners.get(node.path[:2], self.owners[()])

    def value_json(self, start):
        """Serialize a selected subtree from its nodes, preserving numeric tokens.

        Objects use sorted keys; arrays keep their order. An iterative walk
        avoids re-parsing source payloads or rounding numbers through floats.
        Only this subtree's serialized content is held in memory.
        """
        parts, stack = [], [start]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.kind not in {"object", "array"}:
                parts.append(item.scalar_json)
            else:
                children = self.children[item.node_id]
                tokens = []
                if item.kind == "object":
                    children = sorted(children, key=lambda child: child.path[-1][1])
                for index, child in enumerate(children):
                    if index:
                        tokens.append(",")
                    if item.kind == "object":
                        tokens.extend((dumps(child.path[-1][1]), ":"))
                    tokens.append(child)
                parts.append("{" if item.kind == "object" else "[")
                stack.append("}" if item.kind == "object" else "]")
                stack.extend(reversed(tokens))
        return "".join(parts)

    def select(self, start, path, include_values=False):
        """Return a match for each branch, including branches without a value.

        For items[*].x in [{x: 1}, {}], the second item produces a missing
        match with that object's identity. It does not vanish from a future
        presence denominator. Empty arrays and non-traversable scalars also
        have explicit outcomes. Only object anchors become groups.
        """
        results, stack = [], [(start, 0)]
        while stack:
            node, offset = stack.pop()
            if offset == len(path):
                results.append({"status": "present", "node": node,
                                "context": self.nodes.get(node.parent_id), "remaining_path": ()})
                continue
            kind, key = path[offset]
            status = "not_applicable"
            if kind == "key" and node.kind == "object":
                child = self.keys.get((node.node_id, key))
                if child is not None:
                    stack.append((child, offset + 1))
                    continue
                status = "missing"
            elif kind == "item" and node.kind == "array":
                children = self.children[node.node_id]
                if children:
                    stack.extend((child, offset + 1) for child in reversed(children))
                    continue
                status = "empty"
            results.append({"status": status, "node": None, "context": node,
                            "remaining_path": path[offset:]})
        if include_values:
            for match in results:
                node = match["node"] or match["context"]
                # Relative positions retain nested array boundaries without
                # making a component's absolute index part of a context key.
                match["relative_path"] = node.path[len(start.path):]
                if match["status"] == "present":
                    match["value_json"] = self.value_json(node)
        return results


def _legacy_path(path):
    """Bridge to schema-1 reference paths; never parse these ambiguous strings.

    Selection uses typed paths. Reference lookup additionally matches the
    literal and owning resource, so a literal key 'a.b' cannot redirect a link
    selected from nested a/b to another reference with the same display path.
    """
    result = ""
    for kind, value in path:
        result += ("." if result else "") + value if kind == "key" else f"[{value}]"
    return result


class _Resolver:
    def __init__(self, source, fields):
        self.source = source
        # Repeated observations often point to the same encounter. Reuse a few
        # trees without letting memory grow with the entire cohort.
        self.tree = lru_cache(maxsize=8)(lambda rid: _Tree(source, fields, rid))

    def target(self, resource_id):
        row = self.source.execute("SELECT resource_type,contained FROM resources WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            raise DependencyError("A resolved reference target is absent from the ingestion database.")
        root_id = resource_id
        if row["contained"]:
            roots = self.source.execute("SELECT DISTINCT root_resource_id FROM occurrences WHERE resource_id=?", (resource_id,)).fetchall()
            if len(roots) != 1:
                raise DependencyError("A contained target has inconsistent ownership.")
            root_id = roots[0][0]
        tree = self.tree(root_id)
        if resource_id not in tree.resource_nodes:
            raise DependencyError("A reference target is absent from the field database.")
        return row["resource_type"], tree, tree.resource_nodes[resource_id]

    def link(self, tree, anchor, spec, include_values=False):
        selector = spec["reference"]
        start = tree.root if selector["from"] == "root" else anchor
        references, targets = [], {}
        for match in tree.select(start, selector["path"]):
            result = {"selection": match, "status": match["status"], "target_resource_id": None}
            references.append(result)
            if match["status"] != "present":
                continue
            node = match["node"]
            literal_node = tree.keys.get((node.node_id, "reference")) if node.kind == "object" else None
            result["status"] = "unresolved"
            if literal_node is None or literal_node.kind != "string":
                # Identifier-only Reference objects are deliberately not guessed.
                continue
            owner_id, owner_node = tree.owner(literal_node)
            edges = {tuple(row) for row in self.source.execute(
                "SELECT DISTINCT status,target_resource_id FROM resource_references "
                "WHERE source_resource_id=? AND path=? AND literal=? AND kind='literal'",
                (owner_id, _legacy_path(literal_node.path[len(owner_node.path):]), loads(literal_node.scalar_json)))}
            candidates = {target for status, target in edges if status == "resolved" and target is not None}
            # A deduplicated payload may resolve differently in two Bundles.
            # Consolidate agreeing occurrences, but retain ambiguity or partial
            # resolution instead of attaching whichever target was seen first.
            if any(status == "ambiguous" for status, _ in edges) or len(candidates) > 1:
                result["status"] = "ambiguous"
            elif len(candidates) == 1 and edges == {("resolved", next(iter(candidates)))}:
                rid = next(iter(candidates))
                kind, target_tree, target_node = self.target(rid)
                result["status"] = "resolved" if kind == spec["resource_type"] else "wrong_type"
                result["target_resource_id"] = rid
                if result["status"] == "resolved" and rid not in targets:
                    targets[rid] = {"resource_id": rid, "node": target_node,
                                    "fields": {name: target_tree.select(target_node, path, include_values)
                                               for name, path in spec["fields"].items()}}
        # Several reference slots can point to one target; preserve the slots
        # as provenance while returning that target's fields only once.
        return {"references": references, "targets": list(targets.values())}


def iter_groups(cohort_db, field_db, rules, *, include_values=False):
    """Yield groups from completed, matching databases, using schema-1 rules.

    Each group contains rule, resource_id, anchor, fields and links. A field is
    a list of matches, never a Cartesian product with another field's matches.
    Results contain local source data. Exhaust or close this generator to close
    its read transactions; contextlib.closing is useful for partial inspection.
    include_values adds selected value_json/relative_path metadata to matches
    and the root's patient_resource_id to each group, for statistical counting.
    """
    compiled = _compile(rules)
    try:
        with open_source(cohort_db) as (source, source_run, _):
            with _open_fields(field_db, source, source_run) as fields:
                resolver = _Resolver(source, fields)
                try:
                    for rule in compiled:
                        for resource in fields.execute("SELECT resource_id FROM source_resources WHERE resource_type=? ORDER BY resource_id",
                                                       (rule["resource_type"],)):
                            tree = resolver.tree(resource[0])
                            patient = None
                            if include_values:
                                patient = source.execute("SELECT patient_resource_id FROM patient_memberships WHERE resource_id=?", (resource[0],)).fetchone()
                            for match in tree.select(tree.root, rule["anchor"]):
                                anchor = match["node"]
                                if match["status"] != "present" or anchor.kind != "object":
                                    continue
                                group = {"rule": rule["name"], "resource_id": resource[0], "anchor": anchor,
                                       "fields": {name: tree.select(tree.root if selector["from"] == "root" else anchor, selector["path"], include_values)
                                                  for name, selector in rule["fields"].items()},
                                       "links": {name: resolver.link(tree, anchor, spec, include_values)
                                                 for name, spec in rule["links"].items()}}
                                if include_values:
                                    group["patient_resource_id"] = patient[0] if patient else None
                                yield group
                finally:
                    resolver.tree.cache_clear()
    except InputError as error:
        # Keep a single public error type, including source checks shared with
        # profiling. Those diagnostics are already safe to display locally.
        raise DependencyError(str(error)) from None
    except (sqlite3.Error, ValueError, UnicodeError, RecursionError):
        raise DependencyError("Cannot read supported ingestion and field databases.") from None
