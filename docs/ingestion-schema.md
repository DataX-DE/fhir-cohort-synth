# Ingestion index, schema version 1

This is a local staging index containing source patient data. Internal numeric
keys are run-local; they are not synthetic IDs and are not stable across runs.

| Table | Purpose |
| --- | --- |
| `run` | Completion status, schema version and assumed FHIR target |
| `sources` | Local source paths; keep private |
| `bundles` | Bundle envelope metadata, source and JSON locator |
| `bundle_entries` | Entry metadata (including request, response, search and fullUrl), with index into its Bundle |
| `resources` | Deduplicated resources, logical identity, SHA-256 digest and complete parsed JSON payload |
| `occurrences` | Every resource appearance, source locator, Bundle/file context, fullUrl and containment |
| `aliases` | Scoped contained, relative and absolute identities for reference lookup |
| `profiles` | Canonical URL, declared version (empty means unspecified) and recognized MII module |
| `extensions` | Extension URLs and whether they occur as modifier extensions |
| `resource_references` | Source, field path, literal/logical form, resolution status and target |
| `patient_memberships` | At most one patient per resource, with direct or inferred basis |
| `observation_fields` | Main and component codes, value choice and original quantity units |
| `version_evidence` | Explicit FHIR version declarations in conformance resources |
| `issues` | Detailed local issues; locators and internal resource IDs permit source inspection |

`resources.payload` preserves parsed JSON values, including decimal precision;
it is not a byte-for-byte source archive. JSON keys are sorted and whitespace
changes. A containing resource's payload includes its contained resources, which
also have separate indexed rows. Avoid counting these twice when expanding
payloads. Bundle envelopes are in `bundles`, not in resource counts.

The duplicate key is `(identity, digest)`. `identity` is the full URL when
available, otherwise resource type + logical ID, otherwise the source locator.
Contained identities include the root resource's internal ID. Conflicting
payloads at the same identity remain separate rows and make the run incomplete.
Reported patient counts count resource rows, not reconciled people.

Reference totals count occurrences; profile and resource totals count deduplicated
resource rows. `observation_fields` has one row per field coding, so a field
with several codings contributes several inventory rows. Value measurements,
qualitative values, diagnosis roles, timestamps and all other source fields
remain in the payload. This is an ingestion inventory, not cohort statistics.

Only literal FHIR `Reference.reference` values are resolved automatically;
canonical URLs such as `meta.profile` are not resource-reference edges. The
index scans JSON reference fields, but does not load StructureDefinitions to
verify the type of every field. Logical references based only on identifiers
remain unresolved. Resolution never performs a network request.

Example: inspect a patient's indexed resources using parameterized SQL:

```sql
SELECT r.id, r.resource_type, r.payload, m.basis
FROM patient_memberships AS m
JOIN resources AS r ON r.id = m.resource_id
WHERE m.patient_resource_id = ?
ORDER BY r.resource_type, r.id;
```

Example: inspect unresolved links locally:

```sql
SELECT source_resource_id, path, literal, status
FROM resource_references
WHERE status <> 'resolved';
```

Before a later stage reads the index, require `run.status <> 'in_progress'`,
the corresponding reports, and a reviewed issue inventory. `incomplete` indexes
must not be treated as valid cohorts. Warnings still require cohort-specific
decisions, such as whether unresolved links are expected external references or
evidence of missing patient records.
