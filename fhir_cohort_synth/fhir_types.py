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
    path: tuple
    value: object
    datatype: str | None
    parent_type: str | None
    parent: object
    key: object
    reason: str | None
    quantity: dict | None
    quantity_path: tuple | None
    concept: object


class TypeIndex:
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
        elements = self.definitions.get(definition, {}).get('elements', {})
        companion = key.startswith('_')
        actual = key[1:] if companion else key
        path = anchor + '.' + actual
        spec = elements.get(path)
        selected = None
        if spec is None:
            # Choice fields are serialized with their type suffix, e.g.
            # effective[x] + dateTime -> effectiveDateTime.
            for candidate, rule in elements.items():
                if not candidate.endswith('[x]') or candidate.rsplit('.', 1)[0] != anchor:
                    continue
                prefix = candidate.rsplit('.', 1)[1][:-3]
                for typ in rule['types']:
                    if actual == prefix + typ[0].upper() + typ[1:]:
                        path, spec, selected = candidate, rule, typ
                        break
                if spec is not None:
                    break
        if spec is None:
            return None, None, None
        if companion:
            typ = selected or (spec['types'][0] if len(spec['types']) == 1 else None)
            if self.definitions.get(typ, {}).get('kind') != 'primitive-type':
                return None, None, None
            return 'Element', 'Element', 'Element'
        if 'ref' in spec:
            path = spec['ref']
            spec = elements.get(path, {})
        typ = selected or (spec.get('types') or ['BackboneElement'])[0]
        if typ in {'BackboneElement', 'Element'}:
            return typ, definition, path
        return typ, typ, typ

    def walk(self, resource, *, include_context=True):
        """Yield every node with its typed path; only one JSON tree is held.

        Datatype traversal does not alter any data. The caller changes scalar
        slots through parent/key, leaving arrays and sibling associations intact.
        """
        kind = resource.get('resourceType')
        # value, path, datatype, definition, schema anchor, parent type,
        # parent, key, protected reason, enclosing quantity/path, clinical code
        stack = [(resource, (), kind, kind, kind, None, None, None,
                  None if kind in self.definitions else 'unknown_resource_type', None, None, None)]
        while stack:
            value, path, typ, definition, anchor, parent_type, parent, key, reason, quantity, qpath, concept = stack.pop()
            if isinstance(value, dict):
                if typ == 'Resource' and reason is None:
                    typ = value.get('resourceType')
                    definition = anchor = typ
                    if typ not in self.definitions:
                        reason = 'unknown_resource_type'
                    elif kind != 'Bundle' and not (len(path) == 2 and path[0] == ('key', 'contained')):
                        # Parameters.parameter.resource is an inline resource,
                        # not a separately indexed root or contained resource.
                        # Preserve it as one subtree until its own identities,
                        # references and ownership have explicit index support.
                        reason = 'embedded_resource_preserved'
                if typ == 'Extension':
                    reason = reason or 'extension_contents_preserved'
                if typ in {'Narrative', 'Attachment'}:
                    reason = reason or 'narrative_or_attachment_preserved'
                if typ in {'Age', 'Duration', 'Count'}:
                    reason = reason or 'age_duration_count_preserved'
                context = ({k: value[k] for k in MEASUREMENT_FIELDS if isinstance(value.get(k), (dict, list))}
                           if include_context else {})
                if context:
                    normalized = tuple(('item', None) if k == 'index' else (k, v) for k, v in path)
                    # Freeze source context before references are rewritten
                    # later in this traversal. Only ancestor context is held.
                    concept = (concept or ()) + ((normalized, dumps(context)),)
                if typ in {'Quantity', 'Distance'}:
                    quantity, qpath = value, path
            yield Field(path, value, typ, parent_type, parent, key, reason, quantity, qpath, concept)
            if isinstance(value, dict):
                for child_key, child in reversed(list(value.items())):
                    ct, cd, ca = self.child(definition, anchor, child_key) if definition and anchor else (None, None, None)
                    child_reason = reason
                    if child_key == 'resourceType' and child_key in value:
                        ct, cd, ca = 'code', 'code', 'code'
                    elif ct is None:
                        child_reason = reason or 'unknown_field'
                    stack.append((child, path + (('key', child_key),), ct, cd, ca, typ, value, child_key,
                                  child_reason, quantity, qpath, concept))
            elif isinstance(value, list):
                for i in range(len(value) - 1, -1, -1):
                    stack.append((value[i], path + (('index', i),), typ, definition, anchor, parent_type,
                                  value, i, reason, quantity, qpath, concept))
