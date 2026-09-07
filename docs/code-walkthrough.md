# Reading the code from export to perturbed export

The pipeline keeps the source resources and changes selected fields. It does
not generate new patient histories or draw records from field distributions.
Start with `workflow.py: run_export()`: it calls ingestion and perturbation,
stores them in `index/` and `perturbed/`, and records overall status in `run.json`.
Its helpers handle each phase.

| Stage | Start here | What it produces |
| --- | --- | --- |
| Ingest | `ingest.py: ingest()` | `cohort.sqlite`: complete source payloads, identities, references and patient membership |
| Perturb | `perturbation.py: perturb()` | Source-named files in `fhir/`, a local state database and a coverage report |

These modules live under `fhir_cohort_synth/`. `fhir_synth.py` is the launcher;
`cli.py` parses options, calls the public functions and formats safe console errors.

For ingestion details, `Store` is the small entry point. Its `add_document()`
calls `ResourceStore` in `resource_store.py`; `resolve()` calls
`references.resolve_references()`; `group_patients()` calls
`patient_groups.group_patients()`. Each phase uses the same database connection
and issue recorder. `schema.py` contains table definitions; `Store.report()`
contains the summary queries. There is no inheritance between these modules.
The index stores resource JSON, identities, reference scopes and patient
membership. Profile, extension and measurement inventories have been removed;
those fields remain in the JSON. The perturbation ledger still stores mappings,
the secret run key, patient parameters, edits and before/after measurement statistics.

## One Observation through the pipeline

Consider this invented resource together with its referenced `Patient/p1`:

```json
{
  "resourceType": "Observation",
  "id": "weight-1",
  "status": "final",
  "subject": {"reference": "Patient/p1"},
  "code": {"coding": [{"system": "http://loinc.org", "code": "29463-7"}]},
  "effectiveDateTime": "2020-01-01T09:00:00.000+01:00",
  "valueQuantity": {
    "value": 70.00,
    "system": "http://unitsofmeasure.org",
    "code": "kg",
    "unit": "kg"
  }
}
```

1. **Parse and store.** `jsonio.loads()` reads `70.00` as `Decimal`, preserving
   its numeric precision. `Store.add_document()` saves the complete parsed
   payload. The same identity and payload share one resource row; each duplicate
   appearance still gets an occurrence row for provenance. Input bytes and
   whitespace are not retained as a byte-for-byte copy.
2. **Connect the patient.** After all files are loaded, `Store.resolve()` finds
   the Patient target. `Store.group_patients()` assigns this Observation to it.
   This separate pass allows the Patient to appear later in the export.
3. **Prepare changes.** Generate a fresh secret key, or read a compatible completed
   run with `--reuse-key-from`. Commit that key to local state.
   `_prepare_identities()` allocates replacement IDs for
   every target. `_prepare_date_offsets()` checks all
   supported dates before choosing a shared offset that fits calendar bounds.
   This uses the ingestion database directly.
4. **Edit supported slots.** `TypeIndex.walk()` resolves `valueQuantity` to
   Quantity and its `value` to decimal. `Handlers.apply()` checks the exact unit
   system/code and patient ownership, then draws a separate signed percentage
   for this quantity using the secret key, root identity and concrete field path. Repeated
   values and components draw independently. With an illustrative factor `1.01` and
   offset `+7`, the value becomes `70.70` and the date becomes
   `2020-01-08T09:00:00.000+01:00`. These are illustrative parameters, not a
   prediction of the keyed draw. `_rewrite_reference()` points the subject at
   the prepared replacement Patient ID. Codes and units remain unchanged.
5. **Check what was written.** `_validate()` rereads the emitted JSON, checks
   identities, links and transformations using the stored key, then undoes each edit in
   memory. The reconstructed payload must have the original digest. The local
   SQLite ledger records numeric changes, including changes lost to rounding.
   `perturbation_report.build_report()` assembles coverage and validation counts;
   numeric distributions and percentage changes are excluded from the JSON report.
   `export_files.py` writes the checked records into their source files, retaining
   NDJSON/JSONL and gzip formats. Decompressed checksums verify that packaging
   preserves the validated bytes; the temporary flat file is then removed.

## Terms used in the implementation

| Term | Meaning |
| --- | --- |
| Resource row ID | An integer SQLite key; different from the FHIR `id` string |
| Occurrence | One appearance of a deduplicated resource in an input file or Bundle |
| Root | A non-contained resource; its `contained` subtree stays nested in output |
| Owner | The root or contained resource holding a field; its `patient_id` may be absent |
| Concrete path | One exact location, e.g. `component[0].valueQuantity.value` |
| Statistical path | The same location with array positions replaced by `[*]` for counting |
| Measurement context | Ancestor/component coding and unit information used to separate local SQLite numeric summaries |
| Ledger | The state database containing mappings, parameters, edits and action counts |

Paths are tuples of typed segments, not parsed dot strings. For example,
`(("key", "component"), ("index", 0), ("key", "valueQuantity"))` locates one
component's quantity. Its statistical path substitutes `("item", None)` for
`("index", 0)`. Literal field names containing dots or brackets stay unambiguous.

`TypeIndex.walk()` visits source JSON slots with their FHIR datatypes so the
handlers can decide which transformations are supported. It preserves unfamiliar
content, empty containers and explicit nulls. Missing fields are not invented;
array elements keep their original positions and sibling relationships.

## Completion, validation and tests

Perturbation opens the ingestion index read-only through
`cohort.open_source()` and requires a fresh output directory. Intermediate database
commits retain progress; they do not signal success. Perturbation records completion only after writing, checking
and reporting finish. The wrapper records overall completion only afterward.
An interrupted or failed destination is not reusable.

The built-in output check establishes recorded transformations and preservation.
The development tools `tools/check_r4_examples.py` and `tools/check_mii_examples.py`
separately invoke the external HL7 validator against public examples. Their
completed status means the audit finished; inspect per-case errors and unchecked
profiles for conformance results.

For an executable end-to-end example, start with `tests/test_workflow.py`.
Then read `tests/test_ingestion.py` and `tests/test_perturbation.py`. They cover
duplicates, contained
links, nested arrays, Decimal precision, shared offsets, preservation and failures.
Run them from the repository root with:

```sh
python3 -m unittest discover -s tests -v
```

The detailed table descriptions are in `docs/ingestion-schema.md` and
`docs/perturbation.md`.
