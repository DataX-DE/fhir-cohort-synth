"""Aggregate extracted nodes into exact, disk-backed marginal distributions.

SQL groups typed scalar tokens and container shapes without coercing their
values. Numeric summaries use a Decimal collation and weighted nearest ranks:
four occurrences [1, 2, 2, 10] have a median of 2 even though there are only
three distinct tokens. These profiles describe fields, not clinical meaning
or statistical independence between neighboring fields.
"""
from decimal import Decimal

from .jsonio import dumps, loads


QUANTILES = (("p05", 5), ("p25", 25), ("p50", 50), ("p75", 75), ("p95", 95))


def compare_decimals(left, right):
    """Order numeric JSON tokens without conversion to binary floating point."""
    a, b = Decimal(left), Decimal(right)
    return (a > b) - (a < b)


def aggregate(db):
    """Populate every exact distribution, followed by presence and numeric summaries.

    SQLite's temporary store is configured on disk. No list of the cohort's
    values or distinct categories is built in Python. Queries below contain
    only fixed SQL; values and paths from the input remain SQL parameters/data.
    """
    statements = (
        "INSERT INTO type_frequencies SELECT field_id,kind,count(*) FROM nodes GROUP BY field_id,kind",
        # These queries need values absent from the field/kind index. A full
        # table scan reads nodes sequentially; an index-driven scan would fetch
        # scattered payload pages again for each field in a large cohort.
        "INSERT INTO scalar_frequencies SELECT field_id,kind,scalar_json,count(*) FROM nodes NOT INDEXED WHERE scalar_json IS NOT NULL GROUP BY field_id,kind,scalar_json",
        "INSERT INTO number_format_frequencies SELECT field_id,number_format,count(*) FROM nodes NOT INDEXED WHERE kind='number' GROUP BY field_id,number_format",
        "INSERT INTO object_shape_frequencies SELECT field_id,object_keys_json,count(*) FROM nodes NOT INDEXED WHERE kind='object' GROUP BY field_id,object_keys_json",
        "INSERT INTO array_length_frequencies SELECT field_id,array_length,count(*) FROM nodes NOT INDEXED WHERE kind='array' GROUP BY field_id,array_length",
        "INSERT INTO array_type_pattern_frequencies SELECT field_id,array_types_json,count(*) FROM nodes NOT INDEXED WHERE kind='array' GROUP BY field_id,array_types_json",
        "INSERT INTO string_length_frequencies SELECT field_id,string_length,count(*) FROM nodes NOT INDEXED WHERE kind='string' GROUP BY field_id,string_length",
        "INSERT INTO array_element_type_frequencies SELECT p.field_id,c.kind,count(*) FROM nodes p JOIN nodes c ON c.parent_id=p.id WHERE p.kind='array' GROUP BY p.field_id,c.kind",
    )
    for sql in statements:
        db.execute(sql)
    summarize_presence(db)
    summarize_numbers(db)


def summarize_presence(db):
    """Count opportunities to observe each field, not just its occurrences.

    For object keys, only existing object parents are eligible. For array-item
    paths, presence means an array is nonempty; occurrence count separately
    counts its elements. Root-resource presence gives every resource one vote.
    """
    for field in db.execute("SELECT * FROM fields ORDER BY id"):
        fid, rtype = field["id"], field["resource_type"]
        population = db.execute("SELECT count(*) FROM source_resources WHERE resource_type=?", (rtype,)).fetchone()[0]
        occurrences, resources = db.execute(
            "SELECT count(*),count(DISTINCT resource_id) FROM nodes WHERE field_id=?", (fid,)).fetchone()
        if field["relation"] == "root":
            eligible, present, unit = population, resources, "root_resources"
        else:
            parent = db.execute("SELECT id FROM fields WHERE resource_type=? AND path=?", (rtype, field["parent_path"])).fetchone()[0]
            kind = "object" if field["relation"] == "key" else "array"
            eligible = db.execute("SELECT count(*) FROM nodes WHERE field_id=? AND kind=?", (parent, kind)).fetchone()[0]
            present = db.execute("SELECT count(DISTINCT parent_id) FROM nodes WHERE field_id=?", (fid,)).fetchone()[0]
            unit = "parent_objects" if kind == "object" else "parent_arrays"
        db.execute("INSERT INTO field_summaries VALUES (?,?,?,?,?,?,?,?)",
                   (fid, occurrences, resources, population, eligible, present, eligible - present, unit))


def summarize_numbers(db):
    """Compute exact weighted nearest ranks while streaming sorted distinct tokens.

    A pth percentile is the value at ceil(p * n) in the numeric ordering of all
    occurrences. A binary token tie-break makes representations such as 1 and
    1.0 deterministic without changing their numeric ordering. Their distinct
    representation frequencies remain in scalar_frequencies.
    """
    db.create_collation("EXACT_DECIMAL", compare_decimals)
    for fid, count in db.execute("SELECT field_id,sum(frequency) FROM scalar_frequencies WHERE kind='number' GROUP BY field_id"):
        rows = db.execute(
                "SELECT value_json,frequency FROM scalar_frequencies WHERE field_id=? AND kind='number' "
                "ORDER BY value_json COLLATE EXACT_DECIMAL,value_json COLLATE BINARY", (fid,))
        minimum, maximum, quantiles = numeric_summary(rows, count)
        db.execute("INSERT INTO numeric_summaries VALUES (?,?,?,?,?,?)",
                   (fid, count, minimum, maximum, dumps(quantiles), "weighted_nearest_rank"))


def numeric_summary(rows, count):
    """Share the exact estimator between marginal and conditional profiles.

    rows must stream (token, frequency) in Decimal order with a binary token
    tie-break. Frequency weights are observations, not distinct-value weights.
    """
    ranks = [(name, (percent * count + 99) // 100) for name, percent in QUANTILES]
    cumulative, next_rank = 0, 0
    minimum, maximum, quantiles = None, None, {}
    for token, frequency in rows:
        if minimum is None:
            minimum = token
        maximum = token
        cumulative += frequency
        while next_rank < len(ranks) and cumulative >= ranks[next_rank][1]:
            quantiles[ranks[next_rank][0]] = loads(token)
            next_rank += 1
    return minimum, maximum, quantiles


def field_reports(db, statistics=False):
    """Yield one compact report per field, keeping exact large lists in SQLite.

    Internal field IDs can depend on ingestion order, so reports identify fields
    by typed paths. Report iteration is sorted and independent of source order.
    Source string values are not copied into these JSON summaries.
    """
    query = "SELECT f.*,s.* FROM fields f JOIN field_summaries s ON s.field_id=f.id ORDER BY f.resource_type,f.path"
    for field in db.execute(query):
        fid = field["id"]
        eligible, present = field["eligible_parents"], field["present_parents"]
        population, resources = field["resource_population"], field["resources_present"]
        types = dict(db.execute("SELECT kind,frequency FROM type_frequencies WHERE field_id=? ORDER BY kind", (fid,)))
        result = {
            "resource_type": field["resource_type"], "path": loads(field["path"]),
            "display_path": field["display_path"], "relation": field["relation"],
            "occurrences": field["occurrences"],
            "presence": {"unit": field["presence_unit"], "eligible": eligible,
                         "present": present, "absent": field["absent_parents"],
                         "present_probability": present / eligible if eligible else None,
                         "absent_probability": field["absent_parents"] / eligible if eligible else None},
            "root_resource_presence": {"population": population, "present": resources,
                                       "absent": population - resources,
                                       "probability": resources / population if population else None},
            "types": types, "null_count": types.get("null", 0),
        }
        if "object" in types:
            shapes, empty = db.execute("SELECT count(*),coalesce(sum(CASE WHEN keys_json='[]' THEN frequency ELSE 0 END),0) FROM object_shape_frequencies WHERE field_id=?", (fid,)).fetchone()
            result["objects"] = {"count": types["object"], "empty_count": empty,
                                 "distinct_shapes": shapes, "frequency_table": "object_shape_frequencies"}
        if "array" in types:
            distinct, minimum, maximum, empty = db.execute(
                "SELECT count(*),min(length),max(length),coalesce(sum(CASE WHEN length=0 THEN frequency ELSE 0 END),0) FROM array_length_frequencies WHERE field_id=?", (fid,)).fetchone()
            patterns = db.execute("SELECT count(*) FROM array_type_pattern_frequencies WHERE field_id=?", (fid,)).fetchone()[0]
            result["arrays"] = {"count": types["array"], "empty_count": empty,
                                "length": {"minimum": minimum, "maximum": maximum, "distinct": distinct,
                                           "frequency_table": "array_length_frequencies"},
                                "element_types": dict(db.execute("SELECT kind,frequency FROM array_element_type_frequencies WHERE field_id=? ORDER BY kind", (fid,))),
                                "distinct_type_patterns": patterns,
                                "type_pattern_table": "array_type_pattern_frequencies"}
        if "string" in types:
            cardinality, empty = db.execute(
                "SELECT count(*),coalesce(sum(CASE WHEN value_json='\"\"' THEN frequency ELSE 0 END),0) FROM scalar_frequencies WHERE field_id=? AND kind='string'", (fid,)).fetchone()
            minimum, maximum, distinct = db.execute("SELECT min(length),max(length),count(*) FROM string_length_frequencies WHERE field_id=?", (fid,)).fetchone()
            result["strings"] = {"count": types["string"], "distinct_values": cardinality, "empty_count": empty,
                                 "length": {"minimum": minimum, "maximum": maximum, "distinct": distinct,
                                            "unit": "unicode_code_points", "frequency_table": "string_length_frequencies"}}
        if statistics:
            distinct, count = db.execute("SELECT count(*),coalesce(sum(frequency),0) FROM scalar_frequencies WHERE field_id=?", (fid,)).fetchone()
            result["scalar_values"] = {"count": count, "distinct_typed_tokens": distinct,
                                       "frequency_table": "scalar_frequencies", "complete_in_database": True}
            if "boolean" in types:
                result["booleans"] = dict(db.execute("SELECT value_json,frequency FROM scalar_frequencies WHERE field_id=? AND kind='boolean' ORDER BY value_json", (fid,)))
            numeric = db.execute("SELECT * FROM numeric_summaries WHERE field_id=?", (fid,)).fetchone()
            if numeric:
                result["numbers"] = {"sample_count": numeric["sample_count"],
                                     "minimum": loads(numeric["minimum_json"]), "maximum": loads(numeric["maximum_json"]),
                                     "quantiles": loads(numeric["quantiles_json"]), "estimator": numeric["estimator"],
                                     "representations": dict(db.execute("SELECT number_format,frequency FROM number_format_frequencies WHERE field_id=? ORDER BY number_format", (fid,)))}
        yield result
