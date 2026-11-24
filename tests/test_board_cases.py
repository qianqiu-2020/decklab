import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, mock_open, patch

from seriald_core.board_cases import BoardCase, SerialEvidence, config, select_transport, verify_boot
from seriald_core import board_case_target
from seriald_core.board_case_target import run_command
from seriald_core.board_maintenance import maintenance_for
from seriald_core.device import Device
from seriald_core.devices import Manager
from seriald_core.hardware import HardwareBook
from seriald_core.storage import Store
from seriald_core.task_actions import TaskActions
from seriald_core.tasks import TaskError


BOOT1 = '11111111-1111-4111-8111-111111111111'
BOOT2 = '22222222-2222-4222-8222-222222222222'
SERIAL = 'FIXTURE-BOARD'


class BoardCasesTests(unittest.TestCase):
    def test_rtc_wake_waits_for_journal_after_time_spent_suspended(self):
        entry = 'PM: suspend entry (deep)\n'
        journal = iter(('', entry, entry + 'PM: suspend exit\n'))
        interrupts = iter((0, 0, 1, 1))

        def run(argv, _timeout, _emit):
            if argv[:2] == ['journalctl', '-k']:
                if '--show-cursor' in argv:
                    return 0, '-- cursor: s=abcd;i=123\n'
                return 0, next(journal)
            if argv == ['cat', '/proc/interrupts']:
                return 0, f'33: {next(interrupts)} 0 rtc@pmic\n'
            if argv[:2] == ['systemctl', 'show']:
                return 0, 'Result=success\nExecMainStatus=0\n'
            return 0, {
                '/sys/power/mem_sleep': 's2idle [deep]\n',
                '/sys/power/pm_test': '[none]\n',
                '/sys/module/printk/parameters/console_suspend': 'Y\n',
            }.get(argv[-1], '')

        uart = mock_open()
        with patch('builtins.open', uart), \
                patch.object(board_case_target, 'run_command', side_effect=run), \
                patch.object(board_case_target, 'identity', return_value={'serial': SERIAL, 'boot_id': BOOT1}), \
                patch.object(board_case_target.time, 'sleep'), \
                patch.object(board_case_target.time, 'monotonic', side_effect=[100, 101, 104]), \
                patch.object(board_case_target.time, 'clock_gettime', side_effect=[100, 101, 140]):
            board_case_target.main('rtc-wake', '/dev/ttyS0', 'CASE', SERIAL, BOOT1)
        output = b''.join(call.args[0] for call in uart().write.call_args_list).decode()
        result = next(line.removeprefix('CASE RESULT ') for line in output.splitlines()
                      if line.startswith('CASE RESULT '))
        self.assertEqual(json.loads(result)['status'], 'passed')

    def test_subprocess_output_is_emitted_before_command_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            released = Path(directory) / 'released'
            script = ('import pathlib, sys, time\n'
                      'print("step running", flush=True)\n'
                      'while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)\n'
                      'print("command stderr", file=sys.stderr)\n'
                      'sys.exit(7)\n')
            output = []

            def emit(channel, chunk):
                output.append((channel, chunk))
                if channel == 'stdout':
                    released.touch()

            self.assertEqual(run_command([sys.executable, '-c', script, str(released)], 3, emit), (7, 'step running\n'))
            self.assertEqual(output, [('stdout', b'step running\n'), ('stderr', b'command stderr\n')])

    def test_duplicate_android_serial_uses_only_current_transport_at_the_configured_port(self):
        listing = ('cloned device usb:2-1.2 transport_id:81\n'
                   'cloned device usb:2-1.3 transport_id:82\n')
        self.assertEqual(select_transport(listing, '2-1.3'), '82')
        for value in ('', listing.replace('usb:2-1.2', 'usb:2-1.3'),
                      listing.replace('device usb:2-1.3', 'offline usb:2-1.3'),
                      listing + 'cloned device usb:2-1.3\n'):
            with self.assertRaises(ConnectionError):
                select_transport(value, '2-1.3')

    def test_config_rejects_duplicate_ports_and_arbitrary_commands(self):
        row = {'usb_port': '2-1.2', 'uart': '/dev/ttyS0', 'cases': ['rtc-wake']}
        self.assertEqual(config({SERIAL:row}), {SERIAL:row})
        for options in ({SERIAL:dict(row, uart='/tmp/unsafe')},
                        {SERIAL:dict(row, cases=['arbitrary'])},
                        {SERIAL:row, 'ANOTHER':row}):
            with self.assertRaises(ValueError):
                config(options)

    def test_k3_checks_efi_and_actual_root_and_baseline(self):
        facts = {'model':'SpacemiT K3 Pico-ITX', 'multi_user':'active', 'distribution':'debian',
                 'firmware':'0.0.1', 'root':BOOT1+' ext4 rw,noatime', 'root_path':'/fixture/c0e00000.ufshc/host0',
                 'disk_model':'TY7B-128 ', 'esp_partuuid':BOOT2, 'entry':'KINGSTON /UFS(0,0x00){auto_created_boot_option}'}
        baseline = verify_boot(facts)
        self.assertEqual(verify_boot(facts, baseline), baseline)
        for changed in ({'root_path':'/fixture/nvme'}, {'entry':'UEFI NVMe'}, {'root':BOOT1+' ext4 ro'},
                        {'firmware':'0.0.2'}, {'esp_partuuid':BOOT1}):
            with self.assertRaises(ValueError):
                verify_boot(dict(facts, **changed), baseline)

    def fake_case(self):
        def now_off():
            return device.base + len(device.buf)
        device = SimpleNamespace(connection_id='conn1', present=True, stop=threading.Event(),
                                 base=0, buf=bytearray(b'previous\n'), cv=threading.Condition(), now_off=now_off)
        row = {'usb_port':'2-1.2', 'uart':'/dev/ttyS0', 'cases':['rtc-wake']}
        case = BoardCase.__new__(BoardCase)
        case.device, case.connection, case.serial = device, device.connection_id, SERIAL
        case.board = SimpleNamespace(connection=device)
        case.manager = SimpleNamespace(store=Mock(), hardware=SimpleNamespace(board_tests={SERIAL:row}))
        case.row, case.case, case.rounds, case.seconds = row, 'rtc-wake', 2, None
        case.maintenance = Mock()
        case.endpoint = Mock(return_value='82')
        case.facts = Mock(return_value={'serial':SERIAL,'boot_id':BOOT1})
        case.artifact = Mock(return_value='artifact')
        context = Mock(id='a'*24, cancelled=threading.Event())
        return case, context

    def test_lost_trigger_reply_observes_original_and_never_starts_second_round_on_failure(self):
        case, context = self.fake_case()
        def submit(*args):
            case.device.buf.extend(b'<raw line>\n')
            raise ConnectionError('no receipt')
        case.command = Mock(side_effect=submit)

        def observe(tag, before, baseline, evidence, row):
            evidence.read(case.device)
            self.assertEqual(bytes(evidence.data), b'<raw line>\n')
            return 'uncertain', 'no evidence of a wake'

        case.observe = Mock(side_effect=observe)
        with patch('seriald_core.board_cases.FlashStore') as flash, self.assertRaisesRegex(TaskError, 'no evidence'):
            case.run(context)
        case.command.assert_called_once()
        self.assertEqual(case.command.call_args.args[:3], ('-t','82','shell'))
        last = context.store.update.call_args.kwargs['result']
        self.assertEqual(last['first_issue']['trigger'], 'unknown')
        self.assertEqual(last['first_issue']['serial_lines'], 1)
        self.assertEqual(last['first_issue']['serial_artifact_id'], 'artifact')
        case.maintenance.release.assert_not_called()
        case.maintenance.keep_recovery.assert_called_once_with(context.id)
        flash.return_value.update_details.assert_called_once_with(context.id, {'recovery_required': True})

    def test_systemd_rejection_ends_without_waiting_or_holding_board(self):
        case, context = self.fake_case()
        stderr = 'Failed to start transient service unit: Cannot set property JobTimeoutUSec, or unknown property.'
        case.command = Mock(return_value=SimpleNamespace(returncode=1, stdout='', stderr=stderr))
        case.observe = Mock()
        with self.assertRaisesRegex(TaskError, '临时服务启动失败.*JobTimeoutUSec'):
            case.run(context)
        args = case.command.call_args.args[3]
        self.assertIn('--property=RuntimeMaxSec=230s', args)
        self.assertNotIn('--property=JobTimeoutSec', args)
        case.observe.assert_not_called()
        context.mark_outcome.assert_called_once_with('not-issued')
        self.assertEqual(context.store.update.call_args.kwargs['result']['first_issue']['trigger'], 'not-issued')
        case.maintenance.release.assert_called_once_with(context.id, confirmed=True)
        case.maintenance.keep_recovery.assert_not_called()

    def test_systemd_missing_dbus_reply_still_observes_the_original_round(self):
        case, context = self.fake_case()
        stderr = 'Failed to start transient service unit: Connection timed out'
        case.command = Mock(return_value=SimpleNamespace(returncode=1, stdout='', stderr=stderr))
        case.observe = Mock(return_value=('uncertain', 'no evidence of a wake'))
        with patch('seriald_core.board_cases.FlashStore'), self.assertRaisesRegex(TaskError, 'no evidence'):
            case.run(context)
        case.command.assert_called_once()
        case.observe.assert_called_once()
        case.maintenance.keep_recovery.assert_called_once_with(context.id)
        case.maintenance.release.assert_not_called()

    def test_rtc_receipt_requires_same_boot_and_an_armed_marker(self):
        case, context = self.fake_case()
        evidence = SerialEvidence(case.device)
        tag = 'DECKLAB-CASE ' + context.id + ' 1'
        receipt = json.dumps(dict(status='passed', reason='rtc', serial=SERIAL, boot_id=BOOT1))
        case.device.buf.extend((tag+' ARMED\r\n'+tag+' RESULT '+receipt+'\r\n').encode())
        row = {}
        self.assertEqual(case.observe(tag, {'boot_id':BOOT1}, None, evidence, row), ('passed','rtc'))
        self.assertEqual(row['boot_after'], BOOT1)
        evidence = SerialEvidence(case.device)
        case.device.buf.extend((tag+' ARMED\n'+tag+' RESULT '+receipt.replace(BOOT1,BOOT2)+'\n').encode())
        self.assertEqual(case.observe(tag, {'boot_id':BOOT1}, None, evidence, {})[0], 'uncertain')

    def test_reboot_waits_for_current_transport_and_checks_new_boot_against_nor_and_ufs(self):
        case, context = self.fake_case()
        case.case = 'reboot-aging'
        facts = {'model':'SpacemiT K3 Pico-ITX', 'multi_user':'active', 'distribution':'debian',
                 'firmware':'0.0.1', 'root':BOOT1+' ext4 rw', 'root_path':'/fixture/ufshc.ufshc/host0',
                 'disk_model':'TY7B-128', 'esp_partuuid':BOOT2, 'entry':'KINGSTON /UFS(0,0x00)'}
        baseline = verify_boot(facts)
        case.endpoint.side_effect = [ConnectionError('USB temporarily missing'), '91']
        case.facts.return_value = dict(facts, boot_id=BOOT2)
        tag = 'DECKLAB-CASE ' + context.id + ' 1'
        evidence = SerialEvidence(case.device)
        case.device.buf.extend((tag+' ISSUED\nnor m:0x1 d:0x1\n').encode())
        result = {}
        with patch('seriald_core.board_cases.time.sleep'):
            self.assertEqual(case.observe(tag, {'boot_id':BOOT1}, baseline, evidence, result)[0], 'passed')
        case.facts.assert_called_once_with('91', {})
        self.assertEqual(result['boot_after'], BOOT2)
        self.assertTrue(result['round_finished'])

    def test_real_task_admission_deduplicates_and_reserves_across_uart_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'registry.sqlite3')
            self.addCleanup(store.close)
            store.bind({'serial':SERIAL,'serial_source':'device-tree','identity_status':'confirmed','ok':True},
                       'fixture-port', '/fixture/tty')
            manager = Manager(store)
            self.addCleanup(manager.close)
            board = manager.boards[SERIAL]
            device = Device('fixture-port', '/fixture/tty', manager, board=board)
            board.attach(device)
            device.present = True
            manager.hardware = HardwareBook({'board_tests':{
                SERIAL:{'usb_port':'2-1.2','uart':'/dev/ttyS0','cases':['rtc-wake']}}})
            started, release = threading.Event(), threading.Event()

            def run(operation, context):
                started.set()
                try:
                    release.wait(2)
                    return {'case':'rtc-wake','completed_rounds':1}
                finally:
                    operation.maintenance.release(context.id, confirmed=True)

            payload = {'kind':'board-test','serial':SERIAL,'connection_id':device.connection_id,
                       'case':'rtc-wake','rounds':1,'request_id':'test-request-1'}
            actions = TaskActions(manager)
            with patch('seriald_core.board_cases.BoardCase.run', run), patch('seriald_core.board_cases.shutil.which', return_value='/fixture/adb'):
                first = actions.start(payload, device, 'web')
                self.assertEqual(first.status, 202, first.payload)
                self.assertTrue(started.wait(2))
                self.assertEqual(manager.tasks.store.get(first.payload['task']['id'])['outcome'], 'not-issued')
                self.assertTrue(maintenance_for(manager).holds(SERIAL))
                duplicate = actions.start(payload, device, 'web')
                self.assertEqual(first.payload['task']['id'], duplicate.payload['task']['id'])
                self.assertEqual(actions.start(dict(payload, request_id='test-request-2'), device, 'web').status, 423)
                self.assertEqual(actions.start(payload, device, 'agent').status, 403)
                release.set()
                self.assertTrue(manager.tasks.close())
            self.assertFalse(maintenance_for(manager).holds(SERIAL))
