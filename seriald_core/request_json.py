"""Bounded UTF-8 JSON objects at public request boundaries."""
import json
import math

MAX_DEPTH = 64


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('non-finite JSON number')


def load_object(body):
    try:
        payload = json.loads(body.decode('utf-8'), object_pairs_hook=_object,
                             parse_constant=_constant) if body else {}
    except RecursionError as error:
        raise ValueError('JSON nesting too deep') from error
    if not isinstance(payload, dict):
        raise ValueError('JSON object required')
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise ValueError('JSON nesting too deep')
        if isinstance(value, dict):
            pending.extend((key, depth + 1) for key in value)
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            # Escaped lone surrogates survive json.loads but cannot be encoded
            # into our UTF-8 serial writes, filenames, audit or responses.
            value.encode('utf-8')
        elif isinstance(value, float) and not math.isfinite(value):
            # Valid exponent notation can overflow Python float without using
            # JSON's nonstandard Infinity token.
            raise ValueError('non-finite JSON number')
    return payload
