import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.home_assistant_control import call


class HomeAssistantControlTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.token = Path(folder.name) / 'token'
        self.token.write_text('fixture-token')
        client = patch('examples.home_assistant_control.HTTPConnection')
        self.connection = client.start().return_value
        self.addCleanup(client.stop)
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = b'[]'

    def test_slot_pulse_and_download_use_one_service_call(self):
        for entity, service in [('switch.slot_reset', 'switch/turn_on'), ('button.slot_fastboot', 'button/press')]:
            with self.subTest(entity=entity):
                self.connection.reset_mock()
                self.assertEqual(call('http://fixture', self.token, entity), 'acknowledged')
                args, kwargs = self.connection.request.call_args
                self.assertEqual(args, ('POST', '/api/services/' + service))
                self.assertEqual(json.loads(kwargs['body']), {'entity_id': entity})
                self.assertEqual(kwargs['headers']['Authorization'], 'Bearer fixture-token')
                self.connection.request.assert_called_once()

    def test_button_state_check_never_presses(self):
        self.response.read.return_value = b'{"entity_id":"button.slot","state":"unknown"}'
        self.assertEqual(call('http://fixture', self.token, 'button.slot', check=True), 'unknown')
        self.assertEqual(self.connection.request.call_args.args, ('GET', '/api/states/button.slot'))
        self.connection.request.assert_called_once()

    def test_lost_receipt_does_not_repeat_action(self):
        self.connection.getresponse.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            call('http://fixture', self.token, 'button.slot')
        self.connection.request.assert_called_once()
        self.connection.close.assert_called_once()
