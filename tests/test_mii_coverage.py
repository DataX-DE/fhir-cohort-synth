"""The audit must distinguish uncheckable profiles from ordinary source errors."""
import unittest

from tools.check_mii_examples import invalid_id, unchecked_profile_paths


class MIICoverageTests(unittest.TestCase):
    def test_unchecked_profile_warning_is_not_mistaken_for_a_successful_check(self):
        warning = {'severity': 'warning', 'expression': ['Observation.meta.profile[0]'],
                   'extension': [{'url': 'http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id',
                                  'valueCode': 'VALIDATION_VAL_PROFILE_UNKNOWN_NOT_POLICY'}]}
        # The slicing error includes similar words, but its profile was loaded.
        error = {'severity': 'error', 'expression': ['Observation'],
                 'details': {'text': 'Slice matching profile required but not found'}}
        self.assertEqual(unchecked_profile_paths([warning, error]), ['Observation.meta.profile[0]'])
        self.assertEqual(unchecked_profile_paths([error]), [])

    def test_id_boundary_isolates_rejected_sources_without_dropping_missing_ids(self):
        self.assertFalse(invalid_id({'id': 'a' * 64}))
        self.assertTrue(invalid_id({'id': 'a' * 65}))
        self.assertTrue(invalid_id({'id': 'has_underscore'}))
        self.assertFalse(invalid_id({}))
        self.assertTrue(invalid_id({'id': 123}))
