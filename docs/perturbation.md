# Local FHIR perturbation

`perturb` produces **perturbed source-derived data** from a completed ingestion
index. The `run` command creates that index and runs perturbation in one step.
It preserves the resource population and graph while changing supported fields.
No model is trained. Processing and all
outputs stay local; the result has no privacy or anonymization guarantee.

## Run it

```sh
python3 fhir_synth.py run \
  --input examples/mii-demo-bundle.json \
  --output local-data/demo \
  --strength 0.10 --date-shift-days 30 --seed 42
```

Every output directory must be new. Python 3.11+ and its standard-library SQLite
module suffice. There are no runtime downloads or third-party dependencies.
The export files are in `local-data/demo/perturbed/fhir/`. Original indexed data
and ingestion reports are in `local-data/demo/index/`. The overall `run.json`
records completion only after ingestion, perturbation and output checks finish.
The engine uses the ingestion reference graph directly.

To reuse an existing index without ingesting the files again:

```sh
python3 fhir_synth.py perturb --input local-data/demo/index/cohort.sqlite --output local-data/another-perturbed-run
```

```python
from fhir_cohort_synth.perturbation import perturb

summary = perturb(
    "local-data/demo/index/cohort.sqlite",
    "local-data/another-perturbed-run",
    strength=0.10, date_shift_days=30, seed=42,
)
```

The same input snapshot, settings and seed give deterministic records. Strength
sets the upper relative quantity change and must be finite and in `[0.01, 1)`,
or zero to disable numeric changes. The minimum magnitude is fixed at `0.01`.
The day range must be a supported nonnegative integer; zero disables date shifts.
Identity replacement still
runs. Reordering/re-ingesting an export can change its snapshot identities, so
determinism is defined for the same indexed snapshot.

Each eligible quantity occurrence independently draws a magnitude uniformly
between 1% and `strength` (default 10%), and an increase/decrease with equal
probability. For example, `100.00` with a 4% increase becomes `104.00`; another
occurrence with a 7% decrease becomes `93.00`. The seed, prepared root identity
and concrete field path determine its draw, including individual array positions.
Date offsets remain shared per patient. Before/after measurement
statistics are calculated during perturbation; there is no separate full-field
extraction or profiling stage.
The earlier `field_db` API argument and `--fields` CLI option have been removed.

The complete Python entry point is `workflow.run_export(inputs, output_dir, ...)`,
with the same strength, date range and seed options plus an optional `base_url`.

## Field handling

The bundled FHIR R4 4.0.1 index resolves datatypes, choice suffixes such as
`effectiveDateTime`, nested backbone elements, primitive companions and contained
resources. Arbitrary field names use typed paths, never dot-string parsing.
Base definitions guide transformation; the input is not assumed to have passed
full FHIR or hospital-profile validation.

| Fields | Handling |
| --- | --- |
| Eligible `Quantity.value` and `Distance.value` | Independently add or subtract 1% to `strength` of each value, then round |
| `Age`, `Duration`, `Count`, standalone numbers | Preserve |
| Full `date`, `dateTime`, `instant` | Apply one whole-day offset per patient; retain time, offset and fractional suffix |
| Partial/invalid dates, time-only values | Preserve; report the handling |
| Root and contained resource IDs | Replace consistently; add an ID when a root lacks one |
| Resolved literal references | Rewrite to `ResourceType/new-id` or `#new-id` for contained targets |
| Local canonical fragments, e.g. `answerValueSet: "#choices"` | Rewrite to the same contained target's new ID; preserve external canonical URLs |
| `Identifier.value` | Replace deterministically using its system and original value; preserve type/system |
| `HumanName.text`, `family`, `given`, `prefix`, `suffix` | Replace nonempty existing strings with deterministic dummy labels |
| Codes, codings, booleans, other strings | Preserve |
| Narrative, attachments, unknown extension contents | Preserve and report |
| Unknown fields/resource types | Preserve and report unsupported handling |
| Inline resources outside containment, e.g. `Parameters.parameter.resource` | Preserve the complete subtree and report unsupported identity/ownership handling |

An indexed identity or resolved reference is remapped even inside otherwise
preserved content. This precedence preserves graph targets, including a
`valueReference` in an unknown extension. Other extension contents stay unchanged.
References that originally failed to resolve, were ambiguous, or resolved
differently across duplicate occurrences are preserved with warnings. Logical
references are not resolved by guessing; their Identifier values follow the
Identifier rule. External URLs and narrative links are not automatically rewritten.
The datatype is checked again before rewriting a reference, including when
reading an older ingestion database that incorrectly indexed a URI field named
`reference`. Reference-valued objects and Identifier-valued fields named
`reference` receive their own datatype's handling.
For `Identifier.system = "urn:ietf:rfc:3986"`, replacement values remain complete
URIs: OID values use the UUID-derived `urn:oid:2.25.…` form and other values use
`urn:uuid:…`. Bare replacement strings would violate the base Identifier rule.
Local canonical links also work with older indexes: an exact contained ID is
looked up only within that root's existing ownership map. Missing targets remain
unchanged with an explicit warning.

Only exact system/code pairs listed in `fhir_cohort_synth/data/linear-units.json`
are eligible. This small registry covers mass, length, volume, pressure,
frequency, concentration and flow, including MIMIC's unit namespace. Display
text never determines support. Temperature, percentages, logarithmic, missing
and unrecognized units stay unchanged. No unit conversion is performed. Even
equivalent or repeated measurements receive independent changes, so their ratios
can change. Numeric contexts retain ancestor/component codes, medication or substance
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

Suppose three quantities independently draw **+4%, −7% and +2%**, while their
patient's date offset is **+7 days**. These illustrate the calculation; seed 42
does not necessarily draw these values.

| Existing field | Before | After |
| --- | --- | --- |
| Encounter period start | `2020-01-01T09:00:00.000+01:00` | `2020-01-08T09:00:00.000+01:00` |
| Encounter period end | `2020-01-03T09:00:00.000+01:00` | `2020-01-10T09:00:00.000+01:00` |
| Observation quantity, UCUM `mg/dL` (+4%) | `100.00` | `104.00` |
| Repeated quantity for the same patient (−7%) | `120.00` | `111.60` |
| Component quantity, UCUM `kg` (+2%) | `70.0` | `71.4` |
| Component quantity, UCUM `%` | `98` | `98` |
| Clinical coding and quantity units | Existing objects | Identical objects |

The encounter still lasts two days. Observation and component arrays keep their
positions and siblings; no new combinations are sampled. The output Patient,
Encounter and Observations receive replacement IDs and continue to reference
the same corresponding resources. Their codes and all preserved content stay
exactly as supplied.

## Outputs and validation

- `fhir/`: source-named files, preserving NDJSON/JSONL and gzip formats and
  directories relative to the source files' common parent. A deduplicated root
  appears in its first source file, with contained resources nested once.
  Single-resource JSON stays JSON; unpacked Bundle roots use `.ndjson` files.
- `perturbation-state.sqlite`: settings, source fingerprint, definition
  provenance, identity maps, patient parameters, exact changes, action counts,
  numeric frequencies and run status. It contains original source values.
- `perturbation-report.json`: coverage by root resource type and normalized
  field path, original warning counts, before/after numeric summaries and
  actual relative changes, including unchanged numeric values. It omits source
  clinical string examples; exact context coding/unit values are in the local
  database. The `export` section lists source-relative filenames, record counts
  and decompressed checksums. Filename collisions fail before creating output.

Each object, array and scalar has an action count; these counts are not the
number of resources. Numeric contexts include preserved numeric fields as well
as eligible quantities. Their summaries contain sample count, minimum, maximum
and weighted nearest-rank percentiles at 5, 25, 50, 75 and 95 percent. Exact value
frequencies live in SQLite, with decimals stored as text and ordered numerically.

The validator reads the emitted NDJSON afresh, checks replacement identities
and reference targets, reproduces each changed quantity's percentage draw, and
verifies shared patient date offsets. Undoing the recorded changes must reproduce
every source root's digest.
This verifies that codes, booleans, arrays, empty/missing fields, unknown content
and all other unrecorded fields are preserved. Adding a missing root ID is the
only permitted added field.

The flat NDJSON used for that validation is temporary. `export_files.py` then
partitions it by source occurrence, writes the final files and rereads each one
to check its decompressed SHA-256 digest. Only afterward are the export directory
and report published and the temporary stream removed. Numeric statistics still
count deduplicated roots once, independently of how many source files exist.

This proves the specified transformations and preservation properties. It does
not establish every clinical dependency, full FHIR conformance or unchanged
cohort distributions. Independent numeric changes can alter ratios, measurement
trends and aggregate statistics; before/after summaries measure the actual effects.
No categorical randomization, trajectory generation or privacy certification
is performed. Narrative, attachments and other source text remain present.

The perturbation run completes only after output validation, statistical
aggregation and report writing. Exit codes are 0 for completed runs (including warnings), 2 for
failure and 130 for interruption. Check `run.status`; failed/interrupted runs
retain local partial artifacts and require a new destination. For the one-command
workflow, check the top-level `run.json` too: it records the overall stage and
status, including ingestion failures before perturbation starts. A forcibly killed
process can retain `in_progress`. The input database is opened read-only. POSIX
permissions are `0700` for the output directory and `0600` for files; completed
databases need no WAL/SHM sidecars.

## Follow the code and inspect coverage

`fhir_types.py` resolves and walks datatypes. `perturbation_handlers.py` contains
the small transformations. `perturbation.py` prepares mappings and date bounds,
writes records, validates them and completes the run. `perturbation_report.py`
builds the human-readable summaries. `perturbation_store.py` stores audit data
and aggregates distributions. `workflow.py` coordinates ingestion and
perturbation; `cohort.py` supplies shared source checks. Processing holds one
resource tree at a time, bounded caches and bounded frequency batches; mappings and the
cohort stay in SQLite. Large runs may need substantial temporary WAL disk space.

Useful local SQLite queries:

```sql
SELECT status, phase, failure_code FROM run;

SELECT resource_type, action, sum(frequency) AS fields
FROM field_actions GROUP BY resource_type, action;

SELECT reason, sum(frequency) AS fields
FROM field_actions GROUP BY reason ORDER BY fields DESC;

SELECT patient_id, days, minimum_days, maximum_days
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
contains transformation counts, measured changes and validation results.
