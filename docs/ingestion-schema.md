# Ingestion index, schema version 2

This is a local staging index containing source patient data. Internal numeric
keys are run-local; they are not synthetic IDs and are not stable across runs.
SQLite holds the resource payloads and lookups needed for perturbation on disk.
The schema lives in `fhir_cohort_synth/schema.py`.

| Table | Purpose |
| --- | --- |
| `run` | Completion status, schema version and assumed FHIR target |
| `sources` | Local source paths; keep private |
| `resources` | Deduplicated resources, logical identity, SHA-256 digest and complete parsed JSON payload |
| `occurrences` | Every resource appearance, source locator, Bundle/file context, fullUrl and containment |
| `aliases` | Scoped contained, relative and absolute identities for reference lookup |
| `resource_references` | Source, field path, literal/logical form, resolution status and target |
| `patient_memberships` | At most one patient per resource, with direct or inferred basis |
| `issues` | Detailed local issues; locators and internal resource IDs permit source inspection |

`resources.payload` preserves parsed JSON values, including decimal precision;
it is not a byte-for-byte source archive. JSON keys are sorted and whitespace
changes. A containing resource's payload includes its contained resources, which
also have separate indexed rows. Avoid counting these twice when expanding
payloads. Bundle envelopes and entry metadata are not archived in this index.
Their resource payloads are indexed; entry full URLs, lookup contexts and source
locators remain on occurrences. The complete original export stays untouched.

The duplicate key is `(identity, digest)`. `identity` is the full URL when
available, otherwise resource type + logical ID, otherwise the source locator.
Contained identities include the root resource's internal ID. Conflicting
payloads at the same identity remain separate rows and make the run incomplete.
Reported patient counts count resource rows, not reconciled people.

Reference totals count occurrences; resource totals count deduplicated rows.
Profile declarations, extensions, codes, units, measurements and timestamps remain
in the payload. No separate inventories or hospital-module classification are
stored. Missing profiles do not produce warnings. Malformed declarations,
incompatible explicit FHIR versions and conflicting Observation value choices
still produce processing errors. Perturbation computes before/after measurement
statistics directly from resource JSON and stores them in its own state database.

Schema 2 removes `bundles`, `bundle_entries`, `profiles`, `extensions`,
`observation_fields`, `version_evidence` and the `resources.scope` column.
The report omits those inventories and the Bundle count. Existing schema 1
indexes remain readable by perturbation because its required columns are
unchanged; no migration or rewriting of old databases occurs. Their previously
recorded source warnings are still carried into perturbation reports.

Literal FHIR `Reference.reference` values are resolved automatically using the
bundled R4 datatype index. A field named `reference` may instead be a Reference
object, an Identifier or a URI; those are not literal graph edges. Unknown JSON
retains the generic reference-field fallback. Local canonical fragments such as
`Questionnaire.item.answerValueSet = "#choices"` are also indexed against the
containing resource's exact target. External canonical URLs such as
`meta.profile` are not graph edges. Logical references based only on identifiers
remain unresolved. Resolution never performs a network request.

Resources embedded outside containment, such as `Parameters.parameter.resource`,
remain in their parent's JSON payload and perturbed output. They do not have their
own indexed identities or patient ownership; their references are excluded from
the graph and perturbation preserves their complete subtrees with an explicit
`embedded_resource_preserved` reason.

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

Before a later stage reads the index, require `run.status` to be `completed` or
`completed_with_warnings` and the corresponding reports. `incomplete` indexes
must not be treated as valid cohorts. Warnings still require cohort-specific
decisions, such as whether unresolved links are expected external references or
evidence of missing patient records.
