"""Canonical JSON bytes are also inputs to source fingerprints and keyed draws."""
from decimal import Decimal
import unittest

from fhir_cohort_synth.jsonio import dumps, loads


class CanonicalJsonTests(unittest.TestCase):
    def test_unicode_escaping_sorted_keys_and_decimal_precision(self):
        source = r'{"z":[true,false,null,0,-3,1.25,-0.0],"ä":"μg/日 😀\n\"\\","a":12345678901234567890.0012300}'
        expected = r'{"a":12345678901234567890.0012300,"z":[true,false,null,0,-3,1.25,-0.0],"ä":"μg/日 😀\n\"\\"}'
        self.assertEqual(dumps(loads(source)), expected)
        self.assertEqual(dumps([Decimal('0.00'), Decimal('-0.00'), Decimal('1E+4'), Decimal('1E-30')]),
                         '[0.00,-0.00,1E+4,1E-30]')

    def test_existing_tuple_path_encoding_stays_byte_compatible(self):
        # Older local ledgers serialized traversal tuples with standard JSON
        # spacing. Changing it would change stored path/context identities.
        path = (('key', 'a.b[0]'), ('index', 2), ('key', 'value'))
        self.assertEqual(dumps(path), '[["key", "a.b[0]"], ["index", 2], ["key", "value"]]')
        self.assertEqual(dumps(-0.0), '-0.0')

    def test_nonfinite_numbers_remain_rejected(self):
        for value in (float('nan'), float('inf'), float('-inf'), Decimal('NaN'), Decimal('Infinity')):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                dumps({'value': value})


if __name__ == '__main__':
    unittest.main()
