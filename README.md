# fhir-cohort-synth

An offline tool for ingesting, statistically profiling and perturbing local FHIR exports.
**Ingestion, complete recursive JSON extraction and exact field
distributions are implemented, along with the `perturb` command.** Perturbation
changes supported quantities, full dates and identity fields while retaining
the source resource graph. The earlier independent cohort sampler has been
retired. There is no current `generate` command.

The perturbation output is **perturbed source-derived data**, for local hospital use.
The current scope covers structural and statistical fidelity; it makes no
privacy, anonymization or differential-privacy guarantee. Local execution
describes where processing occurs, not whether the data are anonymous.
Privacy assessment is outside the current implementation scope. No model
training is required.

The workflow is **ingest → profile → perturb → validate and report**.
Ingestion resolves resource links and patient ownership. Profiling describes
the original JSON fields. Perturbation reads the original resource trees and
applies patient-specific changes using FHIR datatypes and supported units.

## Run ingestion

Requires **Python 3.11 or newer**, with its standard-library SQLite module.
There are no third-party runtime dependencies and no installation step.
Run from the repository directory:

```sh
python3 fhir_synth.py ingest --input examples/mii-demo-bundle.json --output local-data/demo
```

For a hospital export, use a new output directory outside the input directory:

```sh
python3 fhir_synth.py ingest --input /path/to/fhir-export --output /path/to/ingested-cohort
```

On Windows, use `py -3` in place of `python3`. A bundled runtime and hospital
launcher are planned; this initial version requires Python to be installed.
`python3 -m fhir_cohort_synth` is an equivalent entry point.

The example is entirely invented. It covers the resource shapes in the supplied
hospital list, but is **not a validated MII example or a statistically generated
cohort**. It declares a few known MII canonical URLs and otherwise base FHIR
profiles; exact hospital profile versions have not been supplied.

## Extract and profile every JSON field

After ingestion, run:

```sh
python3 fhir_synth.py profile --input local-data/demo/cohort.sqlite --output local-data/profile
```

The command reads a completed ingestion index without changing it. It walks
every nested object, array and value, including unfamiliar fields, while
preserving parent links, array positions, value types and decimal precision.
It requires a new output directory and uses the same standard-library runtime.

The invented example produces **23 resource roots, 425 nodes and 165 statistical
field paths**. The profiler groups by root resource type and normalized path,
such as `$["component"][*]["valueQuantity"]["value"]`. Individual array positions
and concrete paths remain available in the database.

| Output | Contents |
| --- | --- |
| `field-occurrences.sqlite` | Every extracted node, source provenance and complete exact frequency tables |
| `field-inventory.json` | Field/type inventory, parent-based presence, root-resource presence and container summaries |
| `field-statistics.json` | The same context plus numeric quantiles, string cardinality, boolean counts and pointers to complete distributions |

Frequency tables cover typed scalar values, object key sets, array lengths,
array element types and type sequences, and string lengths. Numeric summaries
use weighted nearest-rank percentiles at 5, 25, 50, 75 and 95 percent. Full value
lists stay in SQLite; the JSON reports are summaries, not truncated substitutes
for the stored distributions.

Missing-field rates use existing parent objects: a missing `valueQuantity`
does not also count as a missing `unit` within an existing quantity. Array-item
counts and the number of resources containing a field are reported separately.
Null, empty string, empty object and empty array remain distinct.

Exact duplicate resource payloads count once. Contained resource JSON is visited
under its original parent path; separately indexed contained rows are excluded
from traversal. Bundle envelope/request metadata remains in the ingestion
database and is outside this resource-root population.

These are **generic JSON distributions**, without clinical grouping or value
dependency modeling. For example, a path shared by several measurement types
gets a marginal distribution across those types. The perturbation report
separately compares measurements within their code, medication and unit contexts.
Dates remain strings, numeric-looking strings are not converted, and units
are not normalized. Parent/resource associations retain the required context.

All profiling artifacts remain local source-derived data. Exact frequencies
include source string values in SQLite. Console output and JSON reports omit
scalar string examples, but paths, counts and numeric summaries can still be
sensitive. This is not anonymization or synthetic generation.

Profiling accepts `completed` and `completed_with_warnings` ingestion runs,
carries their issue context, and rejects incomplete or unsupported indexes.
Exit codes are `0` for completed profiling (including source warnings), `2`
for failure, and `130` for an interrupted command. Success requires both JSON
reports and a completed `run.status` in the output database. Failed/interrupted
runs may retain local partial data; retry in a new directory.

See [profiling data and queries](docs/profiling-schema.md) for the node layout,
exact-frequency queries, denominator definitions and an example walkthrough.

## Perturb existing records

After ingestion and field profiling, run:

```sh
python3 fhir_synth.py perturb --input local-data/demo/cohort.sqlite --fields local-data/profile/field-occurrences.sqlite --output local-data/perturbed --strength 0.02 --date-shift-days 30 --seed 42
```

Each patient receives one factor between **0.98 and 1.02** for supported linear
quantities and one date offset within **−30 to +30 days**. The engine uses bundled
FHIR R4 4.0.1 datatype definitions, Decimal arithmetic and a small exact unit
registry that includes MIMIC aliases. It replaces resource IDs, resolved
references, Identifier values and HumanName strings. Clinical codes, booleans,
narrative, attachments, unknown fields and unsupported units remain unchanged.
Shared or unassigned resources retain their quantities and dates.

The new directory contains `perturbed.ndjson`, `perturbation-state.sqlite` and
`perturbation-report.json`. The report measures actual changes after rounding;
small changes can round back to the original value. Exact local mappings and
numeric distributions remain in SQLite. All files are source-derived, including
text deliberately preserved in the NDJSON and original values in the audit trail.

Validation verifies preserved content, structure, resolved reference targets
and use of the shared patient parameters. It does not establish full FHIR or
hospital-profile conformance. See the [worked example and implementation guide](docs/perturbation.md)
for handling rules, limitations, API usage and report queries.
The [MIMIC perturbation review](docs/validation-mimic-perturbation.md) records
coverage across 928,935 resources and 13 resource types, including rounding effects.
The [R4 example coverage audit](docs/validation-r4.md) records checks across all
146 concrete R4 resource types, with external validator results and explicit gaps.
The [MII 2026 profile audit](docs/validation-mii.md) tests the five packages relevant
to Frankfurt's list, distinguishing introduced errors, existing failures and
unresolved profile declarations.
Inline resources outside containment (for example `Parameters.parameter.resource`)
are preserved and reported; their identities and patient ownership are not managed.

## Supported input

- FHIR JSON resources, including Bundles containing resources.
- NDJSON / JSONL files, with one resource or Bundle per nonblank line.
- Gzip variants: `.json.gz`, `.ndjson.gz`, `.jsonl.gz`.
- Multiple input paths after `--input`, or recursive input directories.
- UTF-8, with or without a byte-order mark.

Only these file extensions are read. Symlinks are rejected. JSON arrays, XML,
FHIR server downloads and bulk-export manifest downloads are not implemented.
Export the referenced resource files locally first. No URLs in the input are
fetched; search pagination links are reported for review.

NDJSON is read incrementally, with ingestion checkpoints every 10,000 lines.
Profiling checkpoints every 1,000 resource roots. Both writers use a bounded
64 MiB SQLite page cache and temporary write-ahead logs to reduce disk churn;
this is not a total process memory limit. Completed databases are checkpointed
back to ordinary journal mode and require no WAL/SHM sidecars. Allow additional
disk space for the growing log during a run; checkpoints separate bulk phases.
Each JSON file, including an individual Bundle, is parsed in memory. Resource
payloads are stored in SQLite; patient grouping also holds resource IDs and
relationship edges in memory. Prefer NDJSON for
large exports. The [MIMIC demo validation](docs/validation-mimic-demo.md) covers
928,935 resources and 28.5 million JSON nodes. Larger hospital exports have not
yet been benchmarked.

## Scope from the hospital's profile list

| Module | Resources and handling |
| --- | --- |
| Person | `Patient`; vital status retained as `Observation` |
| Fall | `Encounter`, including `partOf` hierarchy and location references |
| Diagnosis | `Condition`; primary/secondary roles preserved in source fields |
| Laboratory | Quantitative and qualitative `Observation`, including components, coding and original units |
| Procedure | `Procedure` |
| Medication | `MedicationAdministration`, with supporting `Medication` resources |
| Consent | `Consent` |
| ICU | `Observation` for vital signs; `Procedure` for ventilation and extracorporeal therapies |
| Local location | `Location` referenced by encounters |

Supporting and unrecognized resource types are also retained and counted.
An unrecognized profile is retained, rather than silently replaced. Exact
`meta.profile` canonical URLs and their optional `|version` suffixes are indexed
separately. Extensions and modifier extensions remain in the payload and have
a URL inventory. Ingestion itself transforms no codes, units, dates or patient identifiers.

## Output

Each ingestion run creates a **new** directory containing:

| File | Contents |
| --- | --- |
| `cohort.sqlite` | Source resources, provenance, Bundle metadata, profile inventory, reference graph, patient membership and detailed issues |
| `report.json` | Machine-readable counts, profile/version inventory, observation code/unit inventory and issue totals |
| `report.txt` | A short readable status report |

**The SQLite database contains the original patient data. It is not synthetic
or anonymized.** Keep the entire output on the hospital's approved local
storage. Console messages and reports omit patient names, IDs, clinical values,
literal references and source file paths, but aggregate counts and profile,
extension, code and unit strings are not privacy-protected. Keep reports local
too until reviewed. POSIX output permissions are directory `0700`, files `0600`;
Windows relies on the destination's access controls.

Source files are read only. Existing output directories are never overwritten.
A missing report, or `run.status = 'in_progress'` in SQLite, indicates an
interrupted run. Use a new output directory to retry.

## Reference resolution and patient grouping

References are resolved after every file has been read, so forward and
cross-file references work. Supported forms include relative `Patient/id`,
absolute REST URLs, exact `urn:uuid:…` full URLs, version-specific REST
references and contained `#id` references. Local canonical `#id` links, such as
Questionnaire answer-value-set links, also follow their contained targets.
Contained references stay within
their containing resource. A reference using an identifier without a literal
reference is reported as unresolved; identifiers are not used to guess links.

An absolute source `fullUrl` determines its server namespace. A same-ID patient
from another server is never used as its fallback. For files without server
identity, the importer first checks the enclosing Bundle/file, then accepts a
globally unique relative match. It flags multiple candidates as ambiguous.
Supply exports from one coherent identity namespace per run when full URLs
are absent: the tool cannot recover missing server identities.

If an export from **one known server** contains absolute references but omits
entry full URLs, supply that server's base explicitly:

```sh
python3 fhir_synth.py ingest --input /path/to/export --output /path/to/new-index \
  --base-url https://example.invalid/fhir
```

This supplies identities for non-contained resources lacking `fullUrl` and
makes no network connection. Existing full URLs take precedence.

Patient membership follows explicit patient subjects, encounter context and
the `Encounter.partOf` hierarchy. Contained resources inherit the enclosing
patient context. Disagreeing patient links produce an error and no membership
for the conflicted resource. Shared locations, medications and other linked
support resources are retained in the graph without assigning them to every
patient that references them. Patient links such as `Patient.link` are retained
but do not merge identities.

Exact duplicates with the same identity and canonical JSON payload share one
resource row, with every occurrence preserved. Different payloads at the same
identity produce an error and both are retained for inspection. This includes
multiple versions: ingestion expects a snapshot, not a history reconciliation
job. It does not select a latest version or merge conflicting records.

## Status and limits

The working target is **FHIR R4, 4.0.1**. The supplied profile list does not
confirm the hospital's exact FHIR or module releases. Ordinary FHIR resources
do not necessarily declare a FHIR version. Explicit incompatible declarations
in `CapabilityStatement`, `StructureDefinition` or `ImplementationGuide` are
flagged. A profile package version is not treated as a FHIR version.

These are ingestion checks, **not full FHIR or MII profile validation**. The
importer checks selected structural problems, malformed JSON, identity
conflicts, references, patient-context conflicts and encounter cycles. It does
not yet validate all datatypes, required fields, terminology bindings,
invariants, clinical plausibility or chronological order. Full validation needs
the hospital's exact profile packages, pinned and bundled for offline use.

- `completed`: ingestion checks passed.
- `completed_with_warnings`: inspect missing profiles, unresolved references,
  unassigned records and other warnings before extracting statistics.
- `incomplete`: errors occurred; successfully parsed resources are retained for
  local diagnosis, but the index is not ready for statistical extraction.

Exit code is `0` for completion (including warnings), `2` for errors or invalid
arguments, and `130` for an interrupted command. Add `--strict` to also return
`2` for warnings. A zero exit code is
not a privacy guarantee, proof of export completeness or profile conformance.
History Bundles and entries without resources are reported as unsupported
snapshot inputs. Resource-free entries and Bundle/entry metadata are retained
in the database for inspection.

Generic profiling provides exact field distributions from completed indexes.
Perturbation applies coordinated transformations using FHIR datatype definitions
and the source resource graph. See the [implementation status](docs/implementation-plan.md).
Separate development tools run the external HL7 validator on public
[R4 examples](docs/validation-r4.md) and [MII examples](docs/validation-mii.md).
These audits are not part of the hospital CLI; validating a hospital export
requires its exact deployed profile packages.

## Development

Start with the [code walkthrough](docs/code-walkthrough.md) for one Observation's
journey from input JSON to output, the key terms and the reading order.

To follow the code, start with `ingest()` in
[`ingest.py`](fhir_cohort_synth/ingest.py). Its numbered comments describe the
whole run. Then read the matching phases in [`store.py`](fhir_cohort_synth/store.py):
`add_document()` → `resolve()` → `group_patients()` → `report()`.
The module and method docstrings explain the database IDs, reference scopes
and patient-grouping rules. [`cli.py`](fhir_cohort_synth/cli.py) handles arguments
and exit codes; `jsonio.py` and `profiles.py` contain the smaller helpers.

For profiling, start at `profile_index()` in
[`profiling.py`](fhir_cohort_synth/profiling.py). It calls the generic walker in
[`json_fields.py`](fhir_cohort_synth/json_fields.py), stores the nodes through
[`field_store.py`](fhir_cohort_synth/field_store.py), then computes distributions
and report fragments in [`field_statistics.py`](fhir_cohort_synth/field_statistics.py).

For perturbation, start at `perturb()` in
[`perturbation.py`](fhir_cohort_synth/perturbation.py). Datatype traversal lives in
`fhir_types.py`, scalar transformations in `perturbation_handlers.py`, and the
SQLite audit trail and distribution summaries in `perturbation_store.py`.

```sh
python3 -m unittest discover -s tests -v
```

Tests use only invented resources and temporary directories. See
[the local index schema](docs/ingestion-schema.md) for queries and semantics.

FHIR reference semantics:
[HL7 R4 references](https://hl7.org/fhir/R4/references.html) and
[Bundle reference resolution](https://hl7.org/fhir/R4/bundle.html#references).
Hospital profile packages must be selected from the
[MII Simplifier organization](https://simplifier.net/organization/koordinationsstellemii).
