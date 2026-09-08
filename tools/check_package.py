"""Exercise an extracted native executable outside its source checkout.

This is a developer check, not a second hospital launcher. It tests the archive
we actually deliver, with Python removed from PATH and deliberately invalid
external Python paths. Only invented records are used.
"""
from contextlib import closing
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fhir_cohort_synth import __version__
from fhir_cohort_synth.perturbation import perturb


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(executable, cwd, *arguments, expected=0):
    environment = os.environ.copy()
    # os.environ is case-insensitive on Windows; its copied plain dict is not.
    environment['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32') if os.name == 'nt' else '/nonexistent'
    environment['PYTHONHOME'] = environment['PYTHONPATH'] = str(cwd / 'no-external-python')
    environment.pop('VIRTUAL_ENV', None)
    result = subprocess.run([str(executable), *map(str, arguments)], cwd=cwd,
                            env=environment, capture_output=True, text=True, encoding='utf-8', timeout=120)
    if result.returncode != expected:
        raise AssertionError(f'Packaged command failed ({result.returncode}):\n{result.stdout}\n{result.stderr}')
    return result


def check(archive):
    expected_hash = archive.with_name(archive.name + '.sha256').read_text(encoding='ascii').split()[0]
    assert file_hash(archive) == expected_hash, 'Archive checksum mismatch'
    with tempfile.TemporaryDirectory(prefix='FHIR package ü ') as temporary:
        root = Path(temporary)
        # Only our newly built archive is extracted here. tar preserves Linux
        # executable bits and the relative symlinks in PyInstaller's runtime.
        shutil.unpack_archive(archive, root / 'extracted')
        folder, = (root / 'extracted').iterdir()
        executable = folder / ('fhir-cohort-synth.exe' if os.name == 'nt' else 'fhir-cohort-synth')
        cwd = root / 'unrelated working directory'
        cwd.mkdir()
        assert __version__ in run(executable, cwd, '--version').stdout
        assert '--input' in run(executable, cwd, 'run', '--help').stdout
        assert (folder / 'QUICKSTART.txt').is_file()
        assert (folder / 'licenses/PyInstaller.txt').is_file()
        assert not list(folder.rglob('*.sqlite*')), 'A database was included in the package'
        metadata = json.loads((folder / 'build-info.json').read_text(encoding='utf-8'))
        for name, digest in metadata['data_sha256'].items():
            assert file_hash(folder / '_internal/fhir_cohort_synth/data' / name) == digest

        example = folder / 'examples/mii-demo-bundle.json'
        example_hash = file_hash(example)
        output = cwd / 'example output'
        result = run(executable, cwd, 'run', '--input', example, '--output', output)
        assert 'Perturbation: completed' in result.stdout
        assert 'Validating perturbed resources' in result.stderr
        report = json.loads((output / 'result/reports/perturbation-report.json').read_text(encoding='utf-8'))
        assert report['counts']['root_resources'] == 23
        assert report['validation']['references_checked'] == 37
        assert file_hash(example) == example_hash
        run(executable, cwd, 'ingest', '--input', output / 'result/fhir', '--output', cwd / 'reingested')

        # Split the same example into gzip files to exercise forward references,
        # filenames with spaces and Unicode text, not just the single Bundle path.
        inputs = cwd / 'FHIR input ä'
        inputs.mkdir()
        resources = [entry['resource'] for entry in json.loads(example.read_text(encoding='utf-8'))['entry']]
        for patient, name in ((False, 'a events.ndjson.gz'), (True, 'z patients.ndjson.gz')):
            with gzip.open(inputs / name, 'wt', encoding='utf-8') as stream:
                for resource in resources:
                    if (resource['resourceType'] == 'Patient') == patient:
                        stream.write(json.dumps(resource, ensure_ascii=False) + '\n')
        before = {path.name: file_hash(path) for path in inputs.iterdir()}
        frozen = cwd / 'gzip output'
        run(executable, cwd, 'run', '--input', inputs, '--output', frozen)
        # Reuse the frozen run's key through the existing Python API. Matching
        # compressed bytes proves bundling has not changed the transformations.
        replay = cwd / 'API replay'
        perturb(frozen / 'intermediates/cohort.sqlite', replay,
                reuse_key_from=frozen / 'intermediates/perturbation-state.sqlite')
        for path in (frozen / 'result/fhir').iterdir():
            assert path.read_bytes() == (replay / 'result/fhir' / path.name).read_bytes()
        assert before == {path.name: file_hash(path) for path in inputs.iterdir()}
        with closing(sqlite3.connect(frozen / 'intermediates/perturbation-state.sqlite')) as db:
            assert db.execute('SELECT status FROM run').fetchone()[0].startswith('completed')
        assert not list(frozen.rglob('*-wal')) and not list(frozen.rglob('*-shm'))

        # A second invocation must refuse to overwrite existing work, and a bad
        # input path must return the documented error code without a traceback.
        snapshot = {str(path.relative_to(frozen)): file_hash(path) for path in frozen.rglob('*') if path.is_file()}
        error = run(executable, cwd, 'run', '--input', inputs, '--output', frozen, expected=2)
        assert 'Output already exists' in error.stderr
        assert snapshot == {str(path.relative_to(frozen)): file_hash(path) for path in frozen.rglob('*') if path.is_file()}
        error = run(executable, cwd, 'run', '--input', cwd / 'missing.json', '--output', cwd / 'bad-output', expected=2)
        assert 'Cannot perturb:' in error.stderr and 'Traceback' not in error.stderr
        assert not (cwd / 'bad-output').exists()
    print(f'Package checks passed: {archive.name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    check(parser.parse_args().archive.resolve())
