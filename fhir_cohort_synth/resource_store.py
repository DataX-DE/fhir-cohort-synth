"""Save source resources, their occurrences and outgoing references.

A resource row holds one deduplicated payload. An occurrence records each place
it appeared, including its Bundle lookup scope. Contained children stay in the
root payload and also get scoped rows so their references can resolve later.
"""
import hashlib
import re
from urllib.parse import urlsplit

from .fhir_types import TypeIndex
from .jsonio import dumps
from .references import rest_parts

FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


class ResourceStore:
    """Write resources through Store's connection and issue recorder.

    This class does not commit or resolve targets. Ingestion controls those
    phases after complete documents, and eventually all files, are stored.
    """

    def __init__(self, db, base_url, issue):
        self.db = db
        self.base_url = base_url
        self.issue = issue
        self.types = TypeIndex()

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
            if parent is not None:
                self.issue("contained_bundle_unsupported", "error", source_id, locator)
                return
            self._add_bundle(resource, source_id, locator, depth)
        else:
            self._add_resource(resource, source_id, locator, context, full_url, parent, root, depth)

    def _add_bundle(self, resource, source_id, locator, depth):
        """Unpack entries in their shared Bundle lookup scope.

        Only resource payloads are stored. Bundle/entry metadata stays in the
        original export; fullUrl and lookup context are kept on occurrences.
        Each resource entry re-enters add_document() for the same basic checks.
        """
        context = f"source:{source_id}:{locator}"
        bundle_type = resource.get("type")
        if not isinstance(bundle_type, str) or bundle_type not in {"collection", "searchset", "document", "message", "history",
                               "transaction", "transaction-response", "batch", "batch-response"}:
            self.issue("invalid_bundle_type", "error", source_id, locator)
        if bundle_type == "history":
            # A cohort needs a snapshot. Do not silently choose one revision
            # from a history; record an error and leave the source file intact.
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

    def _add_resource(self, resource, source_id, locator, context, full_url, parent, root, depth):
        """Store one identity/payload and occurrence, then check it and index links.

        Contained children remain in this payload and also get scoped index rows.
        All arguments describe this occurrence; they never alter the source JSON.
        """
        resource_type = resource['resourceType']

        # Standalone NDJSON records in one file share a lookup context when
        # no Bundle exists.
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
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO resources(
                   identity, digest, resource_type, logical_id, version_id,
                   full_url, contained, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (identity, digest, resource_type, logical_id, version, full_url,
             int(parent is not None), payload),
        )
        inserted = bool(cursor.rowcount)
        # Even an exact duplicate needs an occurrence to preserve provenance.
        # Both IDs below are database keys, not the resource's FHIR id string.
        resource_id = self.db.execute(
            "SELECT id FROM resources WHERE identity=? AND digest=?", (identity, digest)
        ).fetchone()[0]
        root = root or resource_id
        occurrence_id = self.db.execute(
            """INSERT INTO occurrences(resource_id, source_id, locator, context,
                                       full_url, root_resource_id, parent_resource_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (resource_id, source_id, locator, context, full_url, root, parent),
        ).lastrowid
        if not inserted:
            self.issue("duplicate_resource", "info", source_id, locator, resource_id)
        elif self.db.execute("SELECT count(*) FROM resources WHERE identity=?", (identity,)).fetchone()[0] > 1:
            self.issue("conflicting_resource_identity", "error", source_id, locator, resource_id)
        # Register the names another resource may use to reference this one.
        # Contained aliases stay local; ordinary resources may have both a
        # relative alias (Patient/p1) and an absolute/URN full URL.
        if parent is not None:
            if logical_id:
                self._add_alias(f"#{logical_id}", resource_id, f"contained:{root}", "contained")
        else:
            if logical_id:
                self._add_alias(f"{resource_type}/{logical_id}", resource_id, context, "relative")
            if full_url:
                self._add_alias(full_url, resource_id, context, "absolute")
                parsed = rest_parts(full_url)
                if parsed and parsed[1] != f"{resource_type}/{logical_id}":
                    self.issue("full_url_identity_mismatch", "error", source_id, locator, resource_id)

        self._check_metadata(resource, meta, resource_id, inserted, source_id, locator)
        self._index_references(resource, resource_id, occurrence_id, source_id, locator)
        if inserted and resource_type == "Observation":
            self._check_observation(resource_id, resource)
        # Keep contained resources in the original payload and also index them
        # individually so their #id references and patient context can resolve.
        contained = resource.get("contained", [])
        if not isinstance(contained, list):
            self.issue("invalid_contained", "error", source_id, locator, resource_id)
            contained = []
        if parent is not None and contained:
            self.issue("nested_contained_resources", "error", source_id, locator, resource_id)
        for index, child in enumerate(contained):
            self.add_document(child, source_id, f"{locator}.contained[{index}]", context,
                              parent=resource_id, root=root, depth=depth + 1)

    def _check_metadata(self, resource, meta, resource_id, inserted, source_id, locator):
        """Keep structural and base-version checks without building inventories."""
        resource_type = resource['resourceType']
        # Declarations stay in the complete payload. A profile need not be
        # present or recognized for base R4 perturbation to process a resource.
        profiles = meta.get("profile", [])
        if not isinstance(profiles, list) or any(not isinstance(p, str) or not p for p in profiles):
            self.issue("invalid_profile_declaration", "error", source_id, locator, resource_id)
        if inserted and resource_type in {"CapabilityStatement", "StructureDefinition", "ImplementationGuide"}:
            # Only explicit FHIR base-version declarations count as evidence.
            # A meta.profile suffix or meta.versionId means something else.
            evidence = resource.get("fhirVersion", [])
            evidence = evidence if isinstance(evidence, list) else [evidence]
            for item in evidence:
                if isinstance(item, str) and item != "4.0.1":
                    self.issue("incompatible_fhir_version_declaration", "error", resource_id=resource_id)

    def _index_references(self, resource, resource_id, occurrence_id, source_id, locator):
        """Save outgoing links per occurrence, including links inside extensions.

        Saving literals here allows resolve() to run after forward targets exist.
        This traversal excludes contained children, whose own rows index their links.
        """
        # Store outgoing reference text once per occurrence, since the same
        # payload can appear in different Bundle contexts.
        for path, field in _walk_fields(resource, self.types):
            key, value, container = field.key, field.value, field.parent
            unknown = field.datatype is None
            local_canonical = (field.datatype == 'canonical' and isinstance(value, str)
                               and value.startswith('#') and len(value) > 1)
            if (key == "reference" and (field.parent_type == 'Reference' or unknown)) or local_canonical:
                if isinstance(value, str) and value:
                    self.db.execute("INSERT INTO resource_references(occurrence_id,source_resource_id,path,literal,kind,status) VALUES (?,?,?,?,?,?)",
                                    (occurrence_id, resource_id, path, value, "literal", "pending"))
                else:
                    self.issue("invalid_reference", "error", source_id, locator, resource_id, path)
            elif (key == "identifier" and isinstance(value, dict) and "reference" not in container
                  and (field.parent_type == 'Reference' or
                       (unknown and set(container) <= {"id", "extension", "type", "identifier", "display"}))):
                # Reference.identifier names a target by business identifier,
                # not by resource URL. Do not guess a match against patient IDs.
                self.db.execute("INSERT INTO resource_references(occurrence_id,source_resource_id,path,kind,status) VALUES (?,?,?,?,?)",
                                (occurrence_id, resource_id, path, "logical", "logical_unresolved"))
                self.issue("logical_reference_unresolved", resource_id=resource_id, detail=path)

    def _add_alias(self, alias, resource_id, context, kind):
        """Add one scoped lookup name for a resource, ignoring exact repeats."""
        self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)", (alias, resource_id, context, kind))

    def _check_observation(self, resource_id, resource):
        """Check value choices on the Observation and each component.

        Codes, units and values stay in the payload. Perturbation reads them
        directly and stores before/after distributions in its local state database.
        """
        components = resource.get("component", [])
        if not isinstance(components, list):
            self.issue("invalid_observation_components", "error", resource_id=resource_id)
            components = []
        for path, element in [("", resource)] + [(f"component[{i}]", c) for i, c in enumerate(components)]:
            if not isinstance(element, dict):
                self.issue("invalid_observation_component", "error", resource_id=resource_id)
                continue
            # FHIR's value[x] is a choice: valueQuantity, valueString, etc.
            # Multiple choices, or a value plus dataAbsentReason, conflict.
            values = [k for k in element if k.startswith("value") and len(k) > 5 and k[5].isupper()]
            if len(values) > 1 or (values and "dataAbsentReason" in element):
                self.issue("observation_value_conflict", "error", resource_id=resource_id, detail=path)


def _walk_fields(value, types):
    """Yield typed fields with index display paths, excluding containment.

    A field named 'reference' is not necessarily Reference.reference:
    CarePlan.activity.reference is an object, Claim.related.reference is an
    Identifier, and Expression.reference is a URI. Only the actual Reference
    datatype supplies graph edges. Unknown JSON retains the existing fallback.
    Contained payloads are indexed when add_document() visits each child.
    """
    for field in types.walk(value, include_context=False):
        if (not field.path or field.path[0] == ('key', 'contained')
                or field.reason == 'embedded_resource_preserved'
                or (not isinstance(field.key, str) and field.datatype != 'canonical')):
            continue
        path = ''
        for kind, key in field.path:
            if kind == 'key':
                path += ('.' if path else '') + key
            else:
                path += f'[{key}]'
        yield path, field
