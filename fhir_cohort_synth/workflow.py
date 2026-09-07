"""The hospital workflow: local export -> ingestion index -> perturbed export.

The two stages retain their own databases and checks. This coordinator only
chooses their directories, stops on ingestion errors and records overall status.
"""
from .ingest import InputError, discover, ingest, normalize_base_url
from .jsonio import dumps
from .perturbation import perturb, validate_settings


def _write_status(output, status, phase):
    """Replace only this run's status file after its new content is fully written."""
    partial = output / '.run.json.partial'
    with partial.open('w', encoding='utf-8') as stream:
        partial.chmod(0o600)
        stream.write(dumps({'status': status, 'phase': phase}) + '\n')
    partial.replace(output / 'run.json')


def run_export(inputs, output_dir, *, strength=0.16, date_shift_days=30, seed=42, base_url=None):
    """Run both stages in a fresh directory and return the perturbation summary.

    index/ contains the original source index and ingestion reports.
    perturbed/ contains the export, local change ledger and coverage report.
    Validate paths/options before creating anything. Later failures retain
    partial work for inspection, and a retry always needs a new destination.
    """
    settings = validate_settings(strength, date_shift_days, seed)
    base_url = normalize_base_url(base_url)
    files, output = discover(inputs, output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    phase = 'ingestion'
    try:
        _write_status(output, 'in_progress', phase)
        source = ingest(files, output / 'index', base_url=base_url)
        if source['status'] not in {'completed', 'completed_with_warnings'}:
            raise InputError('Ingestion reported errors; inspect index/report.json. Use a new output directory to retry.')

        phase = 'perturbation'
        _write_status(output, 'in_progress', phase)
        report = perturb(output / 'index/cohort.sqlite', output / 'perturbed', **settings)
        # perturb() returns only after checking the written records and reports.
        phase = 'complete'
        _write_status(output, report['status'], phase)
        return report
    except BaseException as error:
        status = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
        try:
            _write_status(output, status, phase)
        except OSError:
            # A full disk may also prevent updating the marker. The previous
            # in_progress marker remains unusable; preserve the original error.
            pass
        raise
