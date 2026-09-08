"""Protect source-file boundaries independently of the transformation tests."""
from contextlib import closing
import gzip
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fhir_cohort_synth import perturbation as engine
from fhir_cohort_synth.ingest import InputError, ingest
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.perturbation import perturb


class ExportFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.output = self.root / 'output'
        self.cohort = self.root / 'index/cohort.sqlite'

    def write(self, name, values):
        path = self.inputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if name.endswith('.gz') else open
        with opener(path, 'wt', encoding='utf-8') as stream:
            for value in values:
                stream.write(dumps(value) + '\n')

    def prepare(self):
        self.assertNotEqual(ingest([self.inputs], self.cohort.parent)['status'], 'incomplete')

    def test_mimic_style_files_keep_names_compression_order_and_cross_file_links(self):
        observations = [
            {'resourceType': 'Observation', 'id': 'first', 'subject': {'reference': 'Patient/p'},
             'valueString': 'first result'},
            {'resourceType': 'Observation', 'id': 'second', 'subject': {'reference': 'Patient/p'},
             'valueString': 'second result'},
        ]
        self.write('MimicObservationED.ndjson.gz', observations)
        self.write('MimicObservationLabevents.ndjson.gz', [
            {'resourceType': 'Observation', 'id': 'lab', 'subject': {'reference': 'Patient/p'},
             'valueString': 'lab result'}])
        self.write('MimicPatient.ndjson.gz', [{'resourceType': 'Patient', 'id': 'p'}])
        before = {p.name: p.read_bytes() for p in self.inputs.iterdir()}
        self.prepare()
        report = perturb(self.cohort, self.output)
        files = self.output / 'result/fhir'
        self.assertEqual({p.name for p in self.output.iterdir()}, {'intermediates', 'result'})
        self.assertEqual({p.name for p in (self.output / 'intermediates').iterdir()},
                         {'perturbation-state.sqlite'})
        self.assertEqual(report['export']['directory'], 'result/fhir')
        self.assertEqual({p.name for p in files.iterdir()}, set(before))
        self.assertEqual({p.name for p in (self.output / 'result').iterdir()}, {'fhir', 'reports'})
        self.assertEqual({p.name for p in (self.output / 'result/reports').iterdir()}, {'perturbation-report.json'})
        self.assertFalse((self.output / 'perturbed.ndjson').exists())
        self.assertFalse((self.output / 'intermediates/.perturbed.ndjson.partial').exists())
        with gzip.open(files / 'MimicObservationED.ndjson.gz', 'rt') as stream:
            actual = [loads(line) for line in stream]
        self.assertEqual([r['valueString'] for r in actual], ['first result', 'second result'])
        with gzip.open(files / 'MimicPatient.ndjson.gz', 'rt') as stream:
            patient = loads(stream.read())
        self.assertTrue(all(r['subject']['reference'] == 'Patient/' + patient['id'] for r in actual))
        self.assertEqual([p['records'] for p in report['export']['files']], [2, 1, 1])
        for entry in report['export']['files']:
            with gzip.open(files / entry['file'], 'rb') as stream:
                self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), entry['uncompressed_sha256'])
        reingested = ingest([files], self.root / 'reingested')
        self.assertEqual(reingested['reference_status'], {'resolved': 3})
        self.assertEqual(reingested['counts']['unique_resources'], 4)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.inputs.iterdir()})

        second = self.root / 'second'
        perturb(self.cohort, second, reuse_key_from=self.output / 'intermediates/perturbation-state.sqlite')
        for name in before:
            self.assertEqual((files / name).read_bytes(), (second / 'result/fhir' / name).read_bytes())

    def test_nested_paths_and_single_json_keep_their_formats(self):
        self.write('a/patient.json.gz', [{'resourceType': 'Patient', 'id': 'p'}])
        self.write('b/measurements.jsonl', [{'resourceType': 'Observation', 'id': 'o',
                                           'subject': {'reference': 'Patient/p'}}])
        self.prepare()
        report = perturb(self.cohort, self.output)
        self.assertEqual([p['file'] for p in report['export']['files']], ['a/patient.json.gz', 'b/measurements.jsonl'])
        self.assertEqual([p['format'] for p in report['export']['files']], ['json', 'ndjson'])
        with gzip.open(self.output / 'result/fhir/a/patient.json.gz', 'rt') as stream:
            self.assertEqual(loads(stream.read())['resourceType'], 'Patient')

    def test_duplicate_roots_stay_in_their_first_file(self):
        patient = {'resourceType': 'Patient', 'id': 'p'}
        self.write('a.ndjson', [patient, patient])
        self.write('b.ndjson.gz', [patient])
        self.prepare()
        report = perturb(self.cohort, self.output)
        self.assertEqual([p['records'] for p in report['export']['files']], [1, 0])
        with gzip.open(self.output / 'result/fhir/b.ndjson.gz', 'rb') as stream:
            self.assertEqual(stream.read(), b'')

    def test_bundle_roots_have_an_explicit_ndjson_extension(self):
        self.write('bundle.json', [{'resourceType': 'Bundle', 'type': 'collection', 'entry': [
            {'resource': {'resourceType': 'Patient', 'id': 'p'}},
            {'resource': {'resourceType': 'Observation', 'id': 'o', 'subject': {'reference': 'Patient/p'}}},
        ]}])
        self.prepare()
        report = perturb(self.cohort, self.output)
        entry = report['export']['files'][0]
        self.assertEqual((entry['file'], entry['records'], entry['bundle_unpacked']), ('bundle.ndjson', 2, True))

    def test_filename_collision_fails_before_creating_output(self):
        self.write('same.json', [{'resourceType': 'Bundle', 'type': 'collection', 'entry': [
            {'resource': {'resourceType': 'Patient', 'id': 'p'}}]}])
        self.write('same.ndjson', [{'resourceType': 'Patient', 'id': 'q'}])
        self.prepare()
        with self.assertRaisesRegex(InputError, 'same output filename'):
            perturb(self.cohort, self.output)
        self.assertFalse(self.output.exists())

    def test_report_names_in_source_do_not_collide_with_generated_reports(self):
        for number, name in enumerate(('report.json', 'PERTURBATION-REPORT.JSON',
                                      'report.txt/patient.json', 'perturbation-report.json/patient.json')):
            with self.subTest(name=name):
                self.inputs = self.root / f'input-{number}'
                self.inputs.mkdir()
                # A second file fixes the common parent for nested paths.
                self.write('observation.ndjson', [{'resourceType': 'Observation', 'id': 'o'}])
                self.write(name, [{'resourceType': 'Patient', 'id': 'p'}])
                self.cohort = self.root / f'index-{number}/cohort.sqlite'
                self.output = self.root / f'output-{number}'
                self.prepare()
                perturb(self.cohort, self.output)
                resource = loads((self.output / 'result/fhir' / name).read_text())
                self.assertEqual(resource['resourceType'], 'Patient')
                report = loads((self.output / 'result/reports/perturbation-report.json').read_text())
                self.assertEqual(report['counts']['root_resources'], 2)

    def test_interrupted_export_never_publishes_completion(self):
        self.write('patient.ndjson', [{'resourceType': 'Patient', 'id': 'p'}])
        self.prepare()
        original = engine.write_source_files

        def interrupt(*args):
            original(*args)
            raise KeyboardInterrupt()

        with patch.object(engine, 'write_source_files', interrupt), self.assertRaises(KeyboardInterrupt):
            perturb(self.cohort, self.output)
        self.assertFalse((self.output / 'result').exists())
        self.assertEqual({p.name for p in self.output.iterdir()}, {'intermediates'})
        self.assertTrue((self.output / 'intermediates/.result.partial').is_dir())
        self.assertFalse((self.output / 'result/reports/perturbation-report.json').exists())
        with closing(sqlite3.connect(self.output / 'intermediates/perturbation-state.sqlite')) as db:
            self.assertEqual(db.execute('SELECT status FROM run').fetchone()[0], 'interrupted')


if __name__ == '__main__':
    unittest.main()
