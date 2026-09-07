"""Count configured joint outcomes within exact contexts, one anchor at a time.

The dependency iterator supplies values and associations. This module projects
only the requested content, counts it in SQLite, and writes a compact report.
It never trains or samples a model. Exact source-derived artifacts stay local.
"""
from contextlib import closing
import hashlib
from itertools import chain
from pathlib import Path
import sqlite3

from .dependencies import DependencyError, _compile, iter_groups
from .field_statistics import QUANTILES, compare_decimals, numeric_summary
from .jsonio import dumps, loads
from .profiling import open_source, pretty_json, source_fingerprint


BATCH_SIZE = 1000
SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA wal_autocheckpoint=0;
PRAGMA temp_store=FILE;
PRAGMA cache_size=-65536;
CREATE TABLE run (
 id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL, schema_version INTEGER NOT NULL,
 source_database TEXT NOT NULL, field_database TEXT NOT NULL,
 source_context_json TEXT NOT NULL, failure_code TEXT);
CREATE TABLE rules (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, definition_json TEXT NOT NULL);
CREATE TABLE contexts (
 id INTEGER PRIMARY KEY, rule_id INTEGER NOT NULL REFERENCES rules(id),
 context_json TEXT NOT NULL, context_hash TEXT NOT NULL, group_count INTEGER NOT NULL,
 UNIQUE(rule_id,context_json));
CREATE TABLE outcome_frequencies (
 context_id INTEGER NOT NULL REFERENCES contexts(id), outcome_json TEXT NOT NULL,
 frequency INTEGER NOT NULL, PRIMARY KEY(context_id,outcome_json));
-- Only probability ratios use REAL; source numeric tokens always remain TEXT.
CREATE VIEW conditional_probabilities AS
 SELECT f.context_id,f.outcome_json,f.frequency AS numerator,c.group_count AS denominator,
        CAST(f.frequency AS REAL)/c.group_count AS probability
 FROM outcome_frequencies f JOIN contexts c ON c.id=f.context_id;
CREATE TABLE context_resources (
 context_id INTEGER NOT NULL REFERENCES contexts(id), resource_id INTEGER NOT NULL,
 patient_resource_id INTEGER, group_count INTEGER NOT NULL,
 PRIMARY KEY(context_id,resource_id));
CREATE TABLE context_support (
 context_id INTEGER PRIMARY KEY REFERENCES contexts(id), resource_count INTEGER NOT NULL,
 patient_count INTEGER NOT NULL, unassigned_group_count INTEGER NOT NULL,
 unassigned_resource_count INTEGER NOT NULL);
CREATE TABLE numeric_frequencies (
 context_id INTEGER NOT NULL REFERENCES contexts(id), target_index INTEGER NOT NULL,
 value_json TEXT NOT NULL, frequency INTEGER NOT NULL,
 PRIMARY KEY(context_id,target_index,value_json));
CREATE TABLE numeric_summaries (
 context_id INTEGER NOT NULL REFERENCES contexts(id), target_index INTEGER NOT NULL,
 sample_count INTEGER NOT NULL, minimum_json TEXT, maximum_json TEXT,
 quantiles_json TEXT NOT NULL, estimator TEXT NOT NULL, PRIMARY KEY(context_id,target_index));
CREATE TABLE exclusions (
 rule_id INTEGER NOT NULL REFERENCES rules(id), reasons_json TEXT NOT NULL,
 frequency INTEGER NOT NULL, PRIMARY KEY(rule_id,reasons_json));
"""


def _link_failures(group, statistics):
    """Reject unsupported link contexts without splitting or multiplying groups.

    All failing links/reasons form one exclusion category. Thus a group failing
    two links is still counted once, and exclusion frequencies sum correctly.
    Links not used by these statistics cannot exclude a group.
    """
    required = {ref["link"] for ref in statistics["given"] + statistics["targets"] if "link" in ref}
    failures = []
    for name in sorted(required):
        link = group["links"][name]
        reasons = {ref["status"] for ref in link["references"] if ref["status"] != "resolved"}
        if len(link["targets"]) > 1:
            reasons.add("multiple_targets")
        elif not link["targets"] and not reasons:
            reasons.add("unresolved")
        if reasons:
            failures.append({"link": name, "reasons": sorted(reasons)})
    return failures


def _matches(group, reference):
    if "link" in reference:
        return group["links"][reference["link"]]["targets"][0]["fields"][reference["field"]]
    return group["fields"][reference["field"]]


def _content(matches):
    """Remove provenance from keys while retaining selection state and structure.

    Relative paths preserve nested array boundaries. Absolute anchor positions,
    node IDs, patient IDs and resource IDs would incorrectly split equal content
    into separate contexts. Numeric value_json tokens are strings inside this
    envelope, so 1, 1.0, true and \"1\" remain distinct exact outcomes.
    """
    values = []
    for match in matches:
        entry = {"status": match["status"], "path": match["relative_path"]}
        if match["status"] == "present":
            entry.update(kind=match["node"].kind, value_json=match["value_json"])
        else:
            entry.update(context_kind=match["context"].kind, remaining_path=match["remaining_path"])
        values.append(entry)
    return values


def _count_group(db, group, rule_id, statistics):
    failures = _link_failures(group, statistics)
    if failures:
        db.execute("INSERT INTO exclusions VALUES (?,?,1) ON CONFLICT(rule_id,reasons_json) DO UPDATE SET frequency=frequency+1",
                   (rule_id, dumps(failures)))
        return
    given = [_content(_matches(group, ref)) for ref in statistics["given"]]
    targets = [_matches(group, ref) for ref in statistics["targets"]]
    context = dumps(given)
    outcome = dumps([_content(matches) for matches in targets])
    # The full context, not its hash, defines equality. The digest is just a
    # stable report label that does not copy context string samples into JSON.
    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()
    db.execute("INSERT INTO contexts(rule_id,context_json,context_hash,group_count) VALUES (?,?,?,1) "
               "ON CONFLICT(rule_id,context_json) DO UPDATE SET group_count=group_count+1", (rule_id, context, digest))
    cid = db.execute("SELECT id FROM contexts WHERE rule_id=? AND context_json=?", (rule_id, context)).fetchone()[0]
    db.execute("INSERT INTO outcome_frequencies VALUES (?,?,1) ON CONFLICT(context_id,outcome_json) DO UPDATE SET frequency=frequency+1", (cid, outcome))
    db.execute("INSERT INTO context_resources VALUES (?,?,?,1) ON CONFLICT(context_id,resource_id) DO UPDATE SET group_count=group_count+1",
               (cid, group["resource_id"], group["patient_resource_id"]))
    for index, matches in enumerate(targets):
        # A repeated collection is a joint outcome, not several independent
        # measurements. Summaries use only exactly one present numeric match.
        if len(matches) == 1 and matches[0]["status"] == "present" and matches[0]["node"].kind == "number":
            db.execute("INSERT INTO numeric_frequencies VALUES (?,?,?,1) "
                       "ON CONFLICT(context_id,target_index,value_json) DO UPDATE SET frequency=frequency+1",
                       (cid, index, matches[0]["value_json"]))


def _summarize(db):
    db.execute("INSERT INTO context_support SELECT context_id,count(*),count(DISTINCT patient_resource_id),"
               "sum(CASE WHEN patient_resource_id IS NULL THEN group_count ELSE 0 END),"
               "sum(CASE WHEN patient_resource_id IS NULL THEN 1 ELSE 0 END) "
               "FROM context_resources GROUP BY context_id")
    db.create_collation("EXACT_DECIMAL", compare_decimals)
    for cid, target, count in db.execute("SELECT context_id,target_index,sum(frequency) FROM numeric_frequencies GROUP BY context_id,target_index"):
        rows = db.execute("SELECT value_json,frequency FROM numeric_frequencies WHERE context_id=? AND target_index=? "
                          "ORDER BY value_json COLLATE EXACT_DECIMAL,value_json COLLATE BINARY", (cid, target))
        minimum, maximum, quantiles = numeric_summary(rows, count)
        db.execute("INSERT INTO numeric_summaries VALUES (?,?,?,?,?,?,?)",
                   (cid, target, count, minimum, maximum, dumps(quantiles), "weighted_nearest_rank"))


def _rule_reports(db):
    for rule in db.execute("SELECT * FROM rules ORDER BY name"):
        rid = rule["id"]
        included, contexts = db.execute("SELECT coalesce(sum(group_count),0),count(*) FROM contexts WHERE rule_id=?", (rid,)).fetchone()
        excluded = db.execute("SELECT coalesce(sum(frequency),0) FROM exclusions WHERE rule_id=?", (rid,)).fetchone()[0]
        yield {"name": rule["name"], "definition": loads(rule["definition_json"]),
               "groups_seen": included + excluded, "groups_included": included, "groups_excluded": excluded,
               "contexts": contexts, "exclusions": [
                   {"reasons": loads(row[0]), "groups": row[1]} for row in db.execute(
                       "SELECT reasons_json,frequency FROM exclusions WHERE rule_id=? ORDER BY reasons_json", (rid,))]}


def _context_reports(db):
    query = ("SELECT c.*,s.*,r.name FROM contexts c JOIN context_support s ON s.context_id=c.id "
             "JOIN rules r ON r.id=c.rule_id ORDER BY r.name,c.context_json")
    for context in db.execute(query):
        cid = context["id"]
        numbers = []
        for row in db.execute("SELECT * FROM numeric_summaries WHERE context_id=? ORDER BY target_index", (cid,)):
            numbers.append({"target_index": row["target_index"], "sample_count": row["sample_count"],
                            "minimum": loads(row["minimum_json"]), "maximum": loads(row["maximum_json"]),
                            "quantiles": loads(row["quantiles_json"]), "estimator": row["estimator"]})
        yield {"rule": context["name"], "context_hash": context["context_hash"],
               "group_count": context["group_count"], "resource_count": context["resource_count"],
               "patient_count": context["patient_count"], "unassigned_group_count": context["unassigned_group_count"],
               "unassigned_resource_count": context["unassigned_resource_count"],
               "distinct_joint_outcomes": db.execute("SELECT count(*) FROM outcome_frequencies WHERE context_id=?", (cid,)).fetchone()[0],
               "numbers": numbers}


def _write_report(path, header, db):
    """Stream contexts to a partial report; exact string samples stay in SQLite."""
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        stream.write("{\n")
        for name, value in header.items():
            stream.write("  " + dumps(name) + ": " + pretty_json(value, 1) + ",\n")
        for section, rows in (("rules", _rule_reports(db)), ("contexts", _context_reports(db))):
            stream.write("  " + dumps(section) + ": [")
            separator = "\n"
            for row in rows:
                stream.write(separator + "    " + pretty_json(row, 2))
                separator = ",\n"
            stream.write("\n  ]" + (",\n" if section == "rules" else "\n}"))
        stream.write("\n")


def profile_conditional(cohort_db, field_db, rules, output_dir):
    """Write exact conditional distributions from completed, matching inputs.

    rules is the JSON dictionary returned by load_rules(). Existing definitions
    without statistics are validated but skipped. The return value is the report
    header; the database and JSON report contain the complete results.
    """
    compiled = _compile(rules)
    active = {rule["name"]: rule for rule in compiled if "statistics" in rule}
    if not active:
        raise DependencyError("No statistics definitions were configured.")
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise DependencyError("Output already exists; choose a new output directory.")
    selected = {"schema_version": 1, "rules": [r for r in rules["rules"] if r["name"] in active]}
    # Keep the source metadata snapshot open while the grouping API validates
    # its inputs and streams groups. Priming also validates zero-match runs
    # before creating output; it retains at most one initial group.
    with open_source(cohort_db) as (source, source_run, source_path):
        with closing(iter_groups(cohort_db, field_db, selected, include_values=True)) as groups:
            first = next(groups, None)
            with closing(sqlite3.connect(Path(field_db).resolve().as_uri() + "?mode=ro", uri=True)) as field_meta:
                field_status = field_meta.execute("SELECT status FROM run").fetchone()[0]
            source_context = {"ingestion_status": source_run["status"], "field_status": field_status,
                              "cohort_fingerprint": source_fingerprint(source),
                              "ingestion_schema_version": 1, "field_schema_version": 1,
                              "fhir_target": source_run["fhir_version"], "issues": [dict(row) for row in source.execute(
                                  "SELECT severity,code,count(*) AS frequency FROM issues GROUP BY severity,code ORDER BY severity,code")]}
            output.parent.mkdir(parents=True, exist_ok=True)
            output.mkdir(mode=0o700)
            db_path = output / "conditional-statistics.sqlite"
            with db_path.open("xb"):
                pass
            db_path.chmod(0o600)
            with closing(sqlite3.connect(db_path)) as db:
                db.row_factory = sqlite3.Row
                try:
                    db.executescript(SCHEMA)
                    db.execute("INSERT INTO run VALUES (1,'in_progress',1,?,?,?,NULL)",
                               (str(source_path), str(Path(field_db).resolve()), dumps(source_context)))
                    rule_ids = {}
                    for name in sorted(active):
                        rule_ids[name] = db.execute("INSERT INTO rules(name,definition_json) VALUES (?,?)",
                                                   (name, dumps(active[name]))).lastrowid
                    db.commit()
                    for count, group in enumerate(chain(() if first is None else (first,), groups), 1):
                        _count_group(db, group, rule_ids[group["rule"]], active[group["rule"]]["statistics"])
                        if count % BATCH_SIZE == 0:
                            db.commit()
                    db.commit()
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    _summarize(db)
                    db.commit()
                    db.execute("PRAGMA journal_mode=DELETE")
                    included = db.execute("SELECT coalesce(sum(group_count),0) FROM contexts").fetchone()[0]
                    excluded = db.execute("SELECT coalesce(sum(frequency),0) FROM exclusions").fetchone()[0]
                    status = "completed_with_warnings" if excluded or "completed_with_warnings" in (source_run["status"], field_status) else "completed"
                    header = {"schema_version": 1, "status": status, "data_classification": "source_derived_data_not_synthetic",
                              # The index fingerprint includes local row IDs for
                              # safe later joins. Keep it in SQLite provenance,
                              # not in order-independent aggregate JSON reports.
                              "source": {k: v for k, v in source_context.items() if k != "cohort_fingerprint"},
                              "weighting": "one_count_per_anchor_within_each_rule",
                              "skipped_rules": sorted(r["name"] for r in compiled if r["name"] not in active),
                              "counts": {"rules": len(active), "groups_seen": included + excluded,
                                         "groups_included": included, "groups_excluded": excluded,
                                         "contexts": db.execute("SELECT count(*) FROM contexts").fetchone()[0]},
                              "statistics": {"frequencies": "exact_joint_outcomes_in_sqlite",
                                             "probabilities": "numerator_denominator_and_ratio_in_conditional_probabilities_view",
                                             "numeric_estimator": "weighted_nearest_rank", "percentiles": [p for _, p in QUANTILES],
                                             "numeric_population": "targets_with_exactly_one_present_numeric_match",
                                             "smoothing": False, "sampling_performed": False}}
                    partial = output / ".conditional-statistics.json.partial"
                    _write_report(partial, header, db)
                    partial.rename(output / "conditional-statistics.json")
                    db.execute("UPDATE run SET status=?", (status,))
                    db.commit()
                    return header
                except BaseException as error:
                    try:
                        db.rollback()
                        status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
                        db.execute("UPDATE run SET status=?,failure_code=?", (status, status))
                        db.commit()
                    except sqlite3.Error:
                        pass  # A remaining in_progress marker also means incomplete.
                    raise
