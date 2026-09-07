# Plan: retain FHIR structure and perturb selected values

## Decision and current status

The selected direction is to keep each source patient's linked resource
structure and event sequence and change selected values for local hospital use.
This replaces the earlier independently sampled cohort generator.
The planned output is called **perturbed source-derived data**. The current
scope is structural and statistical fidelity, with no privacy, anonymization
or differential-privacy guarantee. Local execution does not itself anonymize
the data. Privacy assessment is outside the current implementation scope.

Implemented and retained:

- Ingestion of local FHIR JSON/NDJSON exports, preserved payloads, reference
  resolution, containment and patient membership.
- Recursive extraction of every object, array and scalar, with typed paths,
  parent/array associations, empty values and decimal precision.
- Exact field distributions, explicit relationship rules and configured
  joint/conditional distributions with resource/patient support.

The `generate` command, generator, sampling helper and generation-specific
tests/documentation have been removed. Existing local generated files and their
review remain available as evidence of the previous approach's limitations.
A recoverable copy of the removed implementation is kept in ignored local
`work/retired-generation-*` storage. No perturbation command exists yet.

The generic profiler covers all retained resource fields. Semantic mappings,
unit normalization, full profile validation and privacy protection are not
provided by generic JSON traversal. Bundle envelopes are stored separately;
contained subtrees are profiled once under their owning root.

## Replacement workflow

```text
Local FHIR export
  -> ingestion and existing linked resource graph          [implemented]
  -> recursive field inventory and exact statistics        [implemented]
  -> explicit transformation policies and coverage report  [planned]
  -> coordinated changes to existing records               [planned]
  -> structural, statistical and FHIR validation            [planned]
```

Patient counts, encounter membership, nested groups and event ordering come
from the source. There is no new patient/encounter-count or trajectory sampler.
Existing conditional statistics become baselines for choosing compatible
contexts and measuring distortion. No source is modified in place.

## 1. Assign an action to every field

Use the existing typed paths, anchors and reference graph. Add common FHIR
datatype/semantic handlers and a small readable JSON policy file, rather than
a separate JSON parser for each resource type. A JSON string alone does not
identify a code, date, name or reference; profile definitions or explicit
policies supply that distinction.

Each field has a reported action: preserve, replace, shift, perturb, redact
or unsupported. Missing mappings must remain visible. Arbitrary extensions,
primitive companions, modifier extensions and unknown resource fields must
have their handling recorded, including content deliberately left unchanged.

| Field role | Proposed treatment |
| --- | --- |
| Numeric measurements | Calibrate changes within compatible code/unit/comparator contexts; coordinate related and repeated values |
| Categories and booleans | Preserve explicitly or use valid, context-aware substitutions of related fields together |
| IDs and references | Create replacement identities and rewrite all resolved links consistently, including contained and supporting resources |
| Dates and periods | Apply a shared patient-level shift where appropriate, preserving durations, ordering, precision and time zones; report conflicts involving shared resources |
| Names, identifiers, narrative and free text | Use explicit replacement/redaction rules; never treat arbitrary text as a safe categorical code |
| Attachments and external URLs | Require an explicit content policy; changing an attachment URL does not sanitize its contents |
| Resource types, schema keys, public terminology/profile identifiers | Preserve their meaning and syntax; these are not measurement values to noise |
| Objects, arrays and unknown fields | Retain associations; report unsupported transformations and any deliberate structural changes |

Redaction must respect required fields and FHIR types. Unsupported handling
can block an export or be explicitly configured for preservation with that
limitation recorded. Cover every
observed resource type in the report, even when its transformation is unsupported.

## 2. Apply coordinated perturbations

Keep the implementation small: a policy/handler module and a coordinator that
reads resources, applies changes and writes a new output. Reuse existing
database checks, typed paths and relationship groups. No new extraction layer,
neural training or large dependency stack is needed for this stage.

Changes must share context across resources. For example, repeated heights and
their unit equivalents need a consistent transformation. Related measurement
fields cannot receive unrelated changes merely because they occupy different
JSON paths. Shifting all dates for a patient preserves intervals, but does not
by itself conceal a distinctive history or preserve calendar-date distributions.

Select mechanisms through small experiments before fixing the implementation:

- For numeric values, compare bounded or rank-based changes within compatible
  contexts, with transformation parameters shared across dependent groups.
  Bounds, rounding and unit conversions must not introduce invalid values.
- For categories, evaluate invariant post-randomization (PRAM) on valid grouped
  states where appropriate. It can preserve selected frequencies in expectation;
  it does not automatically preserve all joint relationships or every finite
  output's exact counts. See [Statistics Netherlands on PRAM](https://research.cbs.nl/casc/Related/Sdp_98_2.pdf)
  and the [US Census study of invariant PRAM](https://www.census.gov/library/working-papers/2014/adrm/cdar2014-01.html).

These are candidate transformations, not implemented algorithms.
Preserving original links helps retain context, but value changes can still
break clinical relationships; the validation step must detect that.

## 3. Measure structural and statistical fidelity

"Same distribution type" is too weak as an acceptance criterion. For example,
adding independent Gaussian noise to Gaussian values retains that family while
increasing variance. Other combinations change the family itself. Real fields
also need not belong to one simple parametric family.

Compare the original and perturbed outputs using the existing profiler:

- Every resource type and field: coverage/action counts, structure, missingness,
  category frequencies, numeric quantiles and empirical distribution changes.
- Related groups: joint/conditional distributions, repeated-measurement
  variation, unit consistency and dependencies within patients and encounters.
- Resource graph: replacement IDs, resolved references, containment, shared
  resources, encounter hierarchy and patient membership.
- Time and FHIR: ordering, intervals, datatypes, choice fields and the exact
  hospital profile/terminology packages when available.

Set explicit distortion tolerances per relevant context before claiming useful
distribution preservation. Tests must cover numbers, categories, booleans,
dates, text, references, unknown fields, arrays and missing/empty values, not
only height and weight. Preserve the ingestion/profiling tests and use the
invented FHIR example before repeating the local cohort review.

## Local execution and delivery

Retain read-only inputs, fresh output directories, local-only processing,
private file permissions and completion/error reporting. Outputs stay local;
external release and privacy certification are outside the current scope.
Packaging follows a working, validated transformation stage; a bundled Python
runtime can keep hospital setup small.

See [field extraction and profiling](profiling-schema.md),
[relationship rules](dependencies.md), [conditional statistics](conditional-statistics.md)
and [FHIR R4 datatypes](https://hl7.org/fhir/R4/datatypes.html) for the retained
foundation and semantic constraints.
