"""Exercise the hospital's single command with invented data and real stage APIs."""
from contextlib import closing, redirect_stderr, redirect_stdout
import gzip
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fhir_cohort_synth import workflow
from fhir_cohort_synth.cli import main
from fhir_cohort_synth.ingest import InputError, ingest
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.perturbation import perturb
from fhir_cohort_synth.workflow import run_export


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/mii-demo-bundle.json'


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'run'

    def status(self):
        return loads((self.output / 'run.json').read_text())

    def test_one_command_matches_manual_stages(self):
        before = hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()
        args = ['run', '--input', str(EXAMPLE), '--output', str(self.output)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertEqual(self.status()['phase'], 'complete')
        self.assertEqual({path.name for path in self.output.iterdir()}, {'index', 'perturbed', 'run.json'})
        self.assertEqual(before, hashlib.sha256(EXAMPLE.read_bytes()).hexdigest())

        ingest([EXAMPLE], self.root / 'manual-index')
        perturb(self.root / 'manual-index/cohort.sqlite', self.root / 'manual-output',
                reuse_key_from=self.output / 'perturbed/perturbation-state.sqlite')
        for name in ('fhir/mii-demo-bundle.ndjson', 'perturbation-report.json'):
            self.assertEqual((self.output / 'perturbed' / name).read_bytes(),
                             (self.root / 'manual-output' / name).read_bytes())
        reingested = ingest([self.output / 'perturbed/fhir'], self.root / 'reingested')
        self.assertNotEqual(reingested['status'], 'incomplete')
        self.assertEqual(reingested['counts']['unique_resources'], 23)
        self.assertEqual(reingested['reference_status'], {'resolved': 37})

    def test_directory_gzip_duplicates_forward_links_and_base_url(self):
        inputs = self.root / 'input'
        inputs.mkdir()
        patient = {'resourceType': 'Patient', 'id': 'p'}
        observation = {'resourceType': 'Observation', 'id': 'o', 'status': 'final',
                       'subject': {'reference': 'https://example.invalid/fhir/Patient/p'}}
        with gzip.open(inputs / 'a.ndjson.gz', 'wt') as stream:
            stream.write(dumps(observation) + '\n' + dumps(observation) + '\n')
        patient_file = inputs / 'b.json'
        patient_file.write_text(dumps(patient))
        # An explicit server namespace is an advanced Python API setting.
        run_export([inputs, patient_file], self.output, base_url='https://example.invalid/fhir')
        result = loads((self.output / 'perturbed/perturbation-report.json').read_text())
        self.assertEqual(result['counts']['root_resources'], 2)
        self.assertEqual(result['validation']['references_checked'], 1)
        self.assertEqual(self.status()['status'], 'completed')
        self.assertTrue(any(issue['code'] == 'duplicate_resource' for issue in result['source_issues']))

    def test_invalid_options_paths_and_existing_output_fail_before_processing(self):
        for kwargs in ({'strength': 'NaN'}, {'date_shift_days': -1}, {'strength': '.005'},
                       {'base_url': 'not-a-server'}):
            with self.assertRaises(InputError):
                run_export([EXAMPLE], self.output, **kwargs)
            self.assertFalse(self.output.exists())
        with self.assertRaises(InputError):
            run_export([self.root / 'missing.json'], self.output)
        with self.assertRaises(InputError):
            run_export([self.root], self.output)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        sentinel = self.output / 'existing.txt'
        sentinel.write_text('keep')
        with self.assertRaises(InputError):
            run_export([EXAMPLE], self.output)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertFalse((self.output / 'index').exists())

    def test_symlink_destination_is_rejected(self):
        self.output.symlink_to(self.root / 'not-created')
        with self.assertRaises(InputError):
            run_export([EXAMPLE], self.output)
        self.assertFalse((self.root / 'not-created').exists())

    def test_incomplete_ingestion_stops_before_perturbation(self):
        source = self.root / 'invalid.json'
        source.write_text('{"secret": PRIVATE_SOURCE_VALUE}')
        console = io.StringIO()
        with redirect_stderr(console):
            self.assertEqual(main(['run', '--input', str(source), '--output', str(self.output)]), 2)
        self.assertNotIn('PRIVATE_SOURCE_VALUE', console.getvalue())
        self.assertEqual(self.status(), {'status': 'failed', 'phase': 'ingestion'})
        self.assertFalse((self.output / 'perturbed').exists())
        self.assertEqual(loads((self.output / 'index/report.json').read_text())['status'], 'incomplete')

    def test_interruptions_and_report_errors_never_complete_the_workflow(self):
        for target, error, phase in [
                ('fhir_cohort_synth.ingest.read_source', KeyboardInterrupt(), 'ingestion'),
                ('fhir_cohort_synth.perturbation._write', KeyboardInterrupt(), 'perturbation'),
                ('fhir_cohort_synth.perturbation.write_report', OSError('private'), 'perturbation')]:
            self.output = self.root / target.rsplit('.', 1)[1]
            with patch(target, side_effect=error), self.assertRaises(type(error)):
                run_export([EXAMPLE], self.output)
            expected = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
            self.assertEqual(self.status(), {'status': expected, 'phase': phase})
            with self.assertRaises(InputError):
                run_export([EXAMPLE], self.output)

    def test_final_status_write_failure_does_not_claim_overall_success(self):
        original = workflow._write_status
        def fail_completion(output, status, phase):
            if status.startswith('completed'):
                raise OSError('private')
            original(output, status, phase)
        with patch.object(workflow, '_write_status', side_effect=fail_completion), self.assertRaises(OSError):
            run_export([EXAMPLE], self.output)
        self.assertEqual(self.status()['status'], 'failed')

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions')
    def test_private_outputs_and_portable_completed_databases(self):
        run_export([EXAMPLE], self.output)
        for path in [self.output, *self.output.rglob('*')]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertFalse(list(self.output.rglob('*-wal')))
        self.assertFalse(list(self.output.rglob('*-shm')))
        with closing(sqlite3.connect(self.output / 'perturbed/perturbation-state.sqlite')) as db:
            self.assertIn(db.execute('SELECT status FROM run').fetchone()[0], ('completed', 'completed_with_warnings'))

    def test_cli_masks_unexpected_errors_and_handles_interrupt(self):
        for error, expected in [(RuntimeError('PRIVATE_SOURCE_VALUE'), 2), (KeyboardInterrupt(), 130)]:
            with patch('fhir_cohort_synth.cli.run_export', side_effect=error), redirect_stderr(io.StringIO()) as console:
                self.assertEqual(main(['run', '--input', str(EXAMPLE), '--output', str(self.output)]), expected)
            self.assertNotIn('PRIVATE_SOURCE_VALUE', console.getvalue())


if __name__ == '__main__':
    unittest.main()
