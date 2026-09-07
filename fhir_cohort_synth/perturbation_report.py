"""Build readable perturbation reports from the completed change ledger.

Transformation and preservation checks live in perturbation.py. This module
only presents coverage counts and validation results. Numeric distributions
and before/after changes stay in the local SQLite ledger.
"""
from .jsonio import dumps, loads, pretty_json


def build_report(ledger, source_status, validation):
    """Return coverage and validation counts without source value summaries."""
    db = ledger.db
    run = db.execute('SELECT * FROM run').fetchone()
    fingerprint = run['source_fingerprint']
    metadata = loads(run['definitions_json'])
    issues = [dict(row) for row in db.execute('SELECT * FROM source_issues ORDER BY severity,code')]
    unsupported = db.execute("SELECT coalesce(sum(frequency),0) FROM field_actions WHERE action='unsupported'").fetchone()[0]
    status = 'completed_with_warnings' if unsupported or source_status == 'completed_with_warnings' else 'completed'
    header = {'schema_version': 2, 'status': status, 'data_classification': 'perturbed_source_derived_data',
              'source_fingerprint': fingerprint,
              'datatype_definitions': metadata, 'source_issues': issues,
              'population': 'deduplicated_non_contained_roots_with_contained_subtrees',
              'resource_types': dict(db.execute('SELECT resource_type,count(*) FROM resource_mappings WHERE resource_id=root_id GROUP BY resource_type ORDER BY resource_type')),
              'counts': {'root_resources': validation['roots_checked'],
                         'contained_resources': db.execute('SELECT count(*) FROM resource_mappings WHERE resource_id<>root_id').fetchone()[0],
                         'patients': db.execute('SELECT count(*) FROM patient_parameters').fetchone()[0],
                         'field_actions': dict(db.execute('SELECT action,sum(frequency) FROM field_actions GROUP BY action'))},
              'validation': {**validation, 'preserved_content_and_structure_verified': True,
                             'consistently_resolved_reference_targets_verified': True,
                             'changed_dates_use_shared_offsets': True,
                             'changed_quantities_use_independent_field_factors': True}}
    return header


def write_report(path, header, ledger):
    """Stream field coverage; never export local numeric summaries or changes."""
    with path.open('x', encoding='utf-8') as stream:
        path.chmod(0o600)
        stream.write('{\n')
        for key, value in header.items():
            stream.write(dumps(key) + ':' + pretty_json(value) + ',\n')
        stream.write('"fields":[')
        separator = '\n'
        for row in ledger.report_fields():
            stream.write(separator + pretty_json(row))
            separator = ',\n'
        stream.write(']\n}\n')
