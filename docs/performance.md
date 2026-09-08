# Performance

The optimization keeps the same perturbation rules and validation. Reusing the
same local key must still reproduce the original FHIR files, including gzip bytes.

## What changed

- JSON serialization reuses standard-library encoding routines instead of
  constructing an encoder for each scalar. Decimal tokens, escaping, sorted
  keys and the existing tuple-path formatting stay the same.
- Ingestion uses the inserted resource ID directly and formats display paths
  only when it finds a reference. Reference resolution joins occurrence context
  once and caches up to 16,384 candidate lookups, including namespace and version.
- The existing uniqueness indexes also serve identity and alias lookup queries,
  so two redundant indexes no longer need updating during ingestion.
- The read-only source connection uses a bounded 64 MiB SQLite page cache, like
  the writers. It still opens the source in read-only/query-only mode.
- Three composite-key ledger tables use SQLite's
  [WITHOUT ROWID layout](https://www.sqlite.org/withoutrowid.html). This avoids
  storing the same primary key in a separate row lookup index. The columns and
  logical keys remain compatible with existing schema 3 state databases.

No runtime dependency, CLI option or schema migration was added. Completion,
private permissions, interrupted-run handling and fresh-output requirements stay
in place. Unsupported/missing units still preserve both values and unit fields.

## Small benchmark

The local MIMIC benchmark contains three complete patient cohorts and shared
resources: 11,677 roots across all 13 resource types present in the demo.

| Measurement | Original | Optimized |
| --- | ---: | ---: |
| Whole workflow, without cProfile | 12.05 s | 8.85 s |
| Ingestion database | 23,392,256 bytes | 21,524,480 bytes |
| Perturbation state database | 19,161,088 bytes | 15,765,504 bytes |

These are individual local runs; timing varies with storage, caching and other
work on the machine. All exported files and both JSON reports were byte-identical
when reusing the original key. The 131 unit/integration tests also pass, including
new checks for canonical serialization bytes used by fingerprints and keyed draws.

## Full MIMIC demo, 8 September 2026

The complete demo contains 928,935 resource roots, 100 patients and 30 gzip files.
The optimized workflow ran on the same machine and source export, reusing the
previous local key for an exact comparison.

| Measurement | Original | Optimized |
| --- | ---: | ---: |
| Complete workflow | 47.1 min | 33.6 min |
| Ingestion, including graph resolution and reports | 21.5 min | 13.1 min |
| Remaining perturbation, validation and output work | 25.6 min | 20.5 min |
| Ingestion database, decimal GB | 1.93 | 1.78 |
| Perturbation state database, decimal GB | 1.36 | 1.13 |

Elapsed time fell by 28.6% (about 13.5 minutes). The original timing comes from
the run directory's creation and completion/report timestamps; the optimized
run used `time.perf_counter()` around the complete workflow. These are observed
local runs, not a guaranteed runtime for another machine or export.

All 30 compressed FHIR files and both JSON reports were byte-identical. Local
numeric contexts and summaries also matched, and all 31 files in the input
directory retained their SHA-256 hashes. Full validation checked 5,190,230 changes
and 1,998,569 rewritten references. Missing/unsupported-unit handling was unchanged.

The local benchmark artifacts are under `work/performance/`: the original and
optimized small runs, cProfile output, full-run phase timings and `comparison.json`.
Database writing/checkpointing remains a substantial cost; this change does not
remove those durability steps or the full validation pass.

## Find bottlenecks during development

Python's built-in profiler can measure a run without adding options to the
hospital CLI. Use a representative small export and a fresh output directory:

```sh
mkdir -p work
python3 -m cProfile -o work/run.prof fhir_synth.py run \
  --input /path/to/small-fhir-export --output local-data/profile-run
python3 -c 'import pstats; pstats.Stats("work/run.prof").strip_dirs().sort_stats("tottime").print_stats(25)'
```

Profiled timing includes profiler overhead: the original small benchmark took
22.69 seconds with cProfile versus 12.05 seconds without it. Compare ordinary
runs for elapsed-time claims. Keep benchmark exports and profiling artifacts local.
