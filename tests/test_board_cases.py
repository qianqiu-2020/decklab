import base64
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, mock_open, patch

from decklab_core.board_cases import AdbDiscoveryScans, BoardCase, SerialEvidence, adb_binding, config, find_transport, library, resolve_connection, save_case, select_transport, verify_boot
from decklab_core import board_case_target
from decklab_core.board_case_target import run_command
from decklab_core.board_maintenance import maintenance_for
from decklab_core.device import Device
from decklab_core.devices import Manager
from decklab_core.hardware import HardwareBook
from decklab_core.storage import Store
from decklab_core.task_actions import TaskActions
from decklab_core.tasks import TaskError


BOOT1 = '11111111-1111-4111-8111-111111111111'
BOOT2 = '22222222-2222-4222-8222-222222222222'
SERIAL = 'FIXTURE-BOARD'


class BoardCasesTests(unittest.TestCase):
    def test_library_shows_builtin_commands_and_only_edits_saved_shell_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'registry.sqlite3')
            self.addCleanup(store.close)
            cases = {item['id']: item for item in library(store)['cases']}
            self.assertIn('systemctl --no-block reboot', cases['reboot-aging']['script'])
            self.assertIn('rtcwake -m no -s 35', cases['rtc-wake']['script'])
            self.assertTrue(cases['rtc-wake']['steps'])
            self.assertTrue(cases['rtc-wake']['mechanisms'])
            fields = {'name': ' 检查测试 ', 'script': 'printf "中文"\r\nexit 0', 'timeout': 10}
            case = save_case(store, fields)
            self.assertEqual((case['kind'], case['name'], case['revision']), ('shell', '检查测试', 1))
            self.assertEqual(case['script'], 'printf "中文"\nexit 0')
            self.assertIn(case, library(store)['cases'])
            edit = dict(fields, id=case['id'], revision=1, script='exit 3')
            self.assertEqual(save_case(store, edit)['revision'], 2)
            with self.assertRaises(TaskError) as caught:
                save_case(store, edit)
            self.assertEqual(caught.exception.status, 409)
            with self.assertRaises(TaskError):
                save_case(store, dict(edit, id='rtc-wake'))
            for invalid in ({'timeout': True}, {'timeout': 181},
                            {'script': '\x00exit'}, {'script': '中' * 8192}):
                with self.assertRaises(TaskError):
                    save_case(store, dict(fields, **invalid))
            steps = [{'command':'printf "中文"', 'expected':'中文'}, {'command':'true'}]
            structured = save_case(store, {'name':'逐条核对', 'commands':steps, 'timeout':10})
            self.assertEqual(structured['commands'], [steps[0], dict(steps[1], expected='')])
            self.assertIn(structured, library(store)['cases'])
            with self.assertRaises(TaskError):
                save_case(store, {'name':'逐条核对', 'commands':[dict(steps[0], expected='a\nb')], 'timeout':10})

    def test_shell_runs_with_real_sh_and_logs_output_and_exit_code(self):
        for script, timeout, expected in [
                ("printf '中文输出\\n'; printf 'stderr here\\n' >&2; exit 0", 2, 0),
                ('exit 7', 2, 7),
                ('sleep 30 & wait', 1, 124)]:
            with self.subTest(expected=expected):
                uart = mock_open()
                with tempfile.TemporaryDirectory() as directory:
                    serial_path, boot_path = Path(directory) / 'serial', Path(directory) / 'boot'
                    serial_path.write_text(SERIAL + '\n')
                    boot_path.write_text(BOOT1 + '\n')
                    with patch('builtins.open', uart), patch.object(board_case_target, 'SERIAL', str(serial_path)), \
                            patch.object(board_case_target, 'BOOT', str(boot_path)):
                        board_case_target.main('shell', '/dev/ttyS0', 'CASE', SERIAL, BOOT1,
                                               base64.b64encode(script.encode()).decode(), str(timeout))
                output = b''.join(call.args[0] for call in uart().write.call_args_list).decode()
                self.assertIn('CASE CMD /bin/sh -c ', output)
                self.assertIn(f'CASE EXIT {expected}', output)
                result = next(line.removeprefix('CASE RESULT ') for line in output.splitlines()
                              if line.startswith('CASE RESULT '))
                self.assertEqual(json.loads(result)['status'], 'failed' if expected else 'passed')
                if expected == 0:
                    self.assertIn('CASE stdout\n中文输出', output)
                    self.assertIn('CASE stderr\nstderr here', output)

    def test_step_target_forwards_both_streams_inside_serial_markers_without_line_buffering(self):
        uart = mock_open()
        with tempfile.TemporaryDirectory() as directory:
            serial_path, boot_path = Path(directory) / 'serial', Path(directory) / 'boot'
            serial_path.write_text(SERIAL + '\n')
            boot_path.write_text(BOOT1 + '\n')
            script = base64.b64encode(b"printf 'abc'; printf 'def' >&2; exit 0").decode()
            with patch('builtins.open', uart), patch.object(board_case_target, 'SERIAL', str(serial_path)), \
                    patch.object(board_case_target, 'BOOT', str(boot_path)):
                board_case_target.main('shell-step', '/dev/ttyS0', 'CASE', SERIAL, BOOT1, script, '2')
        output = b''.join(call.args[0] for call in uart().write.call_args_list)
        self.assertIn(b'CASE OUTPUT\nabcdef\nCASE OUTPUT-END\n', output)
        self.assertIn(b'CASE EXIT 0\n', output)

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

    def test_registered_usb_diagnostics_distinguish_visible_interface_and_duplicate(self):
        with patch('decklab_core.board_cases.usb_adb_interface', return_value=True):
            with self.assertRaisesRegex(ConnectionError, '已枚举 ADB 接口'):
                select_transport('', '2-1.3')
        with patch('decklab_core.board_cases.usb_adb_interface', return_value=False):
            with self.assertRaisesRegex(ConnectionError, '已登记的 USB 路径'):
                select_transport('', '2-1.3')
        with self.assertRaisesRegex(ConnectionError, '多个在线'):
            select_transport('A device usb:2-1.3 transport_id:1\nB device usb:2-1.3 transport_id:2', '2-1.3')

    def test_auto_discovery_matches_the_board_identity_not_the_adb_serial(self):
        listing = ('cloned device usb:2-1.2 transport_id:81\n'
                   'cloned device usb:2-1.3 transport_id:82\n'
                   'third offline usb:2-1.4 transport_id:83\n')
        def execute(_executable, *args, **_kwargs):
            if args[0] == 'devices':
                return SimpleNamespace(returncode=0, stdout=listing)
            self.assertIn(args[1], ('81','82'))
            serial = SERIAL if args[1] == '82' else 'OTHER-BOARD'
            return SimpleNamespace(returncode=0, stdout=f'serial={serial}\nboot_id={BOOT1}\nuid=0\nconsoles=ttyS0 tty0\n')
        with patch('decklab_core.board_cases.adb_command', side_effect=execute):
            self.assertEqual(resolve_connection('/fixture/adb', SERIAL, None),
                             {'usb_port':'2-1.3','uart':'/dev/ttyS0','cases':['rtc-wake']})
            with self.assertRaisesRegex(ConnectionError, '未找到'):
                resolve_connection('/fixture/adb', 'OFFLINE-BOARD', None)
            registered = {'usb_port':'2-1.3','uart':'/dev/ttyS0','cases':['reboot-aging']}
            self.assertEqual(resolve_connection('/fixture/adb', SERIAL, registered)['cases'], ['reboot-aging', 'rtc-wake'])

    def test_two_boards_share_one_discovery_scan_but_live_lookup_reads_again(self):
        from collections import Counter
        from concurrent.futures import ThreadPoolExecutor
        scans = AdbDiscoveryScans()
        manager = SimpleNamespace(store=SimpleNamespace(has_conflict=lambda serial: False),
                                  hardware=SimpleNamespace(board_tests={}))
        devices = [SimpleNamespace(serial=serial, connection_id='gen-' + serial, present=True)
                   for serial in ('A', 'B')]
        reported = {'81': 'A', '82': 'B'}
        calls = Counter()
        def execute(executable, *args, **options):
            if args == ('devices', '-l'):
                calls['list'] += 1
                output = 'clone device usb:1-1 transport_id:81\nclone device usb:1-2 transport_id:82'
            else:
                endpoint = args[1]
                calls[endpoint] += 1
                output = f'serial={reported[endpoint]}\nboot_id={BOOT1}'
            return SimpleNamespace(returncode=0, stdout=output)
        with patch('decklab_core.board_cases.adb_command', side_effect=execute), \
                patch('decklab_core.platform_tools.adb_executable', return_value='adb'):
            for round_number in (1, 2):
                scan = scans.get(scans.start())
                with ThreadPoolExecutor(2) as pool:
                    rows = list(pool.map(lambda device: adb_binding(manager, device, scan), devices))
                self.assertEqual([(r['serial'], r['usb_port']) for r in rows], [('A', '1-1'), ('B', '1-2')])
                self.assertEqual(calls, {'list': round_number, '81': round_number, '82': round_number})
            reported['81'] = 'replacement'
            with self.assertRaises(TaskError):
                adb_binding(manager, devices[0])
            self.assertEqual(calls['list'], 3)
            with self.assertRaises(TaskError):
                scans.get('unknown')
            self.assertEqual(calls['list'], 3)

    def test_new_adb_operation_follows_unique_board_identity_after_usb_move(self):
        listing = ('cloned device usb:2-3.2 transport_id:81\n'
                   'cloned device usb:2-3.3 transport_id:82\n')
        reported = {'81': 'OTHER-BOARD', '82': SERIAL}

        def execute(_executable, *args, **_kwargs):
            output = listing if args[0] == 'devices' else f'serial={reported[args[1]]}\nboot_id={BOOT1}\nuid=0\n'
            return SimpleNamespace(returncode=0, stdout=output)

        registered = {'usb_port': '2-1.2.3', 'uart': '/dev/ttyS0', 'cases': ['reboot-aging']}
        stages = []
        with patch('decklab_core.board_cases.adb_command', side_effect=execute), \
                patch('decklab_core.board_cases.usb_adb_interface', return_value=False):
            self.assertEqual(find_transport('/fixture/adb', SERIAL, registered, allow_relocation=True,
                                            on_stage=lambda *step: stages.append(step))[:2], ('2-3.3', '82'))
            self.assertEqual(stages, [('listing-adb-usb', '2-1.2.3'), ('matching-adb-board',),
                                      ('verified-moved-adb-port', '2-3.3')])
            with self.assertRaisesRegex(ConnectionError, '已登记的 USB 路径'):
                find_transport('/fixture/adb', SERIAL, registered)
            row = resolve_connection('/fixture/adb', SERIAL, registered)
            self.assertEqual(row, {'usb_port': '2-3.3', 'uart': '/dev/ttyS0', 'cases': ['reboot-aging', 'rtc-wake']})
            self.assertEqual(registered['usb_port'], '2-1.2.3')
            manager = SimpleNamespace(store=SimpleNamespace(has_conflict=lambda serial: False),
                                      hardware=SimpleNamespace(board_tests={SERIAL: registered}))
            device = SimpleNamespace(serial=SERIAL, connection_id='fixture', present=True)
            with patch('decklab_core.platform_tools.adb_executable', return_value='/fixture/adb'):
                self.assertEqual(adb_binding(manager, device)['usb_port'], '2-3.3')
            reported['81'] = SERIAL
            with self.assertRaisesRegex(ConnectionError, '多个 USB 调试连接'):
                find_transport('/fixture/adb', SERIAL, registered, allow_relocation=True)

    def test_rpmi_clock_uses_its_wakeup_event_counter_instead_of_the_shared_irq(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(board_case_target, 'RTC', directory):
            count_file = Path(directory) / 'device/wakeup/wakeup11/event_count'
            count_file.parent.mkdir(parents=True)
            count_file.write_text('10\n')
            def step(args):
                source = Path(args[-1])
                if source.name == 'name':
                    return 0, 'riscv-rpmi-rtc soc:rpmi_rtc@0\n'
                self.assertEqual(source, count_file)
                return 0, source.read_text()
            label, read_count, before = board_case_target.rtc_evidence(step)
            self.assertEqual(label, 'RTC 设备唤醒事件')
            self.assertEqual(before, 10)
            count_file.write_text('11\n')
            self.assertEqual(read_count(), 11)
            count_file.unlink()
            self.assertIsNone(board_case_target.rtc_evidence(step))

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
        case.manager = SimpleNamespace(store=Mock(), hardware=SimpleNamespace(board_tests={SERIAL:row}),
                                       artifacts=SimpleNamespace(policy=SimpleNamespace(max_file=8192)))
        case.row, case.case, case.rounds, case.seconds = row, 'rtc-wake', 2, None
        case.registered = row
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
        with patch('decklab_core.board_cases.FlashStore') as flash, self.assertRaisesRegex(TaskError, 'no evidence'):
            case.run(context)
        case.command.assert_called_once()
        self.assertEqual(case.command.call_args.args[:3], ('-t','82','shell'))
        last = context.store.update.call_args.kwargs['result']
        self.assertEqual(last['first_issue']['trigger'], 'unknown')
        self.assertEqual(last['first_issue']['serial_lines'], 1)
        self.assertEqual(last['serial_artifact_id'], 'artifact')
        self.assertEqual(last['first_issue']['serial_offset'], 0)
        self.assertEqual(last['first_issue']['serial_size'], len(b'<raw line>\n'))
        case.maintenance.release.assert_not_called()
        case.maintenance.keep_recovery.assert_called_once_with(context.id)
        flash.return_value.update_details.assert_called_once_with(context.id, {'recovery_required': True})

    def test_step_matching_reads_split_utf8_without_newline_and_stops_at_first_mismatch(self):
        case, context = self.fake_case()
        case.case, case.rounds = 'shell-fixture', 1
        case.custom = {'name': 'fixture', 'revision': 2, 'timeout': 10, 'commands': [
            {'command':'printf "中文通过"', 'expected':'中文通过'},
            {'command':'true # 中文通过', 'expected':'中文通过'},
            {'command':'never execute', 'expected':''}]}
        tail = b''
        def submit(*args):
            nonlocal tail
            index = case.command.call_count
            tag = f'DECKLAB-CASE {context.id} 1 STEP {index}'.encode()
            raw = '中文通过'.encode() if index == 1 else b'wrong'
            case.device.buf.extend(tag + ' CMD 中文通过\n'.encode() + tag + b' ISSUED\r\n' + tag + b' OUTPUT\r\n' + raw[:2])
            receipt = json.dumps({'serial': SERIAL, 'boot_id': BOOT1, 'status':'passed', 'reason':'exit 0'}).encode()
            tail = raw[2:] + b'\r\n' + tag + b' OUTPUT-END\r\n' + tag + b' RESULT ' + receipt + b'\r\n'
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        def finish_chunk(_seconds):
            case.device.buf.extend(tail)
        case.command = Mock(side_effect=submit)
        with patch('decklab_core.board_cases.time.sleep', side_effect=finish_chunk), \
                self.assertRaisesRegex(TaskError, '第 2 条命令 · 未看到预期串口输出'):
            case.run(context)
        self.assertEqual(case.command.call_count, 2)
        result = context.store.update.call_args.kwargs['result']['first_issue']
        self.assertEqual(result['command_results'], [
            {'index':1,'status':'passed','expected_matched':True}, {'index':2,'status':'failed','expected_matched':False}])
        self.assertEqual(result['failed_expected'], '中文通过')
        case.maintenance.release.assert_called_once_with(context.id, confirmed=True)

    def test_passing_and_failing_rounds_are_ranges_in_one_serial_archive(self):
        case, context = self.fake_case()
        case.command = Mock(return_value=SimpleNamespace(returncode=0, stdout='', stderr=''))
        logs = [b'first command and output\n', b'second command, error and UART\n']
        def observe(tag, before, baseline, evidence, row):
            index = row['round'] - 1
            case.device.buf.extend(logs[index])
            evidence.read(case.device)
            row.update(round_finished=True, boot_after=BOOT1)
            return ('failed', 'fixture failure') if index else ('passed', 'fixture pass')
        case.observe = Mock(side_effect=observe)
        saved = {}
        def archive(context, name, source):
            source.seek(0)
            saved[name] = source.read()
            return name
        case.artifact = archive
        with self.assertRaisesRegex(TaskError, 'fixture failure'):
            case.run(context)
        result = context.store.update.call_args.kwargs['result']
        self.assertEqual(saved[result['serial_artifact_id']], b''.join(logs))
        for row, raw in zip(result['recent_rounds'], logs):
            start = row['serial_offset']
            self.assertEqual(saved[result['serial_artifact_id']][start:start+row['serial_size']], raw)

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
        with patch('decklab_core.board_cases.FlashStore'), self.assertRaisesRegex(TaskError, 'no evidence'):
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
        with patch('decklab_core.board_cases.time.sleep'):
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
            cases_executed = []

            def run(operation, context):
                cases_executed.append(operation.custom)
                started.set()
                try:
                    release.wait(2)
                    return {'case':'rtc-wake','completed_rounds':1}
                finally:
                    operation.maintenance.release(context.id, confirmed=True)

            fields = {'name': 'Read version', 'script': 'test -r /etc/os-release', 'timeout': 20}
            custom = save_case(store, fields)
            payload = {'kind':'board-test','serial':SERIAL,'connection_id':device.connection_id,
                       'case':custom['id'],'case_revision':1,'rounds':1,'request_id':'test-request-1'}
            actions = TaskActions(manager)
            with patch('decklab_core.board_cases.BoardCase.run', run), \
                    patch('decklab_core.platform_tools.adb_executable', return_value='/fixture/adb'), \
                    patch('decklab_core.board_cases.find_transport', return_value=('2-3.3', '82', {'uid': '0'})) as transport:
                first = actions.start(payload, device, 'web')
                self.assertEqual(first.status, 202, first.payload)
                self.assertTrue(started.wait(2))
                self.assertEqual(manager.tasks.store.get(first.payload['task']['id'])['outcome'], 'not-issued')
                self.assertTrue(maintenance_for(manager).holds(SERIAL))
                transport.assert_called_once_with('/fixture/adb', SERIAL, manager.hardware.board_tests[SERIAL],
                                                  allow_relocation=True)
                self.assertEqual(cases_executed[0]['script'], fields['script'])
                save_case(store, dict(fields, id=custom['id'], revision=1, script='exit 1'))
                duplicate = actions.start(payload, device, 'web')
                self.assertEqual(first.payload['task']['id'], duplicate.payload['task']['id'])
                self.assertEqual(actions.start(dict(payload, request_id='test-request-2'), device, 'web').status, 409)
                self.assertEqual(actions.start(dict(payload, case_revision=2, request_id='test-request-3'), device, 'web').status, 423)
                self.assertEqual(actions.start(payload, device, 'agent').status, 403)
                release.set()
                self.assertTrue(manager.tasks.close())
            self.assertFalse(maintenance_for(manager).holds(SERIAL))
