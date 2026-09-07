-- Frozen legacy schema used only to test reading existing hospital indexes.
-- Production creates schema 2 and never creates these retired inventories.
PRAGMA foreign_keys=ON;
-- Append bulk changes to a write-ahead log rather than repeatedly syncing a
-- rollback journal when index pages leave the cache. Ingestion checkpoints
-- and returns to DELETE mode before publishing a self-contained database.
PRAGMA journal_mode=WAL;
-- Checkpoint between bulk phases. Automatic checkpoints repeatedly rewrite
-- the growing identity indexes; committed WAL records remain durable meanwhile.
PRAGMA wal_autocheckpoint=0;
-- Keep frequently updated index pages in a bounded 64 MiB page cache. The
-- default 2 MiB cache caused heavy disk churn on the large NDJSON demo.
PRAGMA cache_size=-65536;
CREATE TABLE run (status TEXT NOT NULL, schema_version INTEGER NOT NULL, fhir_version TEXT NOT NULL);
INSERT INTO run VALUES ('in_progress', 1, '4.0.1');
CREATE TABLE sources (id INTEGER PRIMARY KEY, path TEXT NOT NULL);
CREATE TABLE bundles (id INTEGER PRIMARY KEY, source_id INTEGER, locator TEXT,
                      context TEXT, bundle_type TEXT, metadata_json TEXT);
CREATE TABLE bundle_entries (bundle_id INTEGER REFERENCES bundles(id),
                             entry_index INTEGER, metadata_json TEXT);
CREATE TABLE resources (
 id INTEGER PRIMARY KEY, identity TEXT NOT NULL, digest TEXT NOT NULL,
 resource_type TEXT NOT NULL, logical_id TEXT, version_id TEXT, full_url TEXT,
 scope TEXT NOT NULL, contained INTEGER NOT NULL, payload TEXT NOT NULL,
 UNIQUE(identity, digest));
CREATE INDEX resource_identity ON resources(identity);
-- Patient grouping and type inventories need only type and row ID. This
-- covering index avoids reading every large JSON payload to obtain them.
CREATE INDEX resource_type ON resources(resource_type);
CREATE TABLE occurrences (
 id INTEGER PRIMARY KEY, resource_id INTEGER REFERENCES resources(id),
 source_id INTEGER REFERENCES sources(id), locator TEXT, context TEXT,
 full_url TEXT, root_resource_id INTEGER REFERENCES resources(id),
 parent_resource_id INTEGER REFERENCES resources(id));
CREATE INDEX occurrence_resource ON occurrences(resource_id);
CREATE TABLE aliases (alias TEXT NOT NULL, resource_id INTEGER REFERENCES resources(id),
 context TEXT NOT NULL, kind TEXT NOT NULL,
 UNIQUE(alias, resource_id, context, kind));
CREATE INDEX alias_lookup ON aliases(alias, kind, context);
CREATE TABLE profiles (resource_id INTEGER REFERENCES resources(id), canonical TEXT,
 version TEXT, module TEXT, UNIQUE(resource_id, canonical, version));
CREATE TABLE extensions (resource_id INTEGER REFERENCES resources(id), url TEXT, modifier INTEGER,
 UNIQUE(resource_id, url, modifier));
CREATE TABLE resource_references (
 id INTEGER PRIMARY KEY, occurrence_id INTEGER REFERENCES occurrences(id),
 source_resource_id INTEGER REFERENCES resources(id), path TEXT, literal TEXT,
 kind TEXT, target_resource_id INTEGER REFERENCES resources(id), status TEXT);
CREATE INDEX reference_source ON resource_references(source_resource_id);
CREATE TABLE patient_memberships (
 resource_id INTEGER PRIMARY KEY REFERENCES resources(id),
 patient_resource_id INTEGER REFERENCES resources(id), basis TEXT);
CREATE INDEX membership_patient ON patient_memberships(patient_resource_id);
CREATE TABLE observation_fields (
 resource_id INTEGER REFERENCES resources(id), path TEXT, code_system TEXT,
 code TEXT, value_type TEXT, unit_system TEXT, unit_code TEXT, unit_display TEXT);
CREATE TABLE version_evidence (resource_id INTEGER REFERENCES resources(id),
 declared_fhir_version TEXT);
CREATE TABLE issues (id INTEGER PRIMARY KEY, severity TEXT, code TEXT,
 source_id INTEGER, locator TEXT, resource_id INTEGER, detail TEXT);
