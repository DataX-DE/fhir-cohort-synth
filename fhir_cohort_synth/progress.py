"""Console progress without dependencies, extra database reads or source values.

The processing thread supplies stage names and counts. A small display thread
repeats the latest state during long SQLite/file operations; it never accesses
the cohort. Python API calls are silent unless given a progress callback.
"""
import sys
from threading import Event, Lock, Thread
from time import monotonic


def notify(progress, stage, completed=None, total=None, unit=None):
    """Send only a fixed stage label and aggregate counts to an optional callback."""
    if progress is not None:
        progress(stage, completed, total, unit)


def track(items, progress, stage, *, total=None, unit='resources', every=1000):
    """Count completed loop iterations in batches, including the final short batch.

    Increment after yielding: an interrupted item is not reported as completed.
    Unknown totals (e.g. NDJSON lines) become known only at the end of the stream.
    No input is read in advance just to estimate progress.
    """
    if progress is None:
        yield from items
        return
    notify(progress, stage, 0, total, unit)
    completed = 0
    for item in items:
        yield item
        completed += 1
        if completed % every == 0:
            notify(progress, stage, completed, total, unit)
    notify(progress, stage, completed, completed if total is None else total, unit)


class ConsoleProgress:
    """Print stage changes immediately and repeat current progress every 10 seconds.

    Percentages describe this stage, never the whole run. The periodic message
    indicates that the process is alive, not that a blocked operation has advanced.
    The context manager stops the display thread on success, failure or Ctrl+C.
    """

    def __init__(self, stream=None, interval=10):
        self.stream = sys.stderr if stream is None else stream
        self.interval = interval
        self.started = monotonic()
        self.state = None
        self.lock = Lock()
        self.stopped = Event()
        self.thread = Thread(target=self._repeat, name='fhir-progress', daemon=True)

    @property
    def elapsed(self):
        seconds = max(0, int(monotonic() - self.started))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join()

    def __call__(self, stage, completed=None, total=None, unit=None):
        with self.lock:
            if self.stopped.is_set():
                return
            previous = self.state
            self.state = (stage, completed, total, unit)
            changed_stage = previous is None or previous[0] != stage
            finished_stage = total is not None and completed == total and previous != self.state
            if changed_stage or finished_stage:
                self._print()

    def _repeat(self):
        while not self.stopped.wait(self.interval):
            with self.lock:
                if self.state is not None:
                    self._print(still_working=True)

    def _print(self, still_working=False):
        stage, completed, total, unit = self.state
        message = f'[{self.elapsed}] {stage}'
        if completed is not None:
            count = f'{completed:,}' if total is None else f'{completed:,}/{total:,}'
            message += f': {count} {unit}'
            if total:
                message += f' ({100 * completed / total:.1f}%)'
        if still_working:
            message += ' - still working'
        try:
            print(message, file=self.stream, flush=True)
        except (OSError, ValueError):
            # Losing a console/log pipe must not interrupt the export or emit
            # a background traceback. Processing and final validation continue.
            self.stopped.set()
