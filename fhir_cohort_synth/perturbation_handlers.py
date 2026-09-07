"""Small, deterministic scalar transformations; no clinical resampling.

Each eligible quantity receives its own signed percentage change before rounding.
We deliberately do not apply it to temperatures, percentages, logarithmic
quantities or standalone numbers whose meaning the datatype does not establish.
"""
from datetime import date, timedelta
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from importlib.resources import files
import json
import re
from uuid import UUID

from .jsonio import dumps
from .randomness import label, randbelow


DATE_TYPES = {'date', 'dateTime', 'instant'}
NAME_FIELDS = {'text', 'family', 'given', 'prefix', 'suffix'}
STAMP = re.compile(r'^(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2}))?$')


def full_date(value, datatype):
    """Return (calendar date, None), or (None, reason) if it cannot be shifted.

    Validate the timestamp suffix but leave its original text intact. Partial
    dates such as '2020-02' must not acquire a guessed day during perturbation.
    """
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
    """Shift a validated full date; copy the time/zone/fraction suffix verbatim."""
    shifted = date.fromisoformat(value[:10]) + timedelta(days=days)
    return shifted.isoformat() + value[10:]


def quantity_factor(run_key, root_identity, path, strength):
    """Draw a magnitude in [0.01, strength] and an independent +/- sign.

    The root identity and concrete path give each occurrence its own random
    stream: component[0] and component[1] do not share a draw, even if their
    values match. Revisiting the same slot with the same secret key reproduces it.
    For example, a 4% increase returns 1.04; a 7% decrease returns 0.93.
    Strength zero explicitly disables numeric changes. Otherwise validation
    requires strength >= 0.01. Convert random bits directly to Decimal.
    """
    if strength == 0:
        return Decimal(1)
    identity = [root_identity, path]
    with localcontext() as ctx:
        ctx.prec = max(50, len(strength.as_tuple().digits) + 25)
        u = Decimal(randbelow(run_key, 'quantity-magnitude', identity, 2**53)) / Decimal(2**53 - 1)
        magnitude = Decimal('0.01') + (strength - Decimal('0.01')) * u
        sign = 1 if randbelow(run_key, 'quantity-sign', identity, 2) else -1
        return Decimal(1) + sign * magnitude


def patient_days(run_key, identity, low, high):
    """Draw one inclusive whole-day offset from this patient's prechecked range."""
    return low + randbelow(run_key, 'date-offset', identity, high - low + 1)


def scale(value, factor):
    """Multiply and round to the input's represented precision using half-even.

    The quantum is the smallest represented step: 10.00 uses 0.01, while the
    integer token 10 uses 1. Thus 10.00 * 1.012 becomes 10.12, but 10 * 1.012
    becomes 10. Small changes can disappear; reports count those unchanged values.
    """
    number = Decimal(value)
    quantum = Decimal(1).scaleb(number.as_tuple().exponent)
    with localcontext() as ctx:
        ctx.prec = max(50, len(number.as_tuple().digits) + len(factor.as_tuple().digits) + 10)
        result = (number * factor).quantize(quantum, rounding=ROUND_HALF_EVEN)
    return int(result) if type(value) is int else result


def relative_change(before, after):
    """Return (after - before) / abs(before), or None for a zero baseline."""
    if before == 0:
        return None  # Undefined, even when zero remains zero; report separately.
    with localcontext() as ctx:
        ctx.prec = 34
        return (Decimal(after) - Decimal(before)) / abs(Decimal(before))


class Handlers:
    """Decide scalar edits; resource IDs and graph links are handled by the writer."""

    def __init__(self, run_key, strength):
        self.run_key = run_key
        self.strength = strength
        registry = json.loads(files('fhir_cohort_synth').joinpath('data/linear-units.json').read_text())
        self.units = {(system, code) for system, codes in registry['systems'].items() for code in codes}

    def quantity_reason(self, field):
        """Return None for an eligible Quantity.value, otherwise its exclusion reason.

        The exact unit system/code pair determines support. Display labels such
        as 'unit' are retained text, not evidence that a unit can be scaled.
        """
        if field.reason:
            return field.reason
        if field.quantity is None or field.parent is not field.quantity or field.key != 'value':
            return 'standalone_number_preserved'
        quantity = field.quantity
        if (not isinstance(quantity.get('system'), str) or not isinstance(quantity.get('code'), str)
                or not quantity['system'] or not quantity['code']):
            return 'missing_unit'
        if (quantity['system'], quantity['code']) not in self.units:
            return 'unsupported_unit'
        if type(field.value) not in {int, Decimal}:
            return 'invalid_quantity_value'
        return None

    def apply(self, field, root_identity, patient_assigned, days):
        """Return (replacement, action, reason) without changing the input tree.

        Check preservation rules first, then identities, dates and quantities.
        patient_assigned is false when no unique patient owns the resource. An
        'unsupported' action also preserves the value, but records a limitation.
        """
        value = field.value
        if field.reason:
            unsupported = field.reason.startswith('unknown') or field.reason == 'embedded_resource_preserved'
            action = 'unsupported' if unsupported else 'preserved'
            return value, action, field.reason
        if isinstance(value, (dict, list)) or value is None:
            return value, 'preserved', 'structure_or_null_preserved'
        if value == '':
            return value, 'preserved', 'empty_string_preserved'
        if field.parent_type == 'Identifier' and field.key == 'value' and isinstance(value, str):
            identity = [field.parent.get('system'), value]
            token = label(self.run_key, 'identifier', identity)[:32]
            if field.parent.get('system') == 'urn:ietf:rfc:3986':
                # This identifier system requires a complete URI. A bare dummy
                # string violates base FHIR even though Identifier.value is string.
                unique = UUID(hex=token, version=4)
                if value.startswith('urn:oid:'):
                    result = 'urn:oid:2.25.' + str(unique.int)
                else:
                    result = 'urn:uuid:' + str(unique)
            else:
                result = 'pert-' + token
            return result, 'changed', 'identifier_replaced'
        # HumanName.given is an array: its scalar has an integer key. Recover
        # 'given' from the preceding path segment instead of treating 0 as a name.
        name_key = field.key if isinstance(field.key, str) else field.path[-2][1]
        if field.parent_type == 'HumanName' and name_key in NAME_FIELDS and isinstance(value, str):
            return 'Dummy-' + label(self.run_key, 'name-' + name_key, value)[:16], 'changed', 'name_replaced'
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
            if not patient_assigned:
                return value, 'preserved', 'shared_or_unassigned'
            factor = quantity_factor(self.run_key, root_identity, field.path, self.strength)
            result = scale(value, factor)
            return result, 'changed' if dumps(result) != dumps(value) else 'preserved', 'field_quantity_scale'
        return value, 'preserved', 'clinical_or_other_content_preserved'
