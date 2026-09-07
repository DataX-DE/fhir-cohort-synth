"""Small, deterministic scalar transformations; no clinical resampling.

A shared positive factor preserves a patient's eligible series before rounding.
We deliberately do not apply it to temperatures, percentages, logarithmic
quantities or standalone numbers whose meaning the datatype does not establish.
"""
from datetime import date, timedelta
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
import hashlib
from importlib.resources import files
import json
import random
import re

from .jsonio import dumps


DATE_TYPES = {'date', 'dateTime', 'instant'}
NAME_FIELDS = {'text', 'family', 'given', 'prefix', 'suffix'}
STAMP = re.compile(r'^(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2}))?$')


def full_date(value, datatype):
    """Validate the date and suffix without reformatting timezone/precision."""
    if not isinstance(value, str):
        return None, 'invalid_date'
    if re.fullmatch(r'\d{4}(?:-\d{2})?', value) and datatype != 'instant':
        try:
            date.fromisoformat(value + ('-01-01' if len(value) == 4 else '-01'))
            return None, 'partial_date_preserved'
        except ValueError:
            return None, 'invalid_date'
    match = STAMP.fullmatch(value)
    if match is None or (datatype == 'date' and match[2]) or (datatype == 'instant' and not match[2]):
        return None, 'invalid_date'
    try:
        result = date.fromisoformat(match[1])
        if match[2]:
            if int(match[2]) > 23 or int(match[3]) > 59 or int(match[4]) > 60:
                raise ValueError()
            zone = match[6]
            if zone != 'Z' and (int(zone[1:3]) > 14 or int(zone[4:]) > 59 or
                                (int(zone[1:3]) == 14 and int(zone[4:]) != 0)):
                raise ValueError()
        return result, None
    except ValueError:
        return None, 'invalid_date'


def shift_date(value, days):
    shifted = date.fromisoformat(value[:10]) + timedelta(days=days)
    return shifted.isoformat() + value[10:]


def label(seed, role, value):
    # Domain separation prevents an Identifier and a HumanName with the same
    # source string from receiving the same replacement token. This is NOT a
    # cryptographic privacy guarantee; the seed and mappings stay local.
    return hashlib.sha256(dumps([seed, role, value]).encode()).hexdigest()


def patient_factor(seed, identity, strength):
    rng = random.Random(label(seed, 'quantity-factor', identity))
    with localcontext() as ctx:
        ctx.prec = max(50, len(strength.as_tuple().digits) + 25)
        u = Decimal(rng.getrandbits(53)) / Decimal(2**53 - 1)
        return Decimal(1) + strength * (2 * u - 1)


def patient_days(seed, identity, low, high):
    return random.Random(label(seed, 'date-offset', identity)).randint(low, high)


def scale(value, factor):
    """Preserve the original decimal quantum, including integer JSON tokens."""
    number = Decimal(value)
    quantum = Decimal(1).scaleb(number.as_tuple().exponent)
    with localcontext() as ctx:
        ctx.prec = max(50, len(number.as_tuple().digits) + len(factor.as_tuple().digits) + 10)
        result = (number * factor).quantize(quantum, rounding=ROUND_HALF_EVEN)
    return int(result) if type(value) is int else result


def relative_change(before, after):
    if before == 0:
        return None  # Undefined, even when zero remains zero; report separately.
    with localcontext() as ctx:
        ctx.prec = 34
        return (Decimal(after) - Decimal(before)) / abs(Decimal(before))


class Handlers:
    def __init__(self, seed):
        self.seed = seed
        registry = json.loads(files('fhir_cohort_synth').joinpath('data/linear-units.json').read_text())
        self.units = {(system, code) for system, codes in registry['systems'].items() for code in codes}

    def quantity_reason(self, field):
        if field.reason:
            return field.reason
        if field.quantity is None or field.parent is not field.quantity or field.key != 'value':
            return 'standalone_number_preserved'
        q = field.quantity
        if not isinstance(q.get('system'), str) or not isinstance(q.get('code'), str) or not q['system'] or not q['code']:
            return 'missing_unit'
        if (q['system'], q['code']) not in self.units:
            return 'unsupported_unit'
        if type(field.value) not in {int, Decimal}:
            return 'invalid_quantity_value'
        return None

    def apply(self, field, factor, days):
        """Return (replacement, action, reason) for one existing JSON node."""
        value = field.value
        if field.reason:
            return value, 'unsupported' if field.reason.startswith('unknown') else 'preserved', field.reason
        if isinstance(value, (dict, list)) or value is None:
            return value, 'preserved', 'structure_or_null_preserved'
        if value == '':
            return value, 'preserved', 'empty_string_preserved'
        if field.parent_type == 'Identifier' and field.key == 'value' and isinstance(value, str):
            identity = [field.parent.get('system'), value]
            return 'pert-' + label(self.seed, 'identifier', identity)[:32], 'changed', 'identifier_replaced'
        name_key = field.key if isinstance(field.key, str) else field.path[-2][1]
        if field.parent_type == 'HumanName' and name_key in NAME_FIELDS and isinstance(value, str):
            return 'Dummy-' + label(self.seed, 'name-' + name_key, value)[:16], 'changed', 'name_replaced'
        if field.datatype in DATE_TYPES:
            parsed, reason = full_date(value, field.datatype)
            if parsed is None:
                return value, 'unsupported', reason
            if days is None:
                return value, 'preserved', 'shared_or_unassigned'
            result = shift_date(value, days)
            return result, 'changed' if result != value else 'preserved', 'patient_date_shift'
        if field.quantity is not None and field.key == 'value' and field.parent is field.quantity:
            reason = self.quantity_reason(field)
            if reason:
                return value, 'unsupported', reason
            if factor is None:
                return value, 'preserved', 'shared_or_unassigned'
            result = scale(value, factor)
            return result, 'changed' if dumps(result) != dumps(value) else 'preserved', 'patient_quantity_scale'
        return value, 'preserved', 'clinical_or_other_content_preserved'
