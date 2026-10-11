"""Windows 10+ helper containment, assigned atomically at process creation.

Only this adapter uses Win32 handles. JOB_LIST closes the create/assign race;
KILL_ON_JOB_CLOSE stops descendants if the collector exits unexpectedly.
"""
import ctypes as C
import os
import subprocess
import time

DWORD = C.c_uint32
BOOL = C.c_int32
WORD = C.c_uint16
HANDLE = C.c_void_p
SIZE_T = C.c_size_t


class BasicLimits(C.Structure):
    _fields_ = [('PerProcessUserTimeLimit', C.c_int64), ('PerJobUserTimeLimit', C.c_int64),
                ('LimitFlags', DWORD), ('MinimumWorkingSetSize', SIZE_T), ('MaximumWorkingSetSize', SIZE_T),
                ('ActiveProcessLimit', DWORD), ('Affinity', SIZE_T), ('PriorityClass', DWORD), ('SchedulingClass', DWORD)]


class IOCounters(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimits(C.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IOCounters),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class Accounting(C.Structure):
    _fields_ = [(name, C.c_int64) for name in ('TotalUserTime', 'TotalKernelTime',
                'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                (name, DWORD) for name in ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]


class SecurityAttributes(C.Structure):
    _fields_ = [('nLength', DWORD), ('lpSecurityDescriptor', HANDLE), ('bInheritHandle', BOOL)]


class StartupInfo(C.Structure):
    _fields_ = [('cb', DWORD), ('lpReserved', C.c_wchar_p), ('lpDesktop', C.c_wchar_p), ('lpTitle', C.c_wchar_p)] + [
        (name, DWORD) for name in ('dwX', 'dwY', 'dwXSize', 'dwYSize', 'dwXCountChars', 'dwYCountChars',
                                  'dwFillAttribute', 'dwFlags')] + [
        ('wShowWindow', WORD), ('cbReserved2', WORD), ('lpReserved2', HANDLE),
        ('hStdInput', HANDLE), ('hStdOutput', HANDLE), ('hStdError', HANDLE)]


class StartupInfoEx(C.Structure):
    _fields_ = [('StartupInfo', StartupInfo), ('lpAttributeList', HANDLE)]


class ProcessInfo(C.Structure):
    _fields_ = [('hProcess', HANDLE), ('hThread', HANDLE), ('dwProcessId', DWORD), ('dwThreadId', DWORD)]


class NativeKernel:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Win32 helper adapter requires Windows')
        self.api = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': (HANDLE, [HANDLE, C.c_wchar_p]),
            'SetInformationJobObject': (BOOL, [HANDLE, C.c_int, HANDLE, DWORD]),
            'QueryInformationJobObject': (BOOL, [HANDLE, C.c_int, HANDLE, DWORD, HANDLE]),
            'TerminateJobObject': (BOOL, [HANDLE, C.c_uint]),
            'CreateFileW': (HANDLE, [C.c_wchar_p, DWORD, DWORD, C.POINTER(SecurityAttributes), DWORD, DWORD, HANDLE]),
            'InitializeProcThreadAttributeList': (BOOL, [HANDLE, DWORD, DWORD, C.POINTER(SIZE_T)]),
            'UpdateProcThreadAttribute': (BOOL, [HANDLE, DWORD, SIZE_T, HANDLE, SIZE_T, HANDLE, HANDLE]),
            'DeleteProcThreadAttributeList': (None, [HANDLE]),
            'CreateProcessW': (BOOL, [C.c_wchar_p, C.c_wchar_p, HANDLE, HANDLE, BOOL, DWORD,
                                     HANDLE, C.c_wchar_p, C.POINTER(StartupInfoEx), C.POINTER(ProcessInfo)]),
            'WaitForSingleObject': (DWORD, [HANDLE, DWORD]),
            'GetExitCodeProcess': (BOOL, [HANDLE, C.POINTER(DWORD)]),
            'CloseHandle': (BOOL, [HANDLE]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.api, name)
            function.restype, function.argtypes = result, arguments
            setattr(self, name, function)

    @staticmethod
    def error(operation):
        error = C.WinError(C.get_last_error())
        return OSError('%s: %s' % (operation, error))


class WindowsProcess:
    def __init__(self, argv, kernel=None):
        self.kernel = kernel if kernel is not None else NativeKernel()
        self.job, self.process, self.returncode = None, None, None
        command = subprocess.list2cmdline(argv)
        if len(command.encode('utf-16-le')) // 2 >= 32767:
            raise ValueError('hardware command exceeds Windows command-line limit')
        k = self.kernel
        null, attributes, initialized, thread = None, None, False, None
        try:
            self.job = k.CreateJobObjectW(None, None)
            if not self.job:
                raise k.error('CreateJobObjectW')
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
            self._require(k.SetInformationJobObject(self.job, 9, C.byref(limits), C.sizeof(limits)), 'SetInformationJobObject')
            security = SecurityAttributes(C.sizeof(SecurityAttributes), None, True)
            null = k.CreateFileW('NUL', 0xC0000000, 3, C.byref(security), 3, 0, None)
            if null in (None, 0, HANDLE(-1).value):
                null = None
                raise k.error('CreateFileW')
            size = SIZE_T()
            k.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            if not size.value:
                raise k.error('InitializeProcThreadAttributeList(size)')
            attributes = C.create_string_buffer(size.value)
            self._require(k.InitializeProcThreadAttributeList(attributes, 2, 0, C.byref(size)), 'InitializeProcThreadAttributeList')
            initialized = True
            jobs, handles = (HANDLE * 1)(self.job), (HANDLE * 1)(null)
            for name, value in ((0x2000D, jobs), (0x20002, handles)):
                self._require(k.UpdateProcThreadAttribute(attributes, 0, name, value, C.sizeof(value), None, None),
                              'UpdateProcThreadAttribute')
            startup = StartupInfoEx()
            startup.StartupInfo.cb = C.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # USESTDHANDLES: only the NUL handle is inherited.
            startup.StartupInfo.hStdInput = startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = null
            startup.lpAttributeList = C.cast(attributes, HANDLE)
            info = ProcessInfo()
            mutable_command = C.create_unicode_buffer(command)
            self._require(k.CreateProcessW(argv[0], mutable_command, None, None, True,
                                          0x80000 | 0x8000000, None, None, C.byref(startup), C.byref(info)),
                          'CreateProcessW')  # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW
            self.process, thread, self.pid = info.hProcess, info.hThread, info.dwProcessId
        except BaseException:
            try:
                self.close()
            except OSError:
                pass  # close() still closes the kill-on-close job and process.
            raise
        finally:
            if thread:
                k.CloseHandle(thread)
            if initialized:
                k.DeleteProcThreadAttributeList(attributes)
            if null:
                k.CloseHandle(null)

    def _require(self, result, operation):
        if not result:
            raise self.kernel.error(operation)

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        result = self.kernel.WaitForSingleObject(self.process, 0)
        if result == 258:  # WAIT_TIMEOUT, not an exit code sentinel.
            return None
        if result != 0:
            raise self.kernel.error('WaitForSingleObject')
        code = DWORD()
        self._require(self.kernel.GetExitCodeProcess(self.process, C.byref(code)), 'GetExitCodeProcess')
        self.returncode = code.value
        return self.returncode

    def close(self):
        k = self.kernel
        try:
            if self.job:
                self._require(k.TerminateJobObject(self.job, 1), 'TerminateJobObject')
                deadline = time.monotonic() + 3
                while True:
                    accounting = Accounting()
                    self._require(k.QueryInformationJobObject(self.job, 1, C.byref(accounting), C.sizeof(accounting), None),
                                  'QueryInformationJobObject')
                    if not accounting.ActiveProcesses:
                        break
                    if time.monotonic() >= deadline:
                        raise OSError('Windows helper process tree did not stop')
                    time.sleep(0.01)
                if self.process and k.WaitForSingleObject(self.process, 500) != 0:
                    raise OSError('Windows helper root process did not stop')
        finally:
            # Job handle is non-inherited. Closing it is a second kill guarantee
            # even if querying termination failed or the host is shutting down.
            for field in ('job', 'process'):
                handle = getattr(self, field)
                if handle:
                    k.CloseHandle(handle)
                    setattr(self, field, None)
