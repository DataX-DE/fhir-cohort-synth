# fhir-cohort-synth

Create a **perturbed source-derived FHIR export** locally. The tool retains
existing resources and relationships while changing supported quantities,
full dates and identity fields. No model training or runtime downloads are needed.
It does not provide a privacy or anonymization guarantee.

## Run an export

Requires **Python 3.11+**, using only the standard library and SQLite.
From the repository directory:

```sh
python3 fhir_synth.py run --input /path/to/fhir-export --output /path/to/new-output
```

Try the entirely invented example:

```sh
python3 fhir_synth.py run --input examples/mii-demo-bundle.json --output local-data/demo
```

On Windows, use `py -3` instead of `python3`. No installation step is needed if
Python is already installed; a bundled runtime and launcher remain future work.
The output directory must be new and outside any input directory.

The command **indexes resources and patient links → perturbs → checks and reports**.

```text
new-output/
  run.json                         # Overall status and phase
  index/
    cohort.sqlite                  # Original resources and resolved links
    report.json
    report.txt
  perturbed/
    fhir/                          # Files named and grouped like the input
      MimicPatient.ndjson.gz       # Example input filenames
      MimicObservationED.ndjson.gz
      ...
    perturbation-state.sqlite      # Identity maps, parameters and recorded edits
    perturbation-report.json       # Coverage and actual before/after changes
```

`run.json` is complete only after both stages finish, including output checks
and reporting. Warnings are carried into the reports. An ingestion error stops
the workflow before perturbation; a failure or interruption requires a new
output directory. All outputs, including the original-data index, stay local.

The export preserves NDJSON/JSONL filenames, gzip compression and directories
relative to the source files' common parent. Records remain in source order.
For the MIMIC demo, the 30 input `.ndjson.gz` files produce 30 matching files
under `perturbed/fhir/`, including separate Observation exports.
Single-resource JSON files remain JSON. Bundle inputs are unpacked by ingestion
and use one `.ndjson` file per source Bundle; their envelopes are not rebuilt.
Deduplicated roots stay in their first source file. The report's `export` section
lists every output file, record count and checksum of its decompressed content.

Defaults are **±2% quantity scaling, ±30 days and seed 42**. To change them:

```sh
python3 fhir_synth.py run --input /path/to/fhir-export --output /path/to/new-output \
  --strength 0.02 --date-shift-days 30 --seed 42
```

Each patient shares one factor and one date offset. The same indexed snapshot,
settings and seed produce the same records. Decimal rounding can leave small
changes unchanged. IDs and resolved references are replaced consistently;
clinical codes, booleans, narratives, attachments and unsupported fields remain
unchanged. Shared or unassigned resources retain their quantities and dates.
See [field handling and examples](docs/perturbation.md) for the exact rules.

## Run individual steps

The stage commands remain available when an index already exists:

```sh
python3 fhir_synth.py ingest --input examples/mii-demo-bundle.json --output local-data/index
python3 fhir_synth.py perturb --input local-data/index/cohort.sqlite --output local-data/perturbed
```

The API is `perturb(cohort_db, output_dir, *, strength=0.02, date_shift_days=30, seed=42)`.
The earlier `field_db` argument and CLI `--fields` option have been removed.
There is no cross-database matching step because perturbation now has one input.
The ingestion database still must be completed, supported and readable.

The perturbation report includes measurement-context before/after statistics
and transformation coverage. The full-field `profile` command and its separate
extraction database have been removed.
No independent cohort sampler or `generate` command is present.

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
The SQLite writers use a bounded
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
Profile declarations, including their `|version` suffixes, extensions, codes and
units remain in the complete JSON payload. Ingestion creates no separate
inventories for them and requires no recognized profile. It transforms no codes,
units, dates or patient identifiers.

## Output

Each ingestion run creates a **new** directory containing:

| File | Contents |
| --- | --- |
| `cohort.sqlite` | Source resource JSON, identities, source locations, reference graph, patient membership and processing issues |
| `report.json` | Resource and patient counts, reference status and issue totals |
| `report.txt` | A short readable status report |

**The SQLite database contains the original patient data. It is not synthetic
or anonymized.** Keep the entire output on the hospital's approved local
storage. Ingestion reports omit patient names, IDs, clinical values, literal
references and source paths. Aggregate counts are still source-derived; keep
reports local until reviewed. POSIX output permissions are directory `0700`, files `0600`;
Windows relies on the destination's access controls.

Source files are read only. Existing output directories are never overwritten.
A missing report, or `run.status = 'in_progress'` in SQLite, indicates an
interrupted run. Use a new output directory to retry.

New ingestion indexes use schema version 2, with eight tables for resource
storage, lookup and processing checks. Schema version 1 indexes remain readable
by perturbation without modification. The perturbation state database retains
identity mappings, patient parameters, recorded edits and before/after statistics.

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
python3 fhir_synth.py run --input /path/to/export --output /path/to/new-output \
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
- `completed_with_warnings`: inspect unresolved references, unassigned records
  and other warnings before perturbation.
- `incomplete`: errors occurred; successfully parsed resources are retained for
  local diagnosis, but the index is not ready for perturbation.

Exit code is `0` for completion (including warnings), `2` for errors or invalid
arguments, and `130` for an interrupted command. Add `--strict` to also return
`2` for warnings. A zero exit code is
not a privacy guarantee, proof of export completeness or profile conformance.
History Bundles and entries without resources are reported as unsupported
snapshot inputs. Bundle envelopes and request/response/search metadata remain
in the original export. The index retains entry resource payloads, full URLs and
lookup contexts needed to resolve references; it does not archive the envelope.

Perturbation applies coordinated transformations using FHIR datatype definitions
and the source resource graph. See the [implementation status](docs/implementation-plan.md).
Separate development tools run the external HL7 validator on public
[R4 examples](docs/validation-r4.md) and [MII examples](docs/validation-mii.md).
These audits are not part of the hospital CLI; validating a hospital export
requires its exact deployed profile packages.

## Development

Start with the [code walkthrough](docs/code-walkthrough.md) for one Observation's
journey from input JSON to output, the key terms and the reading order.

Start with `run_export()` in [`workflow.py`](fhir_cohort_synth/workflow.py) for the
hospital command. Then follow `ingest()` in
[`ingest.py`](fhir_cohort_synth/ingest.py). Its numbered comments describe the
whole run. [`store.py`](fhir_cohort_synth/store.py) connects four explicit phases:
`add_document()` → `resolve()` → `group_patients()` → `report()`.
Follow only the part you want to review:

| File | Responsibility |
| --- | --- |
| [`resource_store.py`](fhir_cohort_synth/resource_store.py) | Unpack Bundles, save resource payloads and references, check structure |
| [`references.py`](fhir_cohort_synth/references.py) | Match stored reference text to a unique target |
| [`patient_groups.py`](fhir_cohort_synth/patient_groups.py) | Assign patient ownership through resolved links |
| [`schema.py`](fhir_cohort_synth/schema.py) | Define the ingestion database tables and indexes |
| [`store.py`](fhir_cohort_synth/store.py) | Own the connection, call these phases and summarize the index |

These modules share one connection; ingestion controls commits and closing.
Their comments explain database IDs, reference scopes and patient grouping.
[`cli.py`](fhir_cohort_synth/cli.py) handles arguments
and exit codes; `jsonio.py` preserves decimal precision when reading and writing JSON.

For perturbation, start at `perturb()` in
[`perturbation.py`](fhir_cohort_synth/perturbation.py). Datatype traversal lives in
`fhir_types.py`, scalar transformations in `perturbation_handlers.py`, and the
SQLite audit trail and distribution summaries in `perturbation_store.py`.
`export_files.py` restores source file boundaries and verifies compressed output.
`perturbation_report.py` builds the report; `cohort.py` owns shared read-only
source checks.

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
