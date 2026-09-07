"""Coordinate local, source-derived FHIR perturbation in explicit phases.

1. Verify inputs and prepare identity maps and patient parameters.
2. Restrict date offsets using every supported date for each patient.
3. Stream changed resources and an exact change ledger.
4. Independently read the output, undo recorded changes and compare original
   resource digests. This checks all preserved content, not just selected fields.
5. Write statistical reports before recording completion.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
from itertools import zip_longest
from pathlib import Path

from .fhir_types import TypeIndex
from .ingest import InputError
from .jsonio import dumps, loads
from .perturbation_handlers import DATE_TYPES, Handlers, full_date, label, patient_days, patient_factor, scale, shift_date
from .perturbation_store import Ledger
from .profiling import open_matching_fields, open_source, pretty_json, source_fingerprint


class PerturbationError(InputError):
    """Diagnostics never include source values."""


ROOTS = 'SELECT id,payload,digest,resource_type FROM resources WHERE contained=0 ORDER BY id'


def _settings(strength, date_shift_days, seed):
    try:
        if isinstance(strength, bool):
            raise ValueError()
        strength = Decimal(str(strength))
        if not strength.is_finite() or not 0 <= strength < 1:
            raise ValueError()
        if type(date_shift_days) is not int or not 0 <= date_shift_days <= date.max.toordinal() - 1:
            raise ValueError()
        if type(seed) is not int:
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise PerturbationError('Use finite strength in [0,1), a nonnegative supported day range and an integer seed.') from None
    return {'strength': strength, 'date_shift_days': date_shift_days, 'seed': seed}


def _slot(value, path):
    for kind, key in path:
        value = value[key]
    return value


def _slots(db, root_id, resource):
    """Locate indexed contained resources within their one original tree."""
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
    return slots.get(path[:2], slots[()])


def _prepare(source, ledger, types, settings):
    db = ledger.db
    for r in source.execute('SELECT r.id,r.identity,r.digest,r.resource_type,r.logical_id,r.contained,p.patient_resource_id '
                            'FROM resources r LEFT JOIN patient_memberships p ON p.resource_id=r.id ORDER BY r.id'):
        root_id = int(r['identity'].split(':', 1)[1].split('#', 1)[0]) if r['contained'] else r['id']
        new_id = 'pert-' + label(settings['seed'], 'resource', [r['identity'], r['digest']])[:32]
        db.execute('INSERT INTO resource_mappings VALUES (?,?,?,?,?,?,?,?)',
                   (r['id'], root_id, r['patient_resource_id'], r['resource_type'], r['logical_id'], new_id,
                    None if r['contained'] else '[]', r['digest']))
        if r['resource_type'] == 'Patient':
            factor = patient_factor(settings['seed'], r['identity'], settings['strength'])
            db.execute('INSERT INTO patient_parameters VALUES (?,?,?,?,?,NULL)',
                       (r['id'], r['identity'], dumps(factor), -settings['date_shift_days'], settings['date_shift_days']))
        if r['id'] % 1000 == 0:
            db.commit()
    db.commit()
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
    """Bridge to schema-1 reference paths; never parse these ambiguous strings.

    Traversal uses typed paths. Reference lookup additionally matches the
    literal and owning resource, so a literal key 'a.b' cannot redirect a link
    selected from nested a/b to another reference with the same display path.
    """
    result = ""
    for kind, value in path:
        result += ("." if result else "") + value if kind == "key" else f"[{value}]"
    return result


def _edges(source, slots):
    result = defaultdict(set)
    for owner in slots.values():
        for row in source.execute("SELECT path,literal,status,target_resource_id FROM resource_references WHERE source_resource_id=? AND kind='literal'",
                                  (owner['resource_id'],)):
            result[owner['resource_id'], row['path'], row['literal']].add((row['status'], row['target_resource_id']))
    return result


def _record_change(db, root, path, owner, before, after, reason, datatype=None, target=None, old_present=True):
    db.execute('INSERT INTO changes VALUES (?,?,?,?,?,?,?,?,?)',
               (root, dumps(path), owner, int(old_present), dumps(before) if old_present else None,
                dumps(after), reason, datatype, target))


def _write(source, ledger, types, settings, destination):
    db = ledger.db
    handlers = Handlers(settings['seed'])

    @lru_cache(maxsize=4096)
    def parameters(patient):
        if patient is None:
            return None, None
        row = db.execute('SELECT factor,days FROM patient_parameters WHERE patient_id=?', (patient,)).fetchone()
        if row is None:
            raise PerturbationError('Patient membership has no matching patient resource.')
        return Decimal(row[0]), row[1]

    @lru_cache(maxsize=4096)
    def target(identity):
        return db.execute('SELECT * FROM resource_mappings WHERE resource_id=?', (identity,)).fetchone()

    with destination.open('x', encoding='utf-8') as stream:
        destination.chmod(0o600)
        for number, root in enumerate(source.execute(ROOTS), 1):
            resource = loads(root['payload'])
            slots = _slots(db, root['id'], resource)
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
                factor, days = parameters(owner['patient_id'])
                after, action, reason = handlers.apply(field, factor, days)
                target_id = None
                already_recorded = False
                if field.path in identity_paths:
                    after = identity_paths[field.path]['new_id']
                    action, reason = 'changed', 'resource_id_replaced'
                    already_recorded = identity_paths[field.path]['old_id'] is None
                    if already_recorded:
                        reason = 'resource_id_added'
                elif field.key == 'reference' and isinstance(field.value, str):
                    owner_path = field.path[:2] if field.path[:2] in slots else ()
                    key = (owner['resource_id'], _reference_path(field.path[len(owner_path):]), field.value)
                    resolutions = edges.get(key, set())
                    if len(resolutions) == 1 and next(iter(resolutions))[0] == 'resolved':
                        target_id = next(iter(resolutions))[1]
                        mapped = target(target_id)
                        if mapped is None:
                            raise PerturbationError('A resolved reference target is missing.')
                        if mapped['root_id'] != mapped['resource_id']:
                            if mapped['root_id'] != root['id']:
                                raise PerturbationError('A contained reference crosses resource ownership.')
                            after = '#' + mapped['new_id']
                        else:
                            after = mapped['resource_type'] + '/' + mapped['new_id']
                        action, reason = 'changed', 'reference_rewritten'
                    elif resolutions:
                        after, action, reason = field.value, 'unsupported', 'reference_unresolved_or_ambiguous'
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
    parameters.cache_clear(); target.cache_clear()
    ledger.flush()


def _validate(ledger, destination):
    """Read emitted JSON afresh and prove that only recorded changes occurred.

    Undoing changes in this one output tree must reproduce the original SHA256
    digest, including every untouched category, array, text and unknown field.
    Also check each remapped resource/link and each shared numeric/date parameter.
    """
    db = ledger.db
    roots = db.execute('SELECT * FROM resource_mappings WHERE resource_id=root_id ORDER BY resource_id')
    counts = {'roots_checked': 0, 'changes_checked': 0, 'references_checked': 0,
              'dates_checked': 0, 'quantities_checked': 0}
    with destination.open(encoding='utf-8') as stream:
        for root, line in zip_longest(roots, stream):
            if root is None or line is None:
                raise PerturbationError('Output resource population does not match the source.')
            value = loads(line)
            for mapping in db.execute('SELECT * FROM resource_mappings WHERE root_id=?', (root['root_id'],)):
                obj = _slot(value, loads(mapping['path']))
                if obj.get('id') != mapping['new_id'] or obj.get('resourceType') != mapping['resource_type']:
                    raise PerturbationError('Output identity or resource type does not match the prepared map.')
            for change in db.execute('SELECT c.*,p.factor,p.days FROM changes c JOIN resource_mappings m ON m.resource_id=c.owner_id '
                                     'LEFT JOIN patient_parameters p ON p.patient_id=m.patient_id WHERE c.root_id=? ORDER BY c.path',
                                     (root['root_id'],)):
                path = loads(change['path'])
                parent = _slot(value, path[:-1]); key = path[-1][1]
                if dumps(parent[key]) != change['new_json']:
                    raise PerturbationError('Output differs from its recorded change ledger.')
                before = loads(change['old_json']) if change['old_present'] else None
                after = parent[key]
                if change['reason'] == 'patient_quantity_scale':
                    if dumps(scale(before, Decimal(change['factor']))) != dumps(after):
                        raise PerturbationError('Output quantity does not use its shared patient factor.')
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


def _report(path, header, ledger):
    with path.open('x', encoding='utf-8') as stream:
        path.chmod(0o600)
        stream.write('{\n')
        for key, value in header.items():
            stream.write(dumps(key) + ':' + pretty_json(value) + ',\n')
        for position, (key, rows) in enumerate([('fields', ledger.report_fields()), ('numeric_contexts', ledger.report_numbers())]):
            stream.write(dumps(key) + ':[')
            separator = '\n'
            for row in rows:
                stream.write(separator + pretty_json(row)); separator = ',\n'
            stream.write(']' + (',\n' if position == 0 else '\n'))
        stream.write('}\n')


def perturb(cohort_db, field_db, output_dir, *, strength=0.02, date_shift_days=30, seed=42):
    """Create perturbed source-derived records and their local audit trail."""
    settings = _settings(strength, date_shift_days, seed)
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise PerturbationError('Output already exists; choose a new output directory.')
    types = TypeIndex()
    with open_source(cohort_db) as (source, source_run, _):
        if source_run['fhir_version'] != '4.0.1':
            raise PerturbationError('Perturbation requires a FHIR R4 4.0.1 source index.')
        with open_matching_fields(field_db, source, source_run):
            fingerprint = source_fingerprint(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.mkdir(mode=0o700)
            ledger = Ledger(output / 'perturbation-state.sqlite', settings, fingerprint, types.metadata)
            db = ledger.db
            try:
                issues = [dict(r) for r in source.execute('SELECT severity,code,count(*) frequency FROM issues GROUP BY severity,code ORDER BY severity,code')]
                db.executemany('INSERT INTO source_issues VALUES (?,?,?)', ((r['severity'], r['code'], r['frequency']) for r in issues))
                _prepare(source, ledger, types, settings)
                db.execute("UPDATE run SET phase='writing'"); db.commit()
                partial = output / '.perturbed.ndjson.partial'
                _write(source, ledger, types, settings, partial)
                db.execute("UPDATE run SET phase='validation'"); db.commit()
                validation = _validate(ledger, partial)
                db.execute("UPDATE run SET phase='aggregation'"); db.commit()
                ledger.aggregate()
                unsupported = db.execute("SELECT coalesce(sum(frequency),0) FROM field_actions WHERE action='unsupported'").fetchone()[0]
                status = 'completed_with_warnings' if unsupported or source_run['status'] == 'completed_with_warnings' else 'completed'
                header = {'schema_version': 1, 'status': status, 'data_classification': 'perturbed_source_derived_data',
                          'privacy_guarantee': False, 'settings': settings, 'source_fingerprint': fingerprint,
                          'datatype_definitions': types.metadata, 'source_issues': issues,
                          'population': 'deduplicated_non_contained_roots_with_contained_subtrees',
                          'resource_types': dict(db.execute('SELECT resource_type,count(*) FROM resource_mappings WHERE resource_id=root_id GROUP BY resource_type ORDER BY resource_type')),
                          'counts': {'root_resources': validation['roots_checked'],
                                     'contained_resources': db.execute('SELECT count(*) FROM resource_mappings WHERE resource_id<>root_id').fetchone()[0],
                                     'patients': db.execute('SELECT count(*) FROM patient_parameters').fetchone()[0],
                                     'field_actions': dict(db.execute('SELECT action,sum(frequency) FROM field_actions GROUP BY action'))},
                          'validation': {**validation, 'preserved_content_and_structure_verified': True,
                                         'consistently_resolved_reference_targets_verified': True,
                                         'changed_dates_use_shared_offsets': True,
                                         'changed_quantities_use_shared_factors': True},
                          'limitations': ['No privacy or anonymization guarantee; output and mappings contain source-derived information.',
                                          'No full FHIR, hospital-profile or clinical dependency validation.',
                                          'Unknown extensions/content and unresolved references may remain unchanged; identity/reference remapping takes precedence.',
                                          'Shared or unassigned resources retain quantities/dates. Partial and invalid dates remain unchanged.',
                                          'Temperatures, percentages, logarithmic or unknown units remain unchanged.',
                                          'Narrative, attachments and non-identity text remain unchanged.',
                                          'Rounding can reduce small changes or alter ratios; zero-relative change is undefined for zero baselines.']}
                db.execute("UPDATE run SET phase='reporting'"); db.commit()
                report_partial = output / '.perturbation-report.json.partial'
                _report(report_partial, header, ledger)
                partial.rename(output / 'perturbed.ndjson')
                report_partial.rename(output / 'perturbation-report.json')
                db.execute('UPDATE run SET status=?,phase=?', (status, 'complete')); db.commit()
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.execute('PRAGMA journal_mode=DELETE')
                return header
            except BaseException as error:
                db.rollback()
                state = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
                db.execute('UPDATE run SET status=?,failure_code=?', (state, state)); db.commit()
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.execute('PRAGMA journal_mode=DELETE')
                raise
            finally:
                ledger.close()
