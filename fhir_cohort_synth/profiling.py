"""Coordinate read-only source inspection, extraction, aggregation and reports.

The source ingestion database is opened with SQLite mode=ro and read through a
single transaction. Output completion is recorded only after both reports have
been fully written. Failed/interrupted output must be retried in a new folder.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3

from .field_statistics import aggregate, field_reports
from .field_store import FieldStore
from .ingest import InputError
from .jsonio import dumps, loads


class ProfileError(InputError):
    """Fixed diagnostic text suitable for the CLI, without source values."""


def source_fingerprint(source):
    """Identify a cohort snapshot independently of its filesystem location.

    Generation combines conditional values with patient/encounter counts. Both
    must come from the same resources AND resolved graph. Stream the signature
    rather than collecting the cohort; source strings never enter the report.
    """
    digest = hashlib.sha256()
    for query in (
        "SELECT id,identity,digest,resource_type,contained FROM resources ORDER BY id",
        "SELECT resource_id,patient_resource_id,basis FROM patient_memberships ORDER BY resource_id",
        "SELECT source_resource_id,path,literal,kind,target_resource_id,status FROM resource_references ORDER BY id",
    ):
        digest.update(query.encode())
        for row in source.execute(query):
            digest.update(dumps(list(row)).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


@contextmanager
def open_source(path):
    """Validate an existing schema-1 index before creating any output files."""
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise ProfileError("Input must be an existing ingestion database file, not a symlink.")
    source = None
    try:
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        runs = source.execute("SELECT status,schema_version,fhir_version FROM run").fetchall()
        if len(runs) != 1 or runs[0]["schema_version"] != 1:
            raise ProfileError("Unsupported ingestion database schema; expected version 1.")
        run = runs[0]
        if run["status"] not in {"completed", "completed_with_warnings"}:
            raise ProfileError("The ingestion run is incomplete; profile a completed index.")
        # Check the columns we actually consume. Matching a version label alone
        # is not enough to accept a damaged or unrelated SQLite database.
        for query in (
            "SELECT id,resource_type,identity,digest,contained,payload FROM resources LIMIT 0",
            "SELECT id,resource_id,source_id,locator,context,full_url FROM occurrences LIMIT 0",
            "SELECT id,path FROM sources LIMIT 0",
            "SELECT resource_id,canonical,version,module FROM profiles LIMIT 0",
            "SELECT severity,code FROM issues LIMIT 0",
        ):
            source.execute(query)
        if source.execute("SELECT count(*) FROM issues WHERE severity='error'").fetchone()[0]:
            raise ProfileError("The source index contains ingestion errors; resolve them before profiling.")
        if not source.execute("SELECT 1 FROM resources WHERE contained=0 LIMIT 1").fetchone():
            raise ProfileError("The source index has no non-contained resource roots to profile.")
    except sqlite3.Error:
        if source is not None:
            source.close()
        raise ProfileError("Input is not a readable, supported ingestion database.") from None
    except BaseException:
        if source is not None:
            source.close()
        raise
    try:
        yield source, run, path.resolve()
    finally:
        source.close()


def pretty_json(value, level=0):
    """Indent a report fragment while preserving Decimal tokens as JSON numbers."""
    if isinstance(value, dict) and value:
        parts = ["  " * (level + 1) + dumps(key) + ": " + pretty_json(child, level + 1)
                 for key, child in value.items()]
        return "{\n" + ",\n".join(parts) + "\n" + "  " * level + "}"
    if isinstance(value, list) and value:
        return "[\n" + ",\n".join("  " * (level + 1) + pretty_json(child, level + 1) for child in value) + "\n" + "  " * level + "]"
    return dumps(value)


def write_report(path, header, fields):
    """Stream fields into one report; never assemble the whole report in memory."""
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        stream.write("{\n")
        for key, value in header.items():
            stream.write("  " + dumps(key) + ": " + pretty_json(value, 1) + ",\n")
        stream.write('  "fields": [')
        separator = "\n"
        for field in fields:
            stream.write(separator + "    " + pretty_json(field, 2))
            separator = ",\n"
        stream.write("\n  ]\n}\n")


def profile_index(input_db, output_dir):
    """Create exact field profiles from a completed ingestion index.

    The returned dictionary is the compact report header. Complete distributions
    and node occurrences live in the local SQLite output, not in memory or the
    console. Root-resource populations intentionally include contained JSON
    under its original parent path, never as an additional independent root.
    """
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise ProfileError("Output already exists; choose a new output directory.")
    with open_source(input_db) as (source, source_run, source_path):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir(mode=0o700)
        db_path = output / "field-occurrences.sqlite"
        with db_path.open("xb"):
            pass
        db_path.chmod(0o600)
        store = FieldStore(db_path, source_path, source_run)
        try:
            # 1. Preserve issue context, then extract all deduplicated roots.
            issues = [dict(row) for row in source.execute("SELECT severity,code,count(*) AS frequency FROM issues GROUP BY severity,code ORDER BY severity,code")]
            store.db.executemany("INSERT INTO source_issues VALUES (?,?,?)",
                                 ((row["severity"], row["code"], row["frequency"]) for row in issues))
            for number, resource in enumerate(source.execute("SELECT * FROM resources WHERE contained=0 ORDER BY id"), 1):
                try:
                    value = loads(resource["payload"])
                    if not isinstance(value, dict) or value.get("resourceType") != resource["resource_type"]:
                        raise ValueError("Invalid root")
                    dumps(value).encode("utf-8")
                except (ValueError, UnicodeError, RecursionError, ArithmeticError):
                    raise ProfileError("A source resource payload is invalid; regenerate the ingestion index.") from None
                store.add_resource(source, resource, value)
                # Bound uncommitted extraction work while preserving the
                # in_progress marker for an interrupted run. Batches of 1,000
                # avoid a disk synchronization after every 100 small records.
                if number % 1000 == 0:
                    store.db.commit()
            store.db.commit()

            # 2. Populate exact frequency tables and weighted numeric summaries.
            # Aggregate from the compact database rather than the larger WAL
            # containing repeated versions of growing index pages.
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            aggregate(store.db)
            store.db.commit()
            # Return a single portable database, with bulk WAL data fully
            # checkpointed before either report or the completion marker.
            store.db.execute("PRAGMA journal_mode=DELETE")
            status = "completed_with_warnings" if source_run["status"] == "completed_with_warnings" or any(row["severity"] == "warning" for row in issues) else "completed"
            roots = store.db.execute("SELECT count(*) FROM source_resources").fetchone()[0]
            occurrences = store.db.execute("SELECT count(*) FROM source_occurrences").fetchone()[0]
            header = {
                "schema_version": 1, "status": status,
                "data_classification": "source_derived_data_not_synthetic",
                "population": "deduplicated_non_contained_roots_with_contained_subtrees",
                "grouping": ["root_resource_type", "typed_normalized_path"],
                "counts": {"root_resources": roots,
                           "source_root_occurrences": occurrences,
                           "duplicate_root_occurrences_excluded": occurrences - roots,
                           "separate_contained_rows_excluded": source.execute("SELECT count(*) FROM resources WHERE contained<>0").fetchone()[0],
                           "nodes": store.db.execute("SELECT count(*) FROM nodes").fetchone()[0],
                           "fields": store.db.execute("SELECT count(*) FROM fields").fetchone()[0]},
                "resource_types": dict(store.db.execute("SELECT resource_type,count(*) FROM source_resources GROUP BY resource_type ORDER BY resource_type")),
                "source": {"schema_version": source_run["schema_version"], "status": source_run["status"],
                           "fhir_target": source_run["fhir_version"], "issues": issues},
                "statistics": {"value_frequencies": "exact_typed_json_tokens_in_sqlite",
                               "numeric_quantiles": "weighted_nearest_rank", "percentiles": [5, 25, 50, 75, 95],
                               "probabilities": "ratios_of_exact_counts_rendered_as_json_numbers",
                               "dependencies_modeled": False, "clinical_interpretation_performed": False},
            }

            # 3. Publish fully written reports before setting the success marker.
            # Partial files and any noncompleted run status are never usable
            # results. A second-file failure cannot mark the database complete.
            for name, statistics in [("field-inventory.json", False), ("field-statistics.json", True)]:
                partial = output / ("." + name + ".partial")
                write_report(partial, header, field_reports(store.db, statistics))
            for name in ("field-inventory.json", "field-statistics.json"):
                (output / ("." + name + ".partial")).rename(output / name)
            store.db.execute("UPDATE run SET status=?", (status,))
            store.db.commit()
            return header
        except BaseException as error:
            store.mark_failed(interrupted=isinstance(error, KeyboardInterrupt))
            raise
        finally:
            store.close()
