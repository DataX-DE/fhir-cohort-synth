"""Read-only checks for the ingestion snapshot used by perturbation.

Check the resource graph and ownership needed to transform an export.
Source FHIR profiles remain ingestion metadata, not a second statistics input.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3

from .ingest import InputError
from .jsonio import dumps


def source_fingerprint(source):
    """Identify a cohort snapshot independently of its filesystem location.

    The signature covers resources, patient ownership and the resolved graph.
    Stream it rather than collecting the cohort; source strings never enter
    the report.
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
        raise InputError("Input must be an existing ingestion database file, not a symlink.")
    source = None
    try:
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        runs = source.execute("SELECT status,schema_version,fhir_version FROM run").fetchall()
        if len(runs) != 1 or runs[0]["schema_version"] != 1:
            raise InputError("Unsupported ingestion database schema; expected version 1.")
        run = runs[0]
        if run["status"] not in {"completed", "completed_with_warnings"}:
            raise InputError("The ingestion run is incomplete; use a completed index.")
        # Check the columns we actually consume. Matching a version label alone
        # is not enough to accept a damaged or unrelated SQLite database.
        for query in (
            "SELECT id,resource_type,identity,digest,logical_id,contained,payload FROM resources LIMIT 0",
            "SELECT severity,code FROM issues LIMIT 0",
            "SELECT resource_id,patient_resource_id,basis FROM patient_memberships LIMIT 0",
            "SELECT source_resource_id,path,literal,kind,status,target_resource_id FROM resource_references LIMIT 0",
        ):
            source.execute(query)
        if source.execute("SELECT count(*) FROM issues WHERE severity='error'").fetchone()[0]:
            raise InputError("The source index contains ingestion errors; resolve them before processing.")
        if not source.execute("SELECT 1 FROM resources WHERE contained=0 LIMIT 1").fetchone():
            raise InputError("The source index has no non-contained resource roots.")
    except sqlite3.Error:
        if source is not None:
            source.close()
        raise InputError("Input is not a readable, supported ingestion database.") from None
    except BaseException:
        if source is not None:
            source.close()
        raise
    try:
        yield source, run
    finally:
        source.close()
