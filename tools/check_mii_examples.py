"""Run public MII examples through the pipeline and compare local FHIR validation.

Development tool only: Python/SQLite for the pipeline, the HL7 Java validator
for profile checks. Downloaded packages and an isolated dependency cache are
supplied explicitly; this tool makes no network requests. See validation-mii.md.
"""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fhir_cohort_synth.ingest import ingest
from fhir_cohort_synth.jsonio import loads
from fhir_cohort_synth.perturbation import perturb
from tools.check_r4_examples import issue_key, outcome_files, write_json

PACKAGES = {'base': '2026.0.0', 'laborbefund': '2026.0.3', 'medikation': '2026.0.1',
            'consent': '2026.0.0', 'icu': '2026.0.2'}


def patient_references(value):
    """Only exact relative Patient references; no interpretation of text fields."""
    if isinstance(value, dict):
        reference = value.get('reference')
        if isinstance(reference, str) and reference.startswith('Patient/') and reference.count('/') == 1:
            yield reference[8:]
        for child in value.values():
            yield from patient_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from patient_references(child)


def resource_roots(resource):
    """Yield the resource population ingestion will index, unpacking Bundle envelopes."""
    if resource.get('resourceType') == 'Bundle':
        for entry in resource.get('entry', []):
            if isinstance(entry.get('resource'), dict):
                yield from resource_roots(entry['resource'])
    else:
        yield resource


def invalid_id(resource):
    """Isolate malformed example IDs without repairing or silently dropping them."""
    return 'id' in resource and (not isinstance(resource['id'], str) or
                                 re.fullmatch(r'[A-Za-z0-9.-]{1,64}', resource['id']) is None)


def unchecked_profile_paths(issues):
    """No errors is insufficient if the validator could not load a profile.

    HL7 reports this as a warning, so keep it separate from error counts. Use
    its diagnostic identifier, not a broad search for 'not found' in messages
    (which would also match ordinary failed cardinality/slicing constraints).
    """
    paths = set()
    for issue in issues:
        ids = [e.get('valueCode', e.get('valueString', '')) for e in issue.get('extension', [])
               if e.get('url', '').endswith('/operationoutcome-message-id')]
        if any(code.startswith('VALIDATION_VAL_PROFILE_UNKNOWN') for code in ids):
            paths.update(issue.get('expression', ['unknown']))
    return sorted(paths)


def run(packages, output):
    """Run the fixed public package examples and save paired original/output roots.

    A completed audit means every case was attempted, not that every example
    passed. Each case retains its own ingestion/pipeline status. Invented
    supporting Patients are labelled separately from official example resources.
    """
    output = Path(output).resolve()
    output.mkdir(mode=0o700)
    report = {'status': 'in_progress', 'fhir_version': '4.0.1',
              'settings': {'strength': '0.02', 'date_shift_days': 30, 'seed': 42},
              'selection': 'Every package/examples/*.json file; standalone files grouped per module; Bundles and invalid root IDs isolated.',
              'support': 'Invented minimal Patients at missing exact Patient reference IDs; official examples unchanged.',
              'packages': [], 'cases': []}
    for module, version in PACKAGES.items():
        path = Path(packages) / f'{module}-{version}.tgz'
        with tarfile.open(path) as archive:
            metadata = loads(archive.extractfile('package/package.json').read().decode('utf-8-sig'))
            if (metadata['name'], metadata['version'], metadata['fhirVersions']) != (
                    'de.medizininformatikinitiative.kerndatensatz.' + module, version, ['4.0.1']):
                raise ValueError('Unexpected package identity or FHIR version')
            examples = [(m.name, loads(archive.extractfile(m).read().decode('utf-8-sig')))
                        for m in archive if m.isfile() and '/examples/' in m.name and m.name.endswith('.json')]
        examples.sort(key=lambda item: item[0])
        report['packages'].append({'module': module, 'name': metadata['name'], 'version': version,
                                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                  'example_files': len(examples), 'dependencies': metadata.get('dependencies', {})})
        # 1. Separate Bundle lookup scopes and malformed-ID cases. One rejected
        # example must not prevent testing the other public files in its module.
        standalone = [(n, r) for n, r in examples if r['resourceType'] != 'Bundle']
        selections = [(module, [(n, r) for n, r in standalone if not invalid_id(r)])]
        invalid = [(n, r) for n, r in standalone if invalid_id(r)]
        if invalid:
            selections.append((module + '-invalid-ids', invalid))
        selections.extend((module + '-bundle-' + str(i), [(n, r)])
                          for i, (n, r) in enumerate(examples) if r['resourceType'] == 'Bundle')
        for case_name, selected in selections:
            case = {'name': case_name, 'module': module, 'version': version,
                    'examples': [n for n, _ in selected], 'status': 'in_progress'}
            report['cases'].append(case)
            directory = output / case_name
            inputs = directory / 'input'
            inputs.mkdir(parents=True, mode=0o700)
            originals = [r for _, r in selected]
            roots = [root for resource in originals for root in resource_roots(resource)]
            existing = {r['id'] for r in roots if r['resourceType'] == 'Patient'}
            missing = sorted(set(patient_references(originals)) - existing)
            case['invented_patient_ids'] = missing
            case['official_roots_before_deduplication'] = len(roots)
            for i, resource in enumerate(originals):
                write_json(inputs / f'official-{i:04d}.json', resource)
            # This is test scaffolding, not a claim these Patients came from MII.
            # Keep every official field/reference unchanged while supplying ownership.
            for i, identifier in enumerate(missing):
                write_json(inputs / f'support-{i:04d}.json', {
                    'resourceType': 'Patient', 'id': identifier, 'active': True,
                    'meta': {'tag': [{'system': 'urn:fhir-cohort-synth:test',
                                     'code': 'invented-support', 'display': 'Invented test Patient'}]}})
            write_json(output / 'coverage.json', report)
            try:
                # 2. Use the same ingestion and perturbation APIs as the hospital CLI. This harness
                # supplies fixtures and records results; it does not repair data.
                ingested = ingest([inputs], directory / 'ingested')
                case['ingestion_status'] = ingested['status']
                case['ingestion_issues'] = ingested['issues']
                if ingested['status'] == 'incomplete':
                    case['status'] = 'ingestion_rejected'
                else:
                    cohort = directory / 'ingested/cohort.sqlite'
                    result = perturb(cohort, directory / 'perturbed', seed=42)
                    case.update(status='completed', counts=result['counts'], validation=result['validation'])
                    for side in ('before', 'after'):
                        (directory / side).mkdir(mode=0o700)
                    case['pairs'] = []
                    # 3. Pair by the pipeline's root order before IDs change.
                    # strict=True catches missing or extra output lines.
                    with closing(sqlite3.connect(cohort)) as db, (directory / 'perturbed/perturbed.ndjson').open() as stream:
                        for row, line in zip(db.execute('SELECT id,payload FROM resources WHERE contained=0 ORDER BY id'), stream, strict=True):
                            before, after = loads(row[1]), loads(line)
                            filename = f'{row[0]:04d}.json'
                            for side, value in [('before', before), ('after', after)]:
                                write_json(directory / side / filename, value)
                            case['pairs'].append({'file': filename, 'resource_type': before['resourceType'],
                                                  'support_fixture': before['resourceType'] == 'Patient' and before['id'] in missing,
                                                  'profiles': before.get('meta', {}).get('profile', [])})
            except Exception as error:
                case.update(status='pipeline_failed', error_type=type(error).__name__)
            print(case_name, case['status'], flush=True)
            write_json(output / 'coverage.json', report)
    report['status'] = 'completed'
    report['case_status_counts'] = dict(Counter(c['status'] for c in report['cases']))
    write_json(output / 'coverage.json', report)
    return report


def validate(output, validator, java_home):
    """Compare offline HL7 diagnostics for the same original and perturbed roots.

    Track new errors separately from errors already in public examples. Also
    track unloaded profiles: a warning-only result may still be unvalidated
    against its declared profile. This Java step is development-only.
    """
    output, validator, java_home = (Path(p).resolve() for p in (output, validator, java_home))
    coverage = loads((output / 'coverage.json').read_text())
    if coverage['status'] != 'completed':
        raise ValueError('Complete the pipeline run first')
    destination = output / 'hl7-validation'
    destination.mkdir(mode=0o700)
    report = {'status': 'in_progress', 'fhir_version': '4.0.1', 'http_access': False,
              'terminology_server': None, 'validator_sha256': hashlib.sha256(validator.read_bytes()).hexdigest(),
              'cases': [], 'pairs': [], 'rejected_sources': []}
    for case in coverage['cases']:
        if case['status'] not in {'completed', 'ingestion_rejected'}:
            continue
        source = output / case['name']
        target = destination / case['name']
        target.mkdir(mode=0o700)
        # Each module loads its own published package and dependencies. Loading
        # every module together would obscure conflicting canonical versions.
        package = 'de.medizininformatikinitiative.kerndatensatz.' + case['module'] + '#' + case['version']
        inputs = ([str(source / 'input/official-*.json')] if case['status'] == 'ingestion_rejected'
                  else [str(source / 'before/*.json'), str(source / 'after/*.json')])
        command = ['java', '-Xmx4g', '-Duser.home=' + str(java_home), '-jar', str(validator)] + inputs + [
                   '-version', '4.0.1', '-ig', package, '-tx', 'n/a', '-no-http-access',
                   '-disable-default-resource-fetcher', '-allow-example-urls', 'true',
                   '-no-internal-caching', '-txCache', str(target / 'tx-cache'),
                   '-output', str(target / 'outcomes.json')]
        write_json(target / 'command.json', command)
        print('Validating', case['name'], flush=True)
        with (target / 'validator.log').open('x') as log:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
        entry = {'name': case['name'], 'validator_exit_code': process.returncode}
        report['cases'].append(entry)
        if not (target / 'outcomes.json').exists():
            entry['status'] = 'validator_failed'
        else:
            outcomes = outcome_files(loads((target / 'outcomes.json').read_text()))
            if case['status'] == 'ingestion_rejected':
                if set(outcomes) != {str(p) for p in (source / 'input').glob('official-*.json')}:
                    raise ValueError('Incomplete rejected-source validation')
                entry['status'] = 'completed'
                for filename, issues in outcomes.items():
                    report['rejected_sources'].append({'case': case['name'], 'file': Path(filename).name,
                        'errors': sum(i.get('severity') in {'error', 'fatal'} for i in issues)})
                write_json(destination / 'comparison.json', report)
                continue
            expected = {str(source / side / p['file']) for p in case['pairs'] for side in ('before', 'after')}
            if set(outcomes) != expected:
                raise ValueError('Incomplete or unexpected validator output')
            entry['status'] = 'completed'
            for pair in case['pairs']:
                before = outcomes[str(source / 'before' / pair['file'])]
                after = outcomes[str(source / 'after' / pair['file'])]
                pre = Counter(issue_key(i) for i in before if i.get('severity') in {'error', 'fatal'})
                post = Counter(issue_key(i) for i in after if i.get('severity') in {'error', 'fatal'})
                # Counter subtraction retains positive increases. An existing
                # error is still reported above, but does not become a new error
                # merely because perturbation replaced the resource's ID.
                report['pairs'].append(dict(pair, case=case['name'], before_errors=sum(pre.values()),
                    after_errors=sum(post.values()),
                    new_errors=[{'key': loads(k), 'count': n} for k, n in sorted((post - pre).items())],
                    before_unchecked_profiles=unchecked_profile_paths(before),
                    after_unchecked_profiles=unchecked_profile_paths(after),
                    before_warnings=sum(i.get('severity') == 'warning' for i in before),
                    after_warnings=sum(i.get('severity') == 'warning' for i in after)))
        write_json(destination / 'comparison.json', report)
    # This status describes execution. Per-pair errors/unchecked profiles below
    # describe conformance; zero newly introduced errors alone is not a pass.
    report['status'] = 'completed' if all(c['status'] == 'completed' for c in report['cases']) else 'completed_with_validator_failures'
    report['cached_packages'] = sorted(p.name for p in (java_home / '.fhir/packages').iterdir() if p.is_dir() and '#' in p.name)
    for population in ('official', 'support'):
        pairs = [p for p in report['pairs'] if p['support_fixture'] == (population == 'support')]
        report[population] = {'pairs': len(pairs), 'before_no_errors': sum(p['before_errors'] == 0 for p in pairs),
                              'after_no_errors': sum(p['after_errors'] == 0 for p in pairs),
                              'pairs_with_new_errors': sum(bool(p['new_errors']) for p in pairs),
                              'pairs_with_unchecked_profiles': sum(bool(p['after_unchecked_profiles']) for p in pairs),
                              'after_no_errors_and_declared_profiles_checked': sum(
                                  p['after_errors'] == 0 and bool(p['profiles']) and not p['after_unchecked_profiles']
                                  for p in pairs)}
    write_json(destination / 'comparison.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--validator', type=Path)
    parser.add_argument('--java-home', type=Path)
    args = parser.parse_args()
    if args.validator:
        if not args.java_home:
            parser.error('--java-home is required for validation')
        validate(args.output, args.validator, args.java_home)
    else:
        if not args.packages:
            parser.error('--packages is required for the pipeline run')
        run(args.packages, args.output)
