"""Coordinate the ingestion database's four phases.

    add_document()   resource_store.py   Save complete payloads and occurrences.
    resolve()        references.py       Match links after all files are loaded.
    group_patients() patient_groups.py   Derive ownership from resolved links.
    report()         this module         Summarize the index and set run status.

Store owns one connection; ingest.py controls commits and closes it. All
workers share that connection and record problems through issue(). SQLite row
IDs are local index keys, distinct from FHIR IDs and replacement identities.
"""
import sqlite3

from .patient_groups import group_patients
from .references import resolve_references
from .resource_store import ResourceStore
from .schema import SCHEMA


class Store:
    """Open a new index and expose its ingestion phases to ingest.py."""

    def __init__(self, path, base_url=None):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.resources = ResourceStore(self.db, base_url, self.issue)

    def add_document(self, resource, source_id, locator, context=None, full_url=None,
                     parent=None, root=None, depth=0):
        """Store one input document, including Bundle entries and containment."""
        self.resources.add_document(
            resource, source_id, locator, context=context, full_url=full_url,
            parent=parent, root=root, depth=depth,
        )

    def resolve(self):
        """Connect stored references after all potential targets are available."""
        resolve_references(self.db, self.issue)

    def group_patients(self):
        """Use resolved links to assign each resource's patient, if unambiguous."""
        group_patients(self.db, self.issue)

    def issue(self, code, severity="warning", source_id=None, locator=None,
              resource_id=None, detail=None):
        """Record a problem or notice, optionally tied to a source/resource.

        Use fixed codes and structural detail such as a field path. Patient
        values and raw exception messages do not belong in diagnostics.
        """
        self.db.execute(
            """INSERT INTO issues(severity, code, source_id, locator, resource_id, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (severity, code, source_id, locator, resource_id, detail),
        )

    def report(self):
        """Summarize processing counts and set the run's ingestion status.

        Resource counts use deduplicated rows, while reference
        counts reflect occurrences. Patient counts are Patient resource rows,
        not reconciled people. Reports omit source payloads and literal links;
        aggregate counts still require local review.
        """
        def counts(sql):
            """Convert a two-column grouped query into a label-to-count map."""
            return dict(self.db.execute(sql))

        def table_count(table):
            """Count a table named by this code, never by an input resource."""
            return self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

        def rows(sql):
            """Keep column names in the report's issue entries."""
            return [dict(row) for row in self.db.execute(sql)]

        errors = self.db.execute("SELECT count(*) FROM issues WHERE severity='error'").fetchone()[0]
        warnings = self.db.execute("SELECT count(*) FROM issues WHERE severity='warning'").fetchone()[0]
        # These statuses describe ingestion checks, not profile conformance or
        # clinical completeness. The CLI can additionally fail on warnings.
        if errors:
            status = "incomplete"
        elif warnings:
            status = "completed_with_warnings"
        else:
            status = "completed"
        self.db.execute("UPDATE run SET status=?", (status,))
        return {
            "schema_version": 2,
            "status": status,
            "data_classification": "source_patient_data_not_synthetic",
            "fhir": {
                "target_version": "4.0.1",
                "profile_validation_performed": False,
                "hospital_version_confirmed": False,
            },
            "counts": {
                "files": table_count("sources"),
                "resource_occurrences": table_count("occurrences"),
                "unique_resources": table_count("resources"),
                "duplicate_occurrences": table_count("occurrences") - table_count("resources"),
                "patients": self.db.execute(
                    "SELECT count(*) FROM resources WHERE resource_type='Patient'"
                ).fetchone()[0],
                "grouped_resources": table_count("patient_memberships"),
            },
            "resource_types": counts("SELECT resource_type,count(*) FROM resources GROUP BY 1 ORDER BY 1"),
            "reference_status": counts("SELECT status,count(*) FROM resource_references GROUP BY 1 ORDER BY 1"),
            "issues": rows("""
                SELECT severity, code, count(*) AS count
                FROM issues GROUP BY severity, code ORDER BY severity, code
            """),
        }
