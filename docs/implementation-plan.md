# Implementation plan: generic JSON profiling and statistical synthesis

Status: the foundation below is the approach confirmed by the user. Resource
ingestion, recursive extraction and exact marginal field distributions are
implemented (milestones 1 and 2). Dependency specifications, sampling, clinical
and longitudinal enrichment, and generation validation remain to be built.
See [profiling data and queries](profiling-schema.md) for the implemented schema.

## Agreed foundation

Start with the full nested JSON structure of every resource. Extract every
field, object, array and value, including unfamiliar fields. Build statistical
descriptions of those structures and values across resources of the same type.
Use those distributions and selected dependencies to construct new resources.

The generic extractor must work without a predefined list of clinical features
or a separate handwritten extractor for every resource type. Clinical meaning,
FHIR constraints and longitudinal rules enrich this common representation.

The user selected longitudinal analysis as an eventual fidelity priority.
That remains a requirement for the complete generator; it does not replace or
delay the generic field extraction and distribution-building foundation.

Use counts, distribution summaries and explicit conditional probability tables.
No neural training or automatic graphical-model training is planned. An optional
copula would be a later statistical estimator, with parameters estimated locally.

## Pipeline

```text
Local FHIR JSON resources
  -> preserved resources and reference index                 [implemented]
  -> recursive extraction of all structure and values        [implemented]
  -> distributions for each resource type and field path     [implemented]
  -> joint/conditional distributions for related fields
  -> sampling of new nested resource structures and values
  -> new references, clinical/temporal rules and validation
```

The complete pipeline is iterative: add required field relationships and
longitudinal context before claiming that generated patient records preserve
them. An independent-field sampler is only a diagnostic baseline.

## Milestone 1: generic recursive extraction

Read every ingested resource and walk its JSON tree. Record both container
nodes and leaf values. Observing only leaves would lose the difference between
an absent field, an empty object, an empty array and an explicit null.

For every occurrence, retain:

| Attribute | Purpose |
| --- | --- |
| Resource type and internal resource key | Separate resource populations and connect fields from the same resource |
| Declared profile URLs/versions | Additional grouping and later validation context; not an extraction prerequisite |
| Concrete source path | Locate the exact original field, including array positions |
| Normalized field path | Combine corresponding fields across resources |
| Parent node and array-element identity | Keep sibling fields and repeated groups associated |
| Node/JSON value type | Distinguish object, array, string, number, boolean and null |
| Original value or container shape | Preserve leaf values, child keys, array order and lengths |
| Provenance | Trace the field to the source resource/file occurrence |

For example, the concrete paths

```text
component[0].valueQuantity.value
component[1].valueQuantity.value
```

share the display path `component[*].valueQuantity.value`, but remain associated
with different component instances. Use structured path tokens internally so
literal dots, brackets or asterisks in object keys cannot collide with path
syntax.

Visit arbitrary nested objects, heterogeneous arrays, arrays of arrays,
extensions and primitive companion fields such as `_birthDate`. Preserve
numeric precision and never coerce an identifier-like string into a number.
A missing field is inferred relative to eligible parents, not represented by
inventing a source value.

Use the ingestion index to avoid counting exact duplicate resources twice.
Retain containment ownership: do not count a contained resource's fields again
because its payload appears both inside its parent and in a separate indexed
row. Multiple profile declarations must not multiply the base resource count.

**Deliverable:** a local field-occurrence index and a readable structure
inventory. These contain source information and remain on hospital storage.

**Completion checks:** reconstruct equivalent parsed JSON from the extracted
node structure; preserve nested associations, values and empty containers.
Fixtures include unknown fields, key/path collisions, mixed types, repeated
objects, nulls, booleans, decimals, primitive extensions and containment.
No field may disappear merely because no clinical mapping exists.

## Milestone 2: distributions of structure and values

Group observations first by resource type and normalized field path. Retain
parent/resource association throughout. Profile all observed paths, including
container nodes; enumerate the union of child keys across corresponding parents
so that absence can be counted.

| Observed information | Statistical description |
| --- | --- |
| Field/object presence | Present and absent counts with eligible-parent denominator |
| JSON value types | Counts/proportions of each type at the path |
| Object shape | Child-key presence and supported co-occurring key patterns |
| Array shape | Length distribution, empty-array rate and element-type patterns |
| Numeric values | Counts, range, quantiles and a specified histogram/empirical distribution representation |
| Strings and booleans | Frequencies, cardinality and string-format/length summaries |
| Null/empty values | Separate counts; not silently converted to absence or zero |
| Repeated elements | Per-element distributions and their resource/parent associations |

A generic numeric or string summary describes the source representation; it
does not establish that the field is a meaningful clinical variable or safe
to sample. High-cardinality strings need an explicit storage/reporting policy
such as bounded frequency summaries with a remainder count. This must not
discard their original values from the local source/occurrence index.

Denominators are part of every profile. For example:

- `P(valueQuantity exists | Observation)` uses eligible Observation resources.
- `P(unit exists | valueQuantity exists)` uses existing quantity objects.
- Component-field presence uses eligible component objects, not all patients.

A resource with 100 array elements must not automatically receive 100 times
the weight in a resource-level statistic. Publish element-weighted and
resource-weighted views separately when needed. Keep observed nulls visible as
source anomalies even where a later FHIR validator disallows them.

Declare exact versus approximate estimators, histogram boundaries, precision,
sample counts and configuration in the output. Spill large intermediate counts
to local storage; do not require the whole cohort to fit in memory.

**Deliverable:** versioned structural/value distributions and coverage counts.
Every path has a summary or an explicit reason a particular estimator does
not apply. These are local statistics, not automatically safe release artifacts.

**Completion checks:** hand-calculated examples verify presence denominators,
array lengths, type frequencies, numeric summaries and string counts. Verify
that unrecognized resource fields receive the same generic treatment.

## Milestone 3: dependencies between fields and repeated groups

Keep the generic extraction independent of domain-specific rules. Build
relationships on top of the retained parent, array and resource identities.

Start with explicit dependency specifications for:

1. Object/choice structure: which fields and types can occur together.
2. Sibling fields: categorical combinations and conditional value distributions.
3. Repeated objects: array length, element shape and associated sibling values.
4. Context elsewhere in the same resource, reached through the owning object.
5. Cross-resource context, reached through the existing reference graph.

For a quantity, field-path marginals are still useful as inventory, but sampling
may need a distribution conditioned on the associated measurement coding,
unit and other relevant context. Do not combine incompatible measurements just
because they share `valueQuantity.value`.

For a component, its code and value belong to that component instance.
Normalizing array indices must never turn that into all possible code/value
pairs. Multiple coding translations are attributes of one clinical concept,
not automatically independent measurements.

Use conditional frequency tables, grouped numeric distributions and selected
joint summaries. Specify a bounded conditioning set, sample support, smoothing
and fallback rules. Do not attempt a full joint table over every JSON field.
Never pool incompatible contexts merely to enlarge a small group.

Generic co-occurrence cannot determine every clinical dependency. Optional
FHIR/profile definitions and explicit semantic adapters supply constraints and
context rules while continuing to use the same extracted field representation.
Unknown semantics remain visible as limits on generation, rather than being
silently omitted from extraction.

**Deliverable:** a dependency specification plus conditional distributions,
including supported contexts and fallback behavior.

**Completion checks:** invented examples preserve code/unit/value associations,
choice-field patterns and repeated-group membership. Sampling must not claim
to preserve relationships that were never modeled or checked.

## Milestone 4: sample new nested JSON resources

Generate new structures from the distributions, then populate associated
values according to the dependency specification:

- Choose supported object shapes, optional fields and value types.
- Sample array lengths and construct new element objects.
- Sample compatible field groups and dependent values.
- Assign fresh identities and construct references between new resources.
- Apply required fixed values, derived values and validated constraints.

Do not use source resources or whole source array elements as donor templates.
Generation draws from distributions; ordinary clinical values may naturally
coincide with observed values, but complete patient records are not copied.

Separate extraction coverage from generation eligibility. Every field is
extracted, but source identifiers, names, addresses, narratives and attachments
cannot simply be sampled from their observed values. A generation policy must
identify fields to sample, derive, generate anew, omit when permitted, or block
pending an interpretation. Unknown required fields or modifier semantics may
prevent generation for that profile while remaining fully visible in profiling.

A quantity comparator, its numeric threshold and its unit must stay associated.
A value marked '<' is not treated as an exact measurement. Dates, complex
datatypes and local extensions require appropriate interpretation before making
clinical fidelity claims.

**Deliverable:** new resources generated from a supported subset of the
statistical description, with seeded randomness and an explicit coverage report.

## Milestone 5: add clinical and longitudinal fidelity

Use the generic field facts, relationships and FHIR/profile definitions to
derive linked clinical events and patient timelines. This is an enrichment
layer; do not create a second extraction path that silently loses arbitrary
fields from the generic representation.

For the hospital scope, enrich Patient, Encounter, Condition, Observation,
Procedure, MedicationAdministration and supporting Medication, Consent and
Location. Preserve unknown local resource/profile fields throughout.

The user-selected longitudinal target requires:

- Encounter hierarchy, event times, interval durations and follow-up windows.
- Measurement counts and irregular time gaps by relevant context.
- Value changes conditional on previous values, elapsed time and recent history.
- Joint behavior of paired or aligned measurements.
- Procedure/medication sequences, overlap and treatment start/stop patterns.
- Patient-level weighting and uncertainty, distinct from event-level totals.

Estimate temporal distributions and transitions using explicit definitions
and supported conditional tables. Field histograms alone cannot preserve
trajectories. If a short-memory process is used as an initial sampler, measure
its limitations on longer trends before deciding whether to extend it.

Construct new patient trajectories with generated calendar anchors and fresh
references. Coordinate counts, timing, values and treatment state under the
specified dependencies. Preserve timestamp precision and unknown endpoints;
the end of an export is not evidence of recovery, discharge or death.

## Milestone 6: validate and package the supported scope

Check structural reconstruction, extraction coverage and statistical fidelity
before generation. After generation, compare the source and synthetic data with
the same profiler and denominator definitions. Verify field distributions,
object shapes, array structure and declared conditional relationships.

For the longitudinal release, also compare inter-event gaps, value changes,
event transitions, treatment durations, within-patient variability and selected
downstream longitudinal queries. Define tolerances and minimum support per use
case; matching marginal histograms is not sufficient.

Validate base FHIR, the pinned hospital profiles and reference integrity.
The provisional FHIR target is R4 4.0.1; the supplied profile list does not
confirm the hospital's exact deployed package versions.

Keep all processing offline. Bundle the runtime, any needed numerical
libraries, profiles and terminology assets for the hospital's target OS.
Benchmark realistic export sizes. Keep source data and intermediate occurrence
indexes on approved local storage.

Assess disclosure separately from fidelity. Synthetic data and aggregate
statistics do not automatically provide a privacy guarantee. Identifier/text
leakage, rare combinations and close source trajectories need evaluation; a
formal differential-privacy requirement would need its own design.

## Implementation progress and boundaries

**Milestones 1 and 2 are implemented**, using invented nested JSON fixtures and
the existing FHIR example. Tests check complete extraction, reconstruction,
parent/array associations, exact frequencies and statistical denominators.
Clinical adapters are not prerequisites for this profiler. The next milestone
is explicit dependencies between fields and repeated groups.

Keep the implementation split into small modules:

| Module or planned component | Responsibility |
| --- | --- |
| `json_fields.py` | Recursive nodes, typed paths and parent/array associations |
| `field_store.py` | Local occurrence index and provenance |
| `field_statistics.py` | Structural/value distributions and denominators |
| `dependencies.py` | Explicit context relationships and conditional summaries |
| `sampling.py` | New nested structures and associated values |
| `fhir_rules/` | Identity/reference handling, semantic adapters and constraints |
| `validation/` | Structural, statistical, FHIR and temporal checks |

The `profile` command now produces `field-occurrences.sqlite`,
`field-inventory.json` and `field-statistics.json`. A dependency/generation-policy
specification, synthetic FHIR files and generation validation reports remain
planned outputs.

The existing `store.py` remains the ingestion/reference layer. Reuse its
resources and provenance without expanding it into a large clinical parser.

## Specification references

The design above is the project's chosen approach. FHIR definitions support
the later semantic and conformance layers:

- [HL7 R4 Observation](https://hl7.org/fhir/R4/observation.html)
- [HL7 R4 datatypes](https://hl7.org/fhir/R4/datatypes.html)
- [HL7 R4 profiling](https://hl7.org/fhir/R4/profiling.html)
