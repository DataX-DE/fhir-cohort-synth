# Exact conditional distributions

Conditional profiling answers: **within this configured context, how often does
each joint outcome occur?** It consumes the existing relationship groups and
counts each anchor once. It does not generate synthetic resources.

## Run it

Use a completed ingestion database, its matching field database and a fresh
output directory:

```sh
python3 fhir_synth.py profile-conditional \
  --input local-data/demo/cohort.sqlite \
  --fields local-data/profile/field-occurrences.sqlite \
  --rules examples/dependency-rules.json \
  --output local-data/conditional
```

The Python API accepts the dictionary returned by `load_rules()`:

```python
from fhir_cohort_synth.dependencies import load_rules
from fhir_cohort_synth.conditional_statistics import profile_conditional

summary = profile_conditional(
    "local-data/demo/cohort.sqlite",
    "local-data/profile/field-occurrences.sqlite",
    load_rules("examples/dependency-rules.json"),
    "local-data/conditional",
)
```

Only the Python standard library and SQLite are required. Inputs are opened
read-only. A completed run produces `conditional-statistics.sqlite` and
`conditional-statistics.json`; neither is synthetic or anonymized data.

With the invented FHIR example and supplied rules, the CLI produces 19 included
groups across 12 contexts. One Observation has no Encounter reference and is
excluded from the Encounter-conditioned rule, producing a completed-with-warnings
run. That Observation still contributes to the separate quantity rule.

## Choose context and outcome fields

Add `statistics` to a [dependency rule](dependencies.md):

```json
"statistics": {
  "given": [{"field": "coding"}, {"field": "unit"}],
  "targets": [{"field": "value"}]
}
```

The names refer to that rule's existing field selectors. `given` determines
which groups share a context. `targets` defines one **joint outcome**: two
targets are counted as observed pairs, not separate independent distributions.
An empty `given` list produces one unconditional context. `targets` must contain
at least one field.

To use a linked resource field, write
`{"link": "encounter", "field": "class"}`. The link and field must already be
defined in the rule. Names are separate JSON properties, so dots inside a name
do not become navigation syntax. Missing field/link definitions fail validation.

The example rules calculate:

- Quantity values given coding, unit system/code/display and comparator.
- Component values given their own coding, units and comparator.
- Observation coding given the linked Encounter class.

The quantity rule includes all matching Observation anchors, including those
without a quantity value. Such selections remain explicit missing states in
the joint frequencies; they are not silently converted to zero measurements.

Rules without `statistics` are skipped by this command and still work with
`iter_groups()`. No configured statistics is an error. A configured rule with
no matching anchors is a valid zero-count result.

## Worked calculation

Suppose four anchors have these selected values:

| Anchor | Coding | Unit | Value |
| --- | --- | --- | --- |
| 1 | A | u | 10 |
| 2 | A | u | 10 |
| 3 | A | u | 20 |
| 4 | B | v | 90 |

For context `(A, u)`, the denominator is three anchors:

| Outcome | Frequency | Conditional probability |
| --- | --- | --- |
| 10 | 2 | 2/3 |
| 20 | 1 | 1/3 |

Context `(B, v)` has its own denominator of one and assigns probability 1 to
the observed outcome 90. Values from these contexts are not pooled.

If one anchor has two coding translations, both remain in its context as an
ordered collection. They do not create two anchors. Exact coding content is
used, including selected display fields; this layer does not identify semantic
equivalence between different representations.

## What is counted

Context and outcome keys include only selected content and selection states:

- Objects use sorted keys; arrays preserve their order and nested boundaries.
- Each field retains all its matches together. Array matches do not form a
  Cartesian product with other fields.
- Relative paths keep array positions inside the selection. Absolute anchor
  positions, database resource/node IDs and source file locations are excluded.
  Explicitly selected source fields, including any FHIR identifiers, remain data.
- Missing, explicit null, empty containers, empty wildcard results and
  non-applicable traversal remain distinct. A missing parent retains its
  unfinished path rather than becoming a missing child of an existing object.
- Numeric JSON tokens remain text in SQLite. `1`, `1.0`, `true` and `"1"`
  remain distinct typed outcomes. This follows the existing parsed-JSON codec,
  not original file whitespace or every original escape/numeric spelling.

For a linked field used by the statistics, all selected references must resolve
to exactly one distinct target of the expected type. Repeated reference slots
to that same target are allowed. Missing, unresolved, wrong-type, ambiguous or
multiple-target contexts exclude the whole anchor from those statistics.
All failing links and reasons are recorded together so that the anchor is
counted once in exclusions. Unused links cannot exclude a group. A resolved
target whose selected field is absent contributes a missing field state.

For each context, the report separates:

- `group_count`: the probability denominator, one vote per included anchor.
- `resource_count`: distinct non-contained resource roots contributing anchors.
- `patient_count`: distinct known patients from the ingestion membership table.
- `unassigned_group_count` and `unassigned_resource_count`: unknown membership.

These support counts do not change the weights. A resource with several
component anchors contributes several groups; their patient is counted once
within that context. Counts from different rules or contexts should not be
summed to obtain unique patients.

Numeric summaries use only targets with **exactly one present numeric match**.
All other target forms still contribute joint outcome frequencies. Reports give
the numeric sample count explicitly, which may be smaller than `group_count`.
Minimum, maximum and percentiles 5/25/50/75/95 use Decimal ordering and the same
frequency-weighted nearest-rank estimator as the marginal profiler. These are
descriptive numeric values; comparator thresholds are not interpreted as exact
clinical measurements.

## Inspect the results

| SQLite table/view | Contents |
| --- | --- |
| `rules` | Active definitions; array order identifies context and target fields. |
| `contexts` | Exact context keys, group counts and stable report hashes. |
| `outcome_frequencies` | Complete joint outcome keys and exact frequencies. |
| `conditional_probabilities` | Exact numerator/denominator plus a floating-point display ratio. |
| `context_resources` / `context_support` | Resource/patient support and unassigned counts. |
| `numeric_frequencies` / `numeric_summaries` | Exact numeric tokens and summaries per context/target. |
| `exclusions` | Complete combinations of exclusion reasons and their counts. |
| `run` | Input provenance, status and generic failure marker. |

For example:

```sql
SELECT r.name, c.context_json, p.outcome_json,
       p.numerator, p.denominator, p.probability
FROM conditional_probabilities p
JOIN contexts c ON c.id = p.context_id
JOIN rules r ON r.id = c.rule_id
ORDER BY r.name, c.context_json, p.outcome_json;
```

`context_json` is an array in `given` order; `outcome_json` is an array in
`targets` order. Each element is a list of selection matches with status, relative
path and typed `value_json` content, or a blocked-selection description. Decode
`value_json` with the project's JSON codec to retain decimal precision.

The JSON report contains counts, numeric summaries, definitions and exclusion
reasons; exact context/outcome string samples stay in SQLite. `context_hash`
labels a context consistently across input order; full context content, not
that hash, determines equality. Numeric `target_index` is zero-based within the
rule's `targets` list. Reports, hashes and small counts are still source-derived
information and should remain local.

## Completion and limits

The writer commits every 1,000 groups, uses SQLite for high-cardinality counts
and sorting, and streams the report. Memory depends on individual selected
resources/collections, not the number of distinct contexts or outcomes.
Completed databases have no required WAL sidecars.

New runs also store a cohort fingerprint inside SQLite run provenance. The
generator uses it to verify resource keys and patient/reference associations
before joining source counts to conditional statistics. The aggregate JSON
report excludes this index-specific fingerprint so it remains independent of
source input order. Older databases are still usable for profiling; generation
requires rerunning this command to obtain the fingerprint.

Success requires the final JSON report and a database status of `completed` or
`completed_with_warnings`. Source warnings and excluded groups produce the latter
status and CLI exit code 0. Errors return 2; interruptions return 130. A failed,
interrupted or in-progress destination is not reusable: retry in a new directory.

No smoothing, fallback pooling, unit/date normalization, temporal modeling or
synthetic sampling is performed. The distributions describe only the configured
relationships and observed cohort.
