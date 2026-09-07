# R4 example coverage audit — 7 September 2026

The coverage run exercises **all 146 concrete FHIR R4 resource types**. Official
examples cover 141 types; five additional types use explicitly invented fixtures.
This establishes tested coverage of these examples, not complete FHIR conformance
or support for every field combination and hospital profile.

## Pipeline results

- Scanned every field in the 5,306 resources from `hl7.fhir.r4.examples#4.0.1`,
  plus the official Parameters example from the JSON ZIP. No unknown standard
  fields were reported by the bundled datatype resolver.
- Selected 273 cases before inspecting validation outcomes: all Patient,
  Observation and Bundle examples; one named `example` (otherwise the first
  archive filename) for each other type; and five invented supplemental cases.
- Ran each case in isolation because unrelated official examples reuse IDs.
  Added a supporting official Patient only when the example already named that
  exact, unique target in `subject`, `patient` or `beneficiary`.
- **264 cases completed** ingestion, field profiling, perturbation and the
  engine's independent output verification. They emitted **7,174 roots** across
  145 resource types. Bundle is counted as an input envelope, not an output root.
  These counts sum isolated cases; the same official resource can occur in
  several cases. They are not counts of distinct patients or globally unique examples.
- Verified **12,072 field changes**, including **481 dates**, **14 quantities**
  and **666 references**. Reversing the recorded changes reproduced every source
  root's digest. Settings: strength `0.02`, date range `30`, seed `7`.
- **9 Bundles were rejected** with explicit ingestion errors. The reasons were
  mismatched `fullUrl`/resource identities (3 cases), conflicting identities
  (2 cases), and request/response entries without resource payloads (4 cases).
  A rejected case does not count as a successful perturbation.

The five invented types are `SubstanceNucleicAcid`, `SubstancePolymer`,
`SubstanceProtein`, `SubstanceReferenceInformation` and `SubstanceSourceMaterial`.
They contain nested fields and identity/quantity examples, not real patient data.
Their definitions exist in R4 but neither downloaded example archive supplies
standalone examples for them. The fixtures live in `tools/check_r4_examples.py`.

## External FHIR validation

Validation uses the official HL7 Java validator **6.10.4**, pinned to FHIR
**4.0.1**, with HTTP access and the terminology server disabled. It checks base
structure, datatypes and invariants, plus declared profiles available in its
local cache. It does not verify all external terminology or hospital profiles.
Example URLs are allowed because these are public specification examples.

Every clinical/output root is selected except that `CodeSystem`, `ValueSet`,
`ConceptMap`, `StructureDefinition`, `SearchParameter` and `OperationDefinition`
are capped at one root of each type per case. This selection is deterministic
and independent of pass/fail. The large definition Bundles still run completely
through ingestion, profiling and perturbation. External validation does not
cover all 7,174 roots.

Original and perturbed roots are validated with identical settings. Results are
paired by their file annotations. Error/fatal counts are compared by diagnostic
identifier and FHIR path, preserving multiplicity. A resource with pre-existing
errors still fails this validation configuration even when perturbation introduces
no additional errors; unavailable external definitions can also affect results.
The rejected source Bundles are validated separately and have no output pair.

| Paired validation result | Count |
| --- | ---: |
| Original/output pairs checked | 2,159 |
| Original resources with no error/fatal diagnostics | 2,138 |
| Output resources with no error/fatal diagnostics | 2,138 |
| Resources with pre-existing errors, still failing afterward | 21 |
| Pairs with new error/fatal diagnostics | **0** |

All 145 emitted resource types are represented in this conformance sample;
Bundle is handled separately as an envelope. Warnings can remain on resources
with no errors. These results apply to the specified local validator setup.
The [per-type coverage JSON](validation-r4-coverage.json) records the complete
inventory, case counts, output counts and paired validation results.

Of the nine rejected source Bundles, five have no validator errors but are
unsupported cohort inputs, three have validator errors, and one triggers the
upstream validator exception described below. None produces a perturbed export.
The upstream failure is recorded as `validator_failed`, not as a passed check.

The complete regression suite passes: **116 tests**, retaining all 103 tests
from before this audit.

## Fixes and remaining limits

The audit exposed a bug in reference extraction. `Consent.provision.actor.reference`
and `ImplementationGuide.definition.resource.reference` are Reference objects,
not literal strings. `Claim.related.reference` is an Identifier, while
`Expression.reference` and `DetectedIssue.reference` are URIs. Ingestion now uses
the bundled datatypes to distinguish them. Perturbation independently checks
the datatype, so false URI edges in older ingestion databases cannot rewrite
ordinary URI fields. Regression tests cover all of these cases.

External validation then found two further transformation problems. Identifiers
using `urn:ietf:rfc:3986` require URI values, so their replacements now use valid
UUID/OID URNs. Local canonical fields such as `Questionnaire.item.answerValueSet`
and `PlanDefinition.action.definitionCanonical` must follow contained-resource
ID changes; they now resolve and rewrite those exact scoped targets, including
canonical arrays. External canonical URLs remain unchanged. Regression tests
also cover these transformations when reading older schema-1 indexes.

The comparison removes resource IDs from the validator's comments inside
diagnostic paths while retaining the resource type and array position. Otherwise
an unchanged error on a contained Medication could appear to be a new error just
because its ID was replaced.

`Parameters.parameter.resource` can embed an entire resource outside containment.
Such subtrees are now explicitly preserved, including their names and identities,
and reported as `embedded_resource_preserved`. The index does not yet manage
their own identities, ownership or internal reference scope. It must not partially
transform them while leaving those relationships unmanaged.

Datatype lookup caches now belong to individual indexes, avoiding retention of
complete indexes from many earlier runs in a long coverage process.

Other limits remain: Bundle envelopes are not re-emitted; history/request-only
Bundles are not cohort snapshots; unresolved/ambiguous links are not guessed;
unknown extensions and unsupported units are preserved; patient ownership is
required for quantity/date changes. Zero changed quantities in a type is not
evidence that its fields were skipped. Many official examples lack patients,
supported units or values that change after rounding.

## Reproduce locally

The harness is a development tool. Java and downloaded verification packages
are not new runtime dependencies for the hospital commands. No hospital data
is used or uploaded by this audit.

Download the public inputs from the [HL7 R4 downloads page](https://hl7.org/fhir/R4/downloads.html):

| Input | SHA-256 |
| --- | --- |
| [R4 examples package](https://hl7.org/fhir/R4/hl7.fhir.r4.examples.tgz) | `02aa10ea545301b1e14277123dd43a2a3a9297e6345de677801866df8afe7270` |
| [R4 JSON examples ZIP](https://hl7.org/fhir/R4/examples-json.zip) | `5b3da7fe910fcd20470d63365317bde3b230118844b8adf77e629fc55ebe2f28` |
| [Validator 6.10.4](https://github.com/hapifhir/org.hl7.fhir.core/releases/tag/6.10.4) | `1106b9d58f9e363e47bea7c4fc065841e5fc91fe9d062775c3bfdd212bd653cc` |

With those files downloaded, run:

```sh
python3 tools/check_r4_examples.py \
  --examples work/r4-validation/hl7.fhir.r4.examples-4.0.1.tgz \
  --examples-zip work/r4-validation/examples-json.zip \
  --output local-data/r4-coverage-new
```

Prepare a separate Java user-home package cache with the R4 core definitions
and the validator's public dependencies. A one-time validator run on a public
example can populate that cache; use `-tx n/a` to disable the terminology server.
This audit used these package versions:

```text
hl7.fhir.r4.core#4.0.1
hl7.fhir.xver-extensions#0.1.0
hl7.terminology.r4#6.2.0
hl7.fhir.uv.extensions.r4#5.2.0
hl7.fhir.uv.extensions.r5#5.2.0
hl7.terminology.r5#7.1.0
hl7.fhir.uv.extensions.r5#5.3.0
hl7.terminology#7.3.0
hl7.fhir.uv.extensions#5.3.0
```

Then run the entirely offline validation pass:

```sh
python3 tools/check_r4_examples.py \
  --output local-data/r4-coverage-new \
  --validator work/r4-validation/validator_cli.jar \
  --java-home work/r4-validation/java-home
```

The pipeline requires a new output directory. The validator requires a new
`hl7-validation` subdirectory and verifies that every requested file has exactly
one result; missing/partial results do not produce a completed comparison.
`coverage.json` records source checksums, selection, support resources and case
results. `hl7-validation/` contains the exact command, raw OperationOutcomes,
validator log and comparison JSON. Original and perturbed root pairs remain
available locally in `before/` and `after/`.

The reviewed run is `local-data/r4-coverage-final/`. An earlier full external
validation attempt was stopped before completion and retained separately in
`local-data/r4-coverage-fixed/hl7-validation-full-attempt/`; it supplies no conformance result.
Another attempt combined rejected inputs with the root pairs. Validator 6.10.4
threw a `NullPointerException` (`additionalResourceNames`) on the original
`Bundle-dataelements.json` and did not write its results. That attempt is kept
in `local-data/r4-coverage-fixed/hl7-validation-rejected-source-crash/` and supplies no conformance result.
The harness now checks rejected sources in separate processes, recording a
validator failure explicitly rather than treating missing results as a pass.
