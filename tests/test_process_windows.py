"""Win32 ownership contracts run anywhere; actual kernel checks require Windows."""
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from seriald_core import process_windows as win
from seriald_core import hardware
from seriald_core.tasks import TaskCancelled, TaskError


def dereference(pointer, kind):
    return C.cast(pointer, C.POINTER(kind)).contents


class KernelFixture:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls, self.closed, self.attributes = [], [], {}
        self.job, self.null, self.process, self.thread = [2 ** 32 + number for number in (1, 2, 3, 4)]
        self.active, self.running, self.code = 0, True, 0

    def ok(self, name):
        self.calls.append(name)
        return self.failure != name

    def error(self, operation):
        return OSError(operation + ' failed')

    def CreateJobObjectW(self, security, name):
        assert security is None and name is None
        return self.job if self.ok('CreateJobObjectW') else 0

    def SetInformationJobObject(self, job, kind, pointer, size):
        assert job == self.job and kind == 9 and size == C.sizeof(win.ExtendedLimits)
        assert dereference(pointer, win.ExtendedLimits).BasicLimitInformation.LimitFlags == 0x2000
        return self.ok('SetInformationJobObject')

    def CreateFileW(self, path, access, share, security, mode, flags, template):
        assert path == 'NUL' and dereference(security, win.SecurityAttributes).bInheritHandle == 1
        return self.null if self.ok('CreateFileW') else win.HANDLE(-1).value

    def InitializeProcThreadAttributeList(self, attributes, count, flags, size):
        assert count == 2
        if attributes is None:
            dereference(size, win.SIZE_T).value = 128
            return False
        return self.ok('InitializeProcThreadAttributeList')

    def UpdateProcThreadAttribute(self, attributes, flags, name, value, size, previous, returned):
        assert size == C.sizeof(win.HANDLE)
        self.attributes[name] = C.cast(value, C.POINTER(win.HANDLE))[0]
        return self.ok('UpdateProcThreadAttribute')

    def DeleteProcThreadAttributeList(self, attributes):
        self.calls.append('DeleteProcThreadAttributeList')

    def CreateProcessW(self, executable, command, security, thread_security, inherit, flags,
                       environment, cwd, startup, process):
        assert executable == sys.executable
        assert self.attributes == {0x2000D: self.job, 0x20002: self.null}
        assert inherit and flags == 0x80000 | 0x8000000
        info = dereference(startup, win.StartupInfoEx)
        assert info.StartupInfo.cb == C.sizeof(win.StartupInfoEx)
        assert info.StartupInfo.dwFlags == 0x100 and info.StartupInfo.hStdError == self.null
        assert info.lpAttributeList
        self.command = command.value
        if not self.ok('CreateProcessW'):
            return False
        info = dereference(process, win.ProcessInfo)
        info.hProcess, info.hThread, info.dwProcessId = self.process, self.thread, 123
        self.active = 2
        return True

    def WaitForSingleObject(self, handle, milliseconds):
        assert handle == self.process
        return 258 if self.running else 0

    def GetExitCodeProcess(self, handle, pointer):
        dereference(pointer, win.DWORD).value = self.code
        return self.ok('GetExitCodeProcess')

    def TerminateJobObject(self, job, code):
        assert job == self.job
        if not self.ok('TerminateJobObject'):
            return False
        self.active, self.running = 0, False
        return True

    def QueryInformationJobObject(self, job, kind, pointer, size, returned):
        assert job == self.job and kind == 1
        dereference(pointer, win.Accounting).ActiveProcesses = self.active
        return self.ok('QueryInformationJobObject')

    def CloseHandle(self, handle):
        assert handle not in self.closed
        self.closed.append(handle)
        if handle == self.job:
            self.active, self.running = 0, False
        return True


class WindowsProcessContractTests(unittest.TestCase):
    def test_x64_abi_uses_windows_fixed_width_fields_and_pointer_sized_handles(self):
        self.assertEqual(C.sizeof(win.DWORD), 4)
        if C.sizeof(win.HANDLE) == 8:
            for structure, size in ((win.BasicLimits, 64), (win.ExtendedLimits, 144),
                (win.StartupInfo, 104), (win.StartupInfoEx, 112), (win.ProcessInfo, 24),
                (win.Accounting, 48), (win.SecurityAttributes, 24)):
                self.assertEqual(C.sizeof(structure), size, structure.__name__)

    def test_atomic_job_and_handle_allowlist_precede_execution_and_close_is_idempotent(self):
        kernel = KernelFixture()
        argv = [sys.executable, 'space and 中文', 'a"b', 'slash\\']
        process = win.WindowsProcess(argv, kernel)
        self.assertEqual(kernel.command, subprocess.list2cmdline(argv))
        self.assertEqual(kernel.closed, [kernel.thread, kernel.null])
        self.assertIsNone(process.poll())
        kernel.running, kernel.code = False, 259  # A real exit code 259 is not STILL_ACTIVE here.
        self.assertEqual(process.poll(), 259)
        process.close(); process.close()
        self.assertEqual(kernel.active, 0)
        self.assertEqual(set(kernel.closed), {kernel.job, kernel.process, kernel.thread, kernel.null})

    def test_startup_failure_never_falls_back_to_uncontained_execution_and_releases_handles(self):
        for failure in ('CreateJobObjectW', 'SetInformationJobObject', 'CreateFileW',
                        'InitializeProcThreadAttributeList', 'UpdateProcThreadAttribute', 'CreateProcessW'):
            kernel = KernelFixture(failure)
            with self.subTest(failure=failure), self.assertRaisesRegex(OSError, failure):
                win.WindowsProcess([sys.executable], kernel)
            self.assertEqual(kernel.active, 0)
            self.assertNotIn(kernel.process, kernel.closed)
            if failure != 'CreateJobObjectW':
                self.assertIn(kernel.job, kernel.closed)
            if failure != 'CreateProcessW':
                self.assertNotIn('CreateProcessW', kernel.calls)

    def test_cleanup_errors_still_close_the_kill_on_close_job_and_root_handle(self):
        for failure in ('TerminateJobObject', 'QueryInformationJobObject'):
            kernel = KernelFixture()
            process = win.WindowsProcess([sys.executable], kernel)
            kernel.failure = failure
            with self.assertRaisesRegex(OSError, failure):
                process.close()
            self.assertEqual(kernel.active, 0)
            self.assertIn(kernel.job, kernel.closed)
            self.assertIn(kernel.process, kernel.closed)

    def test_command_line_bound_is_checked_before_creating_a_job(self):
        kernel = KernelFixture()
        with self.assertRaises(ValueError):
            win.WindowsProcess([sys.executable, 'x' * 32767], kernel)
        self.assertEqual(kernel.calls, [])

    def test_runner_closes_windows_tree_on_timeout_and_cancel(self):
        for cancelled in (False, True):
            kernel = KernelFixture()
            process = win.WindowsProcess([sys.executable], kernel)
            count = 0
            def check():
                nonlocal count
                count += 1
                if cancelled and count > 1:
                    raise TaskCancelled('cancelled')
            with mock.patch.object(hardware, 'helper_process', return_value=process), \
                    mock.patch.object(hardware.time, 'monotonic', side_effect=[0, 2, 2]), \
                    self.assertRaises(TaskCancelled if cancelled else TaskError):
                hardware.run_command([sys.executable], 1, check)
            self.assertEqual(kernel.active, 0)
            self.assertIn(kernel.job, kernel.closed)


@unittest.skipUnless(os.name == 'nt', 'requires actual Windows Job Objects')
class NativeWindowsProcessTests(unittest.TestCase):
    def test_real_unicode_arguments_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '参数.json'
            code = 'import json,sys;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]),encoding="utf8")'
            result = hardware.run_command([sys.executable, '-c', code, str(target), 'a"b', '中文 空格', 'slash\\'], 5, lambda: None)
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(json.loads(target.read_text(encoding='utf8')), ['a"b', '中文 空格', 'slash\\'])

    def test_real_descendants_die_when_helper_root_exits_first(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready'
            code = ('import subprocess,sys;from pathlib import Path;'
                    'subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
                    'Path(sys.argv[1]).touch()')
            process = win.WindowsProcess([sys.executable, '-c', code, str(ready)])
            try:
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(process.poll(), 0)
                self.assertTrue(ready.exists())
                info = win.Accounting()
                self.assertTrue(process.kernel.QueryInformationJobObject(process.job, 1, C.byref(info), C.sizeof(info), None))
                self.assertGreaterEqual(info.ActiveProcesses, 1)
            finally:
                process.close()  # Adapter verifies zero active job members before releasing handles.

    def test_real_host_crash_closes_job_and_terminates_children(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready.json'
            helper = ('import os,subprocess,sys,time,json;from pathlib import Path;'
                      'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
                      'ready=Path(sys.argv[1]);temporary=ready.with_suffix(".tmp");'
                      'temporary.write_text(json.dumps([os.getpid(),child.pid]));'
                      'temporary.replace(ready);time.sleep(60)')
            owner_code = ('import sys,time;from seriald_core.process_windows import WindowsProcess;'
                          'process=WindowsProcess([sys.executable,"-c",%r,sys.argv[1]]);time.sleep(60)' % helper)
            owner = subprocess.Popen([sys.executable, '-c', owner_code, str(ready)],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            kernel, handles = win.NativeKernel(), []
            open_process = kernel.api.OpenProcess
            open_process.argtypes, open_process.restype = [win.DWORD, win.BOOL, win.DWORD], win.HANDLE
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), 'contained process did not start')
                for pid in json.loads(ready.read_text()):
                    handle = open_process(0x00100000, False, pid)  # SYNCHRONIZE only.
                    self.assertTrue(handle)
                    handles.append(handle)
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                owner.kill(); owner.wait(timeout=5)
                for handle in handles:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            finally:
                if owner.poll() is None:
                    owner.kill(); owner.wait(timeout=5)
                for handle in handles:
                    kernel.CloseHandle(handle)
