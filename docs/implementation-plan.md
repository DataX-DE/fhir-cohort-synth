# Implementation status: perturb existing FHIR records

The output is **perturbed source-derived data** for local use. The earlier
independent cohort sampler has been removed. No privacy, anonymization or
differential-privacy guarantee is made; privacy assessment remains outside scope.

## Implemented

1. Ingest local FHIR JSON/NDJSON exports, preserve payloads, resolve references
   and determine patient membership.
2. Run `perturb` using bundled FHIR R4 datatype definitions, independent 1–10%
   increases/decreases per eligible quantity, a shared date offset per patient,
   and consistent identity replacement.
3. Validate the written resource population, preserved content, resolved graph
   and use of field-specific numeric draws and patient date offsets. Report numeric changes after
   rounding, unchanged cases and unsupported fields.
4. Run ingestion and perturbation together with `run --input ... --output ...`.
   The hospital does not need to manage stage inputs.
5. Publish source-named export files with their NDJSON/JSONL and gzip formats,
   source ordering and per-file checksums, rather than one merged export.

See [the perturbation guide](perturbation.md) for the API, command, exact field
handling and worked example. The implementation uses Python's standard library
and SQLite, with no runtime downloads or model training.

The full-field profiler, configured relationship-grouping API and conditional
statistics command have been removed. Perturbation uses the ingestion reference graph directly;
its automatic measurement contexts still separate before/after numeric reports.
Ingestion schema 2 also removes the profile, extension, measurement and version
inventories, hospital-scope classification and archived Bundle metadata.
Reference contexts and source resource JSON remain. Perturbation accepts both
schema 1 and schema 2 ingestion indexes. Perturbation state/report schema 2 removes
the patient-level numeric factor and verifies independent field draws. Earlier
outputs remain records of their original runs; no migration is performed.

## Deliberate first-version limits

- Only quantities with exact supported unit-system/code pairs are scaled.
  Temperatures, percentages, logarithmic units, Age, Duration, Count and
  standalone numeric metadata are preserved.
- Codes, booleans, narrative, attachments and unknown content remain unchanged.
  Shared or unassigned resources retain quantities and dates.
- Full supported dates for one patient share an offset; partial, invalid and
  unsupported dates remain unchanged. Intervals involving unchanged dates or
  different patients are outside this guarantee.
- Independent quantity changes can alter ratios, series shape and cohort
  distributions. The report measures the resulting numeric changes.
- No categorical randomization, trajectory sampling, full FHIR validation or
  hospital-specific profile validation is included in the runtime commands.

Separate development audits now compare original and perturbed public examples
with the external HL7 validator: [R4 coverage](validation-r4.md) and
[MII coverage](validation-mii.md). Their reports distinguish pipeline completion,
pre-existing errors, new errors and profiles that could not be checked.

## Subsequent work

Use the coverage report with the hospital to identify additional units and
profile-specific needs. Confirm and pin their deployed profile packages before
integrating offline conformance validation into their workflow. Bundle a Python runtime and launcher after the intended
hospital workflow is tested. Broader statistical fidelity and privacy assessment
are separate work; local execution alone does not anonymize records.
