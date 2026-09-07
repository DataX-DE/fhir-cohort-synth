"""Coordinate local, source-derived FHIR perturbation in explicit phases.

1. Verify inputs and prepare identity maps and patient parameters.
2. Restrict date offsets using every supported date for each patient.
3. Stream changed resources and an exact change ledger.
4. Independently read the output, undo recorded changes and compare original
   resource digests. This checks all preserved content, not just selected fields.
5. Write statistical reports before recording completion.
"""
from collections import defaultdict
from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
from itertools import zip_longest
from pathlib import Path

from .fhir_types import TypeIndex
from .export_files import plan_files, write_source_files
from .ingest import InputError
from .cohort import open_source, source_fingerprint
from .jsonio import dumps, loads
from .perturbation_handlers import DATE_TYPES, Handlers, full_date, label, patient_days, quantity_factor, scale, shift_date
from .perturbation_store import Ledger
from .perturbation_report import build_report, write_report


class PerturbationError(InputError):
    """Diagnostics never include source values."""


ROOTS = 'SELECT id,payload,digest,resource_type FROM resources WHERE contained=0 ORDER BY id'


def validate_settings(strength, date_shift_days, seed):
    """Validate public options once; keep strength as Decimal throughout the run."""
    try:
        if isinstance(strength, bool):
            raise ValueError()
        strength = Decimal(str(strength))
        if not strength.is_finite() or not (strength == 0 or Decimal('0.01') <= strength < 1):
            raise ValueError()
        if type(date_shift_days) is not int or not 0 <= date_shift_days <= date.max.toordinal() - 1:
            raise ValueError()
        if type(seed) is not int:
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise PerturbationError('Use strength 0 or in [0.01,1), a nonnegative supported day range and an integer seed.') from None
    return {'strength': strength, 'date_shift_days': date_shift_days, 'seed': seed}


def _slot(value, path):
    """Find a value by typed path, e.g. (('key', 'contained'), ('index', 0))."""
    for _kind, key in path:
        value = value[key]
    return value


def _slots(db, root_id, resource):
    """Map root/contained paths to their prepared resource-mapping rows.

    The empty path () identifies the root. A path such as
    (('key', 'contained'), ('index', 0)) identifies its first contained resource.
    These are resource boundaries, not the locations of all scalar fields.
    """
    rows = {r['resource_id']: dict(r) for r in db.execute('SELECT * FROM resource_mappings WHERE root_id=?', (root_id,))}
    result = {(): rows[root_id]}
    by_id = {r['old_id']: r for r in rows.values() if r['resource_id'] != root_id}
    seen = set()
    for i, child in enumerate(resource.get('contained', [])):
        row = by_id.get(child.get('id'))
        if row is None or row['resource_id'] in seen or row['resource_type'] != child.get('resourceType'):
            raise PerturbationError('Cannot uniquely associate contained resources with the source index.')
        seen.add(row['resource_id'])
        result[(('key', 'contained'), ('index', i))] = row
    if len(result) != len(rows):
        raise PerturbationError('Contained resource ownership does not match the source index.')
    return result


def _owner(slots, path):
    """Return the resource owning a field; its patient_id may still be absent."""
    return slots.get(path[:2], slots[()])


def _prepare_identities(source, ledger, settings):
    """Allocate replacement IDs and patient date ranges, including forward targets.

    resource_id/root_id/patient_id are ingestion database row IDs. old_id/new_id
    are FHIR strings. A missing patient_id means shared or unassigned ownership;
    such a resource still receives a replacement identity.
    """
    db = ledger.db
    for resource in source.execute(
            'SELECT r.id,r.identity,r.digest,r.resource_type,r.logical_id,r.contained,p.patient_resource_id '
            'FROM resources r LEFT JOIN patient_memberships p ON p.resource_id=r.id ORDER BY r.id'):
        # The ingestion index stores contained identity as "contained:<root row>#<id>".
        # Read that index convention here, never infer ownership from a FHIR ID.
        if resource['contained']:
            root_id = int(resource['identity'].split(':', 1)[1].split('#', 1)[0])
        else:
            root_id = resource['id']
        new_id = 'pert-' + label(settings['seed'], 'resource', [resource['identity'], resource['digest']])[:32]
        db.execute('INSERT INTO resource_mappings VALUES (?,?,?,?,?,?,?,?)',
                   (resource['id'], root_id, resource['patient_resource_id'], resource['resource_type'],
                    resource['logical_id'], new_id, None if resource['contained'] else '[]', resource['digest']))
        if resource['resource_type'] == 'Patient':
            db.execute('INSERT INTO patient_parameters VALUES (?,?,?,?,NULL)',
                       (resource['id'], resource['identity'],
                        -settings['date_shift_days'], settings['date_shift_days']))
        if resource['id'] % 1000 == 0:
            db.commit()
    db.commit()


def _prepare_date_offsets(source, ledger, types, settings):
    """Intersect each patient's allowed day ranges before drawing their one offset.

    For example, a date at year 0001 forbids negative shifts. Restrict the shared
    range instead of clipping individual dates, which would change intervals.
    Only this root's limits are in memory; SQLite combines them across roots.
    """
    db = ledger.db
    db.execute("UPDATE run SET phase='date_bounds'")
    for number, root in enumerate(source.execute(ROOTS), 1):
        resource = loads(root['payload'])
        slots = _slots(db, root['id'], resource)
        for path, owner in slots.items():
            if path:
                db.execute('UPDATE resource_mappings SET path=? WHERE resource_id=?', (dumps(path), owner['resource_id']))
        limits = {}
        # This pass needs datatypes and ownership, not numeric context keys.
        for field in types.walk(resource, include_context=False):
            if field.datatype not in DATE_TYPES or field.reason or isinstance(field.value, (dict, list)):
                continue
            patient = _owner(slots, field.path)['patient_id']
            parsed, _ = full_date(field.value, field.datatype)
            if patient is not None and parsed is not None:
                low, high = 1 - parsed.toordinal(), date.max.toordinal() - parsed.toordinal()
                if patient in limits:
                    low, high = max(low, limits[patient][0]), min(high, limits[patient][1])
                limits[patient] = low, high
        for patient, (low, high) in limits.items():
            db.execute('UPDATE patient_parameters SET minimum_days=max(minimum_days,?),maximum_days=min(maximum_days,?) WHERE patient_id=?',
                       (low, high, patient))
        if number % 1000 == 0:
            db.commit()
    for patient in db.execute('SELECT * FROM patient_parameters ORDER BY patient_id'):
        days = patient_days(settings['seed'], patient['identity'], patient['minimum_days'], patient['maximum_days'])
        db.execute('UPDATE patient_parameters SET days=? WHERE patient_id=?', (days, patient['patient_id']))
    db.commit()


def _reference_path(path):
    """Bridge to ingestion reference paths; never parse these ambiguous strings.

    Traversal uses typed paths. Reference lookup additionally matches the
    literal and owning resource, so a literal key 'a.b' cannot redirect a link
    selected from nested a/b to another reference with the same display path.
    """
    result = ""
    for kind, value in path:
        result += ("." if result else "") + value if kind == "key" else f"[{value}]"
    return result


def _edges(source, slots):
    """Collect reference resolutions for one root, collapsing repeated occurrences.

    Keep a set per (owning resource, path, literal): duplicate appearances of
    the same payload must agree on a single target before we rewrite a link.
    """
    result = defaultdict(set)
    for owner in slots.values():
        for row in source.execute("SELECT path,literal,status,target_resource_id FROM resource_references WHERE source_resource_id=? AND kind='literal'",
                                  (owner['resource_id'],)):
            result[owner['resource_id'], row['path'], row['literal']].add((row['status'], row['target_resource_id']))
    return result


def _record_change(db, root, path, owner, before, after, reason, datatype=None, target=None, old_present=True):
    """Record enough to check and undo this edit; missing is different from null."""
    db.execute('INSERT INTO changes VALUES (?,?,?,?,?,?,?,?,?)',
               (root, dumps(path), owner, int(old_present), dumps(before) if old_present else None,
                dumps(after), reason, datatype, target))


def _rewrite_reference(field, owner, slots, edges, find_target, root_id):
    """Return (value, action, reason, target row ID), or None to keep normal handling.

    Eligibility comes from the datatype, not just the name 'reference'. Match
    the existing graph by owner, relative path and original literal; never pick
    the first of multiple targets. Identity rewriting also applies to indexed
    links inside preserved extensions, but not unsupported embedded resources.
    """
    local_canonical = (field.datatype == 'canonical' and isinstance(field.value, str)
                       and field.value.startswith('#') and len(field.value) > 1)
    literal_reference = (field.key == 'reference' and isinstance(field.value, str)
                         and (field.parent_type == 'Reference' or field.datatype is None))
    if field.reason == 'embedded_resource_preserved' or not (literal_reference or local_canonical):
        return None

    # Contained fields have root-relative traversal paths, whereas ingestion's
    # graph stores paths relative to the contained resource that owns the link.
    owner_path = field.path[:2] if field.path[:2] in slots else ()
    key = (owner['resource_id'], _reference_path(field.path[len(owner_path):]), field.value)
    resolutions = edges.get(key, set())
    if local_canonical and not resolutions:
        # Older indexes did not record canonical # links. Their exact targets
        # still exist in this root's map; do not search other roots or URLs.
        matches = [mapping for path, mapping in slots.items() if path and mapping['old_id'] == field.value[1:]]
        if len(matches) == 1:
            resolutions = {('resolved', matches[0]['resource_id'])}
        else:
            resolutions = {('ambiguous' if matches else 'unresolved', None)}
    if not resolutions:
        return None
    if len(resolutions) != 1:
        return field.value, 'unsupported', 'reference_unresolved_or_ambiguous', None
    status, target_id = next(iter(resolutions))
    if status != 'resolved':
        return field.value, 'unsupported', 'reference_unresolved_or_ambiguous', None

    mapped = find_target(target_id)
    if mapped is None:
        raise PerturbationError('A resolved reference target is missing.')
    if mapped['root_id'] != mapped['resource_id']:
        if mapped['root_id'] != root_id:
            raise PerturbationError('A contained reference crosses resource ownership.')
        replacement = '#' + mapped['new_id']
    else:
        replacement = mapped['resource_type'] + '/' + mapped['new_id']
    return replacement, 'changed', 'reference_rewritten', target_id


def _write(source, ledger, types, settings, destination):
    """Transform one source tree at a time, recording every edit before emission."""
    db = ledger.db
    handlers = Handlers(settings['seed'], settings['strength'])

    @lru_cache(maxsize=4096)
    def date_offset(patient):
        if patient is None:
            return None
        row = db.execute('SELECT days FROM patient_parameters WHERE patient_id=?', (patient,)).fetchone()
        if row is None:
            raise PerturbationError('Patient membership has no matching patient resource.')
        return row[0]

    @lru_cache(maxsize=4096)
    def target(resource_id):
        return db.execute('SELECT * FROM resource_mappings WHERE resource_id=?', (resource_id,)).fetchone()

    with destination.open('x', encoding='utf-8') as stream:
        destination.chmod(0o600)
        for number, root in enumerate(source.execute(ROOTS), 1):
            resource = loads(root['payload'])
            slots = _slots(db, root['id'], resource)
            # Use a stable resource identity, not a database row ID or iteration
            # position, so every field's random draw can be reproduced later.
            root_identity = slots[()]['new_id']
            edges = _edges(source, slots)
            identity_paths = {path + (('key', 'id'),): owner for path, owner in slots.items()}
            # IDs can be absent on non-contained source roots. Add only this
            # required identity slot; record its absence so validation can undo it.
            for path, owner in slots.items():
                obj = _slot(resource, path)
                if 'id' not in obj:
                    obj['id'] = owner['new_id']
                    _record_change(db, root['id'], path + (('key', 'id'),), owner['resource_id'],
                                   None, owner['new_id'], 'resource_id_added', old_present=False)
            for field in types.walk(resource):
                owner = _owner(slots, field.path)
                days = date_offset(owner['patient_id'])
                after, action, reason = handlers.apply(field, root_identity, owner['patient_id'] is not None, days)
                target_id = None
                already_recorded = False
                # Graph identities take precedence over ordinary datatype
                # handling. All other scalar edits come from Handlers.apply().
                if field.path in identity_paths:
                    after = identity_paths[field.path]['new_id']
                    action, reason = 'changed', 'resource_id_replaced'
                    already_recorded = identity_paths[field.path]['old_id'] is None
                    if already_recorded:
                        reason = 'resource_id_added'
                else:
                    rewritten = _rewrite_reference(field, owner, slots, edges, target, root['id'])
                    if rewritten is not None:
                        after, action, reason, target_id = rewritten
                # Compare values before mutating the tree. In Python bool is
                # an int subclass, so exact type checks keep it out of numbers.
                if type(field.value) in {int, Decimal}:
                    ledger.number(root['resource_type'], field, after)
                ledger.action(root['resource_type'], field.path, field.datatype, action, reason)
                if action == 'changed' and not already_recorded:
                    _record_change(db, root['id'], field.path, owner['resource_id'], field.value, after, reason,
                                   field.datatype, target_id)
                    field.parent[field.key] = after
                ledger.maybe_flush()
            stream.write(dumps(resource) + '\n')
            if number % 1000 == 0:
                ledger.flush()
    date_offset.cache_clear()
    target.cache_clear()
    ledger.flush()


def _validate(ledger, destination):
    """Read emitted JSON afresh and prove that only recorded changes occurred.

    Undoing changes in this one output tree must reproduce the original SHA256
    digest, including every untouched category, array, text and unknown field.
    Also check each remapped resource/link, each field's numeric draw and each
    patient's shared date offset.
    This is a transformation check, not the external HL7/profile validator.
    """
    db = ledger.db
    settings = loads(db.execute('SELECT settings_json FROM run').fetchone()[0])
    roots = db.execute('SELECT * FROM resource_mappings WHERE resource_id=root_id ORDER BY resource_id')
    counts = {'roots_checked': 0, 'changes_checked': 0, 'references_checked': 0,
              'dates_checked': 0, 'quantities_checked': 0}
    # Release the active SELECT even on a validation failure, so the failure
    # handler can checkpoint SQLite and preserve the original diagnostic.
    with closing(roots), destination.open(encoding='utf-8') as stream:
        for root, line in zip_longest(roots, stream):
            if root is None or line is None:
                raise PerturbationError('Output resource population does not match the source.')
            value = loads(line)
            for mapping in db.execute('SELECT * FROM resource_mappings WHERE root_id=?', (root['root_id'],)):
                obj = _slot(value, loads(mapping['path']))
                if obj.get('id') != mapping['new_id'] or obj.get('resourceType') != mapping['resource_type']:
                    raise PerturbationError('Output identity or resource type does not match the prepared map.')
            for change in db.execute('SELECT c.*,p.days FROM changes c JOIN resource_mappings m ON m.resource_id=c.owner_id '
                                     'LEFT JOIN patient_parameters p ON p.patient_id=m.patient_id WHERE c.root_id=? ORDER BY c.path',
                                     (root['root_id'],)):
                path = loads(change['path'])
                parent = _slot(value, path[:-1])
                key = path[-1][1]
                if dumps(parent[key]) != change['new_json']:
                    raise PerturbationError('Output differs from its recorded change ledger.')
                before = loads(change['old_json']) if change['old_present'] else None
                after = parent[key]
                if change['reason'] == 'field_quantity_scale':
                    factor = quantity_factor(settings['seed'], root['new_id'], path, settings['strength'])
                    if dumps(scale(before, factor)) != dumps(after):
                        raise PerturbationError('Output quantity does not match its field-specific percentage change.')
                    counts['quantities_checked'] += 1
                elif change['reason'] == 'patient_date_shift':
                    if shift_date(before, change['days']) != after:
                        raise PerturbationError('Output date does not use its shared patient offset.')
                    counts['dates_checked'] += 1
                elif change['reason'] == 'reference_rewritten':
                    target = db.execute('SELECT * FROM resource_mappings WHERE resource_id=?', (change['target_id'],)).fetchone()
                    expected = ('#' if target['root_id'] != target['resource_id'] else target['resource_type'] + '/') + target['new_id']
                    if after != expected or (after.startswith('#') and target['root_id'] != root['root_id']):
                        raise PerturbationError('Output reference target or ownership changed.')
                    counts['references_checked'] += 1
                if change['old_present']:
                    parent[key] = before
                else:
                    del parent[key]
                counts['changes_checked'] += 1
            if hashlib.sha256(dumps(value).encode()).hexdigest() != root['digest']:
                raise PerturbationError('Unrecorded resource content or structure changed.')
            counts['roots_checked'] += 1
    return counts


def perturb(cohort_db, output_dir, *, strength=0.16, date_shift_days=30, seed=42):
    """Create perturbed source-derived records and return the report header.

    The ingestion database must be complete and is opened read-only. We edit
    its original JSON directly and measure the resulting changes.
    The output must be new, and is usable only after its run status completes.
    """
    settings = validate_settings(strength, date_shift_days, seed)
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise PerturbationError('Output already exists; choose a new output directory.')
    types = TypeIndex()
    with open_source(cohort_db) as (source, source_run):
        if source_run['fhir_version'] != '4.0.1':
            raise PerturbationError('Perturbation requires a FHIR R4 4.0.1 source index.')
        fingerprint = source_fingerprint(source)
        files = plan_files(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir(mode=0o700)
        ledger = Ledger(output / 'perturbation-state.sqlite', settings, fingerprint, types.metadata)
        db = ledger.db
        try:
            issues = [dict(r) for r in source.execute('SELECT severity,code,count(*) frequency FROM issues GROUP BY severity,code ORDER BY severity,code')]
            db.executemany('INSERT INTO source_issues VALUES (?,?,?)', ((r['severity'], r['code'], r['frequency']) for r in issues))
            # 1. Allocate every target ID and one date offset per patient.
            _prepare_identities(source, ledger, settings)
            _prepare_date_offsets(source, ledger, types, settings)
            # 2. Write a private partial export and its field-by-field audit trail.
            db.execute("UPDATE run SET phase='writing'")
            db.commit()
            partial = output / '.perturbed.ndjson.partial'
            _write(source, ledger, types, settings, partial)
            # 3. Reread what was actually written before reporting success.
            db.execute("UPDATE run SET phase='validation'")
            db.commit()
            validation = _validate(ledger, partial)
            # Restore file boundaries only after every transformed root passes.
            # The packager rereads each final file and checks its exact bytes.
            db.execute("UPDATE run SET phase='exporting'")
            db.commit()
            export_partial = output / '.fhir.partial'
            exported = write_source_files(source, partial, export_partial, files)
            # 4. Measure the actual changes, including changes lost to rounding.
            db.execute("UPDATE run SET phase='aggregation'")
            db.commit()
            ledger.aggregate()
            header = build_report(ledger, source_run['status'], validation)
            header['export'] = exported
            status = header['status']
            # 5. Publish both files before the final status becomes complete.
            # A crash between renames still leaves an unusable in-progress run.
            db.execute("UPDATE run SET phase='reporting'")
            db.commit()
            report_partial = output / '.perturbation-report.json.partial'
            write_report(report_partial, header, ledger)
            export_partial.rename(output / 'fhir')
            report_partial.rename(output / 'perturbation-report.json')
            partial.unlink()
            db.execute('UPDATE run SET status=?,phase=?', (status, 'complete'))
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
            return header
        except BaseException as error:
            db.rollback()
            state = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
            db.execute('UPDATE run SET status=?,failure_code=?', (state, state))
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
            raise
        finally:
            ledger.close()
