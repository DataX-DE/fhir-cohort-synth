"""Build the offline index from an already-downloaded official R4 core package.

Usage: python3 tools/build_fhir_type_index.py package.tgz output.json
This development tool never downloads data or extracts archive paths to disk.
"""
import hashlib
import json
from pathlib import Path
import sys
import tarfile


def build(package, destination):
    """Keep base datatype lookup information and provenance, not validation rules.

    Runtime TypeIndex needs element types, choice fields and content references.
    Cardinality minima, terminology bindings and invariants are not retained,
    so this compact index cannot substitute for a full FHIR validator.
    """
    definitions = {}
    with tarfile.open(package, 'r:gz') as archive:
        metadata = json.load(archive.extractfile('package/package.json'))
        if (metadata['name'], metadata['version']) != ('hl7.fhir.r4.core', '4.0.1'):
            raise ValueError('Expected hl7.fhir.r4.core 4.0.1')
        for member in archive:
            if not member.name.startswith('package/StructureDefinition-') or not member.name.endswith('.json'):
                continue
            definition = json.load(archive.extractfile(member))
            # Profiles constrain existing types. Only base specializations
            # define entries here; snapshots already include inherited fields.
            if definition.get('derivation') not in {None, 'specialization'} or 'snapshot' not in definition:
                continue
            elements = {}
            for element in definition['snapshot']['element']:
                types = []
                for typ in element.get('type', []):
                    code = typ['code']
                    # Primitive Element.id is represented with a FHIRPath
                    # system type; the extension supplies its actual FHIR type.
                    for extension in typ.get('extension', []):
                        if extension['url'].endswith('/structuredefinition-fhir-type'):
                            code = extension.get('valueUrl', extension.get('valueCode', code))
                    types.append(code)
                elements[element['path']] = {
                    'types': types, 'many': element.get('max') != '1',
                    **({'ref': element['contentReference'].removeprefix('#')} if 'contentReference' in element else {}),
                }
            definitions[definition['type']] = {'kind': definition['kind'], 'elements': elements}
    content = json.dumps(definitions, sort_keys=True, separators=(',', ':')).encode()
    result = {'schema_version': 1, 'fhir_version': '4.0.1',
              'source': 'https://hl7.org/fhir/R4/hl7.fhir.r4.core.tgz',
              'source_sha256': hashlib.sha256(Path(package).read_bytes()).hexdigest(),
              'definitions_sha256': hashlib.sha256(content).hexdigest(),
              'license': 'CC0-1.0; derived from HL7 FHIR R4 core definitions',
              'definitions': definitions}
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text(json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n')
    print(f'Built {len(definitions)} resource/datatype definitions.')


if __name__ == '__main__':
    build(*sys.argv[1:])
