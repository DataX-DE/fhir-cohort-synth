# Terminal messages for review

This is the current wording in the working tree, including the new progress display.
It covers terminal messages from `run`, `ingest` and `perturb`, plus help and
argument errors. It does not catalogue JSON report field names or issue codes
that are written only to files. No application wording was changed for this review.

Angle brackets below denote changing values, not literal output. Progress uses
stderr; completion summaries use stdout. Errors use stderr. Terminal ordering
across the two streams can vary when an external program captures them separately.

## 1. Normal `run` sequence, start to finish

The reading messages repeat for each input file. Count updates repeat during the
counted stages. This condensed sequence shows each stage once, rather than every
start/count/completion update.

```text
[<HH:MM:SS>] Finding input files
[<HH:MM:SS>] Reading FHIR file <file number>/<file count>
[<HH:MM:SS>] Saving source index
[<HH:MM:SS>] Resolving resource references
[<HH:MM:SS>] Assigning resources to patients
[<HH:MM:SS>] Saving linked source index
[<HH:MM:SS>] Writing ingestion reports
[<HH:MM:SS>] Checking input database and FHIR definitions
[<HH:MM:SS>] Checking source snapshot
[<HH:MM:SS>] Planning output files
[<HH:MM:SS>] Preparing replacement identities: <count> identities prepared
[<HH:MM:SS>] Checking patient date ranges: <count>/<total> resources (<percent>%)
[<HH:MM:SS>] Perturbing resources: <count>/<total> resources (<percent>%)
[<HH:MM:SS>] Validating perturbed resources: <count>/<total> resources (<percent>%)
[<HH:MM:SS>] Writing and checking FHIR files: <count>/<total> files (<percent>%)
[<HH:MM:SS>] Calculating report statistics
[<HH:MM:SS>] Writing result reports
[<HH:MM:SS>] Finishing and saving output databases
Perturbation: <status>. <root count> resource roots; <changed field count> changed fields verified.
Created <file count> source-layout files in result/fhir/ and reports in result/reports/; databases are in intermediates/.
Elapsed: <HH:MM:SS>.
```

`<status>` is `completed` or `completed_with_warnings`. Both return exit code 0.
Individual warning descriptions are not printed in the terminal; their counts
and codes are in the reports.

## 2. Progress formatting and repeated updates

Stage changes and known stage completion print immediately. Every approximately
10 seconds, the latest state is printed with this exact suffix:

```text
 - still working
```

Possible message forms:

```text
[<HH:MM:SS>] <stage>
[<HH:MM:SS>] <stage>: <count> <unit>
[<HH:MM:SS>] <stage>: <count>/<total> <unit> (<percent>%)
[<HH:MM:SS>] <stage> - still working
[<HH:MM:SS>] <stage>: <count> <unit> - still working
[<HH:MM:SS>] <stage>: <count>/<total> <unit> (<percent>%) - still working
```

Exact unit labels:

- `lines read`: NDJSON/JSONL input, including blank or invalid lines. Lines can contain Bundles or duplicates.
- `documents read`: ordinary JSON input; a document may be a Bundle.
- `identities prepared`: root and contained resource identities.
- `resources`: deduplicated root resources in the date, perturbation and validation passes.
- `files`: written and checked export files.

Counts use comma grouping, such as `120,000`. Percentages use one decimal place,
such as `12.9%`, and describe the current stage. Unknown totals have no percentage;
their final count becomes their total when the stream ends. A zero total is shown
as `0/0` without a percentage. Initial known counts are zero.
The elapsed prefix measures time since the command began, not time in that stage.
Repeated identical counts indicate that the process is alive; they do not prove
that the current database/file operation advanced. No estimated finish time is printed.

## 3. Standalone commands

`perturb` starts at `Checking input database and FHIR definitions`, then uses the
remaining perturbation stages and the same three completion lines as `run`.

`ingest` uses `Finding input files` through `Writing ingestion reports`, followed by:

```text
Ingestion: <status>. <resource count> resources; <patient count> patient resources.
Created cohort.sqlite, report.json and report.txt in the output directory.
Elapsed: <HH:MM:SS>.
The database contains source patient data. Record perturbation and full profile validation have not run.
```

Ingestion status is `completed`, `completed_with_warnings` or `incomplete`.
`incomplete` returns exit code 2; the other two return 0. The standalone ingestion
command can therefore print its summary and still return an error status.

## 4. Failure and interruption messages

Expected failures use one of these prefixes, followed by a specific reason:

```text
Cannot perturb: <reason>
Cannot ingest: <reason>
```

The `run` command uses `Cannot perturb:` even if its ingestion stage failed.

Ctrl+C (exit code 130):

```text
Perturbation interrupted. Use a new output directory to retry.
Ingestion interrupted. Use a new output directory to retry.
```

Unexpected processing failures (exit code 2):

```text
Perturbation could not finish. Check the inputs, access and disk space; use a new output directory to retry.
Ingestion could not finish due to a local file or database error. Check access and free disk space; use a new output directory to retry.
```

Only the applicable line is printed. Exceptions are not appended to these generic
messages. The normal final summary and elapsed-time line are not printed on these
exception paths. The following sections enumerate every explicit `InputError` /
`PerturbationError` reason in the current source, including API-only settings and
key-reuse reasons that cannot normally be triggered by the fixed-default CLI.

### Input files and output paths

| Exact reason | Code location |
| --- | --- |
| Output already exists; choose a new output directory. | [ingest.py:38](../fhir_cohort_synth/ingest.py#L38) |
| Input symlinks are unsupported; select the real file or directory. | [ingest.py:45](../fhir_cohort_synth/ingest.py#L45) |
| Output must be outside every input directory. | [ingest.py:50](../fhir_cohort_synth/ingest.py#L50) |
| Input directory contains a symlink; use an export without symlinks. | [ingest.py:55](../fhir_cohort_synth/ingest.py#L55) |
| Input must be a readable JSON/NDJSON/JSONL file (optionally gzip) or directory. | [ingest.py:61](../fhir_cohort_synth/ingest.py#L61) |
| No supported input files found. | [ingest.py:63](../fhir_cohort_synth/ingest.py#L63) |
| Base URL must be an HTTP(S) FHIR server base without credentials, query or fragment. | [ingest.py:85](../fhir_cohort_synth/ingest.py#L85) |

### Ingestion stopping the workflow

| Exact reason | Code location |
| --- | --- |
| Ingestion reported errors; inspect intermediates/report.json. Use a new output directory to retry. | [workflow.py:42](../fhir_cohort_synth/workflow.py#L42) |

### Source database checks

| Exact reason | Code location |
| --- | --- |
| Input must be an existing ingestion database file, not a symlink. | [cohort.py:44](../fhir_cohort_synth/cohort.py#L44) |
| Unsupported ingestion database schema; expected version 1 or 2. | [cohort.py:56](../fhir_cohort_synth/cohort.py#L56) |
| The ingestion run is incomplete; use a completed index. | [cohort.py:59](../fhir_cohort_synth/cohort.py#L59) |
| The source index contains ingestion errors; resolve them before processing. | [cohort.py:72](../fhir_cohort_synth/cohort.py#L72) |
| The source index has no non-contained resource roots. | [cohort.py:74](../fhir_cohort_synth/cohort.py#L74) |
| Input is not a readable, supported ingestion database. | [cohort.py:78](../fhir_cohort_synth/cohort.py#L78) |

### Perturbation settings, ownership and validation

| Exact reason | Code location |
| --- | --- |
| Use strength 0 or in [0.01,1) and a nonnegative supported day range. | [perturbation.py:49](../fhir_cohort_synth/perturbation.py#L49) |
| Cannot uniquely associate contained resources with the source index. | [perturbation.py:74](../fhir_cohort_synth/perturbation.py#L74) |
| Contained resource ownership does not match the source index. | [perturbation.py:78](../fhir_cohort_synth/perturbation.py#L78) |
| A resolved reference target is missing. | [perturbation.py:229](../fhir_cohort_synth/perturbation.py#L229) |
| A contained reference crosses resource ownership. | [perturbation.py:232](../fhir_cohort_synth/perturbation.py#L232) |
| Patient membership has no matching patient resource. | [perturbation.py:250](../fhir_cohort_synth/perturbation.py#L250) |
| Cannot validate an unsupported algorithm or invalid run key. | [perturbation.py:325](../fhir_cohort_synth/perturbation.py#L325) |
| Patient date offset does not match its keyed draw. | [perturbation.py:332](../fhir_cohort_synth/perturbation.py#L332) |
| Output resource population does not match the source. | [perturbation.py:341](../fhir_cohort_synth/perturbation.py#L341) |
| Output identity or resource type does not match the prepared map. | [perturbation.py:346](../fhir_cohort_synth/perturbation.py#L346) |
| Output differs from its recorded change ledger. | [perturbation.py:354](../fhir_cohort_synth/perturbation.py#L354) |
| Output quantity does not match its field-specific percentage change. | [perturbation.py:360](../fhir_cohort_synth/perturbation.py#L360) |
| Output personal field does not match its keyed replacement. | [perturbation.py:382](../fhir_cohort_synth/perturbation.py#L382) |
| Output date does not use its shared patient offset. | [perturbation.py:364](../fhir_cohort_synth/perturbation.py#L364) |
| Output reference target or ownership changed. | [perturbation.py:370](../fhir_cohort_synth/perturbation.py#L370) |
| Unrecorded resource content or structure changed. | [perturbation.py:389](../fhir_cohort_synth/perturbation.py#L389) |
| Output already exists; choose a new output directory. | [perturbation.py:404](../fhir_cohort_synth/perturbation.py#L404) |
| Perturbation requires a FHIR R4 4.0.1 source index. | [perturbation.py:419](../fhir_cohort_synth/perturbation.py#L419) |

### File layout and export verification

| Exact reason | Code location |
| --- | --- |
| The source index has no file locations for export. | [export_files.py:34](../fhir_cohort_synth/export_files.py#L34) |
| A source filename cannot be represented inside the output directory. | [export_files.py:43](../fhir_cohort_synth/export_files.py#L43) |
| Source files map to the same output filename; use distinct source names. | [export_files.py:56](../fhir_cohort_synth/export_files.py#L56) |
| Verified resources do not match their source file locations. | [export_files.py:97](../fhir_cohort_synth/export_files.py#L97) |
| An exported file differs from the verified resource stream. | [export_files.py:104](../fhir_cohort_synth/export_files.py#L104) |
| The exported resource population differs from the verified stream. | [export_files.py:109](../fhir_cohort_synth/export_files.py#L109) |

### Local key reuse and key checks

| Exact reason | Code location |
| --- | --- |
| Key reuse requires an existing state database file, not a symlink. | [perturbation_store.py:128](../fhir_cohort_synth/perturbation_store.py#L128) |
| Key reuse requires a supported schema 3 state database. | [perturbation_store.py:136](../fhir_cohort_synth/perturbation_store.py#L136) |
| Key reuse requires a completed perturbation run. | [perturbation_store.py:139](../fhir_cohort_synth/perturbation_store.py#L139) |
| State database has an unsupported algorithm or invalid run key. | [perturbation_store.py:141](../fhir_cohort_synth/perturbation_store.py#L141) |
| Key reuse requires the same indexed source snapshot. | [perturbation_store.py:143](../fhir_cohort_synth/perturbation_store.py#L143) |
| Key reuse requires the same datatype definitions. | [perturbation_store.py:145](../fhir_cohort_synth/perturbation_store.py#L145) |
| Key reuse requires the same strength and date range as the previous run. | [perturbation_store.py:147](../fhir_cohort_synth/perturbation_store.py#L147) |
| Cannot reuse a key from this state database; use a completed, supported keyed run. | [perturbation_store.py:152](../fhir_cohort_synth/perturbation_store.py#L152) |
| Run key must contain exactly 32 bytes. | [perturbation_store.py:160](../fhir_cohort_synth/perturbation_store.py#L160) |

The duplicate output-directory reason is defined in both ingestion and
perturbation. Invalid numeric/date settings and base URL messages concern Python
API overrides; key-reuse messages likewise concern the Python API. The CLI does
not accept those settings. API callers receive exceptions unless they catch and
print them themselves.

## 5. Help and version output

These blocks were captured directly from the current checkout. Line wrapping
can change with terminal width and Python version; the executable name will
change when packaged.

### `python3 fhir_synth.py --help`

```text
usage: fhir_synth.py [-h] [--version] {run,ingest,perturb} ...

Local FHIR ingestion and source-derived perturbation.

positional arguments:
  {run,ingest,perturb}
    run                 Create a perturbed export directly from local FHIR
                        files.
    ingest              Index a local FHIR R4 export and report its structure.
    perturb             Perturb supported values while retaining existing
                        linked records.

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
```

### `python3 fhir_synth.py run --help`

```text
usage: fhir_synth.py run [-h] --input INPUT [INPUT ...] --output OUTPUT

options:
  -h, --help            show this help message and exit
  --input INPUT [INPUT ...]
                        Files or directories: JSON, NDJSON, JSONL, optionally
                        .gz.
  --output OUTPUT       New directory outside the input directories.
```

### `python3 fhir_synth.py ingest --help`

```text
usage: fhir_synth.py ingest [-h] --input INPUT [INPUT ...] --output OUTPUT

options:
  -h, --help            show this help message and exit
  --input INPUT [INPUT ...]
                        Files or directories: JSON, NDJSON, JSONL, optionally
                        .gz.
  --output OUTPUT       New directory outside the input directories.
```

### `python3 fhir_synth.py perturb --help`

```text
usage: fhir_synth.py perturb [-h] --input INPUT --output OUTPUT

options:
  -h, --help       show this help message and exit
  --input INPUT    Completed ingestion database (cohort.sqlite).
  --output OUTPUT  New output directory.
```

### `python3 fhir_synth.py --version`

```text
0.1.1
```

## 6. Argument-parser errors

Python's `argparse` emits a usage line and an error before any progress starts.
Below are captured examples using placeholder input/output paths; these errors
are rejected before opening files. Parser errors can quote the user's command-line
arguments. Unlike the processing diagnostics above, they are generated by Python.
Exact formatting may vary with Python version.

### `python3 fhir_synth.py`

```text
usage: fhir_synth.py [-h] [--version] {run,ingest,perturb} ...
fhir_synth.py: error: the following arguments are required: command
```

### `python3 fhir_synth.py run`

```text
usage: fhir_synth.py run [-h] --input INPUT [INPUT ...] --output OUTPUT
fhir_synth.py run: error: the following arguments are required: --input, --output
```

### `python3 fhir_synth.py run --input example.ndjson`

```text
usage: fhir_synth.py run [-h] --input INPUT [INPUT ...] --output OUTPUT
fhir_synth.py run: error: the following arguments are required: --output
```

### `python3 fhir_synth.py run --output new-output`

```text
usage: fhir_synth.py run [-h] --input INPUT [INPUT ...] --output OUTPUT
fhir_synth.py run: error: the following arguments are required: --input
```

### `python3 fhir_synth.py run --input --output new-output`

```text
usage: fhir_synth.py run [-h] --input INPUT [INPUT ...] --output OUTPUT
fhir_synth.py run: error: argument --input: expected at least one argument
```

### `python3 fhir_synth.py run --input example.ndjson --output`

```text
usage: fhir_synth.py run [-h] --input INPUT [INPUT ...] --output OUTPUT
fhir_synth.py run: error: argument --output: expected one argument
```

### `python3 fhir_synth.py unknown`

```text
usage: fhir_synth.py [-h] [--version] {run,ingest,perturb} ...
fhir_synth.py: error: argument command: invalid choice: 'unknown' (choose from run, ingest, perturb)
```

### `python3 fhir_synth.py run --input example.ndjson --output new-output --seed 42`

```text
usage: fhir_synth.py [-h] [--version] {run,ingest,perturb} ...
fhir_synth.py: error: unrecognized arguments: --seed 42
```

Other invalid command-line combinations use the same parser-generated patterns,
substituting the offending command, option or argument. These are templates with
user-provided values, not a finite additional set of application-written strings.
