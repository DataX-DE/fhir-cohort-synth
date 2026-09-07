# MIMIC demo perturbation validation — 7 September 2026

The local run completed with warnings for missing and unsupported quantity units.
It processed **928,935 resource roots, 100 patients and all 13 resource types**.
The output is **perturbed source-derived data**. No privacy assessment or full
FHIR/hospital-profile validation was performed.

## Reproduce

The source is the already-ingested local MIMIC-IV FHIR demo 2.1.0:

- Ingestion: `local-data/physionet-validation-20260907T100530Z/verified-ingestion/cohort.sqlite`
- Fields: `local-data/physionet-validation-20260907T100530Z/verified-profile/field-occurrences.sqlite`
- Output: `local-data/mimic-perturbation-20260907T153025Z/`
- Settings: strength `0.02`, date range `30`, seed `42`.

Database checks on the external drive were slow, so the completed input databases
were copied to a private temporary directory on the same computer. The engine
opened those copies read-only; its completed outputs were copied to the directory
above. The engine took **574.86 seconds** using those working copies. This timing
excludes copying and is not a benchmark for direct external-drive operation.
The original ingestion and profiling databases were not modified.
The copied outputs were checked for completion, matching file sizes and report
contents, source fingerprint and private permissions. Temporary input/output
working copies were removed after this check.

The independently calculated source fingerprint matches the output:
`d4d5a11805edaeb4b3e5282c01440c8bb471337fd4161c7849a8d41ff64cc576`.

## Verified results

The engine re-read every output root, checked recorded transformations, then
undid them and matched the original resource digest. This verified:

- **4,971,353 changed fields**, including resource IDs and other identity fields.
- **1,998,569 rewritten references**, matching the independently counted source
  graph. All these targets remained consistent under ID replacement.
- **195,174 changed quantities**, each using its patient's shared factor and
  original numeric precision.
- **1,690,101 changed date fields**, each using its patient's shared day offset.
- Unrecorded fields and nested structures, including codes and booleans, were
  unchanged. Source resource counts and ownership were retained.

The selected factors ranged from approximately **0.9806974 to 1.0194967**.
Selected date offsets ranged from **−30 to +30 days**; three patients received
zero days. The 1,766,426 eligible date occurrences included 76,325 unchanged
occurrences. Supported dates within one patient retain intervals and ordering;
this does not cover preserved dates in unsupported content or other patients.

## Coverage by resource type

Counts below are field occurrences, not distinct patients or clinical variables.
An unchanged eligible quantity can be zero or round back to its original value.
Identity/reference fields include resource IDs, Identifier values, HumanName
strings and literal references. Unsupported fields were preserved.

| Resource type | Roots | Quantities changed | Eligible quantities unchanged | Dates changed | Identity/reference fields changed | Unsupported fields |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Condition | 5,051 | 0 | 0 | 0 | 15,153 | 0 |
| Encounter | 637 | 0 | 0 | 3,220 | 4,229 | 0 |
| Location | 31 | 0 | 0 | 0 | 62 | 0 |
| Medication | 1,794 | 0 | 0 | 0 | 7,098 | 0 |
| MedicationAdministration | 56,535 | 22,556 | 19,460 | 65,896 | 203,572 | 21,949 |
| MedicationDispense | 15,375 | 0 | 0 | 1,047 | 74,711 | 120 |
| MedicationRequest | 17,552 | 4,560 | 6,543 | 45,320 | 85,433 | 3,350 |
| MedicationStatement | 2,411 | 0 | 0 | 2,396 | 7,233 | 0 |
| Observation | 813,540 | 168,058 | 210,937 | 1,555,404 | 2,640,461 | 201,285 |
| Organization | 1 | 0 | 0 | 0 | 2 | 0 |
| Patient | 100 | 0 | 0 | 128 | 400 | 0 |
| Procedure | 3,450 | 0 | 0 | 4,712 | 10,350 | 0 |
| Specimen | 12,458 | 0 | 0 | 11,978 | 37,374 | 0 |

Zero changed quantities or dates in a resource type is not evidence that its
resources were skipped. Every root received an ID mapping; the table records
which eligible fields were actually present and changed in this export.

## Numeric distribution review

The report contains **4,394 numeric contexts and 661,020 numeric occurrences**,
including preserved metadata. Contexts distinguish clinical codes, ancestor and
component codes, medication references, paths, comparators and exact units.
Before/after counts, ranges and nearest-rank quantiles are available per context;
actual clinical context strings remain in the local SQLite database.

Of **432,114 eligible quantity occurrences**, **195,174 changed (45.17%)** and
**236,940 stayed numerically unchanged**. The report also records:

- **105,114 quantity values with missing unit identifiers**, preserved.
- **121,590 quantity values with unsupported unit pairs**, preserved.
- **38,003 zero numeric baselines**, reported separately from relative changes.

The largest actual absolute relative change was **3.8461538%**. This can exceed
2% because the factor is bounded before rounding: for example, scaling integer
26 by 0.981 gives 25.506, which rounds to 26, while scaling it by 0.9807 gives
25.4982, which rounds to 25, a 3.846% change. The report measures these effects;
it does not claim that final rounded changes are always at most 2% or that every
distribution is unchanged.

All **28,511,367 JSON nodes** were accounted for: 4,971,353 changed, 23,313,310
preserved and 226,704 unsupported (also preserved). No unknown base-field or
invalid-date cases were found in this run. Extensions and narrative/attachment
content were deliberately preserved; their base-node action counts were 302,477
and 942 respectively. Hospital-specific meanings were not validated.

The original 35,338 `outside_requested_scope` informational issues were carried
into the report. The corresponding supporting resource types were retained.

## Regression and invented example

At the time of this MIMIC run, **144 tests passed**, retaining the previous 115. Tests cover Decimal
precision, unit handling, shared dates/factors, contained and forward references,
empty/unknown structures, deterministic output, matching databases, permissions,
no overwrite and failure/interruption handling.

After removing the unused configured grouping and conditional-statistics branch,
the current suite has **103 passing tests**: 44 retired-feature tests were removed
and three focused input/reference regression checks were added. A repeat run on
the same invented-example index produced byte-for-byte identical perturbed NDJSON
and report JSON and re-ingested successfully. The full MIMIC run above was not
repeated for this removal; its local output files remain unchanged.

A fresh invented-example run at
`local-data/perturbation-demo-20260907T151601Z/` processed 23 roots. Its output
re-ingested successfully with 23 resources and all 37 references resolved.
That default-seed run changed 87 fields (including 25 dates); its eligible small
numeric changes rounded back to the original values. Dedicated tests exercise
nonzero quantity changes, including repeated values and unit equivalents.

The bundled package was also imported from a ZIP archive: all 214 FHIR datatype
and resource definitions and 59 exact unit pairs were available without network
access. See [the engine guide](perturbation.md) for usage, provenance and limits.
