"""Public invented fixtures for keyed draws, local reproduction and key handling."""
import base64
from contextlib import closing, redirect_stderr, redirect_stdout
from decimal import Decimal
import io
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import call, patch

from fhir_cohort_synth import perturbation as engine, randomness
from fhir_cohort_synth.cli import main
from fhir_cohort_synth.ingest import InputError, ingest
from fhir_cohort_synth.jsonio import loads
from fhir_cohort_synth.perturbation import perturb
from fhir_cohort_synth.perturbation_handlers import patient_days
from fhir_cohort_synth.workflow import run_export


KEY_A = bytes(range(32))
KEY_B = bytes(reversed(KEY_A))
FIXTURE = '''{"resourceType":"Patient","id":"p","birthDate":"1980-01-01","name":[{"family":"Example","given":["Alex"]}],"identifier":[{"system":"urn:test","value":"123"}]}
{"resourceType":"Observation","id":"o","status":"final","subject":{"reference":"Patient/p"},"effectiveDateTime":"2020-01-01T12:34:56.000Z","code":{"coding":[{"system":"urn:test","code":"weight"}]},"valueQuantity":{"value":100.00000000,"system":"http://unitsofmeasure.org","code":"kg"}}
'''


class RandomnessTests(unittest.TestCase):
    def test_key_generation_uses_32_bytes_of_os_randomness_without_fallback(self):
        with patch.object(randomness.secrets, 'token_bytes', return_value=KEY_A) as entropy:
            self.assertEqual(randomness.new_key(), KEY_A)
        entropy.assert_called_once_with(32)
        with patch.object(randomness.secrets, 'token_bytes', side_effect=OSError('entropy unavailable')):
            with self.assertRaises(OSError):
                randomness.new_key()

    def test_hmac_matches_fixed_message_vector(self):
        # Independent vector for the exact versioned message:
        # ["hmac-sha256-v1","identifier",["urn:test","123"],0]
        expected = 'fce8bb733e64c6d82a824050f5fbdd9c5f8ef86a02dcfec15b9f9d8457e915b0'
        self.assertEqual(randomness.label(KEY_A, 'identifier', ['urn:test', '123']), expected)

    def test_purposes_keys_and_structured_inputs_do_not_collide(self):
        purposes = ['quantity-magnitude', 'quantity-sign', 'date-offset', 'resource',
                    'identifier', 'name-family', 'name-given']
        labels = [randomness.label(KEY_A, purpose, 'same-input') for purpose in purposes]
        self.assertEqual(len(set(labels)), len(purposes))
        self.assertNotEqual(labels[0], randomness.label(KEY_B, purposes[0], 'same-input'))
        self.assertEqual(randomness.digest(KEY_A, 'path', (('key', 'a'), ('index', 0))),
                         randomness.digest(KEY_A, 'path', [['key', 'a'], ['index', 0]]))
        self.assertNotEqual(randomness.digest(KEY_A, 'name', ['ab', 'c']),
                            randomness.digest(KEY_A, 'name', ['a', 'bc']))
        self.assertNotEqual(randomness.digest(KEY_A, 'value', True), randomness.digest(KEY_A, 'value', 1))

    def test_bounded_draw_rejects_modulo_bias_and_retries_with_new_counter(self):
        space = 1 << 256
        rejected = (space - space % 10).to_bytes(32, 'big')
        accepted = (27).to_bytes(32, 'big')
        with patch.object(randomness, 'digest', side_effect=[rejected, accepted]) as digest:
            self.assertEqual(randomness.randbelow(KEY_A, 'date-offset', 'patient', 10), 7)
        self.assertEqual(digest.call_args_list,
                         [call(KEY_A, 'date-offset', 'patient', 0), call(KEY_A, 'date-offset', 'patient', 1)])
        with patch.object(randomness, 'digest', return_value=(space - 1).to_bytes(32, 'big')):
            self.assertEqual(randomness.randbelow(KEY_A, 'quantity-magnitude', 'field', 2**53), 2**53 - 1)
        for invalid in (0, -1, True, space + 1):
            with self.assertRaises(ValueError):
                randomness.randbelow(KEY_A, 'test', [], invalid)
        for invalid in (None, b'short', 'x' * 32):
            with self.assertRaises(ValueError):
                randomness.digest(invalid, 'test', [])


class RunKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.ndjson'
        self.source.write_text(FIXTURE)
        ingest([self.source], self.root / 'index')
        self.cohort = self.root / 'index/cohort.sqlite'

    def run_export(self, name, **options):
        output = self.root / name
        perturb(self.cohort, output, **options)
        return output

    def state(self, output):
        with closing(sqlite3.connect(output / 'intermediates/perturbation-state.sqlite')) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM run').fetchone())

    def records(self, output):
        return [loads(line) for line in (output / 'result/fhir/source.ndjson').read_text().splitlines()]

    def test_fresh_keys_change_all_generated_identity_fields_and_quantity_draws(self):
        with patch.object(randomness.secrets, 'token_bytes', side_effect=[KEY_A, KEY_B]) as entropy:
            first = self.run_export('first')
            second = self.run_export('second')
        self.assertEqual(entropy.call_args_list, [call(32), call(32)])
        self.assertEqual(self.state(first)['run_key'], KEY_A)
        self.assertEqual(self.state(second)['run_key'], KEY_B)
        a, b = self.records(first), self.records(second)
        for field in ('id', 'name', 'identifier'):
            self.assertNotEqual(a[0][field], b[0][field])
        self.assertNotEqual(a[1]['valueQuantity']['value'], b[1]['valueQuantity']['value'])
        for output, records in ((first, a), (second, b)):
            self.assertEqual(records[1]['subject']['reference'], 'Patient/' + records[0]['id'])
            with closing(sqlite3.connect(output / 'intermediates/perturbation-state.sqlite')) as db:
                identity, low, high, days = db.execute('SELECT identity,minimum_days,maximum_days,days FROM patient_parameters').fetchone()
                self.assertEqual(days, patient_days(self.state(output)['run_key'], identity, low, high))

    def test_reuse_reproduces_nondefault_run_without_modifying_inputs(self):
        first = self.run_export('first', strength='.03', date_shift_days=7)
        key_file = first / 'intermediates/perturbation-state.sqlite'
        before = {path: path.read_bytes() for path in (self.cohort, key_file)}
        with patch.object(engine, 'new_key', side_effect=AssertionError('must not generate a new key')):
            second = self.run_export('second', strength=Decimal('.03'), date_shift_days=7, reuse_key_from=key_file)
        for name in ('result/fhir/source.ndjson', 'result/reports/perturbation-report.json'):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(self.state(first)['run_key'], self.state(second)['run_key'])
        with self.assertRaisesRegex(InputError, 'already exists'):
            perturb(self.cohort, second, reuse_key_from=key_file)

    def test_reuse_rejects_missing_corrupt_symlinked_and_keyless_databases(self):
        first = self.run_export('first')
        corrupt = self.root / 'corrupt.sqlite'
        corrupt.write_bytes(b'not a database')
        keyless = self.root / 'old.sqlite'
        with closing(sqlite3.connect(keyless)) as db:
            db.execute('CREATE TABLE run (id INTEGER, schema_version INTEGER)')
            db.execute('INSERT INTO run VALUES (1,2)')
            db.commit()
        alias = self.root / 'alias.sqlite'
        alias.symlink_to(first / 'intermediates/perturbation-state.sqlite')
        for path in (self.root / 'missing.sqlite', corrupt, keyless, alias):
            with self.subTest(path=path.name), patch.object(engine, 'new_key') as generate:
                with self.assertRaises(InputError):
                    self.run_export('invalid', reuse_key_from=path)
                generate.assert_not_called()
                self.assertFalse((self.root / 'invalid').exists())

    def test_reuse_rejects_incompatible_or_damaged_state(self):
        first = self.run_export('first')
        cases = [('schema_version', 2), ('schema_version', 99), ('status', 'in_progress'),
                 ('status', 'failed'), ('status', 'interrupted'), ('randomness_algorithm', 'unknown'),
                 ('run_key', b'short'), ('run_key', 'x' * 32), ('run_key', None),
                 ('source_fingerprint', 'different'), ('definitions_json', '{}'),
                 ('settings_json', '{}'), ('settings_json', '{malformed')]
        for index, (column, value) in enumerate(cases):
            bad = self.root / f'invalid-{index}.sqlite'
            shutil.copyfile(first / 'intermediates/perturbation-state.sqlite', bad)
            with closing(sqlite3.connect(bad)) as db:
                # Deliberately create damaged inputs that the reader must reject.
                if value is None:
                    db.execute('ALTER TABLE run RENAME TO old_run')
                    db.execute('CREATE TABLE run AS SELECT * FROM old_run')
                db.execute('PRAGMA ignore_check_constraints=ON')
                db.execute(f'UPDATE run SET {column}=?', (value,))
                db.commit()
            with self.subTest(column=column, index=index), self.assertRaises(InputError):
                self.run_export('invalid', reuse_key_from=bad)
            self.assertFalse((self.root / 'invalid').exists())
        for options in ({'strength': '.03'}, {'date_shift_days': 7}):
            with self.assertRaisesRegex(InputError, 'strength and date range'):
                self.run_export('invalid', reuse_key_from=first / 'intermediates/perturbation-state.sqlite', **options)

    def test_key_stays_in_committed_local_state_and_never_in_export_or_console(self):
        console = io.StringIO()
        output = self.root / 'cli'
        with patch.object(engine, 'new_key', return_value=KEY_A), redirect_stdout(console), redirect_stderr(console):
            self.assertEqual(main(['perturb', '--input', str(self.cohort), '--output', str(output)]), 0)
        state = self.state(output)
        self.assertEqual((state['schema_version'], state['randomness_algorithm'], state['run_key']),
                         (3, 'hmac-sha256-v1', KEY_A))
        text = console.getvalue()
        for path in output.rglob('*'):
            text += path.name
            if path.is_file() and path.suffix != '.sqlite':
                text += path.read_text()
        for representation in (KEY_A.hex(), base64.b64encode(KEY_A).decode(), repr(KEY_A)):
            self.assertNotIn(representation, text)
        report = loads((output / 'result/reports/perturbation-report.json').read_text())
        self.assertFalse({'run_key', 'settings', 'numeric_contexts'}.intersection(report))

    def test_interruption_retains_committed_key_and_cannot_be_reused(self):
        output = self.root / 'interrupted'

        def interrupt(*args, **kwargs):
            # A separate connection can see the key before any export is written.
            self.assertEqual(self.state(output)['run_key'], KEY_A)
            raise KeyboardInterrupt()

        with patch.object(engine, 'new_key', return_value=KEY_A), patch.object(engine, '_write', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                perturb(self.cohort, output)
        self.assertEqual(self.state(output)['status'], 'interrupted')
        self.assertEqual(self.state(output)['run_key'], KEY_A)
        with self.assertRaisesRegex(InputError, 'completed'):
            self.run_export('retry', reuse_key_from=output / 'intermediates/perturbation-state.sqlite')

    def test_key_generation_failure_is_masked_without_creating_perturbation_output(self):
        output = self.root / 'failed'
        with patch.object(engine, 'new_key', side_effect=RuntimeError(KEY_A.hex())), redirect_stderr(io.StringIO()) as console:
            self.assertEqual(main(['perturb', '--input', str(self.cohort), '--output', str(output)]), 2)
        self.assertNotIn(KEY_A.hex(), console.getvalue())
        self.assertFalse(output.exists())

    def test_validation_reads_the_stored_key_and_rechecks_date_draws(self):
        original = engine._write
        for change, expected in [('key', 'keyed replacement'), ('date', 'date offset')]:
            def corrupt(*args, **kwargs):
                original(*args, **kwargs)
                db = args[1].db
                if change == 'key':
                    db.execute('UPDATE run SET run_key=?', (KEY_B,))
                else:
                    db.execute('UPDATE patient_parameters SET days=999')
                db.commit()
            with patch.object(engine, 'new_key', return_value=KEY_A), patch.object(engine, '_write', side_effect=corrupt):
                with self.assertRaisesRegex(InputError, expected):
                    self.run_export(change, date_shift_days=0)
            self.assertEqual(self.state(self.root / change)['status'], 'failed')

    def test_run_cli_defaults_and_api_reuse_with_failed_workflow_status(self):
        first = self.root / 'full'
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', '--input', str(self.source), '--output', str(first)]), 0)
        settings = loads(self.state(first)['settings_json'])
        self.assertEqual(settings, {'strength': Decimal('.16'), 'date_shift_days': 30})
        second = self.root / 'full-replay'
        run_export([self.source], second, reuse_key_from=first / 'intermediates/perturbation-state.sqlite')
        self.assertEqual((first / 'result/fhir/source.ndjson').read_bytes(),
                         (second / 'result/fhir/source.ndjson').read_bytes())
        failed = self.root / 'full-failed'
        with self.assertRaises(InputError):
            run_export([self.source], failed, reuse_key_from=self.root / 'missing.sqlite')
        self.assertEqual(loads((failed / 'intermediates/run.json').read_text()), {'status': 'failed', 'phase': 'perturbation'})
        self.assertFalse((failed / 'result').exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['perturb', '--input', str(self.cohort), '--output', str(self.root / 'old-cli'), '--seed', '42'])


if __name__ == '__main__':
    unittest.main()
