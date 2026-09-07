# Generic field profiling, schema version 1

`profile` extracts every field from the ingestion index's deduplicated,
non-contained resource roots. Every root's contained subtree remains under its
original path; separate contained rows are not traversed again. The original
ingestion database is opened read-only through one consistent read transaction.

All outputs contain source-derived information. Exact string and numeric
frequencies remain local in `field-occurrences.sqlite`. No synthetic samples,
clinical interpretation or conditional value distributions are produced yet.

## Data flow

```text
profile_index()
  -> read a completed schema-1 ingestion index
  -> walk_json() for each non-contained root
  -> FieldStore.add_resource(): nodes, paths and provenance
  -> aggregate(): exact frequency tables
       -> summarize_presence(): eligible-parent and root denominators
       -> summarize_numbers(): weighted numeric quantiles
  -> stream both JSON reports to private partial files
  -> publish reports, then commit the completed run status
```

Python holds the current resource tree, its node-ID map, a bounded path cache
and one report fragment at a time. SQLite performs grouping and sorting, with
disk-backed temporary storage available. The complete cohort and all distinct
scalar values are not collected in Python lists. Largest-resource size still
affects memory use, and exact occurrence/frequency storage requires disk space.

## Paths, nodes and provenance

Paths are JSON arrays of typed segments. Concrete array indices use `index`;
statistical paths use `item` with null. Object keys always use `key`.

```json
[["key", "component"], ["index", 0], ["key", "valueQuantity"], ["key", "value"]]
```

Its normalized form is:

```json
[["key", "component"], ["item", null], ["key", "valueQuantity"], ["key", "value"]]
```

`display_path` renders this as `$["component"][*]["valueQuantity"]["value"]`.
A literal key named `component[*]` is different. The empty path `[]` identifies
the root. Neither array index nor display text defines a clinical concept.

| Table | Purpose |
| --- | --- |
| `run` | Profiling/source schema versions, source database location, source status, profiling status and generic failure code |
| `source_resources` | Profiled root identities and digests, keyed by the ingestion resource ID |
| `source_occurrences` | Every source appearance of each profiled root, with file path, locator and lookup context |
| `source_profiles` | All declared profiles for the roots, without multiplying the root population |
| `source_issues` | Original ingestion issue codes, severities and counts |
| `fields` | Root resource type, normalized typed path, parent path and relation (`root`, `key`, `item`) |
| `nodes` | Every container and scalar occurrence with parent ID, field ID, concrete path and source resource ID |
| `field_summaries` | Occurrence counts, eligible/present/absent parents, and root-resource coverage |

Node ordinals are local to their resource; `nodes.id` is a global output-database
key. `parent_id` references another node in the same resource. Array position,
length and immediate element-type sequence are retained. Object shapes are
sorted child-key sets, so key ordering does not split equivalent shapes.

`scalar_json` contains a JSON token in a TEXT column. Thus the string `"1"`,
number `1`, decimal `1.0`, boolean `true` and null `null` stay distinct. Containers
have SQL NULL in this column; explicit JSON null has the text token `null`.
Numeric representations distinguish integer/decimal tokens, and decimal tokens
retain their precision. Normalization follows the ingestion JSON codec; this is
a representation of parsed JSON values, not an archive of original whitespace,
escape spelling or every original numeric spelling.

The node tree can reconstruct equivalent parsed JSON without using the source
payload. There is deliberately no reconstruction/generation CLI yet; a test
helper verifies this property.

## Exact distributions

Each frequency table references `fields.id`; `frequency` is an exact integer
count. Categories are not truncated to a top-N list in the database.

| Table | Grouping within each field |
| --- | --- |
| `type_frequencies` | JSON kind: object, array, number, string, boolean or null |
| `scalar_frequencies` | JSON kind and serialized scalar token |
| `number_format_frequencies` | Integer versus decimal representation |
| `object_shape_frequencies` | Exact sorted child-key set |
| `array_length_frequencies` | Array length, including zero |
| `array_type_pattern_frequencies` | Ordered sequence of immediate element types, including the empty sequence |
| `array_element_type_frequencies` | Total immediate array elements of each JSON kind |
| `string_length_frequencies` | String length in Unicode code points, including zero |
| `numeric_summaries` | Numeric count, min/max tokens and weighted nearest-rank quantiles |

Numbers are sorted by Decimal comparison, without casting to SQLite REAL.
For percentile p and n occurrences, use rank `ceil(p*n)`, counting frequencies
as repeated observations. Percentiles are 5, 25, 50, 75 and 95. For `[1,2,2,10]`,
the 50th and 75th percentiles both equal 2. Numerically equal representations
use binary token order as a deterministic tie-break; representation frequencies
remain separate. Min/max/quantiles retain numeric tokens in JSON reports too.

Presence counts and their integer denominators are exact. Probabilities in the
reports are floating-point renderings of count ratios; clients needing exact
rational probabilities can use the provided numerator and denominator.

Only paths actually observed at least once are inventoried. Schema-defined
fields absent throughout the export are not inferred by this generic profiler.

## Missingness and weighting example

Consider three resource roots:

```json
[
  {"group": {"x": 1}, "items": [{"x": 1}, {}]},
  {"group": {}, "items": []},
  {"items": [null, {"x": 3}]}
]
```

- `group` is present in 2 of 3 roots.
- `group.x` is present in 1 of 2 existing group objects. Its root-resource
  presence is separately 1 of 3. The missing group is not a third eligible parent.
- `items[*]` has 4 element occurrences across 2 nonempty arrays. Its parent
  presence is 2 of 3 arrays, with 1 empty array.
- `items[*].x` is present in 2 of 3 object elements. The null element is not an
  eligible object parent. Its root-resource presence is 2 of 3.

Every observed array element contributes to an element-level distribution.
`root_resource_presence` separately gives each root one vote, regardless of its
array length. This does not imply patient weighting when a patient has several
resource roots; patient-level statistics belong to later enrichment.

## Inspect a distribution locally

Open `field-occurrences.sqlite` in a SQLite client. For top-level Observation
quantity values, this query returns every exact scalar token and its count:

```sql
SELECT s.kind, s.value_json, s.frequency
FROM scalar_frequencies AS s
JOIN fields AS f ON f.id = s.field_id
WHERE f.resource_type = 'Observation'
  AND f.display_path = '$["valueQuantity"]["value"]';
```

This is a marginal across that path, which may contain different assays or
measurement concepts. It does not assert clinical comparability. Associated
codes/units remain available in the same resource and parent-linked nodes for
the later dependency layer.

For example, preserve sibling component associations by joining on parent ID:

```sql
SELECT a.resource_id, a.parent_id, a.concrete_path, b.concrete_path
FROM nodes AS a
JOIN nodes AS b ON b.parent_id = a.parent_id
WHERE a.field_id = ? AND b.field_id = ?;
```

Parameters are selected field IDs. This joins fields in the same repeated
object, rather than pairing every component with every other component.

## Completion contract

Success requires both final JSON reports and database `run.status` equal to
`completed` or `completed_with_warnings`. Input warnings are retained in source
context; they do not suppress unfamiliar fields. Incomplete source indexes,
unsupported schema versions, source ingestion errors and indexes with no roots
are rejected before output creation.

During processing, status is `in_progress`. Failures are marked `failed` and
interruptions `interrupted` when storage permits. If failure prevents the marker
from being written, `in_progress` still means the output is not complete.
Partial report files begin with a dot and end in `.partial`; completed-looking
reports alone are never sufficient if the database status is not complete.

Outputs use a fresh directory, POSIX directory mode 0700 and file mode 0600;
Windows relies on destination access controls. Retry in a new directory.
Reports omit source scalar string samples, but field names, numeric summaries
and small counts can still be sensitive. Keep all artifacts local.
