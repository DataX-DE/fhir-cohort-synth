# Local FHIR perturbation

`perturb` produces **perturbed source-derived data** from a completed ingestion
index and its matching field profile. It preserves the resource population and
graph while changing supported fields. No model is trained. Processing and all
outputs stay local; the result has no privacy or anonymization guarantee.

## Run it

```sh
python3 fhir_synth.py ingest --input examples/mii-demo-bundle.json --output local-data/demo
python3 fhir_synth.py profile --input local-data/demo/cohort.sqlite --output local-data/profile
python3 fhir_synth.py perturb \
  --input local-data/demo/cohort.sqlite \
  --fields local-data/profile/field-occurrences.sqlite \
  --output local-data/perturbed \
  --strength 0.02 --date-shift-days 30 --seed 42
```

Every output directory must be new. Python 3.11+ and its standard-library SQLite
module suffice. There are no runtime downloads or third-party dependencies.
No relationship rules file is needed. The engine uses the ingestion reference
graph directly and checks that the field profile describes the same snapshot.

```python
from fhir_cohort_synth.perturbation import perturb

summary = perturb(
    "local-data/demo/cohort.sqlite",
    "local-data/profile/field-occurrences.sqlite",
    "local-data/another-perturbed-run",
    strength=0.02, date_shift_days=30, seed=42,
)
```

The same input snapshot, settings and seed give deterministic records. Strength
must be finite and in `[0, 1)`; the day range must be a supported nonnegative
integer. Zero disables that value transformation; identity replacement still
runs. Reordering/re-ingesting an export can change its snapshot identities, so
determinism is defined for the same indexed snapshot.

The field profile is required for the matching-input check. Its frequency tables
do not choose the changes: factors and offsets come from the configured ranges.
Before/after measurement statistics are calculated as part of perturbation.

## Field handling

The bundled FHIR R4 4.0.1 index resolves datatypes, choice suffixes such as
`effectiveDateTime`, nested backbone elements, primitive companions and contained
resources. Arbitrary field names use typed paths, never dot-string parsing.
Base definitions guide transformation; the input is not assumed to have passed
full FHIR or hospital-profile validation.

| Fields | Handling |
| --- | --- |
| Eligible `Quantity.value` and `Distance.value` | Multiply by one factor per patient, uniformly selected in `1 ± strength` |
| `Age`, `Duration`, `Count`, standalone numbers | Preserve |
| Full `date`, `dateTime`, `instant` | Apply one whole-day offset per patient; retain time, offset and fractional suffix |
| Partial/invalid dates, time-only values | Preserve; report the handling |
| Root and contained resource IDs | Replace consistently; add an ID when a root lacks one |
| Resolved literal references | Rewrite to `ResourceType/new-id` or `#new-id` for contained targets |
| `Identifier.value` | Replace deterministically using its system and original value; preserve type/system |
| `HumanName.text`, `family`, `given`, `prefix`, `suffix` | Replace nonempty existing strings with deterministic dummy labels |
| Codes, codings, booleans, other strings | Preserve |
| Narrative, attachments, unknown extension contents | Preserve and report |
| Unknown fields/resource types | Preserve and report unsupported handling |

An indexed identity or resolved reference is remapped even inside otherwise
preserved content. This precedence preserves graph targets, including a
`valueReference` in an unknown extension. Other extension contents stay unchanged.
References that originally failed to resolve, were ambiguous, or resolved
differently across duplicate occurrences are preserved with warnings. Logical
references are not resolved by guessing; their Identifier values follow the
Identifier rule. External URLs and narrative links are not automatically rewritten.

Only exact system/code pairs listed in `fhir_cohort_synth/data/linear-units.json`
are eligible. This small registry covers mass, length, volume, pressure,
frequency, concentration and flow, including MIMIC's unit namespace. Display
text never determines support. Temperature, percentages, logarithmic, missing
and unrecognized units stay unchanged. No unit conversion is performed: applying
the same factor to equivalent linear measurements preserves their relationship
before rounding. Numeric contexts retain ancestor/component codes, medication or substance
references, comparator and exact unit information. Different drugs and panels
are therefore not pooled merely because a quantity shares the same JSON path
and unit. Reference contexts use the original source literals; equivalent
spellings can remain separate because clinical normalization is outside scope.

Empty strings, explicit nulls, empty containers and missing fields are retained.

Decimal arithmetic rounds half-even to the original represented precision:
`100.00` keeps two decimal places and an integer JSON token remains an integer.
This can leave small values unchanged. Relative change is `(after − before) /
abs(before)`; zero baselines are reported separately because this ratio is
undefined. Relative summaries use 34 significant digits, while numeric value
frequencies retain the original and transformed Decimal representations.

Shared or unassigned resources retain their quantities and dates. Identity
replacement still applies. Each patient's date offset is chosen from the
requested range intersected with the representable bounds of all their supported
dates. Boundary dates can force the offset to zero. Supported dates belonging
to the same patient keep their intervals and order; this does not cover partial
dates, preserved extension dates, shared resources or comparisons across patients.

## Worked example

Suppose the prepared parameters for one patient are **factor `1.01`** and
**offset `+7 days`**. These are illustrative parameters, not a promise about
which values seed 42 will draw.

| Existing field | Before | After |
| --- | --- | --- |
| Encounter period start | `2020-01-01T09:00:00.000+01:00` | `2020-01-08T09:00:00.000+01:00` |
| Encounter period end | `2020-01-03T09:00:00.000+01:00` | `2020-01-10T09:00:00.000+01:00` |
| Observation quantity, UCUM `mg/dL` | `100.00` | `101.00` |
| Repeated quantity for the same patient | `120.00` | `121.20` |
| Component quantity, UCUM `kg` | `70.0` | `70.7` |
| Component quantity, UCUM `%` | `98` | `98` |
| Clinical coding and quantity units | Existing objects | Identical objects |

The encounter still lasts two days. Observation and component arrays keep their
positions and siblings; no new combinations are sampled. The output Patient,
Encounter and Observations receive replacement IDs and continue to reference
the same corresponding resources. Their codes and all preserved content stay
exactly as supplied.

## Outputs and validation

- `perturbed.ndjson`: one line per deduplicated resource root, with contained
  resources nested once. Bundle envelopes are outside this population.
- `perturbation-state.sqlite`: settings, source fingerprint, definition
  provenance, identity maps, patient parameters, exact changes, action counts,
  numeric frequencies and run status. It contains original source values.
- `perturbation-report.json`: coverage by root resource type and normalized
  field path, original warning counts, before/after numeric summaries and
  actual relative changes, including unchanged numeric values. It omits source
  string examples; exact context coding/unit values are in the local database.

Each object, array and scalar has an action count; these counts are not the
number of resources. Numeric contexts include preserved numeric fields as well
as eligible quantities. Their summaries contain sample count, minimum, maximum
and weighted nearest-rank percentiles at 5, 25, 50, 75 and 95 percent. Exact value
frequencies live in SQLite, with decimals stored as text and ordered numerically.

The validator reads the emitted NDJSON afresh, checks replacement identities
and reference targets, and verifies the shared factors and offsets for changed
values. Undoing the recorded changes must reproduce every source root's digest.
This verifies that codes, booleans, arrays, empty/missing fields, unknown content
and all other unrecorded fields are preserved. Adding a missing root ID is the
only permitted added field.

This proves the specified transformations and preservation properties. It does
not establish every clinical dependency, full FHIR conformance or unchanged
cohort distributions. Shared scaling preserves ratios and series shape before
rounding; rounding and different patient factors can alter aggregate statistics.
No categorical randomization, trajectory generation or privacy certification
is performed. Narrative, attachments and other source text remain present.

The run completes only after output validation, statistical aggregation and
report writing. Exit codes are 0 for completed runs (including warnings), 2 for
failure and 130 for interruption. Check `run.status`; failed/interrupted runs
retain local partial artifacts and require a new destination. A forcibly killed
process can retain `in_progress`. Input databases are opened read-only. POSIX
permissions are `0700` for the output directory and `0600` for files; completed
databases need no WAL/SHM sidecars.

## Follow the code and inspect coverage

`fhir_types.py` resolves and walks datatypes. `perturbation_handlers.py` contains
the small transformations. `perturbation.py` prepares mappings and date bounds,
writes records, validates them and completes the run. `perturbation_store.py`
stores audit data and aggregates distributions. Processing holds one resource
tree at a time, bounded caches and bounded frequency batches; mappings and the
cohort stay in SQLite. Large runs may need substantial temporary WAL disk space.

Useful local SQLite queries:

```sql
SELECT status, phase, failure_code FROM run;

SELECT resource_type, action, sum(frequency) AS fields
FROM field_actions GROUP BY resource_type, action;

SELECT reason, sum(frequency) AS fields
FROM field_actions GROUP BY reason ORDER BY fields DESC;

SELECT patient_id, factor, days, minimum_days, maximum_days
FROM patient_parameters;

-- Context contents remain local source-derived strings.
SELECT c.id, c.context_json, c.samples, c.changed, s.phase, s.summary_json
FROM numeric_contexts c JOIN numeric_summaries s ON s.context_id=c.id;
```

## Bundled definitions

The compact index contains 214 base datatype/resource definitions derived from
the official [FHIR R4 core package](https://hl7.org/fhir/R4/hl7.fhir.r4.core.tgz),
listed on the [HL7 downloads page](https://hl7.org/fhir/R4/downloads.html).
Source package: `hl7.fhir.r4.core`, version `4.0.1`, CC0-1.0.

Source SHA256:
`b090bf929e1f665cf2c91583720849695bc38d2892a7c5037c56cb00817fb091`

Definition-index SHA256:
`dc66b429d9efea18ee693a3d42854160e3be3a497efdae3d8158b3fd3e59abbb`

Both hashes and the source URL are embedded in `fhir-r4-types.json` and copied
into each run. The runtime verifies the definition-index checksum. To rebuild
from an already downloaded public package during development:

```sh
python3 tools/build_fhir_type_index.py \
  work/hl7.fhir.r4.core-4.0.1.tgz \
  fhir_cohort_synth/data/fhir-r4-types.json
```

Hospital profile packages are not bundled; recognized fields use the base
definitions even when `meta.profile` names an unfamiliar profile. The report
explicitly records that hospital-profile conformance has not been established.
