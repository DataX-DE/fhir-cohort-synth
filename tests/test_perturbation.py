"""Invented FHIR data: check transformations, preserved content and failures."""
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import io
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from fhir_cohort_synth.cli import main
from fhir_cohort_synth.fhir_types import TypeIndex
from fhir_cohort_synth.ingest import InputError, ingest
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.perturbation import perturb
from fhir_cohort_synth import perturbation as engine
from fhir_cohort_synth.perturbation_handlers import full_date, scale, shift_date
from fhir_cohort_synth.cohort import open_source
from fhir_cohort_synth.perturbation_store import compare_decimals, numeric_summary


REPO = Path(__file__).resolve().parents[1]
UCUM = 'http://unitsofmeasure.org'


def resource(kind, identity, **fields):
    return {'resourceType': kind, 'id': identity, **fields}


def quantity(value, code='mg/dL', system=UCUM):
    return {'value': value, 'system': system, 'code': code, 'unit': code}


def observation(identity, value, **fields):
    return resource('Observation', identity, status='final', subject={'reference': 'Patient/p'},
                    code={'coding': [{'system': 'urn:invented', 'code': 'measurement'}]},
                    valueQuantity=quantity(value), **fields)


class HandlerTests(unittest.TestCase):
    def test_numeric_report_quantiles_use_decimal_order_and_occurrence_weights(self):
        # 10E-1 is smaller than 1.000...001; float coercion would lose that
        # distinction. Three copies of the latter also determine the median.
        precise = '1.00000000000000000001'
        with closing(sqlite3.connect(':memory:')) as db:
            db.create_collation('DECIMAL', compare_decimals)
            db.execute('CREATE TABLE samples (value TEXT, frequency INTEGER)')
            db.executemany('INSERT INTO samples VALUES (?,?)', [(precise, 3), ('2', 2), ('10', 1), ('10E-1', 1)])
            rows = db.execute('SELECT value,frequency FROM samples ORDER BY value COLLATE DECIMAL,value')
            low, high, quantiles = numeric_summary(rows, 7)
        self.assertEqual((low, high), ('10E-1', '10'))
        self.assertEqual(quantiles, {'p05': Decimal('1'), 'p25': Decimal(precise),
                                    'p50': Decimal(precise), 'p75': 2, 'p95': 10})

    def test_decimal_quantum_half_even_and_large_precision(self):
        self.assertEqual(scale(5, Decimal('1.1')), 6)
        self.assertEqual(scale(15, Decimal('1.1')), 16)
        result = scale(Decimal('1.0000000000000000000000000000000001'), Decimal('1.01'))
        self.assertEqual(result, Decimal('1.0100000000000000000000000000000001'))
        self.assertEqual(result.as_tuple().exponent, -34)
        self.assertEqual(dumps(scale(Decimal('10.00'), Decimal('1'))), '10.00')

    def test_date_validation_and_suffix_preservation(self):
        text = '2020-02-29T23:12:34.001230+05:30'
        self.assertEqual(full_date(text, 'dateTime')[0], date(2020, 2, 29))
        self.assertEqual(shift_date(text, 1), '2020-03-01T23:12:34.001230+05:30')
        for value, kind in [('2020-02-30', 'date'), ('0000', 'date'), ('2020-13', 'date'),
                            ('2020-01-01T24:00:00Z', 'dateTime'), ('2020-01-01T12:00:00+14:01', 'instant'),
                            ('2020-01-01T12:00:00', 'dateTime'), ('2020-01-01', 'instant')]:
            self.assertEqual(full_date(value, kind)[1], 'invalid_date')
        self.assertEqual(full_date('2020-02', 'date')[1], 'partial_date_preserved')

    def test_core_types_choice_backbone_content_reference_and_companion(self):
        index = TypeIndex()
        specimens = [
            (resource('Condition', 'c', onsetDateTime='2020-01-01'), ('onsetDateTime',), 'dateTime'),
            (resource('Procedure', 'p', performedPeriod={'start': '2020-01-01'}), ('performedPeriod', 'start'), 'dateTime'),
            (resource('MedicationAdministration', 'm', dosage={'dose': quantity(10)}), ('dosage', 'dose', 'value'), 'decimal'),
            (resource('QuestionnaireResponse', 'q', item=[{'item': [{'answer': [{'valueDate': '2020-01-01'}]}]}]),
             ('item', 0, 'item', 0, 'answer', 0, 'valueDate'), 'date'),
            (resource('Patient', 'p', _birthDate={'id': 'x'}), ('_birthDate', 'id'), 'string'),
        ]
        for item, path, typ in specimens:
            fields = {tuple(k for _, k in f.path): f for f in index.walk(item)}
            self.assertEqual(fields[path].datatype, typ)
            self.assertIsNone(fields[path].reason)
        self.assertGreaterEqual(len(index.definitions), 200)


class PerturbationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cohort = self.root / 'input/cohort.sqlite'
        self.output = self.root / 'output'

    def prepare(self, resources):
        path = self.root / 'source.ndjson'
        path.write_text('\n'.join(dumps(r) for r in resources) + '\n')
        report = ingest([path], self.cohort.parent)
        self.assertNotEqual(report['status'], 'incomplete')

    def run_engine(self, **kwargs):
        self.header = perturb(self.cohort, self.output, **kwargs)
        self.records = [loads(line) for line in (self.output/'perturbed.ndjson').read_text().splitlines()]
        self.db = sqlite3.connect(self.output/'perturbation-state.sqlite')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.report = loads((self.output/'perturbation-report.json').read_text())
        return self.records

    def factor(self):
        return Decimal(self.db.execute('SELECT factor FROM patient_parameters ORDER BY patient_id').fetchone()[0])

    def test_shared_factor_components_repeats_and_unit_equivalence(self):
        p = resource('Patient', 'p')
        a = observation('a', Decimal('10.00000'), component=[
            {'code': {'coding': [{'system': 's', 'code': 'c'}]}, 'valueQuantity': quantity(Decimal('25.40000'), 'cm')},
            {'code': {'coding': [{'system': 's', 'code': 'c'}]}, 'valueQuantity': quantity(Decimal('10.00000'), '[in_i]')}])
        self.prepare([p, a, observation('b', Decimal('10.00000'))])
        records = self.run_engine()
        factor = self.factor()
        self.assertTrue(Decimal('.98') <= factor <= Decimal('1.02'))
        self.assertEqual(records[1]['valueQuantity']['value'], scale(Decimal('10.00000'), factor))
        self.assertEqual(records[1]['valueQuantity'], records[2]['valueQuantity'])
        cm, inch = [c['valueQuantity']['value'] for c in records[1]['component']]
        self.assertLessEqual(abs(cm - inch * Decimal('2.54')), Decimal('.00002'))
        self.assertEqual(self.header['validation']['quantities_checked'], 4)

    def test_unsupported_units_and_numeric_metadata_remain_unchanged(self):
        resources = [resource('Patient', 'p'),
            observation('temp', Decimal('37.00'), effectiveDateTime='2020-01-01'),
            observation('percent', 100), observation('missing', 10), observation('ph', 7),
            resource('Condition', 'age', subject={'reference': 'Patient/p'}, onsetAge=quantity(50, 'a')),
            resource('Location', 'loc', position={'latitude': Decimal('45.0'), 'longitude': Decimal('1.0')})]
        resources[1]['valueQuantity'] = quantity(Decimal('37.00'), 'Cel')
        resources[2]['valueQuantity'] = quantity(100, '%')
        resources[3]['valueQuantity'] = {'value': 10, 'unit': 'mg/dL'}
        resources[4]['valueQuantity'] = quantity(7, '[pH]')
        self.prepare(resources)
        result = self.run_engine()
        for i in range(1, 5):
            self.assertEqual(resources[i]['valueQuantity'], result[i]['valueQuantity'])
        self.assertEqual(result[5]['onsetAge'], resources[5]['onsetAge'])
        self.assertEqual(result[6]['position'], resources[6]['position'])
        self.assertTrue(self.report['numeric_contexts'])

    def test_mimic_alias_uses_system_and_code_not_display(self):
        a = observation('a', Decimal('100.000'))
        a['valueQuantity'] = quantity(Decimal('100.000'), 'bpm', 'http://mimic.mit.edu/fhir/mimic/CodeSystem/mimic-units')
        b = deepcopy(a); b['id'] = 'b'; b['valueQuantity']['system'] = 'urn:unknown-units'
        self.prepare([resource('Patient', 'p'), a, b])
        result = self.run_engine()
        self.assertEqual(result[1]['valueQuantity']['value'], scale(Decimal('100.000'), self.factor()))
        self.assertEqual(result[2]['valueQuantity'], b['valueQuantity'])

    def test_patient_date_offset_birthdate_period_and_observations(self):
        p = resource('Patient', 'p', birthDate='1980-02-29')
        e = resource('Encounter', 'e', status='finished', **{'class': {'code': 'IMP'}},
                     subject={'reference': 'Patient/p'}, period={'start': '2020-02-28T10:00:00.000+01:00', 'end': '2020-03-02T10:00:00.000+01:00'})
        self.prepare([p, e, observation('o', 100, encounter={'reference': 'Encounter/e'}, effectiveDateTime='2020-02-29T01:23:45.123456Z')])
        result = self.run_engine()
        days = self.db.execute('SELECT days FROM patient_parameters').fetchone()[0]
        self.assertTrue(-30 <= days <= 30)
        self.assertEqual(result[0]['birthDate'], shift_date(p['birthDate'], days))
        for key in ('start', 'end'):
            self.assertEqual(result[1]['period'][key], shift_date(e['period'][key], days))
        self.assertEqual(result[2]['effectiveDateTime'], shift_date('2020-02-29T01:23:45.123456Z', days))

    def test_date_boundaries_share_one_feasible_offset(self):
        self.prepare([resource('Patient', 'p', birthDate='0001-01-01'),
                      observation('o', 10, effectiveDateTime='9999-12-31T23:59:59Z')])
        result = self.run_engine(date_shift_days=1000)
        self.assertEqual(self.db.execute('SELECT days FROM patient_parameters').fetchone()[0], 0)
        self.assertEqual(result[0]['birthDate'], '0001-01-01')
        self.assertEqual(result[1]['effectiveDateTime'], '9999-12-31T23:59:59Z')

    def test_partial_invalid_time_only_dates_preserved(self):
        self.prepare([resource('Patient', 'p', birthDate='1980-02'),
                      observation('a', 10, effectiveDateTime='2020-02-30'),
                      resource('Observation', 'b', subject={'reference': 'Patient/p'}, valueTime='12:00:00')])
        records = self.run_engine()
        self.assertEqual(records[0]['birthDate'], '1980-02')
        self.assertEqual(records[1]['effectiveDateTime'], '2020-02-30')
        self.assertEqual(records[2]['valueTime'], '12:00:00')

    def test_shared_resources_preserve_quantities_and_dates(self):
        device = resource('Device', 'd', manufactureDate='2020-01-01', property=[{
            'type': {'coding': [{'code': 'x'}]}, 'valueQuantity': [quantity(Decimal('12.000'), 'kg')]}])
        self.prepare([resource('Patient', 'p'), device])
        result = self.run_engine()[1]
        self.assertEqual(result['property'], device['property'])
        self.assertEqual(result['manufactureDate'], device['manufactureDate'])
        self.assertNotEqual(result['id'], 'd')

    def test_names_identifiers_and_preserved_clinical_content(self):
        p = resource('Patient', 'p', gender='female', active=True,
                     name=[{'use': 'official', 'family': 'Invented', 'given': ['Test', 'Second'], 'prefix': ['Dr']}],
                     identifier=[{'system': 'urn:local', 'value': 'SENSITIVE-EXAMPLE'}],
                     text={'status': 'generated', 'div': '<div xmlns="http://www.w3.org/1999/xhtml">Original text</div>'})
        e = resource('Encounter', 'e', subject={'reference': 'Patient/p'}, identifier=[{'system': 'urn:local', 'value': 'SENSITIVE-EXAMPLE'}])
        self.prepare([p, e])
        result = self.run_engine()
        self.assertEqual(result[0]['gender'], 'female'); self.assertIs(result[0]['active'], True)
        self.assertEqual(result[0]['name'][0]['use'], 'official')
        self.assertEqual(len(result[0]['name'][0]['given']), 2)
        self.assertTrue(all(v.startswith('Dummy-') for v in result[0]['name'][0]['given']))
        self.assertEqual(result[0]['text'], p['text'])
        self.assertEqual(result[0]['identifier'], result[1]['identifier'])
        self.assertNotIn('SENSITIVE-EXAMPLE', (self.output/'perturbation-report.json').read_text())

    def test_unknown_extensions_companions_attachments_and_path_collisions(self):
        p = resource('Patient', 'p', birthDate='1980-01-01', _birthDate={'extension': [{'url': 'urn:x', 'valueDate': '1980-01-01'}]},
            extension=[{'url': 'urn:y', 'valueQuantity': quantity(Decimal('10.000'))}],
            photo=[{'contentType': 'text/plain', 'data': 'U29tZSB0ZXh0', 'creation': '2020-01-01'}],
            arbitrary={'a.b': [None, {}, [], True, 1, '1']}, **{'name.family': 'literal'})
        self.prepare([p])
        result = self.run_engine()[0]
        for key in ('_birthDate', 'extension', 'photo', 'arbitrary', 'name.family'):
            self.assertEqual(result[key], p[key])

    def test_forward_contained_and_external_references_preserve_targets(self):
        med = resource('Medication', 'm', code={'text': 'invented'})
        a = resource('MedicationAdministration', 'a', subject={'reference': 'Patient/p'},
                     medicationReference={'reference': '#m'}, contained=[med])
        p = resource('Patient', 'p')
        self.prepare([a, p, deepcopy(a)])
        result = self.run_engine()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['subject']['reference'], 'Patient/' + result[1]['id'])
        self.assertEqual(result[0]['medicationReference']['reference'], '#' + result[0]['contained'][0]['id'])
        self.assertEqual(self.header['counts']['contained_resources'], 1)

    def test_literal_and_nested_reference_paths_keep_their_targets(self):
        # The ingestion index uses the same display path for these keys.
        # Matching the source literal as well must retain the two targets.
        o = observation('o', 10, **{'a.b': {'reference': 'Practitioner/one'},
                                  'a': {'b': {'reference': 'Practitioner/two'}}})
        self.prepare([resource('Patient', 'p'), o, resource('Practitioner', 'one'),
                      resource('Practitioner', 'two')])
        result = self.run_engine()
        self.assertEqual(result[1]['a.b']['reference'], 'Practitioner/' + result[2]['id'])
        self.assertEqual(result[1]['a']['b']['reference'], 'Practitioner/' + result[3]['id'])

    def test_reference_named_objects_identifiers_and_uris_are_not_literal_links(self):
        # Official Consent/ImplementationGuide examples exposed the object case.
        # URI-valued fields must also stay unchanged even when their text happens
        # to equal a locally resolvable resource reference.
        self.prepare([
            resource('Patient', 'p'),
            resource('Consent', 'consent', patient={'reference': 'Patient/p'}, provision={
                'actor': [{'reference': {'reference': 'Practitioner/practitioner'}}],
                'data': [{'reference': {'reference': 'Patient/p'}}]}),
            resource('ImplementationGuide', 'ig', fhirVersion=['4.0.1'], definition={
                'resource': [{'reference': {'reference': 'Patient/p'}}]}),
            resource('Claim', 'claim', related=[{'reference': {'system': 'urn:invented', 'value': 'claim-1'}}]),
            resource('DetectedIssue', 'issue', reference='Patient/p'),
            resource('Immunization', 'immunization', patient={'reference': 'Patient/p'},
                     education=[{'reference': 'Patient/p'}]),
            resource('PlanDefinition', 'plan', action=[{'condition': [{'expression': {
                'language': 'text/fhirpath', 'reference': 'Patient/p'}}]}]),
            resource('Practitioner', 'practitioner')])
        result = self.run_engine()
        self.assertEqual(result[1]['provision']['actor'][0]['reference']['reference'],
                         'Practitioner/' + result[7]['id'])
        self.assertEqual(result[2]['definition']['resource'][0]['reference']['reference'],
                         'Patient/' + result[0]['id'])
        self.assertTrue(result[3]['related'][0]['reference']['value'].startswith('pert-'))
        self.assertEqual(result[4]['reference'], 'Patient/p')
        self.assertEqual(result[5]['education'][0]['reference'], 'Patient/p')
        self.assertEqual(result[6]['action'][0]['condition'][0]['expression']['reference'], 'Patient/p')
        with closing(sqlite3.connect(self.cohort)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM resource_references').fetchone()[0], 5)

    def test_parameters_inline_resources_are_explicitly_preserved(self):
        # Inline Resources need a separate ownership/reference scope, which the
        # current index does not provide. Never partially transform their names
        # while leaving their own identities and links unmanaged.
        inline = resource('Patient', 'inline', name=[{'family': 'Invented'}], birthDate='1980-01-01',
                          managingOrganization={'reference': 'Organization/org'})
        self.prepare([resource('Parameters', 'parameters', parameter=[{'name': 'patient', 'resource': inline}]),
                      resource('Organization', 'org')])
        result = self.run_engine()
        self.assertEqual(result[0]['parameter'][0]['resource'], inline)
        self.assertNotEqual(result[0]['id'], 'parameters')
        self.assertGreater(self.db.execute("SELECT sum(frequency) FROM field_actions WHERE reason='embedded_resource_preserved'").fetchone()[0], 0)
        with closing(sqlite3.connect(self.cohort)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM resource_references').fetchone()[0], 0)

    def test_inline_resource_inside_contained_parameters_is_also_preserved(self):
        inline = resource('Patient', 'inline', name=[{'family': 'Invented'}], birthDate='1980-01-01')
        parameters = resource('Parameters', 'params', parameter=[{'name': 'patient', 'resource': inline}])
        self.prepare([resource('Patient', 'p', contained=[parameters], extension=[{
            'url': 'urn:invented', 'valueReference': {'reference': '#params'}}])])
        result = self.run_engine()[0]
        self.assertEqual(result['contained'][0]['parameter'][0]['resource'], inline)
        self.assertNotEqual(result['contained'][0]['id'], 'params')

    def test_legacy_index_uri_edge_cannot_rewrite_a_non_reference(self):
        self.prepare([resource('Patient', 'p'), resource('DetectedIssue', 'issue', reference='Patient/p')])
        # Schema-1 databases made by older ingestion could contain this false
        # edge. Perturbation must independently check the actual field datatype.
        with closing(sqlite3.connect(self.cohort)) as db:
            db.execute("INSERT INTO resource_references(occurrence_id,source_resource_id,path,literal,kind,target_resource_id,status) "
                       "VALUES (2,2,'reference','Patient/p','literal',1,'resolved')")
            db.commit()
        result = self.run_engine()
        self.assertEqual(result[1]['reference'], 'Patient/p')
        self.assertEqual(self.header['validation']['references_checked'], 0)

    def test_ietf_identifiers_remain_complete_uris_and_share_replacements(self):
        identifiers = [{'system': 'urn:ietf:rfc:3986', 'value': value} for value in
                       ('urn:oid:1.2.3.4', 'urn:uuid:550e8400-e29b-41d4-a716-446655440000', 'https://example.invalid/id/1')]
        self.prepare([resource('Patient', 'p', identifier=identifiers),
                      resource('Observation', 'o', identifier=deepcopy(identifiers))])
        result = self.run_engine()
        self.assertEqual(result[0]['identifier'], result[1]['identifier'])
        oid, uuid, url = [i['value'] for i in result[0]['identifier']]
        self.assertRegex(oid, r'^urn:oid:2\.25\.\d+$')
        for value in (uuid, url):
            self.assertTrue(value.startswith('urn:uuid:'))
            self.assertEqual(UUID(value.removeprefix('urn:uuid:')).version, 4)
        self.assertTrue(all(i['system'] == 'urn:ietf:rfc:3986' for i in result[0]['identifier']))

    def test_contained_canonical_links_and_arrays_are_rewritten(self):
        self.prepare([resource('Questionnaire', 'q', contained=[resource('ValueSet', 'vs')],
                               item=[{'linkId': '1', 'type': 'choice', 'answerValueSet': '#vs'}]),
                      resource('PlanDefinition', 'p', contained=[resource('Library', 'lib')],
                               library=['#lib', 'https://example.invalid/Library/unchanged'])])
        result = self.run_engine()
        self.assertEqual(result[0]['item'][0]['answerValueSet'], '#' + result[0]['contained'][0]['id'])
        self.assertEqual(result[1]['library'][0], '#' + result[1]['contained'][0]['id'])
        self.assertEqual(result[1]['library'][1], 'https://example.invalid/Library/unchanged')
        self.assertEqual(self.header['validation']['references_checked'], 2)
        with closing(sqlite3.connect(self.cohort)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM resource_references WHERE status='resolved'").fetchone()[0], 2)

    def test_legacy_canonical_links_use_exact_contained_ownership(self):
        self.prepare([resource('Questionnaire', 'q', contained=[resource('ValueSet', 'vs')],
                               item=[{'linkId': '1', 'type': 'choice', 'answerValueSet': '#vs'},
                                     {'linkId': '2', 'type': 'choice', 'answerValueSet': '#missing'}]),
                      resource('ValueSet', 'missing')])
        with closing(sqlite3.connect(self.cohort)) as db:
            db.execute('DELETE FROM resource_references')
            db.commit()
        result = self.run_engine()
        self.assertEqual(result[0]['item'][0]['answerValueSet'], '#' + result[0]['contained'][0]['id'])
        self.assertEqual(result[0]['item'][1]['answerValueSet'], '#missing')
        self.assertEqual(self.header['validation']['references_checked'], 1)

    def test_repeated_occurrences_with_conflicting_reference_contexts_preserved(self):
        def bundle(pid, gender):
            return {'resourceType': 'Bundle', 'type': 'collection', 'entry': [
                {'fullUrl': 'https://' + pid + '.invalid/Practitioner/p', 'resource': resource('Practitioner', 'p', gender=gender)},
                {'fullUrl': 'urn:uuid:shared', 'resource': resource('Observation', 'o', performer=[{'reference': 'Practitioner/p'}])}]}
        self.prepare([bundle('a', 'female'), bundle('b', 'male')])
        result = self.run_engine()
        o = next(r for r in result if r['resourceType'] == 'Observation')
        self.assertEqual(o['performer'][0]['reference'], 'Practitioner/p')
        self.assertEqual(self.header['status'], 'completed_with_warnings')

    def test_unresolved_and_logical_references_are_not_guessed(self):
        a = observation('a', 10, performer=[{'reference': 'Practitioner/missing'},
            {'identifier': {'system': 'urn:staff', 'value': 'example'}}])
        self.prepare([resource('Patient', 'p'), a])
        r = self.run_engine()[1]
        self.assertEqual(r['performer'][0]['reference'], 'Practitioner/missing')
        self.assertTrue(r['performer'][1]['identifier']['value'].startswith('pert-'))

    def test_missing_root_id_fullurl_reference(self):
        p = resource('Patient', 'p'); del p['id']
        o = resource('Observation', 'o', subject={'reference': 'urn:uuid:patient'})
        self.prepare([{'resourceType': 'Bundle', 'type': 'collection', 'entry': [
            {'fullUrl': 'urn:uuid:patient', 'resource': p}, {'resource': o}]}])
        result = self.run_engine()
        self.assertEqual(result[1]['subject']['reference'], 'Patient/' + result[0]['id'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM changes WHERE reason='resource_id_added'").fetchone()[0], 1)

    def test_absolute_versioned_reference_keeps_its_target(self):
        p = resource('Patient', 'p', meta={'versionId': '7'})
        o = resource('Observation', 'o', subject={'reference': 'https://example.invalid/fhir/Patient/p/_history/7'})
        self.prepare([{'resourceType': 'Bundle', 'type': 'collection', 'entry': [
            {'resource': o}, {'fullUrl': 'https://example.invalid/fhir/Patient/p', 'resource': p}]}])
        result = self.run_engine()
        self.assertEqual(result[0]['subject']['reference'], 'Patient/' + result[1]['id'])
        self.assertEqual(result[1]['meta']['versionId'], '7')
        self.assertEqual(self.header['validation']['references_checked'], 1)

    def test_empty_names_identifiers_and_nulls_keep_their_structure(self):
        p = resource('Patient', 'p', name=[{'text': '', 'given': ['', None], 'family': ''}],
                     identifier=[{'value': ''}, {}], birthDate=None)
        self.prepare([p])
        result = self.run_engine()[0]
        for key in ('name', 'identifier', 'birthDate'):
            self.assertEqual(result[key], p[key])

    def test_exact_numeric_counts_contexts_quantiles_and_zero_baseline(self):
        self.prepare([resource('Patient', 'p')] + [observation(str(i), Decimal(v)) for i, v in enumerate(['0.00','10.00','10.00','20.00'])])
        self.run_engine()
        contexts = self.report['numeric_contexts']
        self.assertEqual(len(contexts), 1)
        c = contexts[0]
        self.assertEqual(c['samples'], 4); self.assertEqual(c['zero_baselines'], 1)
        self.assertEqual(c['statistics']['before']['quantiles']['p50'], 10)
        self.assertEqual(c['statistics']['after']['quantiles']['p50'], scale(Decimal('10.00'), self.factor()))
        self.assertEqual(c['statistics']['relative']['sample_count'], 3)

    def test_high_cardinality_and_distinct_measurement_contexts(self):
        obs = [observation(str(i), Decimal(f'{i}.000000')) for i in range(160)]
        obs[-1]['code']['coding'][0]['code'] = 'different'
        self.prepare([resource('Patient', 'p')] + obs)
        self.run_engine()
        self.assertEqual(len(self.report['numeric_contexts']), 2)
        self.assertEqual(self.db.execute("SELECT sum(frequency) FROM numeric_frequencies WHERE phase='after'").fetchone()[0], 160)
        self.assertEqual(sum(c['samples'] for c in self.report['numeric_contexts']), 160)

    def test_medication_doses_and_panel_components_keep_distinct_contexts(self):
        resources = [resource('Patient', 'p')]
        for i in range(2):
            resources.append(resource('Medication', 'm' + str(i)))
            resources.append(resource('MedicationAdministration', 'a' + str(i),
                subject={'reference': 'Patient/p'}, medicationReference={'reference': 'Medication/m' + str(i)},
                dosage={'dose': quantity(Decimal('10.000'), 'mg')}))
            resources.append(resource('Observation', 'o' + str(i), subject={'reference': 'Patient/p'},
                code={'coding': [{'code': 'panel' + str(i)}]}, component=[{
                    'code': {'coding': [{'code': 'same-component'}]}, 'valueQuantity': quantity(Decimal('10.000'))}]))
        self.prepare(resources)
        self.run_engine()
        self.assertEqual(len(self.report['numeric_contexts']), 4)
        self.assertTrue(all(c['samples'] == 1 for c in self.report['numeric_contexts']))

    def test_deterministic_inputs_unchanged_and_private_outputs(self):
        self.prepare([resource('Patient', 'p', birthDate='1980-01-01'), observation('o', Decimal('10.000'))])
        before = hashlib.sha256(self.cohort.read_bytes()).hexdigest()
        self.run_engine()
        second = self.root/'second'
        perturb(self.cohort, second)
        self.assertEqual((self.output/'perturbed.ndjson').read_bytes(), (second/'perturbed.ndjson').read_bytes())
        self.assertEqual(before, hashlib.sha256(self.cohort.read_bytes()).hexdigest())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for name in ('perturbed.ndjson', 'perturbation-state.sqlite', 'perturbation-report.json'):
            self.assertEqual((self.output/name).stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.output.glob('*-wal')))

    def test_zero_strength_and_date_range_still_replace_identity(self):
        p = resource('Patient', 'p', birthDate='1980-01-01')
        self.prepare([p, observation('o', Decimal('10.000'))])
        result = self.run_engine(strength=0, date_shift_days=0)
        self.assertEqual(result[0]['birthDate'], p['birthDate'])
        self.assertNotEqual(result[0]['id'], p['id'])
        self.assertEqual(result[1]['valueQuantity']['value'], Decimal('10.000'))

    def test_copied_source_is_accepted_without_modification(self):
        self.prepare([resource('Patient', 'p'), observation('o', Decimal('10.000'))])
        copied = self.root/'copied-cohort.sqlite'
        shutil.copyfile(self.cohort, copied)
        before = hashlib.sha256(copied.read_bytes()).hexdigest()
        report = perturb(copied, self.output)
        self.assertEqual(report['counts']['root_resources'], 2)
        self.assertEqual(before, hashlib.sha256(copied.read_bytes()).hexdigest())

    def test_missing_reference_graph_is_rejected_before_output(self):
        self.prepare([resource('Patient', 'p')])
        with closing(sqlite3.connect(self.cohort)) as db, db:
            db.execute('DROP TABLE resource_references')
        with self.assertRaises(InputError):
            perturb(self.cohort, self.output)
        self.assertFalse(self.output.exists())

    def test_source_connection_rejects_writes(self):
        self.prepare([resource('Patient', 'p')])
        before = self.cohort.read_bytes()
        with open_source(self.cohort) as (db, run):
            self.assertEqual(run['schema_version'], 2)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE run SET status='in_progress'")
        self.assertEqual(self.cohort.read_bytes(), before)

    def test_legacy_index_matches_new_output_without_modification(self):
        # Use the frozen, complete schema 1 rather than changing a version
        # label on a schema 2 database. Extra inventory columns/tables must
        # have no influence on patient parameters, references or measurements.
        self.prepare([
            observation('o', Decimal('4.20'), encounter={'reference': 'Encounter/e'},
                        contained=[resource('Specimen', 'inside')], specimen={'reference': '#inside'}),
            resource('Encounter', 'e', subject={'reference': 'Patient/p'},
                     period={'start': '2020-01-01', 'end': '2020-01-03'}),
            resource('Patient', 'p', birthDate='1980-01-01'),
        ])
        legacy_path = self.root / 'legacy.sqlite'
        with closing(sqlite3.connect(legacy_path)) as legacy, \
                closing(sqlite3.connect(self.cohort.as_uri() + '?mode=ro', uri=True)) as source:
            legacy.executescript((REPO / 'tests/fixtures/ingestion-v1.sql').read_text())
            legacy.execute('DELETE FROM run')
            tables = ('run', 'sources', 'resources', 'occurrences', 'aliases',
                      'resource_references', 'patient_memberships', 'issues')
            for table in tables:
                columns = [row[1] for row in source.execute(f'PRAGMA table_info({table})')]
                rows = source.execute(f'SELECT * FROM {table} ORDER BY rowid')
                if table == 'resources':
                    columns.append('scope')
                    rows = (tuple(row) + ('requested',) for row in rows)
                placeholders = ','.join('?' for _ in columns)
                legacy.executemany(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({placeholders})', rows)
            legacy.execute('UPDATE run SET schema_version=1')
            legacy.commit()
            legacy.execute('PRAGMA journal_mode=DELETE')

        before = legacy_path.read_bytes()
        with open_source(legacy_path) as (_, run):
            self.assertEqual(run['schema_version'], 1)
        perturb(legacy_path, self.root / 'legacy-output')
        perturb(self.cohort, self.output)
        for name in ('perturbed.ndjson', 'perturbation-report.json'):
            self.assertEqual((self.root / 'legacy-output' / name).read_bytes(),
                             (self.output / name).read_bytes())
        self.assertEqual(legacy_path.read_bytes(), before)

    def test_missing_corrupt_and_symlinked_source_indexes_are_rejected(self):
        missing = self.root / 'missing.sqlite'
        corrupt = self.root / 'corrupt.sqlite'
        corrupt.write_bytes(b'not a database')
        self.prepare([resource('Patient', 'p')])
        alias = self.root / 'alias.sqlite'
        alias.symlink_to(self.cohort)
        for source in (missing, corrupt, alias):
            with self.subTest(source=source.name), self.assertRaises(InputError):
                perturb(source, self.output)
            self.assertFalse(self.output.exists())

    def test_source_errors_and_empty_population_override_a_completed_status(self):
        self.prepare([resource('Patient', 'p')])
        for query in ("INSERT INTO issues(severity,code) VALUES ('error','test_error')", 'DELETE FROM resources'):
            copied = self.root / 'invalid.sqlite'
            shutil.copyfile(self.cohort, copied)
            with closing(sqlite3.connect(copied)) as db, db:
                db.execute(query)
            with self.assertRaises(InputError):
                perturb(copied, self.output)
            self.assertFalse(self.output.exists())

    def test_no_overwrite_and_invalid_parameters(self):
        self.prepare([resource('Patient', 'p')])
        for kwargs in ({'strength': 'NaN'}, {'strength': 1}, {'strength': -1}, {'strength': True},
                       {'date_shift_days': -1}, {'date_shift_days': True}, {'seed': 1.5}):
            with self.assertRaises(InputError):
                perturb(self.cohort, self.output, **kwargs)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        with self.assertRaises(InputError):
            perturb(self.cohort, self.output)

    def test_incomplete_and_unsupported_indexes_rejected(self):
        self.prepare([resource('Patient', 'p')])
        for status, version, fhir in [('in_progress', 1, '4.0.1'), ('completed_with_warnings', 99, '4.0.1'),
                                      ('completed_with_warnings', 1, '5.0.0')]:
            with closing(sqlite3.connect(self.cohort)) as db, db:
                db.execute('UPDATE run SET status=?,schema_version=?,fhir_version=?', (status, version, fhir))
            with self.assertRaises(InputError):
                perturb(self.cohort, self.output)
            self.assertFalse(self.output.exists())

    def test_interrupted_and_report_failure_never_complete(self):
        self.prepare([resource('Patient', 'p')])
        for method, exception in [('_write', KeyboardInterrupt()), ('write_report', OSError('private-value'))]:
            output = self.root/method
            with patch.object(engine, method, side_effect=exception):
                with self.assertRaises(type(exception)):
                    perturb(self.cohort, output)
            with closing(sqlite3.connect(output/'perturbation-state.sqlite')) as db, db:
                self.assertIn(db.execute('SELECT status FROM run').fetchone()[0], ['failed', 'interrupted'])
            with self.assertRaises(InputError):
                perturb(self.cohort, output)

    def test_validation_detects_unrecorded_content_change(self):
        self.prepare([resource('Patient', 'p', gender='female')])
        original = engine._write
        def corrupt(*args):
            original(*args)
            path = args[-1]
            path.write_text(path.read_text().replace('female', 'male'))
        with patch.object(engine, '_write', side_effect=corrupt):
            with self.assertRaisesRegex(InputError, 'Unrecorded'):
                perturb(self.cohort, self.output)

    def test_cli_and_private_errors(self):
        self.prepare([resource('Patient', 'p')])
        args = ['perturb', '--input', str(self.cohort), '--output', str(self.output)]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args), 0)
        self.assertIn('perturbed source-derived data', output.getvalue())
        with patch('fhir_cohort_synth.cli.perturb', side_effect=RuntimeError('SENSITIVE-EXAMPLE')):
            with redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(args), 2)
        self.assertNotIn('SENSITIVE-EXAMPLE', error.getvalue())

    def test_invented_example_end_to_end_and_reingestion(self):
        ingest([REPO/'examples/mii-demo-bundle.json'], self.cohort.parent)
        self.run_engine()
        self.assertEqual(self.header['counts']['root_resources'], 23)
        report = ingest([self.output/'perturbed.ndjson'], self.root/'reingestion')
        self.assertNotEqual(report['status'], 'incomplete')
        with closing(sqlite3.connect(self.root/'reingestion/cohort.sqlite')) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM resource_references WHERE status<>'resolved'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM resources').fetchone()[0], 23)


if __name__ == '__main__':
    unittest.main()
