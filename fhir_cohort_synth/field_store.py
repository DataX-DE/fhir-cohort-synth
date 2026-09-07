"""SQLite storage for extracted JSON nodes and their exact distributions.

The tables separate source provenance, field definitions and node occurrences.
Distribution tables are populated later by field_statistics.py. Numeric tokens
are TEXT so SQLite never rounds them through its floating-point storage type.
"""
from functools import lru_cache
import sqlite3

from .json_fields import display_path, path_json, walk_json


SCHEMA = """
PRAGMA foreign_keys=ON;
-- Temporary bulk-write mode; profiling checkpoints back to DELETE before
-- publishing reports, so completed outputs need no WAL/SHM sidecar files.
PRAGMA journal_mode=WAL;
-- Avoid repeatedly rewriting growing indexes during small commit batches.
-- The orchestrator checkpoints explicitly between extraction and aggregation.
PRAGMA wal_autocheckpoint=0;
PRAGMA temp_store=FILE;
-- A bounded 64 MiB cache reduces index-page churn during bulk extraction.
-- Temporary aggregation data still spills to disk, not cohort-sized lists.
PRAGMA cache_size=-65536;
CREATE TABLE run (
 id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL,
 schema_version INTEGER NOT NULL, source_schema_version INTEGER NOT NULL,
 source_database TEXT NOT NULL, source_status TEXT NOT NULL,
 source_fhir_version TEXT NOT NULL, failure_code TEXT);
CREATE TABLE source_resources (
 resource_id INTEGER PRIMARY KEY, resource_type TEXT NOT NULL,
 identity TEXT NOT NULL, digest TEXT NOT NULL);
CREATE INDEX source_resources_type ON source_resources(resource_type);
CREATE TABLE source_occurrences (
 occurrence_id INTEGER PRIMARY KEY,
 resource_id INTEGER NOT NULL REFERENCES source_resources(resource_id),
 source_path TEXT, locator TEXT, context TEXT, full_url TEXT);
CREATE TABLE source_profiles (
 resource_id INTEGER NOT NULL REFERENCES source_resources(resource_id),
 canonical TEXT, version TEXT, module TEXT);
CREATE TABLE source_issues (severity TEXT, code TEXT, frequency INTEGER);
CREATE TABLE fields (
 id INTEGER PRIMARY KEY, resource_type TEXT NOT NULL, path TEXT NOT NULL,
 display_path TEXT NOT NULL, parent_path TEXT,
 relation TEXT NOT NULL CHECK(relation IN ('root','key','item')), field_name TEXT,
 UNIQUE(resource_type,path));
CREATE TABLE nodes (
 id INTEGER PRIMARY KEY,
 resource_id INTEGER NOT NULL REFERENCES source_resources(resource_id),
 ordinal INTEGER NOT NULL, parent_id INTEGER REFERENCES nodes(id),
 field_id INTEGER NOT NULL REFERENCES fields(id), concrete_path TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('object','array','number','string','boolean','null')),
 scalar_json TEXT, number_format TEXT, string_length INTEGER,
 array_index INTEGER, array_length INTEGER, object_keys_json TEXT, array_types_json TEXT,
 UNIQUE(resource_id,ordinal));
CREATE INDEX nodes_field_kind ON nodes(field_id,kind);
-- Cover both presence denominators and array child-type counts without
-- repeatedly seeking into the much larger node table for parent/type values.
CREATE INDEX nodes_field_resource ON nodes(field_id,resource_id,parent_id);
CREATE INDEX nodes_parent ON nodes(parent_id,kind);
CREATE TABLE field_summaries (
 field_id INTEGER PRIMARY KEY REFERENCES fields(id), occurrences INTEGER NOT NULL,
 resources_present INTEGER NOT NULL, resource_population INTEGER NOT NULL,
 eligible_parents INTEGER NOT NULL, present_parents INTEGER NOT NULL,
 absent_parents INTEGER NOT NULL, presence_unit TEXT NOT NULL);
CREATE TABLE type_frequencies (
 field_id INTEGER REFERENCES fields(id), kind TEXT, frequency INTEGER NOT NULL,
 PRIMARY KEY(field_id,kind));
CREATE TABLE scalar_frequencies (
 field_id INTEGER REFERENCES fields(id), kind TEXT, value_json TEXT,
 frequency INTEGER NOT NULL, PRIMARY KEY(field_id,kind,value_json));
CREATE TABLE number_format_frequencies (
 field_id INTEGER REFERENCES fields(id), number_format TEXT, frequency INTEGER NOT NULL,
 PRIMARY KEY(field_id,number_format));
CREATE TABLE object_shape_frequencies (
 field_id INTEGER REFERENCES fields(id), keys_json TEXT, frequency INTEGER NOT NULL,
 PRIMARY KEY(field_id,keys_json));
CREATE TABLE array_length_frequencies (
 field_id INTEGER REFERENCES fields(id), length INTEGER, frequency INTEGER NOT NULL,
 PRIMARY KEY(field_id,length));
CREATE TABLE array_type_pattern_frequencies (
 field_id INTEGER REFERENCES fields(id), types_json TEXT, frequency INTEGER NOT NULL,
 PRIMARY KEY(field_id,types_json));
CREATE TABLE array_element_type_frequencies (
 field_id INTEGER REFERENCES fields(id), kind TEXT, frequency INTEGER NOT NULL,
 PRIMARY KEY(field_id,kind));
CREATE TABLE string_length_frequencies (
 field_id INTEGER REFERENCES fields(id), length INTEGER, frequency INTEGER NOT NULL,
 PRIMARY KEY(field_id,length));
CREATE TABLE numeric_summaries (
 field_id INTEGER PRIMARY KEY REFERENCES fields(id), sample_count INTEGER NOT NULL,
 minimum_json TEXT NOT NULL, maximum_json TEXT NOT NULL,
 quantiles_json TEXT NOT NULL, estimator TEXT NOT NULL);
"""


class FieldStore:
    """Own the output connection; the orchestrator controls completion/closing."""

    def __init__(self, path, source_path, source_run):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.executescript(SCHEMA)
            self.db.execute("INSERT INTO run VALUES (1,'in_progress',1,?,?,?,?,NULL)",
                            (source_run["schema_version"], str(source_path),
                             source_run["status"], source_run["fhir_version"]))
            self.db.commit()
        except BaseException:
            self.db.close()
            raise
        # Bound the lookup cache independently of the number of distinct paths
        # in the cohort (object keys may themselves be high cardinality).
        self.field_id = lru_cache(maxsize=2048)(self._field_id)

    def _field_id(self, resource_type, path):
        """Register one statistical path within its root-resource population."""
        encoded = path_json(path)
        relation = path[-1][0] if path else "root"
        name = path[-1][1] if relation == "key" else None
        self.db.execute("INSERT OR IGNORE INTO fields(resource_type,path,display_path,parent_path,relation,field_name) VALUES (?,?,?,?,?,?)",
                        (resource_type, encoded, display_path(path), path_json(path[:-1]) if path else None, relation, name))
        return self.db.execute("SELECT id FROM fields WHERE resource_type=? AND path=?", (resource_type, encoded)).fetchone()[0]

    def add_resource(self, source, resource, value):
        """Store one root tree, keeping all duplicate source occurrences as provenance.

        Only the source payload currently being traversed and its ordinal-to-ID
        map are held in memory. A global node ID connects children to parents
        without confusing fields from different resources or array elements.
        """
        rid = resource["id"]
        self.db.execute("INSERT INTO source_resources VALUES (?,?,?,?)",
                        (rid, resource["resource_type"], resource["identity"], resource["digest"]))
        self.db.executemany("INSERT INTO source_occurrences VALUES (?,?,?,?,?,?)", source.execute(
            "SELECT o.id,o.resource_id,s.path,o.locator,o.context,o.full_url "
            "FROM occurrences o JOIN sources s ON s.id=o.source_id WHERE o.resource_id=? ORDER BY o.id", (rid,)))
        self.db.executemany("INSERT INTO source_profiles VALUES (?,?,?,?)", source.execute(
            "SELECT resource_id,canonical,version,module FROM profiles WHERE resource_id=?", (rid,)))
        node_ids = {}
        for node in walk_json(value):
            parent_id = node_ids[node.parent_ordinal] if node.parent_ordinal is not None else None
            field_id = self.field_id(resource["resource_type"], node.statistical_path)
            cursor = self.db.execute(
                "INSERT INTO nodes(resource_id,ordinal,parent_id,field_id,concrete_path,kind,scalar_json,number_format,string_length,array_index,array_length,object_keys_json,array_types_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, node.ordinal, parent_id, field_id, path_json(node.path), node.kind,
                 node.scalar_json, node.number_format, node.string_length, node.array_index,
                 node.array_length, node.object_keys_json, node.array_types_json))
            node_ids[node.ordinal] = cursor.lastrowid

    def mark_failed(self, interrupted=False):
        """Best-effort failure marker; never replace the original exception."""
        try:
            self.db.rollback()
            status = "interrupted" if interrupted else "failed"
            self.db.execute("UPDATE run SET status=?,failure_code=?", (status, status))
            self.db.commit()
        except sqlite3.Error:
            # Disk failure may prevent even this marker. 'in_progress' still
            # means unusable, so success is never inferred from partial rows.
            pass

    def close(self):
        self.field_id.cache_clear()
        self.db.close()
