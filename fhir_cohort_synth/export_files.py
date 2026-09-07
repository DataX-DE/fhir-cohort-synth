"""Place verified resource JSON back into files named after the source export.

The temporary flat stream is an internal validation artifact. Published output
uses source filenames and relative directories, preserving NDJSON/JSONL and gzip.
Each deduplicated resource belongs to its first source occurrence, as before.
Bundle envelopes are not in the index; their resources use one NDJSON file per
source Bundle instead of inventing an envelope.
"""
from contextlib import ExitStack
import gzip
import hashlib
from os.path import commonpath
from pathlib import Path

from .ingest import InputError


# IDs increase as ingestion visits files. The first occurrence says which file
# owns a deduplicated root; a contained child stays inside that root's JSON.
ROOT_SOURCES = """
    SELECT r.id, o.source_id, o.locator
    FROM resources r JOIN occurrences o ON o.id=(
        SELECT min(id) FROM occurrences WHERE resource_id=r.id
    )
    WHERE r.contained=0
"""


def plan_files(source):
    """Choose source-relative output names and count roots without loading them."""
    files = list(source.execute('SELECT id,path FROM sources ORDER BY id'))
    if not files:
        raise InputError('The source index has no file locations for export.')
    base = Path(commonpath([str(Path(row['path']).parent) for row in files]))
    counts = {row['source_id']: row for row in source.execute(
        'SELECT source_id,count(*) AS records,min(locator) AS locator FROM (' +
        ROOT_SOURCES + ') GROUP BY source_id')}
    plans, names = [], set()
    for row in files:
        original = Path(row['path']).relative_to(base)
        if original.is_absolute() or '..' in original.parts:
            raise InputError('A source filename cannot be represented inside the output directory.')
        name = original.as_posix()
        compressed = name.lower().endswith('.gz')
        stem = name[:-3] if compressed else name
        population = counts.get(row['id'])
        records = population['records'] if population is not None else 0
        single_json = stem.lower().endswith('.json') and records == 1 and population['locator'] == '$'
        unpacked = stem.lower().endswith('.json') and not single_json
        if unpacked:
            # A Bundle's roots need a line-delimited file; .json must never
            # misleadingly contain several top-level JSON documents.
            name = stem[:-5] + '.ndjson' + ('.gz' if compressed else '')
        if name.casefold() in names:
            raise InputError('Source files map to the same output filename; use distinct source names.')
        names.add(name.casefold())
        plans.append({'source_id': row['id'], 'source_file': original.as_posix(),
                      'file': name, 'records': records, 'compressed': compressed,
                      'format': 'json' if single_json else 'ndjson',
                      'bundle_unpacked': unpacked})
    return plans


def write_source_files(source, verified_stream, destination, plans):
    """Partition the verified stream, then check each file's decompressed bytes.

    Only one writer is open at a time. Zero-count source files produce empty
    NDJSON files; repeated source occurrences never duplicate transformed roots.
    Hashing the decompressed output proves that packaging changed no JSON values
    or precision after the field-by-field validation of the temporary stream.
    """
    destination.mkdir(mode=0o700)
    roots = iter(source.execute(ROOT_SOURCES + ' ORDER BY r.id'))
    manifest = []
    stream_digest = hashlib.sha256()
    with verified_stream.open('rb') as stream:
        for plan in plans:
            target = destination / plan['file']
            # Create nested directories privately, including on the first file.
            for parent in reversed(target.parents):
                if parent == destination or destination in parent.parents:
                    parent.mkdir(mode=0o700, exist_ok=True)
            digest = hashlib.sha256()
            with ExitStack() as stack:
                raw = stack.enter_context(target.open('xb'))
                target.chmod(0o600)
                writer = raw
                if plan['compressed']:
                    # A fixed header makes key-reuse runs byte-for-byte reproducible.
                    writer = stack.enter_context(gzip.GzipFile(
                        filename='', fileobj=raw, mode='wb', mtime=0, compresslevel=6))
                for _ in range(plan['records']):
                    root = next(roots, None)
                    line = stream.readline()
                    if root is None or root['source_id'] != plan['source_id'] or not line:
                        raise InputError('Verified resources do not match their source file locations.')
                    writer.write(line)
                    digest.update(line)
                    stream_digest.update(line)
            opener = gzip.open if plan['compressed'] else open
            with opener(target, 'rb') as reread:
                if hashlib.file_digest(reread, 'sha256').hexdigest() != digest.hexdigest():
                    raise InputError('An exported file differs from the verified resource stream.')
            entry = {key: value for key, value in plan.items() if key != 'source_id'}
            entry['uncompressed_sha256'] = digest.hexdigest()
            manifest.append(entry)
        if next(roots, None) is not None or stream.read(1):
            raise InputError('The exported resource population differs from the verified stream.')
    return {'layout': 'source_files', 'directory': 'fhir', 'files': manifest,
            'files_checked': len(manifest), 'records': sum(p['records'] for p in plans),
            'decompressed_bytes_verified': True,
            'resource_stream_sha256': stream_digest.hexdigest()}


def iter_export_lines(output):
    """Read published resource lines in ingestion order (for audits and tests)."""
    # Manifest order preserves the same root ordering used by the change ledger.
    from .jsonio import loads

    report = loads((output / 'perturbation-report.json').read_text())
    for entry in report['export']['files']:
        path = output / 'fhir' / entry['file']
        opener = gzip.open if entry['compressed'] else open
        with opener(path, 'rt', encoding='utf-8') as stream:
            yield from stream
