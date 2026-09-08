"""Coordinate an offline ingestion run, from input files to the local index.

Start reading at ``ingest()`` for the overall flow:
    discover files -> parse and store resources -> resolve links -> group patients
    -> write reports.

This module handles files and the run lifecycle. ``store.py`` exposes the
database phases; resource_store.py, references.py and patient_groups.py implement
them. Ingestion does not fetch URLs or modify source files; the resulting index
still contains source patient data.
"""
import gzip
import json
import zlib
from pathlib import Path
from urllib.parse import urlsplit

from .jsonio import dumps, loads
from .store import Store
from .progress import notify, track

SUFFIXES = (".json", ".ndjson", ".jsonl", ".json.gz", ".ndjson.gz", ".jsonl.gz")
INGEST_CHECKPOINT_LINES = 10000


class InputError(ValueError):
    """Configuration error safe to display without source patient data."""


def discover(inputs, output):
    """Return sorted, unique source paths and a new output directory path.

    Inputs can mix individual files and directories. Validation happens before
    creating output, so a bad path does not leave a partially initialized run.
    """
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise InputError("Output already exists; choose a new output directory.")
    # A file can be supplied explicitly and through its containing directory.
    # Read that path only once, regardless of how many input arguments find it.
    files = set()
    for item in inputs:
        path = Path(item).absolute()
        if path.is_symlink():
            raise InputError("Input symlinks are unsupported; select the real file or directory.")
        path = path.resolve()
        if path.is_dir():
            # Otherwise a later run might ingest its own JSON report as input.
            if output.resolve().is_relative_to(path):
                raise InputError("Output must be outside every input directory.")
            # Path.walk is unavailable on Python 3.11. rglob does not descend
            # into symlink directories; explicit symlink entries are rejected.
            for child in sorted(path.rglob("*")):
                if child.is_symlink():
                    raise InputError("Input directory contains a symlink; use an export without symlinks.")
                if child.is_file() and child.name.lower().endswith(SUFFIXES):
                    files.add(child)
        elif path.is_file() and path.name.lower().endswith(SUFFIXES):
            files.add(path)
        else:
            raise InputError("Input must be a readable JSON/NDJSON/JSONL file (optionally gzip) or directory.")
    if not files:
        raise InputError("No supported input files found.")
    # Stable ordering also makes run-local resource IDs easier to reproduce.
    return sorted(files), output


def normalize_base_url(value):
    """Validate an optional server identity; this never connects to the server.

    The base is used only when an exported resource lacks a full URL, e.g.
    'https://example.invalid/fhir' + '/Patient/p1'. Credentials and request
    parameters do not belong in this resource identity prefix.
    """
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and not any(c.isspace() for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise InputError("Base URL must be an HTTP(S) FHIR server base without credentials, query or fragment.")
    return value.rstrip("/")


def read_source(store, path, source_id, *, progress=None, stage='Reading FHIR file'):
    """Parse one file and register each resource with its source location.

    ``source_id`` identifies a row in SQLite's sources table. A ``locator``
    identifies a JSON document or NDJSON line within that file. Bundle/contained
    positions are appended by Store.add_document() when it descends further.

    Bad NDJSON lines become issues without discarding subsequent valid lines.
    A file/decompression failure stops this file; ingestion can continue with
    the remaining files, and the report marks the run incomplete.
    """
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    stem = path.name.lower().removesuffix(".gz")
    ndjson = stem.endswith((".ndjson", ".jsonl"))
    try:
        with opener(path, "rt", encoding="utf-8-sig") as stream:
            # NDJSON streams one line at a time. Ordinary JSON must be parsed
            # as a whole document, so its largest file determines memory use
            # during this parsing step. utf-8-sig accepts an optional BOM.
            documents = enumerate(stream, 1) if ndjson else [(1, stream.read())]
            for line, raw in track(documents, progress, stage, unit='lines read' if ndjson else 'documents read'):
                # Checkpoint the previous batch before starting another
                # document. Checking here also handles blank or invalid lines
                # at the boundary. Bundle/contained children stay atomic, and
                # interrupted progress still has run.status='in_progress'.
                if ndjson and line > 1 and (line - 1) % INGEST_CHECKPOINT_LINES == 0:
                    store.db.commit()
                locator = f"line:{line}" if ndjson else "$"
                if ndjson and not raw.strip():
                    continue
                try:
                    resource = loads(raw)
                    # Reject lone surrogate escapes that cannot be encoded as
                    # UTF-8 before they reach SQLite or a diagnostic message.
                    dumps(resource).encode("utf-8")
                except (ValueError, RecursionError, UnicodeError):
                    # Parser exception text may quote patient data. Record only
                    # an issue code and locator, not the raw line or exception.
                    store.issue("invalid_json", "error", source_id, locator)
                    continue
                store.add_document(resource, source_id, locator)
    except (OSError, EOFError, UnicodeError, zlib.error):
        store.issue("source_read_error", "error", source_id)


def text_report(report):
    """Format a short human-readable summary of the aggregate JSON report."""
    lines = ["FHIR ingestion report", f"Status: {report['status']}",
             "Data: source patient data; this stage does not generate synthetic data.",
             "Target: FHIR R4 (4.0.1); hospital version is unconfirmed.",
             "Checks: ingestion and references; full FHIR/MII profile validation has not run.", ""]
    lines += [f"{key.replace('_', ' ').capitalize()}: {value}" for key, value in report["counts"].items()]
    lines += ["", "References:"] + [f"  {k}: {v}" for k, v in report["reference_status"].items()]
    lines += ["", "Issues:"] + [f"  {i['severity']}: {i['code']} ({i['count']})" for i in report["issues"]]
    return "\n".join(lines) + "\n"


def ingest(inputs, output, base_url=None, *, progress=None):
    """Build a fresh local index and return its aggregate report dictionary.

    Expected data problems are recorded as issues, allowing valid resources to
    be inspected even in an incomplete run. Configuration and infrastructure
    failures propagate to the CLI. The caller checks the report status before
    using an index for perturbation.
    """
    # 1. Validate paths and create a private destination for the source data.
    base_url = normalize_base_url(base_url)
    notify(progress, 'Finding input files')
    files, output = discover(inputs, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # mkdir is exclusive, so a concurrent run cannot replace this directory.
    output.mkdir(mode=0o700)
    return _ingest(files, output, base_url, progress=progress)


def _ingest(files, output, base_url=None, *, progress=None):
    """Write into a private directory already prepared by ingest() or run_export().

    Both callers validate input paths and reserve a fresh output first. Keeping
    directory setup separate lets the workflow put all state in intermediates/.
    """
    db_path = output / "cohort.sqlite"
    with db_path.open("xb"):
        pass
    db_path.chmod(0o600)
    store = Store(db_path, base_url=base_url)
    try:
        # 2. Register all resources before attempting to follow references.
        #    An Encounter may appear before its Patient, even in another file.
        for number, path in enumerate(files, 1):
            stage = f'Reading FHIR file {number}/{len(files)}'
            notify(progress, stage)
            source_id = store.db.execute("INSERT INTO sources(path) VALUES (?)", (str(path),)).lastrowid
            before = store.db.execute("SELECT count(*) FROM occurrences").fetchone()[0]
            read_source(store, path, source_id, progress=progress, stage=stage)
            after = store.db.execute("SELECT count(*) FROM occurrences").fetchone()[0]
            if before == after:
                store.issue("source_without_resources", "warning", source_id)
            # Keep progress for local inspection if a later file fails. The
            # run remains 'in_progress' until the final report is assembled.
            store.db.commit()
        if not store.db.execute("SELECT 1 FROM resources LIMIT 1").fetchone():
            store.issue("no_resources_ingested", "error")
        elif not store.db.execute("SELECT 1 FROM resources WHERE resource_type='Patient' LIMIT 1").fetchone():
            store.issue("no_patient_resources", "warning")
        # 3. Turn literal references into resource IDs, then infer membership
        #    through the resolved patient and encounter relationships.
        # Consolidate append-only bulk writes before random reference lookups.
        # Otherwise reads have to seek through a much larger, fragmented WAL.
        notify(progress, 'Saving source index')
        store.db.commit()
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        notify(progress, 'Resolving resource references')
        store.resolve()
        notify(progress, 'Assigning resources to patients')
        store.group_patients()
        # Finish bulk writes while the run is still in_progress. DELETE mode
        # checkpoints and removes WAL sidecars, making the completed index
        # portable and readable from a directory with read-only permissions.
        notify(progress, 'Saving linked source index')
        store.db.commit()
        store.db.execute("PRAGMA journal_mode=DELETE")
        # 4. Summarize the index and issues, then write both report formats.
        notify(progress, 'Writing ingestion reports')
        report = store.report()
        # Written only after indexing is complete. A missing report always
        # means the run did not finish, even if some database rows exist.
        for name, content in [("report.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n"),
                              ("report.txt", text_report(report))]:
            destination = output / name
            with destination.open("x", encoding="utf-8") as stream:
                stream.write(content)
            destination.chmod(0o600)
        store.db.commit()
        return report
    finally:
        store.db.close()
