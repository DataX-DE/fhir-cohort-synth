# MII profile example audit — 7 September 2026

This report records an earlier run that included the now-removed full-field
profiler. The current workflow is ingestion → perturbation → checks and reporting.
Historical counts and artifacts below are retained as validation evidence.


The pipeline processed the usable examples from all five selected MII packages.
Comparing the original and perturbed resources found **zero new error/fatal
diagnostics** with the local HL7 validator. This is not a claim that every example
passes every profile: some sources already fail, and some declared ICU profile
URLs cannot be resolved from the packages.

## Results

- Selected **all 201 JSON example files** from the five published packages below.
- **187 files completed** ingestion, recursive field profiling, perturbation and
  the engine's output verification. The Basis transaction Bundle was unpacked
  into its 21 resources, giving **207 official source/output resource pairs**.
- **14 ICU files were rejected** for resource IDs exceeding FHIR's 64-character
  limit. All 14 also have errors in external validation. They have no outputs.
- Added **7 invented supporting Patient resources** across successful cases to
  exercise patient-owned quantity/date transformations. These are reported
  separately and bring the total export count to **214 roots**.
- Verified **1,030 changed fields**, including **45 quantities** and **422 dates**,
  and checked **251 resolved reference links**. The engine's reverse-change
  verification reproduced the original resource content and structure.
- **118 regression tests pass**, including all 116 earlier tests.

The same official resources can appear in the separate Basis standalone and
Bundle cases. These are counts across cases, not globally unique patient records.

| Package | Version | Example files | Official output roots | Roots with existing errors | Roots with unchecked declared profiles | New error/fatal diagnostics |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| [Basis](https://simplifier.net/packages/de.medizininformatikinitiative.kerndatensatz.base/2026.0.0) | 2026.0.0 | 28 | 48 | 0 | 0 | 0 |
| [Laborbefund](https://simplifier.net/packages/de.medizininformatikinitiative.kerndatensatz.laborbefund/2026.0.3) | 2026.0.3 | 6 | 6 | 0 | 0 | 0 |
| [Medikation](https://simplifier.net/packages/de.medizininformatikinitiative.kerndatensatz.medikation/2026.0.1) | 2026.0.1 | 25 | 25 | 4 | 0 | 0 |
| [Consent](https://simplifier.net/packages/de.medizininformatikinitiative.kerndatensatz.consent/2026.0.0) | 2026.0.0 | 6 | 6 | 4 | 0 | 0 |
| [ICU](https://simplifier.net/packages/de.medizininformatikinitiative.kerndatensatz.icu/2026.0.2) | 2026.0.2 | 136 | 122 | 34 | 64 | 0 |

Existing errors and unchecked profiles overlap. They must not be added together.
Of the 207 official pairs, **165 have no error/fatal diagnostics** before and after.
Only **111** also declare profiles that were all resolved and checked. One
Medication-package Procedure declares no profile and receives base R4 checks.
The remaining error-free cases with unresolved declarations must not be counted
as successful MII profile checks. Terminology limitations below apply to all rows.

## Observed changes

These are actual values from the completed ICU run, with strength 0.02, date range
30 days and seed 42. The invented supporting Patient/111 receives a factor of
approximately 1.0191645 and a date offset of -10 days.

| Measurement | Original | Perturbed |
| --- | ---: | ---: |
| Weight | 70 kg | 71 kg |
| Height | 170 cm | 173 cm |
| Heart rate | 70/min | 71/min |
| Respiratory rate | 15/min | 15/min |
| Blood pressure: systolic / mean / diastolic | 120 / 90 / 80 mmHg | 122 / 92 / 82 mmHg |

The respiratory-rate adjustment rounds back to 15 at its represented precision.
Across the successful cases, 71 quantity occurrences were eligible for scaling:
45 changed and 26 rounded back to their original values. Rounding can also make
the final relative change exceed the nominal 2% factor, such as 80 becoming 82.
Supported dates retain time-of-day and timezone; an example period from
2019-12-23 to 2019-12-24 becomes 2019-12-13 to 2019-12-14 with its interval retained.
Codes, units, booleans and unsupported content remain preserved according to the
engine's existing rules. No numeric change occurred in the laboratory examples:
one eligible value rounded back; the others used missing/unsupported units or
preserved extension content. This is reported preservation, not a parsing failure.

## Source and profile findings

- Four MedicationStatement examples fail the dosage constraint requiring both
  `timing` and `doseAndRate` for structured dosage. The allowed `de.fhir.medication`
  dependency `1.0.x` resolved to 1.0.7 for this audit.
- Four Consent examples have existing category-slice, policy-code binding and/or
  display-name errors under the shipped definitions and local terminology.
- ICU examples include invalid status codes, category/code/quantity mismatches
  and slicing diagnostics that the validator cannot evaluate. These are existing
  diagnostics under this package/validator setup, not changes introduced by us.
- **64 processed ICU examples contain unresolved profile declarations.** For
  example, an instance declares a URL ending in
  `mii-pr-icu-vent-unterstuetzungsdruck-beatmung`, while the supplied definition's
  actual canonical ends in `mii-pr-icu-unterstuetzungsdruck-beatmung`. The tool
  preserves that declaration and explicitly reports that its profile was not
  checked. It does not guess a substitute or rewrite public source examples.
- The 14 overlong-ID examples include the package's pulse-oximetry examples.
  Those files have rejection results, not successful perturbation results.

These packages also exercise supporting resource types beyond Frankfurt's image,
including Medication, MedicationStatement, DiagnosticReport and DeviceMetric.
The image's non-MII Location resource has no example in this package selection;
Location was exercised in the separate base R4 audit.

## How the test was run

All five package manifests specify FHIR **4.0.1**. Every
`package/examples/*.json` file is selected, independently of validation results.
Standalone examples are grouped within their module to retain available links.
The Basis Bundle is tested separately to avoid mixing its copies with standalone
examples. Malformed root IDs are isolated into an explicit rejected-source case
so they cannot prevent testing the other unchanged ICU files.

The hospital pipeline uses its normal Python/SQLite APIs. Missing exact relative
Patient reference targets receive minimal invented Patient fixtures marked with
`urn:fhir-cohort-synth:test` / `invented-support` in `meta.tag`. No official example
field is changed before the pipeline. Other missing or ambiguous targets remain
unresolved and are not invented. Counts for supporting fixtures are kept separate.

The official HL7 Java validator **6.10.4** loads each module's pinned package and
its dependencies from an isolated local cache. It checks all emitted original and
output roots, including fixtures, with identical settings. Results must cover
exactly the requested files. Comparison preserves diagnostic identity, path and
multiplicity, excluding replaced resource IDs in diagnostic path comments.
Rejected sources receive independent original-only validation.

HTTP access, default reference fetching and the external terminology server are
disabled during validation. Locally available terminology is checked; external
SNOMED CT, historical ICD/OPS and other unavailable definitions remain limitations.
The validator's attempts to discover current common-package versions are blocked
by its offline policy and it uses the recorded local cache. A transitive
subscription-backport dependency names core 4.0.0; the validation engine is
explicitly fixed to R4 4.0.1. No fake 4.0.0 cache entry is supplied.

The current hospital runtime gained no dependencies or transformation changes.
This audit adds the development harness and evidence, including explicit detection
of unchecked profiles. It does not establish Frankfurt's exact deployed package
versions, complete clinical validity or full terminology conformance.

## Reproduce and inspect

The completed run is `local-data/mii-coverage-final/`. Its six successful case
directories contain the original inputs, ingestion and field databases, perturbed
exports, transformation reports and individual before/after JSON resources.
`icu-invalid-ids/` retains the rejected inputs and ingestion report.
`hl7-validation/` contains commands, logs, raw OperationOutcomes and comparison JSON.

Download these public packages from `https://packages.simplifier.net/<name>/<version>`
into `work/mii-validation/`, named `base-2026.0.0.tgz`,
`laborbefund-2026.0.3.tgz`, `medikation-2026.0.1.tgz`, `consent-2026.0.0.tgz` and
`icu-2026.0.2.tgz`. Full names start with
`de.medizininformatikinitiative.kerndatensatz.` followed by the module name.

Prepare an isolated Java user-home cache containing their dependencies and the
validator's common packages. Use the exact resolved versions and archive checksums
in [the coverage evidence](validation-mii-coverage.json). Wildcard dependencies
are resolved once to published releases and the selected versions recorded.

```sh
python3 tools/check_mii_examples.py \
  --packages work/mii-validation \
  --output local-data/mii-coverage-new

python3 tools/check_mii_examples.py \
  --output local-data/mii-coverage-new \
  --validator work/r4-validation/validator_cli.jar \
  --java-home work/mii-validation/java-home

python3 -m unittest discover -s tests -q
```

The pipeline requires a fresh output directory and validation a fresh
`hl7-validation/` subdirectory. `completed` means the audit finished; consult the
separate rejection, error and unchecked-profile counts for conformance results.
The initial run, `local-data/mii-coverage-initial/`, is retained: its combined ICU
input was rejected because of the 14 malformed IDs. The final run isolates those
files. No source data was repaired to obtain a better result.
