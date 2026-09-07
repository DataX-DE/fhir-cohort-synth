# Group related fields

The dependency API applies explicit rules to the existing field nodes and
reference graph. It returns **one group per matching object**, containing the
selected nodes and resolved links. It does not calculate conditional
distributions or generate synthetic records yet.

## Use the API

After running ingestion and profiling:

```python
from contextlib import closing
from fhir_cohort_synth.dependencies import load_rules, iter_groups

rules = load_rules("examples/dependency-rules.json")
with closing(iter_groups(
    "local-data/demo/cohort.sqlite",
    "local-data/profile/field-occurrences.sqlite",
    rules,
)) as groups:
    for group in groups:
        # A later statistics layer will consume these associated nodes.
        print(group["rule"], len(group["fields"]))
```

`load_rules(path)` returns a validated JSON dictionary. You can also construct
that dictionary in Python; `iter_groups` validates it again before reading the
databases. Errors raise `DependencyError`. Validation and reading happen when
iteration starts. Exhaust the iterator or close it when stopping early.

Both databases must be completed schema-1 runs; warnings are accepted. Their
resource IDs, types, identities, digests and root occurrence contexts must
match. Copied databases are accepted even when their file locations change.
The API opens read-only transactions and creates no files or tables. Groups
contain source information and remain local, just like the field index.

For consumers that need selected content, `iter_groups(..., include_values=True)`
also supplies `value_json` on present matches, `relative_path` on all field
matches and `patient_resource_id` on each group. Container values are serialized
from the existing nodes with sorted object keys and preserved array order. The
default remains node associations only. The
[conditional profiler](conditional-statistics.md) uses this optional view.

## Write a rule

This example creates one group per Observation component:

```json
{
  "schema_version": 1,
  "rules": [
    {
      "name": "component_values",
      "resource_type": "Observation",
      "anchor": [["key", "component"], ["item", null]],
      "fields": {
        "coding": {"path": [["key", "code"], ["key", "coding"], ["item", null]]},
        "value": {"path": [["key", "valueQuantity"], ["key", "value"]]},
        "unit": {"path": [["key", "valueQuantity"], ["key", "unit"]]},
        "status": {"from": "root", "path": [["key", "status"]]}
      },
      "links": {
        "encounter": {
          "reference": {"from": "root", "path": [["key", "encounter"]]},
          "resource_type": "Encounter",
          "fields": {
            "class": [["key", "class"]],
            "period": [["key", "period"]]
          }
        }
      }
    }
  ]
}
```

- `resource_type` selects the **profiled root type**. Contained subtrees remain
  beneath that root; they do not become extra resource groups automatically.
- `anchor` selects objects from the root. `[]` selects the root itself. Missing,
  null or scalar anchors produce no group; empty objects do produce groups.
- `fields` maps names to selectors. `from` defaults to `anchor`; `root` means
  the original non-contained resource, even for a contained anchor.
- `links` is optional. Each link selects Reference **objects**, not their
  `.reference` strings. The target type must match `resource_type` on the link.
  Linked `fields` are paths relative to the target resource itself, including
  when that target is contained. Links follow one outgoing reference only.
- Paths use `['key', name]` and `['item', null]` segments. In JSON, use double
  quotes as above. Empty paths are valid; numeric index selectors are not part
  of this format. A literal key `a.b` is distinct from nested keys `a`, `b`.

Rule names must be unique. Unknown configuration keys, malformed paths and
duplicate JSON keys fail instead of silently changing a rule's meaning. No
match is a valid empty result. The example file supplies three independent
rules for quantities, components and Observation-to-Encounter links; it is a
starting point, not a complete clinical dependency specification.

Rules may also contain `statistics`, with `given` and `targets` lists referencing
their named fields or linked fields. The loader validates those references;
the grouping API itself still performs no counting. See the
[statistics configuration](conditional-statistics.md#choose-context-and-outcome-fields).

## Worked example and returned groups

Consider this invented Observation excerpt:

```json
{
  "resourceType": "Observation",
  "status": "final",
  "component": [
    {
      "code": {"coding": [{"system": "demo", "code": "A"}, {"system": "translation", "code": "AA"}]},
      "valueQuantity": {"value": 10, "unit": "u"}
    },
    {
      "code": {"coding": [{"system": "demo", "code": "B"}]},
      "valueQuantity": {"value": 20, "unit": "v"}
    }
  ]
}
```

The component rule produces two groups. The table below displays the selected
values for readability; the actual API returns node references and matches.

| Anchor | Coding objects | Value | Unit | Root status |
| --- | --- | --- | --- | --- |
| `component[0]` | A and AA, in their original order | 10 | u | final |
| `component[1]` | B | 20 | v | final |

The two codings in the first component do **not** create two measurements.
Code A never pairs with the second component's value of 20. Each group has:

```python
{
    "rule": "component_values",
    "resource_id": ...,  # Ingestion key of the profiled root.
    "anchor": ...,       # NodeRef for this specific component object.
    "fields": {
        "value": [{"status": "present", "node": ..., "context": ..., "remaining_path": ()}],
        # Other named fields have their own lists of matches.
    },
    "links": {
        "encounter": {"references": [...], "targets": [...]}
    },
}
```

`(rule, anchor.node_id)` identifies a group within a field database. `NodeRef`
is immutable and carries `node_id`, root `resource_id`, `parent_id`, concrete
typed `path`, `kind`, `scalar_json`, `array_index`, `array_length` and
`object_keys_json`. Its IDs refer directly to the existing field index.
Containers are pointers with shape metadata, not copied payloads. Select
descendant paths when their scalar values are needed. Scalar JSON tokens retain
decimal precision and distinguish numbers, strings, booleans and nulls.

Each selected field is a list of matches, including unsuccessful branches:

| Status | Meaning |
| --- | --- |
| `present` | `node` is the selected node; `context` is its immediate parent. |
| `missing` | A key is absent from an existing object. |
| `empty` | An array wildcard encountered an empty array. |
| `not_applicable` | Traversal needs an object/array but encountered another type. |

For unsuccessful matches, `node` is `None`, `context` is the object/array/value
where traversal stopped, and `remaining_path` records the unfinished selection.
For `items[*].x` over `[{"x": 1}, {}]`, the second object produces a `missing`
match. It remains available for future presence denominators. Selecting an
explicit null or an empty container directly produces `present`; the node's
kind and shape preserve that distinction.

Each link returns a `references` list and a deduplicated `targets` list.
Reference entries contain the original `selection`, a resolution `status` and
`target_resource_id`. Status is `resolved`, `unresolved`, `ambiguous`,
`wrong_type`, or an unsuccessful selection status from the table above.
Identifier-only references remain unresolved. Repeated source occurrences
agreeing on one target do not duplicate results. Disagreeing targets are
ambiguous; mixed resolved/unresolved occurrences remain unresolved.

Each successful target has `resource_id`, `node` and its named `fields`.
`target_resource_id` and the target's `resource_id` refer to the target's own
ingestion row. For a contained target, `node.resource_id` still identifies its
outer root, and its path remains `contained[index]`. Repeated reference slots
are retained as provenance, while the same successful target appears once per
link in each group. Targets shared by different anchors remain in each group.

The resolver streams groups and caches at most eight resource trees. Memory
still depends on individual resource size and the groups retained by callers.
It does not infer clinical dependencies, interpret dates, normalize units,
calculate conditional frequencies or perform synthetic sampling.
