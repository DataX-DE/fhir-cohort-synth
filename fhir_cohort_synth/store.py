"""Store FHIR resources and connect them into a patient-level index.

The main methods follow the ingestion phases:
    add_document(): store payloads, identities and outgoing reference text.
    resolve(): match reference text to resources after all files are loaded.
    group_patients(): follow selected resolved links to identify each patient.
    report(): summarize the index and record the run's final status.

Two concepts recur throughout this module. A *resource* is a deduplicated
payload; an *occurrence* is one appearance of that resource in an input file.
Keeping both prevents duplicate counting while retaining source locations and
the lookup context of each reference. SQLite numeric IDs are local index keys,
not FHIR IDs, hospital pseudonyms or generated synthetic identities.
"""
from collections import defaultdict, deque
import hashlib
import re
import sqlite3
from urllib.parse import urlsplit

from .jsonio import dumps
from .profiles import module_for, profile_parts, scope_for

# A relative reference can name a particular revision, e.g.
# Patient/p1/_history/2. Its regex groups are resource type, ID and version.
FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
RELATIVE = re.compile(r"^([A-Z][A-Za-z0-9]+)/([A-Za-z0-9\-.]{1,64})(?:/_history/([A-Za-z0-9\-.]{1,64}))?$")
# These types are expected to acquire a patient context in a cohort export.
# A shared Location or Medication is not subject to that expectation.
PATIENT_RESOURCES = {"Encounter", "Condition", "Observation", "Procedure",
                     "MedicationAdministration", "Consent"}

# Schema overview (full column semantics are in docs/ingestion-schema.md):
# - sources / bundles / bundle_entries / occurrences preserve provenance.
# - resources hold complete payloads; aliases enable scoped identity lookup.
# - profiles / extensions / observation_fields inventory selected metadata.
# - resource_references holds the graph; patient_memberships holds its result.
# - run / version_evidence / issues describe completion and known problems.
# The UNIQUE(identity, digest) key deduplicates repeated payloads but allows
# conflicting payloads with the same identity to remain available for review.
SCHEMA = """
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
"""


def rest_parts(full_url):
    """Split a recognizable HTTP(S) FHIR resource URL, or return None.

    For 'https://host/fhir/Patient/p1/_history/2', return
    ('https://host/fhir/', 'Patient/p1', '2'). Unversioned URLs return None
    for the version. URNs and arbitrary non-REST URLs must use exact matching
    instead; stripping their final segments would invent an identity.
    """
    if not isinstance(full_url, str):
        return None
    try:
        parsed = urlsplit(full_url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment or full_url.endswith("/"):
        return None
    parts = parsed.path.strip("/").split("/")
    tail = "/".join(parts[-4:] if len(parts) >= 4 and parts[-2] == "_history" else parts[-2:])
    match = RELATIVE.fullmatch(tail)
    if not match:
        return None
    return full_url[:-len(tail)], f"{match[1]}/{match[2]}", match[3]


def walk_fields(value, path=""):
    """Yield (path, field name, value, containing object) for JSON fields.

    Paths such as 'subject.reference' or 'component[0].code' let later code
    distinguish a patient's subject link from other relationships. The parent
    object helps recognize identifier-only Reference shapes. This is a JSON
    walk, not validation against FHIR StructureDefinitions.

    Skip contained payloads: add_document() indexes them separately with their
    own reference scope, so their fields must not be attributed to the parent.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "contained":
                continue
            child_path = f"{path}.{key}" if path else key
            yield child_path, key, child, value
            yield from walk_fields(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_fields(child, f"{path}[{index}]")


class Store:
    """Manage one new ingestion database; the caller commits and closes it.

    Methods accept parsed source resources, retain their payloads, and record
    selected ingestion problems as issues rather than repairing patient data.
    """

    def __init__(self, path, base_url=None):
        """Initialize the schema and optional fallback server identity prefix."""
        self.base_url = base_url
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def issue(self, code, severity="warning", source_id=None, locator=None,
              resource_id=None, detail=None):
        """Record a problem or notice, optionally tied to a source/resource.

        Use fixed codes and structural detail such as a field path. Patient
        values and raw exception messages do not belong in diagnostics.
        """
        self.db.execute("INSERT INTO issues(severity,code,source_id,locator,resource_id,detail) VALUES (?,?,?,?,?,?)",
                        (severity, code, source_id, locator, resource_id, detail))

    def add_document(self, resource, source_id, locator, context=None, full_url=None,
                     parent=None, root=None, depth=0):
        """Register a resource, recursively unpacking Bundles and containment.

        ``source_id`` and ``locator`` say where this occurrence came from.
        ``context`` is its Bundle/file lookup scope, not a clinical encounter.
        ``full_url`` is the Bundle entry's declared identity, when supplied.
        ``parent`` and ``root`` are SQLite resource IDs for containment: parent
        is the immediate container, root is the outer resource that scopes #id
        references. ``depth`` bounds Bundle/contained recursion.

        References are saved as text here, then resolved in a separate pass so
        file order does not determine whether a target is found. This method
        records selected structural issues, not full profile conformance.
        """
        if depth > 64:
            self.issue("nesting_limit", "error", source_id, locator)
            return
        if not isinstance(resource, dict) or not isinstance(resource.get("resourceType"), str):
            self.issue("invalid_resource", "error", source_id, locator)
            return
        resource_type = resource["resourceType"]
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]+", resource_type):
            self.issue("invalid_resource_type", "error", source_id, locator)
            return
        if resource_type == "Bundle":
            # A Bundle is an envelope, not a patient resource row. Retain its
            # metadata separately and give its entries a shared lookup scope.
            if parent is not None:
                self.issue("contained_bundle_unsupported", "error", source_id, locator)
                return
            context = f"source:{source_id}:{locator}"
            bundle_type = resource.get("type")
            bundle_id = self.db.execute("INSERT INTO bundles(source_id,locator,context,bundle_type,metadata_json) VALUES (?,?,?,?,?)",
                            (source_id, locator, context, bundle_type if isinstance(bundle_type, str) else None,
                             dumps({k: v for k, v in resource.items() if k != "entry"}))).lastrowid
            if not isinstance(bundle_type, str) or bundle_type not in {"collection", "searchset", "document", "message", "history",
                                   "transaction", "transaction-response", "batch", "batch-response"}:
                self.issue("invalid_bundle_type", "error", source_id, locator)
            if bundle_type == "history":
                # A cohort needs a snapshot. Keep the input for inspection,
                # but do not silently choose one revision from a history.
                self.issue("history_bundle_requires_snapshot", "error", source_id, locator)
            links = resource.get("link", [])
            if isinstance(links, list) and any(isinstance(x, dict) and x.get("relation") == "next" for x in links):
                self.issue("pagination_link_present", source_id=source_id, locator=locator)
            entries = resource.get("entry", [])
            if not isinstance(entries, list):
                self.issue("invalid_bundle_entries", "error", source_id, locator)
                return
            for index, entry in enumerate(entries):
                position = f"{locator}.entry[{index}]"
                # Keep request/response/search metadata, including entries
                # without resources (e.g. deletion records in history exports).
                self.db.execute("INSERT INTO bundle_entries VALUES (?,?,?)",
                                (bundle_id, index, dumps({k: v for k, v in entry.items() if k != "resource"}
                                                         if isinstance(entry, dict) else entry)))
                if not isinstance(entry, dict):
                    self.issue("invalid_bundle_entry", "error", source_id, position)
                elif "resource" not in entry:
                    self.issue("bundle_entry_without_resource", "error", source_id, position)
                else:
                    url = entry.get("fullUrl")
                    if url is not None and (not isinstance(url, str) or not url):
                        self.issue("invalid_full_url", "error", source_id, position)
                        url = None
                    if isinstance(url, str) and "/_history/" in url:
                        self.issue("versioned_full_url", "error", source_id, position)
                    if isinstance(url, str):
                        try:
                            parsed_url = urlsplit(url)
                            valid_url = (parsed_url.scheme and not parsed_url.fragment
                                         and not any(c.isspace() for c in url)
                                         and (parsed_url.netloc if parsed_url.scheme in {"http", "https"} else parsed_url.path))
                        except ValueError:
                            valid_url = False
                        if not valid_url:
                            self.issue("invalid_full_url", "error", source_id, position)
                    self.add_document(entry["resource"], source_id, position + ".resource",
                                      context, url, depth=depth + 1)
            return

        # From here on, we have an individual resource. Standalone NDJSON
        # records in one file share a lookup context when no Bundle exists.
        context = context or f"source:{source_id}:standalone"
        logical_id = resource.get("id")
        if logical_id is not None and (not isinstance(logical_id, str) or not FHIR_ID.fullmatch(logical_id)):
            self.issue("invalid_resource_id", "error", source_id, locator)
            logical_id = None
        if logical_id is None:
            self.issue("missing_resource_id", source_id=source_id, locator=locator)
        if parent is not None and logical_id is None:
            self.issue("contained_resource_without_id", "error", source_id, locator)
        if parent is None and full_url is None and self.base_url and logical_id:
            # The user explicitly supplied this server namespace. It supplies
            # an index identity only; the source payload is not rewritten.
            full_url = f"{self.base_url}/{resource_type}/{logical_id}"
        meta = resource.get("meta", {})
        if not isinstance(meta, dict):
            self.issue("invalid_meta", "error", source_id, locator)
            meta = {}
        version = meta.get("versionId")
        if version is not None and (not isinstance(version, str) or not FHIR_ID.fullmatch(version)):
            self.issue("invalid_version_id", "error", source_id, locator)
            version = None
        if parent is not None:
            # Two different parent resources may both contain a child named
            # 'med1'. The outer root keeps those local IDs distinct.
            identity = f"contained:{root}#{logical_id or locator}"
        else:
            identity = full_url or (f"{resource_type}/{logical_id}" if logical_id else f"{context}:{locator}")
        # Identity alone cannot establish a duplicate: two revisions may share
        # an ID but contain different facts. Compare the normalized payload as
        # well, and retain conflicting versions instead of overwriting them.
        payload = dumps(resource)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cursor = self.db.execute("INSERT OR IGNORE INTO resources(identity,digest,resource_type,logical_id,version_id,full_url,scope,contained,payload) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (identity, digest, resource_type, logical_id, version, full_url,
                                  scope_for(resource_type), int(parent is not None), payload))
        inserted = bool(cursor.rowcount)
        # rid = deduplicated resource row; oid = this source occurrence row.
        # Even an exact duplicate needs an occurrence to preserve provenance.
        rid = self.db.execute("SELECT id FROM resources WHERE identity=? AND digest=?", (identity, digest)).fetchone()[0]
        root = root or rid
        oid = self.db.execute("INSERT INTO occurrences(resource_id,source_id,locator,context,full_url,root_resource_id,parent_resource_id) VALUES (?,?,?,?,?,?,?)",
                              (rid, source_id, locator, context, full_url, root, parent)).lastrowid
        if not inserted:
            self.issue("duplicate_resource", "info", source_id, locator, rid)
        elif self.db.execute("SELECT count(*) FROM resources WHERE identity=?", (identity,)).fetchone()[0] > 1:
            self.issue("conflicting_resource_identity", "error", source_id, locator, rid)
        # Register the names another resource may use to reference this one.
        # Contained aliases stay local; ordinary resources may have both a
        # relative alias (Patient/p1) and an absolute/URN full URL.
        if parent is not None:
            if logical_id:
                self.alias(f"#{logical_id}", rid, f"contained:{root}", "contained")
        else:
            if logical_id:
                self.alias(f"{resource_type}/{logical_id}", rid, context, "relative")
            if full_url:
                self.alias(full_url, rid, context, "absolute")
                parsed = rest_parts(full_url)
                if parsed and parsed[1] != f"{resource_type}/{logical_id}":
                    self.issue("full_url_identity_mismatch", "error", source_id, locator, rid)

        # Preserve declared profile URLs and versions as inventory metadata.
        # Recognizing an MII module does not validate the resource against it.
        profiles = meta.get("profile", [])
        if not isinstance(profiles, list) or any(not isinstance(p, str) or not p for p in profiles):
            self.issue("invalid_profile_declaration", "error", source_id, locator, rid)
            profiles = []
        for profile in profiles:
            canonical, profile_version = profile_parts(profile)
            self.db.execute("INSERT OR IGNORE INTO profiles VALUES (?,?,?,?)",
                            (rid, canonical, profile_version, module_for(canonical)))
        if inserted and not profiles and scope_for(resource_type) == "requested":
            self.issue("missing_profile_declaration", resource_id=rid)
        if inserted and scope_for(resource_type) == "other":
            self.issue("outside_requested_scope", "info", resource_id=rid)
        if inserted and resource_type in {"CapabilityStatement", "StructureDefinition", "ImplementationGuide"}:
            # Only explicit FHIR base-version declarations count as evidence.
            # A meta.profile suffix or meta.versionId means something else.
            evidence = resource.get("fhirVersion", [])
            evidence = evidence if isinstance(evidence, list) else [evidence]
            for item in evidence:
                if isinstance(item, str):
                    self.db.execute("INSERT INTO version_evidence VALUES (?,?)", (rid, item))
                    if item != "4.0.1":
                        self.issue("incompatible_fhir_version_declaration", "error", resource_id=rid)

        # Store outgoing reference text once per occurrence, since the same
        # payload can appear in different Bundle contexts. URL inventories are
        # deduplicated per resource by their table constraints.
        for path, key, value, container in walk_fields(resource):
            if key == "reference":
                if isinstance(value, str) and value:
                    self.db.execute("INSERT INTO resource_references(occurrence_id,source_resource_id,path,literal,kind,status) VALUES (?,?,?,?,?,?)",
                                    (oid, rid, path, value, "literal", "pending"))
                else:
                    self.issue("invalid_reference", "error", source_id, locator, rid, path)
            elif key == "identifier" and isinstance(value, dict) and "reference" not in container and set(container) <= {"id", "extension", "type", "identifier", "display"}:
                # Reference.identifier names a target by business identifier,
                # not by resource URL. Do not guess a match against patient IDs.
                self.db.execute("INSERT INTO resource_references(occurrence_id,source_resource_id,path,kind,status) VALUES (?,?,?,?,?)",
                                (oid, rid, path, "logical", "logical_unresolved"))
                self.issue("logical_reference_unresolved", resource_id=rid, detail=path)
            elif key in {"extension", "modifierExtension"} and isinstance(value, list):
                for extension in value:
                    if isinstance(extension, dict) and isinstance(extension.get("url"), str):
                        self.db.execute("INSERT OR IGNORE INTO extensions VALUES (?,?,?)",
                                        (rid, extension["url"], int(key == "modifierExtension")))
        if inserted and resource_type == "Observation":
            self.index_observation(rid, resource)
        # Keep contained resources in the original payload and also index them
        # individually so their #id references and patient context can resolve.
        contained = resource.get("contained", [])
        if not isinstance(contained, list):
            self.issue("invalid_contained", "error", source_id, locator, rid)
            contained = []
        if parent is not None and contained:
            self.issue("nested_contained_resources", "error", source_id, locator, rid)
        for index, child in enumerate(contained):
            self.add_document(child, source_id, f"{locator}.contained[{index}]", context,
                              parent=rid, root=root, depth=depth + 1)

    def alias(self, alias, rid, context, kind):
        """Add one scoped lookup name for a resource, ignoring exact repeats."""
        self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)", (alias, rid, context, kind))

    def index_observation(self, rid, resource):
        """Inventory measurement fields without transforming their values.

        A blood-pressure Observation can hold its values in components, while
        a laboratory Observation may use a top-level valueQuantity or
        valueCodeableConcept. Index both layouts, one row per field coding.
        Actual results and absent-reason detail remain in the full payload.
        """
        components = resource.get("component", [])
        if not isinstance(components, list):
            self.issue("invalid_observation_components", "error", resource_id=rid)
            components = []
        for path, element in [("", resource)] + [(f"component[{i}]", c) for i, c in enumerate(components)]:
            if not isinstance(element, dict):
                self.issue("invalid_observation_component", "error", resource_id=rid)
                continue
            code = element.get("code", {})
            codings = code.get("coding", []) if isinstance(code, dict) else []
            if not isinstance(codings, list):
                codings = []
            # FHIR's value[x] is a choice: valueQuantity, valueString, etc.
            # Multiple choices, or a value plus dataAbsentReason, conflict.
            values = [k for k in element if k.startswith("value") and len(k) > 5 and k[5].isupper()]
            if len(values) > 1 or (values and "dataAbsentReason" in element):
                self.issue("observation_value_conflict", "error", resource_id=rid, detail=path)
            value_type = values[0] if values else "absent"
            quantity = element.get("valueQuantity", {})
            quantity = quantity if isinstance(quantity, dict) else {}
            # Retain a field even without coding; several codings produce
            # several inventory rows, not several measured results.
            for coding in codings or [{}]:
                if not isinstance(coding, dict):
                    continue
                text = lambda v: v if isinstance(v, str) else None
                self.db.execute("INSERT INTO observation_fields VALUES (?,?,?,?,?,?,?,?)",
                                (rid, path, text(coding.get("system")), text(coding.get("code")), value_type,
                                 text(quantity.get("system")), text(quantity.get("code")), text(quantity.get("unit"))))

    def candidates(self, alias, kind, context=None, version=None):
        """Return all matching resource IDs, optionally restricted by scope/revision.

        A set lets the caller distinguish missing, unique and ambiguous targets.
        DISTINCT prevents repeated occurrences of one resource from creating
        false ambiguity. ``version`` matches meta.versionId, not a profile version.
        """
        sql = "SELECT DISTINCT r.id FROM aliases a JOIN resources r ON r.id=a.resource_id WHERE a.alias=? AND a.kind=?"
        args = [alias, kind]
        if context is not None:
            sql += " AND a.context=?"
            args.append(context)
        if version is not None:
            sql += " AND r.version_id=?"
            args.append(version)
        return {r[0] for r in self.db.execute(sql, args)}

    def resolve_one(self, literal, occurrence):
        """Find candidate targets using the referencing occurrence's scope.

        Handle local # references, relative REST references, absolute REST
        references, then exact full URLs such as URNs. Return a set rather than
        choosing a target here: resolve() accepts only a unique match.
        """
        # '#' refers back to the containing root; '#med1' finds a child under
        # that same root. Neither form searches another patient's resource.
        if literal == "#":
            return {occurrence["root_resource_id"]}
        if literal.startswith("#"):
            return self.candidates(literal, "contained", f"contained:{occurrence['root_resource_id']}")
        relative = RELATIVE.fullmatch(literal)
        if relative:
            alias, version = f"{relative[1]}/{relative[2]}", relative[3]
            # Absolute source identity determines the server namespace. A same-ID
            # resource from another server must never be used as a fallback.
            root_url = self.db.execute("SELECT full_url FROM resources WHERE id=?",
                                       (occurrence["root_resource_id"],)).fetchone()[0]
            source = rest_parts(occurrence["full_url"] or root_url)
            if source:
                return self.candidates(source[0] + alias, "absolute", version=version)
            # With no known server base, prefer the enclosing Bundle/file.
            # Only if that identity is absent locally do we search other files.
            local = self.candidates(alias, "relative", occurrence["context"])
            if local:
                # A local identity with the wrong version must not cause a
                # search in an unrelated Bundle/server for that version.
                return local if version is None else self.candidates(alias, "relative", occurrence["context"], version)
            return self.candidates(alias, "relative", version=version)
        absolute = rest_parts(literal)
        if absolute:
            # Match the declared server and revision exactly. Falling back to
            # type/ID could attach a resource from an unrelated server.
            base, alias, version = absolute
            return self.candidates(base + alias, "absolute", version=version)
        # URNs and non-REST fullUrls are resolved by exact identity only.
        return self.candidates(literal, "absolute")

    def resolve(self):
        """Resolve stored literal references after the entire export is indexed.

        Missing targets become warnings; several candidates become errors.
        Identifier-only references remain logical_unresolved from ingestion.
        """
        for ref in self.db.execute("SELECT * FROM resource_references WHERE kind='literal'"):
            occurrence = self.db.execute("SELECT * FROM occurrences WHERE id=?", (ref["occurrence_id"],)).fetchone()
            matches = self.resolve_one(ref["literal"], occurrence)
            status = "resolved" if len(matches) == 1 else "ambiguous" if matches else "unresolved"
            # Never use the first of several matches to break an ambiguity.
            target = next(iter(matches)) if len(matches) == 1 else None
            self.db.execute("UPDATE resource_references SET target_resource_id=?,status=? WHERE id=?", (target, status, ref["id"]))
            if status != "resolved":
                self.issue(f"{status}_reference", "error" if status == "ambiguous" else "warning",
                           resource_id=ref["source_resource_id"], detail=ref["path"])

    def group_patients(self):
        """Assign a resource only when its resolved context identifies one patient.

        Start with Patient rows and explicit patient subjects. Then propagate
        that context through encounters, Encounter.partOf and containment.
        Track candidate patients as sets so contradictory paths remain visible
        instead of whichever path was visited first winning the assignment.
        """
        types = dict(self.db.execute("SELECT id, resource_type FROM resources"))
        # owners[resource] = candidate patient resource IDs.
        # dependents[provider] = resources that inherit that provider's context.
        # direct marks self/explicit patient links for membership provenance.
        owners = defaultdict(set)
        dependents = defaultdict(set)
        direct = set()
        encounter_parents = defaultdict(set)
        # 1. Every Patient is the starting point for its own patient group.
        for rid, rtype in types.items():
            if rtype == "Patient":
                owners[rid].add(rid)
                direct.add(rid)
        # 2. Select relationships that convey patient context. An Observation
        #    inherits from its Encounter, although its reference points toward
        #    that Encounter. Hence dependents[target].add(source) below.
        #    Arbitrary links to shared Medication/Location rows do not propagate
        #    context; otherwise unrelated patients could be grouped together.
        for ref in self.db.execute("SELECT DISTINCT source_resource_id,path,target_resource_id FROM resource_references WHERE status='resolved'"):
            source, path, target = ref
            if path in {"subject.reference", "patient.reference", "beneficiary.reference"} and types[target] == "Patient":
                owners[source].add(target)
                direct.add(source)
            elif path in {"encounter.reference", "context.reference"} and types[target] == "Encounter":
                dependents[target].add(source)
            elif path == "partOf.reference" and types[source] == types[target] == "Encounter":
                dependents[target].add(source)
                encounter_parents[source].add(target)
        for row in self.db.execute("SELECT DISTINCT resource_id,parent_resource_id FROM occurrences WHERE parent_resource_id IS NOT NULL"):
            dependents[row[1]].add(row[0])
        # 3. Spread known patient sets until no set grows. For example:
        #    facility Encounter -> department Encounter -> unit -> Observation.
        #    Requeue on change so late discoveries (including contradictions)
        #    reach every dependent. Finite sets also make cycles terminate.
        queue = deque(owners)
        while queue:
            provider = queue.popleft()
            for dependent in dependents[provider]:
                before = len(owners[dependent])
                owners[dependent].update(owners[provider])
                if len(owners[dependent]) != before:
                    queue.append(dependent)
        # 4. Persist only unique assignments. Conflicting context remains an
        #    error; patient-scoped resources with no context become warnings.
        for rid, patient_ids in owners.items():
            if len(patient_ids) == 1:
                self.db.execute("INSERT INTO patient_memberships VALUES (?,?,?)",
                                (rid, next(iter(patient_ids)), "direct" if rid in direct else "inferred_from_context"))
            elif patient_ids:
                self.issue("conflicting_patient_context", "error", resource_id=rid)
        for rid, rtype in types.items():
            if rtype in PATIENT_RESOURCES and not owners[rid]:
                self.issue("unassigned_patient_resource", resource_id=rid)
        # 5. Detect invalid encounter hierarchies separately from propagation.
        #    Repeatedly remove encounters with no remaining parent dependency
        #    (a topological check). Nodes left over form, or depend on, a cycle.
        #    indegree tracks the number of parent dependencies still remaining.
        nodes = set(encounter_parents) | {p for ps in encounter_parents.values() for p in ps}
        indegree = {n: len(encounter_parents[n]) for n in nodes}
        children = defaultdict(set)
        for child, parents in encounter_parents.items():
            for parent in parents:
                children[parent].add(child)
        queue = deque(n for n in nodes if indegree[n] == 0)
        while queue:
            node = queue.popleft()
            for child in children[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        for rid, degree in indegree.items():
            if degree:
                self.issue("encounter_cycle_or_dependency", "error", resource_id=rid)

    def report(self):
        """Build aggregate inventories and set the run's ingestion status.

        Counts of resources/profiles use deduplicated rows, while reference
        counts reflect occurrences. Patient counts are Patient resource rows,
        not reconciled people. Reports omit source payloads and literal links;
        their counts and code/URL inventories still require local review.
        """
        def counts(sql):
            """Convert a two-column grouped query into a label-to-count map."""
            return dict(self.db.execute(sql))
        def scalar(table):
            """Count a table named by this code, never by an input resource."""
            return self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        errors = self.db.execute("SELECT count(*) FROM issues WHERE severity='error'").fetchone()[0]
        warnings = self.db.execute("SELECT count(*) FROM issues WHERE severity='warning'").fetchone()[0]
        # These statuses describe ingestion checks, not profile conformance or
        # clinical completeness. The CLI can additionally fail on warnings.
        status = "incomplete" if errors else "completed_with_warnings" if warnings else "completed"
        self.db.execute("UPDATE run SET status=?", (status,))
        return {
            "schema_version": 1, "status": status,
            "data_classification": "source_patient_data_not_synthetic",
            "fhir": {"target_version": "4.0.1", "profile_validation_performed": False,
                     "hospital_version_confirmed": False,
                     "declarations_in_input": counts("SELECT declared_fhir_version,count(*) FROM version_evidence GROUP BY 1 ORDER BY 1")},
            "counts": {"files": scalar("sources"), "bundles": scalar("bundles"),
                       "resource_occurrences": scalar("occurrences"), "unique_resources": scalar("resources"),
                       "duplicate_occurrences": scalar("occurrences") - scalar("resources"),
                       "patients": self.db.execute("SELECT count(*) FROM resources WHERE resource_type='Patient'").fetchone()[0],
                       "grouped_resources": scalar("patient_memberships")},
            "resource_types": counts("SELECT resource_type,count(*) FROM resources GROUP BY 1 ORDER BY 1"),
            "scope": counts("SELECT scope,count(*) FROM resources GROUP BY 1 ORDER BY 1"),
            "profiles": [dict(r) for r in self.db.execute("SELECT canonical,version,module,count(*) AS resource_count FROM profiles GROUP BY canonical,version,module ORDER BY canonical,version")],
            "extensions": [dict(r) for r in self.db.execute("SELECT url,modifier,count(*) AS resource_count FROM extensions GROUP BY url,modifier ORDER BY url,modifier")],
            "reference_status": counts("SELECT status,count(*) FROM resource_references GROUP BY 1 ORDER BY 1"),
            "observation_fields": [dict(r) for r in self.db.execute("SELECT code_system,code,value_type,unit_system,unit_code,unit_display,count(*) AS field_count FROM observation_fields GROUP BY code_system,code,value_type,unit_system,unit_code,unit_display ORDER BY code_system,code,value_type,unit_code")],
            "issues": [dict(r) for r in self.db.execute("SELECT severity,code,count(*) AS count FROM issues GROUP BY severity,code ORDER BY severity,code")],
        }
