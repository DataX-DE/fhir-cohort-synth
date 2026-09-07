"""Small regression tests for the development coverage harness, without Java."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tools.check_r4_examples import issue_key, outcome_files, select_examples, supplemental_examples, validation_sample
from fhir_cohort_synth.fhir_types import TypeIndex


class R4CoverageTests(unittest.TestCase):
    def test_selection_is_stable_and_does_not_select_by_validation_success(self):
        examples = {
            'package/Account-z.json': {'resourceType': 'Account'},
            'package/Account-example.json': {'resourceType': 'Account'},
            'package/Observation-b.json': {'resourceType': 'Observation'},
            'package/Observation-a.json': {'resourceType': 'Observation'},
            'package/Bundle-request.json': {'resourceType': 'Bundle'},
        }
        selected = select_examples(examples)
        self.assertEqual(selected, select_examples(dict(reversed(list(examples.items())))))
        self.assertIn('package/Account-example.json', selected)
        self.assertNotIn('package/Account-z.json', selected)
        self.assertEqual(len(selected), 4)

    def test_supplemental_resources_are_invented_and_use_recognized_fields(self):
        index = TypeIndex()
        examples = supplemental_examples()
        self.assertEqual(len({r['resourceType'] for r in examples}), 5)
        for resource in examples:
            self.assertTrue(resource['id'].startswith('invented-'))
            self.assertFalse(any(f.reason for f in index.walk(resource)))

    def test_validator_results_match_files_and_reject_missing_or_duplicate_annotations(self):
        outcome = {'resourceType': 'OperationOutcome', 'extension': [{
            'url': 'http://hl7.org/fhir/StructureDefinition/operationoutcome-file', 'valueString': '/before/a.json'}],
            'issue': [{'severity': 'error', 'code': 'invalid'}]}
        self.assertEqual(outcome_files(outcome)['/before/a.json'], outcome['issue'])
        with self.assertRaises(ValueError):
            outcome_files({'resourceType': 'OperationOutcome'})
        with self.assertRaises(ValueError):
            outcome_files({'resourceType': 'Bundle', 'entry': [{'resource': outcome}, {'resource': outcome}]})

    def test_comparison_detects_new_paths_and_multiplicity_without_counting_ids(self):
        issue = {'severity': 'error', 'code': 'invariant', 'expression': ['Patient.birthDate'],
                 'extension': [{'url': 'http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id',
                                'valueString': 'test-constraint'}], 'details': {'text': 'Resource old-id'}}
        updated = deepcopy(issue)
        updated['details']['text'] = 'Resource pert-new-id'
        self.assertEqual(issue_key(issue), issue_key(updated))
        before = Counter([issue_key(issue)])
        after = Counter([issue_key(updated)] * 2)
        self.assertEqual(sum((after - before).values()), 1)
        updated['expression'] = ['Patient.name']
        self.assertNotEqual(issue_key(issue), issue_key(updated))

    def test_validation_sample_keeps_clinical_roots_and_definition_types_per_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'before').mkdir()
            for filename, kind in [('0001-1.json', 'Observation'), ('0001-2.json', 'Observation'),
                                   ('0001-3.json', 'StructureDefinition'), ('0001-4.json', 'StructureDefinition'),
                                   ('0002-1.json', 'StructureDefinition')]:
                (root / 'before' / filename).write_text(json.dumps({'resourceType': kind}))
            names = [p.name for p in validation_sample(root)]
            self.assertEqual(names, ['0001-1.json', '0001-2.json', '0001-3.json', '0002-1.json'])

    def test_diagnostic_resource_id_comments_do_not_look_like_new_paths(self):
        issue = {'severity': 'error', 'code': 'invalid', 'details': {'text': 'A fixed diagnostic'},
                 'expression': ['MedicationStatement.contained[0]/*Medication/old-id*/.code']}
        updated = deepcopy(issue)
        updated['expression'] = ['MedicationStatement.contained[0]/*Medication/pert-new-id*/.code']
        self.assertEqual(issue_key(issue), issue_key(updated))
        updated['expression'] = ['MedicationStatement.contained[1]/*Medication/pert-new-id*/.code']
        self.assertNotEqual(issue_key(issue), issue_key(updated))
