# MIMIC-IV FHIR demo validation

The full local MIMIC-IV FHIR demo 2.1.0 passed ingestion, recursive extraction
and exact statistical profiling on 7 September 2026. Both commands exited with
code 0. All 71 automated tests and 24 independent dataset checks passed.

## Dataset and results

| Check | Result |
| --- | --- |
| Compressed NDJSON files | 30; all matched the supplied SHA-256 manifest |
| Resources | 928,935 across 13 resource types; no duplicates or discarded roots |
| Patient resources | 100 |
| Resolved references | 1,998,569; no unresolved references |
| Resources assigned to patients | 927,109 |
| Shared resources without patient assignments | 1,794 Medication, 31 Location, 1 Organization |
| Extracted JSON nodes | 28,511,367; matched an independent source traversal |
| Statistical field paths | 512, grouped by root resource type and normalized path |
| Ingestion errors / warnings | 0 / 0 |

The 35,338 `outside_requested_scope` notices are informational. These resources
were retained and profiled, including medication resource types beyond the
original hospital list. Field paths include roots, containers and scalar fields.

## Independent verification

- Both SQLite databases passed `quick_check` and foreign-key checks.
- Every JSON type count and all 142 observed root-level field-presence counts
  matched an independent scan of the original compressed files.
- Exact frequencies for all 366,433 numeric `Observation.valueQuantity.value`
  occurrences matched the source, including all 3,577 distinct numeric tokens.
  Their minima, maxima and all five nearest-rank quantiles also matched.
- Quantity object shapes, nested presence denominators, component array lengths
  and component element types matched the source.
- All scalar occurrences were accounted for in complete frequency tables;
  numeric tokens remained SQLite text rather than floating-point values.
- Reconstructing 37 resources spanning all 13 types recovered equivalent parsed
  JSON, including values, array order and parent associations.
- The source files and the ingestion database remained unchanged. Output
  directories and files retained private permissions.

These checks verify descriptive JSON statistics. Clinical interpretation,
full FHIR/MII profile validation, dependency modeling and synthetic generation
remain later milestones.

## Runtime and storage

The completed benchmark used Python 3.14.2 and SQLite 3.53.1 on macOS, with
working databases on the system drive. The original compressed files remained
on the project drive. Completed artifacts were copied back to the project.

| Stage | Elapsed time | Peak process memory | Database size |
| --- | --- | --- | --- |
| Ingestion | 5 min 13 sec | 972 MiB | 2.12 GiB |
| Profiling | 13 min 43 sec | 210 MiB | 5.61 GiB |

Independent verification took another 4 min 18 sec. Copying artifacts is
additional and depends on the destination drive. Temporary logs and SQLite
sorting need additional disk space during processing.

The project drive was substantially slower for SQLite work. This test exposed
repeated disk reads and writes, so the implementation now uses a 64 MiB writer
cache, batched commits, temporary write-ahead logs, phase checkpoints and
indexes covering common lookups. Bulk value aggregation uses sequential table
scans. Completed databases require no WAL/SHM sidecar files, and SQLite's normal
durability settings are retained.

These measurements describe this demo and machine. Larger hospital exports
have not been benchmarked; ingestion's patient grouping holds resource IDs and
relationships in memory.

## Repeating the run

Choose new output directories on fast local storage:

```sh
python3 fhir_synth.py ingest \
  --input data/physionet.org/files/mimic-iv-fhir-demo/2.1.0/fhir \
  --output /path/to/fast-local-storage/mimic-ingested

python3 fhir_synth.py profile \
  --input /path/to/fast-local-storage/mimic-ingested/cohort.sqlite \
  --output /path/to/fast-local-storage/mimic-profile
```

The verified artifacts and aggregate QA report for this run are under
`local-data/physionet-validation-20260907T100530Z/`, in `verified-ingestion/`,
`verified-profile/` and `validation.json`. These source-derived artifacts and
the `data/` directory are excluded from Git. This document contains only the
validation summary; the exact source-value distributions stay local.

The profile records the temporary input-database path used during testing.
For subsequent queries, use the archived `verified-ingestion/cohort.sqlite`.
