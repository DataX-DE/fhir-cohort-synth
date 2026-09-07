"""Resolve FHIR types without guessing from field names or source values.

The bundled index is derived from official StructureDefinition snapshots.
For example Observation.component is a backbone in Observation's snapshot,
while its valueQuantity switches to the Quantity snapshot. Unknown extension
contents remain opaque even when their JSON happens to resemble a known type.
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files
import json

from .jsonio import dumps


# Keep ancestor codes as well as component codes: a component may occur in
# different panels. Medication references distinguish doses of different drugs
# without pooling by unit alone. Patient references are deliberately excluded.
MEASUREMENT_FIELDS = ('code', 'type', 'medicationCodeableConcept', 'medicationReference',
                      'itemCodeableConcept', 'itemReference', 'substanceCodeableConcept', 'substanceReference')


@dataclass
class Field:
    """One visited JSON slot plus the FHIR information needed by handlers.

    ``datatype`` is the slot's FHIR type, e.g. decimal; ``parent_type`` is its
    enclosing type, e.g. Quantity. ``parent[key]`` is the actual mutable slot
    in the source tree (both are None at the root). This lets the writer change
    a value without rebuilding or flattening its surrounding JSON.

    ``reason`` explains protected/unknown content and propagates to descendants.
    ``quantity`` is the enclosing measurement object, when one exists.
    ``concept`` holds serialized ancestor coding contexts for numeric reports;
    it is captured before the writer can change any referenced identities.
    """
    path: tuple
    value: object
    datatype: str | None
    parent_type: str | None
    parent: object
    key: object
    reason: str | None
    quantity: dict | None
    concept: object


class TypeIndex:
    """Read the bundled R4 definitions; resolve datatypes without profile validation."""

    def __init__(self):
        document = json.loads(files('fhir_cohort_synth').joinpath('data/fhir-r4-types.json').read_text())
        self.definitions = document.pop('definitions')
        actual = hashlib.sha256(json.dumps(self.definitions, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if document['fhir_version'] != '4.0.1' or actual != document['definitions_sha256']:
            raise ValueError('Invalid bundled datatype index')
        self.metadata = document
        # Keep cached lookups on this index. A class-level cache retains entire
        # indexes from earlier runs, which is costly in the all-resource audit.
        self.child = lru_cache(maxsize=32768)(self._child)

    def _child(self, definition, anchor, key):
        """Return (datatype, definition to inspect next, anchor within it).

        At Observation.component, stay in the Observation definition because
        component is a resource-specific backbone. At component.valueQuantity,
        switch to the reusable Quantity definition, whose child is Quantity.value.
        An unknown field returns three Nones; its JSON remains available to walk().
        """
        elements = self.definitions.get(definition, {}).get('elements', {})
        companion = key.startswith('_')
        field_name = key[1:] if companion else key
        path = anchor + '.' + field_name
        spec = elements.get(path)
        selected_type = None
        if spec is None:
            # Choice fields are serialized with their type suffix, e.g.
            # effective[x] + dateTime -> effectiveDateTime.
            for candidate, rule in elements.items():
                if not candidate.endswith('[x]') or candidate.rsplit('.', 1)[0] != anchor:
                    continue
                prefix = candidate.rsplit('.', 1)[1][:-3]
                for datatype in rule['types']:
                    if field_name == prefix + datatype[0].upper() + datatype[1:]:
                        path, spec, selected_type = candidate, rule, datatype
                        break
                if spec is not None:
                    break
        if spec is None:
            return None, None, None
        if companion:
            # _birthDate carries the primitive's Element metadata/extensions;
            # it is not another date to shift. Companions of complex types are invalid.
            datatype = selected_type or (spec['types'][0] if len(spec['types']) == 1 else None)
            if self.definitions.get(datatype, {}).get('kind') != 'primitive-type':
                return None, None, None
            return 'Element', 'Element', 'Element'
        if 'ref' in spec:
            # A StructureDefinition contentReference reuses another backbone,
            # e.g. recursive QuestionnaireResponse items, in this same definition.
            path = spec['ref']
            spec = elements.get(path, {})
        datatype = selected_type or (spec.get('types') or ['BackboneElement'])[0]
        if datatype in {'BackboneElement', 'Element'}:
            return datatype, definition, path
        return datatype, datatype, datatype

    def walk(self, resource, *, include_context=True):
        """Yield every node with its typed path; only one JSON tree is held.

        Datatype traversal does not alter any data. The caller changes scalar
        slots through parent/key, leaving arrays and sibling associations intact.
        include_context=False skips report-only coding context during ingestion
        and the date-bound pass. Both modes resolve the same datatypes.
        """
        root_type = resource.get('resourceType')
        # value, path, datatype, definition, schema anchor, parent type,
        # parent, key, protected reason, enclosing quantity, clinical code
        stack = [(resource, (), root_type, root_type, root_type, None, None, None,
                  None if root_type in self.definitions else 'unknown_resource_type', None, None)]
        while stack:
            (value, path, datatype, definition, anchor, parent_type,
             parent, key, reason, quantity, concept) = stack.pop()
            if isinstance(value, dict):
                if datatype == 'Resource' and reason is None:
                    datatype = value.get('resourceType')
                    definition = anchor = datatype
                    if datatype not in self.definitions:
                        reason = 'unknown_resource_type'
                    elif root_type != 'Bundle' and not (len(path) == 2 and path[0] == ('key', 'contained')):
                        # Parameters.parameter.resource is an inline resource,
                        # not a separately indexed root or contained resource.
                        # Preserve it as one subtree until its own identities,
                        # references and ownership have explicit index support.
                        reason = 'embedded_resource_preserved'
                # Carry a preservation reason through the whole subtree even
                # if a descendant has a normally supported datatype.
                if datatype == 'Extension':
                    reason = reason or 'extension_contents_preserved'
                if datatype in {'Narrative', 'Attachment'}:
                    reason = reason or 'narrative_or_attachment_preserved'
                if datatype in {'Age', 'Duration', 'Count'}:
                    reason = reason or 'age_duration_count_preserved'
                context = {}
                if include_context:
                    context = {name: value[name] for name in MEASUREMENT_FIELDS
                               if isinstance(value.get(name), (dict, list))}
                if context:
                    normalized = tuple(('item', None) if k == 'index' else (k, v) for k, v in path)
                    # Freeze source context before references are rewritten
                    # later in this traversal. Only ancestor context is held.
                    concept = (concept or ()) + ((normalized, dumps(context)),)
                if datatype in {'Quantity', 'Distance'}:
                    quantity = value
            yield Field(path, value, datatype, parent_type, parent, key, reason, quantity, concept)
            # Reverse pushes keep original object/array order when popping a
            # last-in-first-out stack. Array elements inherit their declared
            # datatype but keep separate parent/index slots; no combinations form.
            if isinstance(value, dict):
                for child_key, child in reversed(list(value.items())):
                    child_type, child_definition, child_anchor = (None, None, None)
                    if definition and anchor:
                        child_type, child_definition, child_anchor = self.child(definition, anchor, child_key)
                    child_reason = reason
                    if child_key == 'resourceType' and child_key in value:
                        child_type, child_definition, child_anchor = 'code', 'code', 'code'
                    elif child_type is None:
                        child_reason = reason or 'unknown_field'
                    stack.append((child, path + (('key', child_key),), child_type, child_definition, child_anchor,
                                  datatype, value, child_key, child_reason, quantity, concept))
            elif isinstance(value, list):
                for index in range(len(value) - 1, -1, -1):
                    stack.append((value[index], path + (('index', index),), datatype, definition, anchor, parent_type,
                                  value, index, reason, quantity, concept))
