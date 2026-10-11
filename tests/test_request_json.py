"""Request JSON constraints preserve Unicode and fail before device operations."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from seriald_core.request_json import load_object, MAX_DEPTH
from seriald_core.artifact_actions import ArtifactActions


class RequestJsonTests(unittest.TestCase):
    def test_artifact_service_non_objects_never_touch_the_spool(self):
        spool = mock.Mock()
        actions = ArtifactActions(SimpleNamespace(artifacts=spool))
        for payload in (None, [], 42, 'upload'):
            with self.subTest(payload=payload):
                result = actions.execute('create', payload)
                self.assertEqual(result.status, 400)
        self.assertEqual(spool.mock_calls, [])

    def test_valid_unicode_commands_are_preserved(self):
        command = 'printf "设备 🧪\\n"\r'
        body = {'data': command, 'overwrite': True, 'size': 0, 'nested': [None, {'x': 1.5}]}
        for escaped in (True, False):
            self.assertEqual(load_object(json.dumps(body, ensure_ascii=escaped).encode()), body)
        self.assertEqual(load_object(b'{"data":"\\ud83d\\ude00"}'), {'data': '😀'})

    def test_nonfinite_numbers_and_exponent_overflow_are_rejected(self):
        for value in ('NaN', 'Infinity', '-Infinity', '1e999'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_object(('{"cols":' + value + '}').encode())

    def test_duplicate_fields_and_lone_surrogates_are_rejected_at_any_level(self):
        for raw in (b'{"key":"first","key":"second"}',
                    b'{"nested":{"actor":"one","actor":"two"}}',
                    b'{"data":"\\ud800"}', b'{"\\udfff":1}',
                    b'{"data":["\\udfff"]}', b'{"data":"\xff"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load_object(raw)

    def test_deep_input_and_parser_recursion_become_validation_errors(self):
        for depth in (MAX_DEPTH + 1, 2000):
            raw = b'{"nested":' + b'[' * depth + b'0' + b']' * depth + b'}'
            with self.subTest(depth=depth), self.assertRaises(ValueError):
                load_object(raw)
        raw = b'{"nested":' + b'[' * (MAX_DEPTH - 1) + b'0' + b']' * (MAX_DEPTH - 1) + b'}'
        self.assertIsInstance(load_object(raw), dict)

    def test_non_objects_fail_and_empty_body_retains_existing_behavior(self):
        for raw in (b'[]', b'null', b'1', b'"text"', b'true'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'JSON object required'):
                load_object(raw)
        self.assertEqual(load_object(b''), {})
