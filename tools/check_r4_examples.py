"""Development-only R4 coverage check using locally downloaded public examples.

Run from the repository root; see docs/validation-r4.md for the complete recipe.
The hospital commands retain their standard-library-only runtime. This tool
does not download anything, alter the public examples, or infer missing links.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fhir_cohort_synth.fhir_types import TypeIndex
from fhir_cohort_synth.ingest import ingest
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.perturbation import perturb
from fhir_cohort_synth.export_files import iter_export_lines


def supplemental_examples():
    """Invented, explicitly labelled cases for types absent from R4 examples."""
    return [
        {'resourceType': 'SubstanceNucleicAcid', 'id': 'invented-nucleic-acid',
         'numberOfSubunits': 1, 'subunit': [{'subunit': 1, 'sequence': 'ACGT', 'length': 4}]},
        {'resourceType': 'SubstancePolymer', 'id': 'invented-polymer',
         'modification': ['Invented test material'], 'repeat': [{'numberOfUnits': 2, 'averageMolecularFormula': 'C2H4'}]},
        {'resourceType': 'SubstanceProtein', 'id': 'invented-protein', 'numberOfSubunits': 1,
         'subunit': [{'subunit': 1, 'sequence': 'ACDE', 'length': 4,
                     'nTerminalModificationId': {'system': 'urn:invented', 'value': 'test-modification'}}]},
        {'resourceType': 'SubstanceReferenceInformation', 'id': 'invented-reference-information',
         'comment': 'Invented test material', 'target': [{'target': {'system': 'urn:invented', 'value': 'target-1'},
             'amountQuantity': {'value': 10, 'system': 'http://unitsofmeasure.org', 'code': 'mg'}}]},
        {'resourceType': 'SubstanceSourceMaterial', 'id': 'invented-source-material',
         'organismId': {'system': 'urn:invented', 'value': 'organism-1'}, 'organismName': 'Invented test organism',
         'partDescription': [{'part': {'text': 'Invented test part'}}]},
    ]


def read_examples(package, extra_zip):
    """Read archive members without extracting archive-supplied filesystem paths."""
    examples = {}
    with tarfile.open(package) as archive:
        metadata = json.load(archive.extractfile('package/package.json'))
        if (metadata['name'], metadata['version']) != ('hl7.fhir.r4.examples', '4.0.1'):
            raise ValueError('Expected the official hl7.fhir.r4.examples 4.0.1 package')
        for member in archive:
            if member.isfile() and member.name.endswith('.json'):
                value = loads(archive.extractfile(member).read().decode('utf-8-sig'))
                if isinstance(value, dict) and isinstance(value.get('resourceType'), str):
                    examples[member.name] = value
    # The published package omits Parameters; the official JSON ZIP contains it.
    with ZipFile(extra_zip) as archive:
        examples['zip/parameters-example.json'] = loads(archive.read('parameters-example.json').decode('utf-8-sig'))
    return examples


def select_examples(examples):
    """Select deterministically before validation, never by whether a case passes.

    Use the named 'example' where available, otherwise the first archive name.
    Every Bundle is included because envelope behaviour differs by Bundle type.
    All Patient/Observation examples add repeated, component and unit coverage.
    """
    by_type = defaultdict(list)
    for name, value in examples.items():
        by_type[value['resourceType']].append(name)
    selected = []
    for kind, names in sorted(by_type.items()):
        names.sort()
        if kind in {'Bundle', 'Patient', 'Observation'}:
            selected.extend(names)
        else:
            selected.append(next((n for n in names if n == f'package/{kind}-example.json'), names[0]))
    return selected


def write_json(path, value):
    path.write_text(dumps(value) + '\n', encoding='utf-8')
    path.chmod(0o600)


def run(package, extra_zip, output):
    """Inventory all example datatypes, then run the deterministic pipeline selection.

    Coverage completion records that cases were attempted. Inspect each case's
    status and the separate validate() results to determine what passed.
    """
    output = Path(output).resolve()
    output.mkdir(mode=0o700)  # A retry must use a new directory.
    examples = read_examples(package, extra_zip)
    index = TypeIndex()
    expected = {k for k, v in index.definitions.items() if v['kind'] == 'resource'} - {'Resource', 'DomainResource'}
    inventory = Counter(r['resourceType'] for r in examples.values())
    unknown = Counter()
    # Scan EVERY official example, including the large conformance collections.
    # This checks resolution coverage, not full FHIR conformance.
    for resource in examples.values():
        for field in index.walk(resource, include_context=False):
            if field.reason in {'unknown_field', 'unknown_resource_type'}:
                unknown[resource['resourceType']] += 1
    selected = [(name, examples[name], 'official') for name in select_examples(examples)]
    for value in supplemental_examples():
        if value['resourceType'] not in inventory:
            selected.append(('invented/' + value['resourceType'], value, 'invented'))
    patient_examples = defaultdict(list)
    for name, resource in examples.items():
        if resource['resourceType'] == 'Patient' and resource.get('id'):
            patient_examples['Patient/' + resource['id']].append((name, resource))
    report = {'status': 'in_progress', 'fhir_version': '4.0.1', 'seed': 7,
              'archives': [{'name': Path(p).name, 'sha256': hashlib.sha256(Path(p).read_bytes()).hexdigest()}
                           for p in (package, extra_zip)],
              'official_inventory': dict(sorted(inventory.items())),
              'missing_official_types': sorted(expected - inventory.keys()),
              'unknown_standard_fields': dict(unknown), 'cases': []}
    for side in ('before', 'after', 'source'):
        (output / side).mkdir(mode=0o700)
    for number, (name, resource, origin) in enumerate(selected, 1):
        case_id = f'{number:04d}'
        case_dir = output / 'cases' / case_id
        inputs = case_dir / 'input'
        inputs.mkdir(parents=True, mode=0o700)
        write_json(inputs / 'resource.json', resource)
        write_json(output / 'source' / f'{case_id}.json', resource)
        # Add only an exact, unique official Patient target already named by the
        # example. No invented patient links or ownership are introduced.
        support = set()
        if resource['resourceType'] not in {'Patient', 'Bundle'}:
            for key in ('subject', 'patient', 'beneficiary'):
                ref = resource.get(key, {})
                candidates = patient_examples.get(ref.get('reference'), []) if isinstance(ref, dict) else []
                if len(candidates) == 1:
                    support.add(candidates[0][0])
            for i, support_name in enumerate(sorted(support)):
                write_json(inputs / f'patient-{i}.json', examples[support_name])
        case = {'case': case_id, 'example': name, 'origin': origin,
                'resource_type': resource['resourceType'], 'support_examples': sorted(support)}
        try:
            ingested = ingest([inputs], case_dir / 'ingested')
            case['ingestion_status'] = ingested['status']
            case['ingestion_issues'] = ingested['issues']
            if ingested['status'] == 'incomplete':
                case['status'] = 'ingestion_rejected'
            else:
                cohort = case_dir / 'ingested/cohort.sqlite'
                result = perturb(cohort, case_dir / 'perturbed', seed=7)
                case['status'] = 'completed'
                case['counts'] = result['counts']
                case['validation'] = result['validation']
                # Pair exactly the roots actually emitted, using source row order.
                # A Bundle's envelope is kept in source/, never fabricated as output.
                with closing(sqlite3.connect(cohort)) as db:
                    stream = iter_export_lines(case_dir / 'perturbed')
                    roots = db.execute('SELECT id,payload FROM resources WHERE contained=0 ORDER BY id')
                    for root, line in zip(roots, stream, strict=True):
                        pair = f'{case_id}-{root[0]}.json'
                        write_json(output / 'before' / pair, loads(root[1]))
                        write_json(output / 'after' / pair, loads(line))
        except Exception as error:
            case['status'] = 'pipeline_failed'
            case['error_type'] = type(error).__name__
            # Keep raw exceptions out of summaries even though these are public cases.
        report['cases'].append(case)
        write_json(output / 'coverage.json', report)
        print(f'{number}/{len(selected)} {resource["resourceType"]}: {case["status"]}', flush=True)
    report['status'] = 'completed'
    report['case_status_counts'] = dict(Counter(c['status'] for c in report['cases']))
    report['missing_test_types'] = sorted(expected - {c['resource_type'] for c in report['cases']})
    write_json(output / 'coverage.json', report)
    return report


def outcome_files(document):
    """Read validator results by their file annotation, never by result order."""
    outcomes = [e['resource'] for e in document.get('entry', [])] if document.get('resourceType') == 'Bundle' else [document]
    result = {}
    for outcome in outcomes:
        if outcome.get('resourceType') != 'OperationOutcome':
            raise ValueError('Expected OperationOutcome results')
        names = [e['valueString'] for e in outcome.get('extension', [])
                 if e.get('url', '').endswith('/operationoutcome-file')]
        if len(names) != 1 or names[0] in result:
            raise ValueError('Missing or duplicated validator file result')
        result[names[0]] = outcome.get('issue', [])
    return result


def issue_key(issue):
    """Match diagnostic categories and paths, excluding changing IDs/values.

    A pre-existing invalid field is still invalid; equal diagnostic counts do
    not certify that resource. Zero errors is necessary; a profile the validator
    could not load also prevents a claim of conformance to that profile.
    Counts, rather than sets, also detect repeated new failures at one path.
    """
    message_ids = [e.get('valueString', e.get('valueCode')) for e in issue.get('extension', [])
                   if e.get('url', '').endswith('/operationoutcome-message-id')]
    # HL7 embeds a resource ID in comments inside some contained-resource paths.
    # Keep the resource type and array position while removing that changing ID.
    paths = [re.sub(r'/\*([A-Z][A-Za-z0-9]*)/[A-Za-z0-9.-]+\*/', r'/*\1*/', p)
             for p in issue.get('expression', [])]
    return dumps([issue.get('severity'), issue.get('code'), paths,
                  message_ids or issue.get('details', {}).get('text', '')])


DEFINITION_TYPES = {'CodeSystem', 'ValueSet', 'ConceptMap', 'StructureDefinition', 'SearchParameter', 'OperationDefinition'}


def validation_sample(output):
    """Keep every clinical root and one of each definition type per case.

    Several official Bundles contain thousands of generated terminology and
    definition resources. They all pass through the pipeline; this deterministic
    conformance sample limits repeated checks without selecting by pass/fail.
    """
    seen = set()
    for path in sorted((output / 'before').glob('*.json')):
        kind = loads(path.read_text())['resourceType']
        key = (path.stem.split('-', 1)[0], kind)
        if kind in DEFINITION_TYPES and key in seen:
            continue
        seen.add(key)
        yield path


def validate(output, validator, java_home):
    """Validate original and output roots with the same offline HL7 process."""
    output, validator, java_home = (Path(p).resolve() for p in (output, validator, java_home))
    destination = output / 'hl7-validation'
    destination.mkdir(mode=0o700)
    result_path = destination / 'outcomes.json'
    coverage = loads((output / 'coverage.json').read_text())
    if coverage['status'] != 'completed':
        raise ValueError('Complete the pipeline coverage run first')
    sample = list(validation_sample(output))
    expected = set()
    for side in ('before', 'after'):
        (destination / side).mkdir(mode=0o700)
        for path in sample:
            target = destination / side / path.name
            shutil.copyfile(output / side / path.name, target)
            target.chmod(0o600)
            expected.add(str(target))
    # Rejected envelopes run separately: an upstream validator exception on
    # malformed input must not discard all completed before/after results.
    rejected = [output / 'source' / (c['case'] + '.json') for c in coverage['cases'] if c['status'] == 'ingestion_rejected']
    runtime = ['java', '-Xmx4g', '-Duser.home=' + str(java_home), '-jar', str(validator)]
    options = ['-version', '4.0.1', '-tx', 'n/a', '-no-http-access', '-disable-default-resource-fetcher',
               '-allow-example-urls', 'true', '-no-internal-caching', '-txCache', str(destination / 'tx-cache')]
    command = runtime + [str(destination / 'before' / '*.json'), str(destination / 'after' / '*.json')] + options + ['-output', str(result_path)]
    write_json(destination / 'command.json', command)
    with (destination / 'validator.log').open('x') as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=3600)
    if not result_path.is_file():
        raise ValueError('Validator did not produce results; inspect the local log')
    outcomes = outcome_files(loads(result_path.read_text()))
    if set(outcomes) != expected:
        raise ValueError('Validator results do not cover exactly the requested files')
    summary = {'status': 'in_progress', 'fhir_version': '4.0.1', 'terminology_server': None,
               'http_access': False, 'validator_sha256': hashlib.sha256(validator.read_bytes()).hexdigest(),
               'validator_exit_code': process.returncode, 'files_validated': len(outcomes),
               'selection': 'One root per definition type per case, plus every other root; selected before validation.',
               'pipeline_roots': len(list((output / 'before').glob('*.json'))),
               'pairs': [], 'rejected_sources': []}
    # All loaded package versions are recorded, including the validator's
    # extension dependencies, so a future run can use the same local cache.
    summary['cached_packages'] = sorted(p.name for p in (java_home / '.fhir/packages').iterdir()
                                        if p.is_dir() and '#' in p.name)
    for before in sorted((destination / 'before').glob('*.json')):
        after = destination / 'after' / before.name
        pre, post = outcomes[str(before)], outcomes[str(after)]
        pre_errors = Counter(issue_key(i) for i in pre if i.get('severity') in {'error', 'fatal'})
        post_errors = Counter(issue_key(i) for i in post if i.get('severity') in {'error', 'fatal'})
        introduced = post_errors - pre_errors
        summary['pairs'].append({'file': before.name, 'resource_type': loads(before.read_text())['resourceType'],
                                 'before_errors': sum(pre_errors.values()), 'after_errors': sum(post_errors.values()),
                                 'new_errors': [{'key': loads(k), 'count': n} for k, n in sorted(introduced.items())],
                                 'before_warnings': sum(i.get('severity') == 'warning' for i in pre),
                                 'after_warnings': sum(i.get('severity') == 'warning' for i in post)})
    summary['counts'] = {'pairs': len(summary['pairs']),
                         'before_valid': sum(p['before_errors'] == 0 for p in summary['pairs']),
                         'after_valid': sum(p['after_errors'] == 0 for p in summary['pairs']),
                         'pairs_with_new_errors': sum(bool(p['new_errors']) for p in summary['pairs'])}
    write_json(destination / 'comparison.json', summary)
    for path in rejected:
        case_output = destination / ('rejected-' + path.name)
        case_command = runtime + [str(path)] + options + ['-output', str(case_output)]
        with (destination / ('rejected-' + path.stem + '.log')).open('x') as log:
            process = subprocess.run(case_command, stdout=log, stderr=subprocess.STDOUT, timeout=300)
        entry = {'file': path.name, 'validator_exit_code': process.returncode}
        if case_output.exists():
            results = outcome_files(loads(case_output.read_text()))
            if set(results) != {str(path)}:
                raise ValueError('Unexpected validator result for a rejected source')
            entry.update(status='validated', errors=sum(i.get('severity') in {'error', 'fatal'} for i in results[str(path)]))
        else:
            entry['status'] = 'validator_failed'
        summary['rejected_sources'].append(entry)
        write_json(destination / 'comparison.json', summary)
    summary['status'] = ('completed_with_validator_failures'
                         if any(r['status'] == 'validator_failed' for r in summary['rejected_sources']) else 'completed')
    write_json(destination / 'comparison.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--examples', type=Path)
    parser.add_argument('--examples-zip', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--validator', type=Path, help='Validate a completed coverage output with this local HL7 jar')
    parser.add_argument('--java-home', type=Path, help='Isolated Java user.home containing the prepared package cache')
    args = parser.parse_args()
    if args.validator:
        if not args.java_home:
            parser.error('--validator requires --java-home')
        validate(args.output, args.validator, args.java_home)
    else:
        if not args.examples or not args.examples_zip:
            parser.error('Pipeline checks require --examples and --examples-zip')
        run(args.examples, args.examples_zip, args.output)
