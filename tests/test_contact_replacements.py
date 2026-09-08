"""Invented personal fields: replacements must preserve every field and array."""
from contextlib import closing
from copy import deepcopy
import gzip
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fhir_cohort_synth import perturbation as engine
from fhir_cohort_synth.export_files import iter_export_lines
from fhir_cohort_synth.fhir_types import TypeIndex
from fhir_cohort_synth.ingest import InputError
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.perturbation_handlers import ADDRESS_FIELDS, NAME_FIELDS
from fhir_cohort_synth.workflow import run_export


NAME = {'use': 'official', 'text': 'Invented Alex Example', 'family': 'Example',
        'given': ['Alex', 'Second'], 'prefix': ['Dr'], 'suffix': ['Junior']}
TELECOM = [{'system': 'phone', 'value': '000000000', 'use': 'home', 'rank': 1},
           {'system': 'email', 'value': 'invented@example.invalid'}]
ADDRESS = {'use': 'home', 'type': 'both', 'text': 'Invented address',
           'line': ['123 Fictional Lane', 'Floor 2'], 'city': 'Example City',
           'district': 'Example District', 'state': 'Example State',
           'postalCode': '00000', 'country': 'DE'}


def resource(kind, identity, **fields):
    return {'resourceType': kind, 'id': identity, **deepcopy(fields)}


def shape(value):
    """Retain keys, array positions and scalar types, ignoring only scalar values."""
    if isinstance(value, dict):
        return {key: shape(child) for key, child in value.items()}
    if isinstance(value, list):
        return [shape(child) for child in value]
    return type(value).__name__


class ContactReplacementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.ndjson.gz'
        self.output = self.root / 'output'

    def run_records(self, records):
        with gzip.open(self.source, 'wt', encoding='utf-8') as stream:
            for item in records:
                stream.write(dumps(item) + '\n')
        before = self.source.read_bytes()
        self.report = run_export([self.source], self.output)
        self.assertEqual(self.source.read_bytes(), before)
        result = [loads(line) for line in iter_export_lines(self.output)]
        self.assertEqual(shape(result), shape(records))
        return result

    def test_patient_person_and_supporting_resource_fields_are_replaced(self):
        originals = [resource(kind, str(i), name=[NAME], telecom=TELECOM, address=[ADDRESS])
                     for i, kind in enumerate(('Patient', 'Person', 'Practitioner', 'RelatedPerson'))]
        originals += [resource('Organization', 'org', telecom=TELECOM, address=[ADDRESS],
                               contact=[{'name': NAME, 'telecom': TELECOM, 'address': ADDRESS}]),
                      resource('Location', 'loc', telecom=TELECOM, address=ADDRESS)]
        result = self.run_records(originals)
        index = TypeIndex()
        for original, output in zip(originals, result):
            before = {field.path: field.value for field in index.walk(original)}
            for field in index.walk(output):
                key = field.path[-2][1] if isinstance(field.key, int) else field.key
                if ((field.parent_type == 'HumanName' and key in NAME_FIELDS)
                        or (field.parent_type == 'Address' and key in ADDRESS_FIELDS)) and isinstance(field.value, str):
                    self.assertRegex(field.value, r'^[a-f0-9]{16}$')
                    self.assertNotEqual(field.value, before[field.path])
            self.assertRegex(output['telecom'][0]['value'], r'^000[0-9]{10}$')
            self.assertRegex(output['telecom'][1]['value'], r'^[a-f0-9]{16}@example\.invalid$')
            self.assertEqual(output['telecom'][0]['rank'], 1)
            self.assertEqual(output['telecom'][0]['use'], 'home')
        # Repeated original contacts map consistently across different resources.
        self.assertEqual(result[0]['telecom'], result[1]['telecom'])
        self.assertEqual(result[0]['address'], result[1]['address'])
        report_text = (self.output / 'result/reports/perturbation-report.json').read_text()
        for text in ('invented@example.invalid', '123 Fictional Lane', 'Invented Alex Example'):
            self.assertNotIn(text, report_text)
        reasons = {field['reason'] for field in loads(report_text)['fields']}
        self.assertTrue({'name_replaced', 'address_replaced', 'contact_point_replaced'} <= reasons)
        with closing(sqlite3.connect(self.output / 'intermediates/cohort.sqlite')) as db:
            original = loads(db.execute("SELECT payload FROM resources WHERE resource_type='Patient'").fetchone()[0])
            self.assertEqual(original['telecom'], TELECOM)

    def test_nested_contacts_containment_and_links_keep_associations(self):
        patient = resource('Patient', 'p', contact=[
            {'relationship': [{'text': 'emergency'}], 'telecom': TELECOM, 'address': ADDRESS},
            {'name': NAME, 'organization': {'reference': 'Organization/org'}, 'telecom': TELECOM}],
            contained=[resource('Practitioner', 'inside', name=[NAME], telecom=TELECOM, address=[ADDRESS])],
            generalPractitioner=[{'reference': '#inside'}])
        result = self.run_records([patient, resource('Organization', 'org')])
        self.assertNotEqual(result[0]['contact'][0]['telecom'], TELECOM)
        self.assertNotEqual(result[0]['contained'][0]['address'], [ADDRESS])
        self.assertEqual(result[0]['contact'][0]['relationship'], patient['contact'][0]['relationship'])
        self.assertEqual(result[0]['contact'][1]['organization']['reference'], 'Organization/' + result[1]['id'])
        self.assertEqual(result[0]['generalPractitioner'][0]['reference'], '#' + result[0]['contained'][0]['id'])

    def test_typed_extensions_inline_resources_and_companions_are_handled(self):
        patient = resource('Patient', 'p', name=[NAME], extension=[
            {'url': 'urn:address', 'valueAddress': ADDRESS},
            {'url': 'urn:name', 'valueHumanName': NAME}],
            _birthDate={'extension': [{'url': 'urn:contact', 'valueContactPoint': TELECOM[0]}]})
        parameters = resource('Parameters', 'params', parameter=[
            {'name': 'person', 'resource': resource('Person', 'inline', name=[NAME], telecom=TELECOM, address=[ADDRESS])}])
        result = self.run_records([patient, parameters])
        self.assertEqual(result[0]['name'][0], result[0]['extension'][1]['valueHumanName'])
        self.assertNotEqual(result[0]['extension'][0]['valueAddress'], ADDRESS)
        self.assertNotEqual(result[0]['_birthDate']['extension'][0]['valueContactPoint'], TELECOM[0])
        inline = result[1]['parameter'][0]['resource']
        self.assertEqual(inline['name'][0], result[0]['name'][0])
        self.assertNotEqual(inline['telecom'], TELECOM)
        self.assertEqual(inline['id'], 'inline')

    def test_empty_missing_and_unrelated_fields_keep_their_values(self):
        patient = resource('Patient', 'p', name=[{'family': '', 'given': [None, '']}],
                           telecom=[{'system': 'email', 'value': ''}, {'value': None}, {}],
                           address=[{'line': [], 'city': None}, {}], active=True,
                           arbitrary={'telecom': 'literal', 'address': {}, 'empty': []},
                           **{'name.family': 'literal'})
        result = self.run_records([patient])[0]
        self.assertEqual({**result, 'id': patient['id']}, patient)

    def test_contact_formats_for_all_systems_and_missing_system(self):
        systems = ['phone', 'fax', 'sms', 'pager', 'email', 'url', 'other', 'unfamiliar', None]
        contacts = [{'value': 'Invented contact', **({'system': system} if system else {})} for system in systems]
        result = self.run_records([resource('Patient', 'p', telecom=contacts)])[0]
        for i in range(4):
            self.assertRegex(result['telecom'][i]['value'], r'^000[0-9]{10}$')
        self.assertRegex(result['telecom'][4]['value'], r'^[a-f0-9]{16}@example\.invalid$')
        self.assertRegex(result['telecom'][5]['value'], r'^https://example\.invalid/[a-f0-9]{16}$')
        for i in range(6, len(systems)):
            self.assertRegex(result['telecom'][i]['value'], r'^[a-f0-9]{16}$')

    def test_key_reuse_reproduces_gzip_and_fresh_keys_change_replacements(self):
        records = [resource('Patient', 'p', name=[NAME], telecom=TELECOM, address=[ADDRESS])]
        first = self.run_records(records)
        replay, fresh = self.root / 'replay', self.root / 'fresh'
        run_export([self.source], replay, reuse_key_from=self.output / 'intermediates/perturbation-state.sqlite')
        self.assertEqual((self.output / 'result/fhir/source.ndjson.gz').read_bytes(),
                         (replay / 'result/fhir/source.ndjson.gz').read_bytes())
        run_export([self.source], fresh)
        second = [loads(line) for line in iter_export_lines(fresh)]
        for field in ('name', 'telecom', 'address'):
            self.assertNotEqual(first[0][field], second[0][field])

    def test_validation_rejects_original_contact_value_in_output(self):
        original_write = engine._write

        def corrupt(*args, **kwargs):
            original_write(*args, **kwargs)
            destination = args[4]
            item = loads(destination.read_text())
            item['telecom'] = deepcopy(TELECOM)
            destination.write_text(dumps(item) + '\n')

        with patch.object(engine, '_write', side_effect=corrupt):
            with self.assertRaisesRegex(InputError, 'recorded change ledger'):
                self.run_records([resource('Patient', 'p', telecom=TELECOM)])
        with closing(sqlite3.connect(self.output / 'intermediates/perturbation-state.sqlite')) as db:
            self.assertEqual(db.execute('SELECT status FROM run').fetchone()[0], 'failed')
        self.assertFalse((self.output / 'result').exists())
