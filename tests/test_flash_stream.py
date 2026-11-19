"""Fast mode (stream-flash) over real HTTP with a simulated executor; no USB or network."""
import copy
import threading
import unittest

from seriald_core.flash_runner import FlashRunnerError
import test_flash_discovery_integration as usb_fixture


class StreamExecutor(usb_fixture.FakeDiscoveryExecutor):
    supports_stream_flash = True

    def __init__(self, root):
        super().__init__()
        self.root = root
        self.streams, self.stream_failure = [], None
        candidate = usb_fixture.usb_candidate()
        candidate['profile'] = {'id': 'auto-spacemit-gpt-v1', 'chip_family': 'k3', 'identity': {},
            'ram_loader_protocol': 'spacemit-ram-v1',
            'storages': [{'id': 'active', 'layout_source': 'spacemit-gpt', 'size_getvar': '__decklab_gpt_capacity',
                          'sector_size': 512, 'sparse': True, 'partitions': []}]}
        self.candidates = [candidate]

    def stream_loader_workspace(self):
        return self.root

    def stream_flash(self, binding, request, on_event=None, check=lambda: None):
        self.streams.append(copy.deepcopy(request))
        on_event({'event': 'progress', 'phase': 'streaming-image', 'done': 10, 'total': 100})
        on_event({'event': 'progress', 'phase': 'writing-gpt', 'storage': 'nvme:0'})
        on_event({'event': 'progress', 'phase': 'writing', 'storage': 'nvme:0', 'partition': 'rootfs', 'done': 5, 'total': 50})
        if self.stream_failure:
            raise self.stream_failure
        return {'ok': True, 'write_result': 'completed', 'verify_result': 'package-digests',
                'boot_result': 'reset-command-acknowledged', 'system_disk': 'nvme:0'}


class FlashStreamTests(usb_fixture.FlashDiscoveryHTTPIntegrationTests):
    URL = 'https://images.example/k3/Debian-13-GNOME-K3.tar.gz'

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def target(self, fixture):
        fixture.manager.flash_executor = fixture.executor = StreamExecutor(self.tmp.name)
        result = self.probe(fixture, self.discover(fixture)[0])
        self.assertTrue(result['capabilities']['stream_flash_supported'], result['capabilities'])
        return result['target']

    def test_human_operator_streams_package_and_records_results(self):
        with usb_fixture.automatic_flashing() as fixture:
            target = self.target(fixture)
            body = dict(self.scope(target), url=self.URL, system_storage='nvme:0', reset_after=True,
                        request_id='fast-flash-001')
            code, result = self.request(fixture, '/api/flash-stream', body)
            self.assertEqual(code, 202, result)
            task = self.terminal(fixture, result['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual(task['flash']['operation'], 'stream')
            self.assertEqual((task['flash']['write_result'], task['flash']['verify_result']), ('completed', 'package-digests'))
            request = fixture.executor.streams[0]
            self.assertEqual((request['source_url'], request['system_storage'], request['chip_family']), (self.URL, 'nvme:0', 'k3'))
            self.assertNotIn('loader', request)  # already in the RAM loader
            code, events = self.request(fixture, '/api/flash-events', query={'id': task['id'], 'after': 0})
            self.assertEqual(code, 200, events)
            self.assertEqual([e['phase'] for e in events['events']], ['streaming-image', 'writing-gpt', 'writing'])
            # A lost response is recovered by the same request, never a second write.
            code, again = self.request(fixture, '/api/flash-stream', body)
            self.assertEqual((code, again['task']['id']), (202, task['id']))
            self.assertEqual(len(fixture.executor.streams), 1)

    def test_first_flash_request_after_start_is_answered_not_crashed(self):
        with usb_fixture.automatic_flashing() as fixture:
            for name in ('_flash_waiters', '_flash_waiters_lock', '_flash_plans_lock', '_flash_recovery_pins'):
                if hasattr(fixture.manager, name):
                    delattr(fixture.manager, name)
            code, result = self.request(fixture, '/api/flash-stream', {'target_id': '0' * 24,
                'target_generation': 'missing', 'url': self.URL, 'request_id': 'fast-first'})
            self.assertIn(code, (404, 409), result)
            self.assertNotEqual(result.get('code'), 'unavailable', result)

    def test_failure_after_writing_started_is_reported_partial(self):
        with usb_fixture.automatic_flashing() as fixture:
            target = self.target(fixture)
            fixture.executor.stream_failure = FlashRunnerError('fixture stream failure')
            code, result = self.request(fixture, '/api/flash-stream', dict(self.scope(target), url=self.URL,
                request_id='fast-flash-002'))
            self.assertEqual(code, 202, result)
            task = self.terminal(fixture, result['task']['id'])
            self.assertEqual(task['state'], 'failed', task)
            self.assertEqual(task['flash']['write_result'], 'partial-or-unknown')

    def test_invalid_inputs_and_agents_are_refused_before_any_write(self):
        with usb_fixture.automatic_flashing() as fixture:
            target = self.target(fixture)
            for change in ({'url': 'file:///tmp/a.tar.gz'}, {'url': 'https://u:p@images.example/a.tar.gz'},
                           {'url': 'https://images.example/a.zip'}, {'system_storage': 'nvme:0;reset'},
                           {'memory_budget': 1}, {'reset_after': 'yes'}, {'artifact_id': 'x'}):
                with self.subTest(change=change):
                    body = dict(self.scope(target), url=self.URL, request_id='fast-invalid')
                    body.update(change)
                    code, result = self.request(fixture, '/api/flash-stream', body)
                    self.assertIn(code, (400, 403, 409), result)
            token, _ = fixture.grants.issue('all')
            code, result = self.request(fixture, '/api/flash-stream', dict(self.scope(target), url=self.URL,
                request_id='fast-agent'), token=token)
            self.assertEqual(code, 403, result)
            self.assertEqual(fixture.executor.streams, [])


# The base class's own tests are not repeated here.
for name in list(vars(usb_fixture.FlashDiscoveryHTTPIntegrationTests)):
    if name.startswith('test_') and name not in vars(FlashStreamTests):
        setattr(FlashStreamTests, name, None)


if __name__ == '__main__':
    unittest.main()
