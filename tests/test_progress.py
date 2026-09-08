"""Progress is observable during quiet work and never changes generated records."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from fhir_cohort_synth.progress import ConsoleProgress, track
from fhir_cohort_synth.workflow import run_export


class ProgressTests(unittest.TestCase):
    def test_counts_follow_completed_work_and_do_not_estimate_unknown_totals(self):
        events = []
        rows = track(range(3), lambda *event: events.append(event), 'Reading', unit='lines', every=2)
        self.assertEqual(next(rows), 0)
        self.assertEqual(events, [('Reading', 0, None, 'lines')])
        self.assertEqual(next(rows), 1)
        self.assertEqual(next(rows), 2)
        self.assertEqual(events[-1], ('Reading', 2, None, 'lines'))
        self.assertEqual(list(rows), [])
        self.assertEqual(events[-1], ('Reading', 3, 3, 'lines'))
        self.assertEqual(list(track(range(3), None, 'Reading')), [0, 1, 2])

    def test_stage_and_completion_print_immediately_but_counts_are_throttled(self):
        stream = io.StringIO()
        with patch('fhir_cohort_synth.progress.monotonic', return_value=0):
            with ConsoleProgress(stream) as progress:
                progress('Perturbing resources', 0, 2000, 'resources')
                progress('Perturbing resources', 1000, 2000, 'resources')
                self.assertEqual(len(stream.getvalue().splitlines()), 1)
                progress('Perturbing resources', 2000, 2000, 'resources')
                progress('Saving output')
        lines = stream.getvalue().splitlines()
        self.assertIn('2,000/2,000 resources (100.0%)', lines[1])
        self.assertEqual(lines[2], '[00:00:00] Saving output')
        self.assertFalse(progress.thread.is_alive())

    def test_periodic_update_works_without_new_processing_callbacks(self):
        repeated = Event()

        class Stream(io.StringIO):
            def flush(self):
                if 'still working' in self.getvalue():
                    repeated.set()

        stream = Stream()
        with ConsoleProgress(stream, interval=0.01) as progress:
            progress('Checking source snapshot')
            # The event is raised by a real background update, without sleeps
            # or another call from the processing thread.
            self.assertTrue(repeated.wait(2))
        self.assertNotIn('%', stream.getvalue())
        self.assertFalse(progress.thread.is_alive())

    def test_errors_interruptions_and_closed_console_stop_the_display(self):
        for error in (RuntimeError('invented failure'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                stream = io.StringIO()
                with self.assertRaises(type(error)):
                    with ConsoleProgress(stream) as progress:
                        progress('Validating resources')
                        raise error
                self.assertFalse(progress.thread.is_alive())
                self.assertNotIn('completed', stream.getvalue())
        closed = io.StringIO()
        closed.close()
        with ConsoleProgress(closed) as progress:
            progress('Writing resources')
            self.assertTrue(progress.stopped.is_set())

    def test_progress_is_optional_and_preserves_key_reuse_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'PRIVATE-SOURCE-PATH.ndjson'
            source.write_text(
                '{"resourceType":"Patient","id":"PRIVATE-PATIENT-ID",'
                '"name":[{"family":"PRIVATE-FAMILY-NAME"}],"birthDate":"1980-01-01"}\n'
                '{"resourceType":"Observation","id":"PRIVATE-OBSERVATION-ID",'
                '"status":"final","subject":{"reference":"Patient/PRIVATE-PATIENT-ID"},'
                '"valueQuantity":{"value":137.123456,"system":"http://unitsofmeasure.org","code":"kg"}}\n')
            first, second = root / 'silent', root / 'visible'
            with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                run_export([source], first)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))
            events, stream = [], io.StringIO()
            with ConsoleProgress(stream) as progress:
                def record(*event):
                    events.append(event)
                    progress(*event)
                run_export([source], second, progress=record,
                           reuse_key_from=first / 'intermediates/perturbation-state.sqlite')
            self.assertIn(('Perturbing resources', 2, 2, 'resources'), events)
            self.assertIn(('Validating perturbed resources', 2, 2, 'resources'), events)
            self.assertNotIn('PRIVATE-', stream.getvalue())
            self.assertNotIn('137.123456', stream.getvalue())
            for file in (first / 'result').rglob('*'):
                if file.is_file():
                    self.assertEqual(file.read_bytes(), (second / file.relative_to(first)).read_bytes())


if __name__ == '__main__':
    unittest.main()
