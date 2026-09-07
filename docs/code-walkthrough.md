# Reading the code from export to perturbed export

The pipeline keeps the source resources and changes selected fields. It does
not generate new patient histories or draw records from the field distributions.
Start with the three public functions below; their helpers handle each phase.

| Stage | Start here | What it produces |
| --- | --- | --- |
| Ingest | `ingest.py: ingest()` | `cohort.sqlite`: complete source payloads, identities, references and patient membership |
| Profile | `profiling.py: profile_index()` | `field-occurrences.sqlite` and two JSON reports: every nested node and exact marginal distributions |
| Perturb | `perturbation.py: perturb()` | `perturbed.ndjson`, a state database and a report of actual changes |

These modules live under `fhir_cohort_synth/`. `fhir_synth.py` is the launcher;
`cli.py` parses options, calls the public functions and formats safe console errors.

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
3. **Extract every field.** `json_fields.walk_json()` visits the root, nested
   objects, arrays and scalars. `FieldStore.add_resource()` saves their parent
   links and paths. The quantity's value, code and unit remain siblings under
   the same object. Nothing is flattened into independent clinical measurements.
4. **Count distributions.** `field_statistics.aggregate()` groups occurrences
   by root resource type and statistical path. Numeric tokens stay as text in
   SQLite and are ordered using Decimal for quantiles. These are marginal
   descriptions; the same path can contain different clinical measurements.
5. **Prepare changes.** `_prepare_identities()` allocates replacement IDs for
   every target and a factor per patient. `_prepare_date_offsets()` checks all
   supported dates before choosing a shared offset that fits calendar bounds.
   The field database is checked against the input snapshot; its distributions
   do not determine these factors or offsets.
6. **Edit supported slots.** `TypeIndex.walk()` resolves `valueQuantity` to
   Quantity and its `value` to decimal. `Handlers.apply()` checks the exact unit
   system/code and patient ownership. With an illustrative factor `1.01` and
   offset `+7`, the value becomes `70.70` and the date becomes
   `2020-01-08T09:00:00.000+01:00`. These are illustrative parameters, not a
   prediction of the seed's draw. `_rewrite_reference()` points the subject at
   the prepared replacement Patient ID. Codes and units remain unchanged.
7. **Check what was written.** `_validate()` rereads the emitted JSON, checks
   identities, links and recorded transformations, then undoes each edit in
   memory. The reconstructed payload must have the original digest. The report
   records the actual numeric changes, including changes lost to rounding.

## Terms used in the implementation

| Term | Meaning |
| --- | --- |
| Resource row ID | An integer SQLite key; different from the FHIR `id` string |
| Occurrence | One appearance of a deduplicated resource in an input file or Bundle |
| Root | A non-contained resource; its `contained` subtree stays nested in output |
| Owner | The root or contained resource holding a field; its `patient_id` may be absent |
| Concrete path | One exact location, e.g. `component[0].valueQuantity.value` |
| Statistical path | The same location with array positions replaced by `[*]` for counting |
| Measurement context | Ancestor/component coding and unit information used to separate numeric before/after reports |
| Ledger | The state database containing mappings, parameters, edits and action counts |

Paths are tuples of typed segments, not parsed dot strings. For example,
`(("key", "component"), ("index", 0), ("key", "valueQuantity"))` locates one
component's quantity. Its statistical path substitutes `("item", None)` for
`("index", 0)`. Literal field names containing dots or brackets stay unambiguous.

Two walkers serve different purposes: `walk_json()` extracts arbitrary JSON
without FHIR assumptions; `TypeIndex.walk()` adds datatype context to guide
supported transformations. Unknown content survives both. Empty containers and
explicit nulls have nodes; missing keys do not. Array positions and parent links
keep repeated components separate throughout processing.

## Completion, validation and tests

Profiling and perturbation open their inputs read-only and require a fresh
output directory. Intermediate database commits retain progress; they do not
signal success. Perturbation records completion only after writing, checking
and reporting finish. An interrupted or failed destination is not reusable.

The built-in output check establishes recorded transformations and preservation.
The development tools `tools/check_r4_examples.py` and `tools/check_mii_examples.py`
separately invoke the external HL7 validator against public examples. Their
completed status means the audit finished; inspect per-case errors and unchecked
profiles for conformance results.

For executable examples, read `tests/test_ingestion.py`, `tests/test_profiling.py`
and `tests/test_perturbation.py` in that order. They cover duplicates, contained
links, nested arrays, Decimal precision, shared offsets, preservation and failures.
Run them from the repository root with:

```sh
python3 -m unittest discover -s tests -v
```

The detailed table descriptions are in `docs/ingestion-schema.md`,
`docs/profiling-schema.md` and `docs/perturbation.md`.
