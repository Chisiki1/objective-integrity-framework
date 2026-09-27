"""Current-user execution, available only through explicitly selected full access.

The small launcher waits for the controller's go message. On Windows it is
assigned to a kill-on-close Job Object before it can spawn user code. Descendants
inherit that job (Microsoft AssignProcessToJobObject / Job Objects documentation).
This contains process lifetime, not filesystem or network authority.
"""
import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from .access_paths import operation_path
from .capabilities import sha_file
from .models import OperationResult, PolicyError


LAUNCHER = """import json,subprocess,sys
line=sys.stdin.buffer.readline()
if not line: sys.exit(125)
request=json.loads(line)
try:
    result=subprocess.run(request['command'],cwd=request['cwd'],stdin=subprocess.DEVNULL)
    sys.exit(result.returncode)
except Exception as error:
    print(str(error),file=sys.stderr)
    sys.exit(125)
"""


class WindowsJob:
    def __init__(self, pid):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', w.DWORD), ('min_working', ctypes.c_size_t),
                        ('max_working', ctypes.c_size_t), ('active', w.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, args, result in [
            ('CreateJobObjectW', [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            ('SetInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            ('OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL),
            ('CloseHandle', [w.HANDLE], w.BOOL),
        ]:
            fn = getattr(self.api, name); fn.argtypes = args; fn.restype = result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            info = Extended(); info.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not self.api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def command_input(workspace, args):
    if set(args) - {'command', 'cwd', 'timeout_seconds'}:
        raise PolicyError('COMMAND_ARGUMENTS: use command, cwd and timeout_seconds')
    command = args.get('command')
    if (not isinstance(command, list) or not command or len(command) > 256
            or any(not isinstance(v, str) or '\x00' in v for v in command) or not command[0]):
        raise PolicyError('COMMAND_ARGUMENTS: command must be an argv list, e.g. ["git", "status"]')
    if sum(len(v) for v in command) > 64000:
        raise PolicyError('COMMAND_ARGUMENTS: command is too long')
    seconds = args.get('timeout_seconds', 60)
    if type(seconds) not in (int, float) or not 1 <= seconds <= 600:
        raise PolicyError('COMMAND_TIMEOUT: timeout_seconds must be 1..600')
    cwd = operation_path(workspace, args.get('cwd', '.'), full_access=True, allow_root=True)
    if not cwd.is_dir():
        raise PolicyError('COMMAND_CWD: choose an existing directory')
    return dict(command=command, cwd=str(cwd), timeout_seconds=seconds)


async def execute_native(executor, workspace, operation, record):
    args = command_input(workspace, operation.args)
    record.update(native=True, phase='native_prepared', native_command=args)
    executor._save(record)
    directory = executor.control / ('native-' + operation.id)
    directory.mkdir(exist_ok=False)
    record['streams'] = str(directory)
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-u', '-c', LAUNCHER,
        cwd=executor.control, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **options)
    job, readers, previews = None, [], {}
    truncated = False

    async def capture(channel, stream):
        nonlocal truncated
        count = 0; preview = bytearray()
        with (directory / (channel + '.bin')).open('xb') as output:
            while chunk := await stream.read(65536):
                if len(preview) < 131072:
                    preview.extend(chunk[:131072-len(preview)])
                kept = chunk[:max(0, 4 * 1024 * 1024 - count)]
                output.write(kept); count += len(kept)
                if len(kept) != len(chunk):
                    truncated = True
        previews[channel] = preview.decode('utf-8', errors='replace')

    def stop_tree():
        if job is not None:
            job.close()
        elif os.name != 'nt':
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        elif process.returncode is None:
            process.kill()  # Unassigned launcher has not received go.

    async def wait_for_exit():
        # asyncio Process.wait() can await pipe EOF after the parent has exited.
        # Descendants may still own those pipes; close their job before draining.
        while process.returncode is None:
            await asyncio.sleep(.02)

    try:
        if os.name == 'nt':
            job = WindowsJob(process.pid)
        readers = [asyncio.create_task(capture(name, stream)) for name, stream in
                   [('stdout', process.stdout), ('stderr', process.stderr)]]
        record.update(phase='native_started', native_pid=process.pid, target_started=True)
        executor._save(record)
        process.stdin.write((json.dumps(args, ensure_ascii=True) + '\n').encode())
        await process.stdin.drain()
        process.stdin.close()
        try:
            await asyncio.wait_for(wait_for_exit(), args['timeout_seconds'])
        except asyncio.TimeoutError:
            executor._first_fault(record, 'PROGRAM_TIMEOUT', 'Native command exceeded its time limit')
        except asyncio.CancelledError:
            executor._first_fault(record, 'CANCELLED', 'Native command stopped by the controller')
    finally:
        # Also close descendants after a normal command exit; background processes
        # are not detached from an operation and silently left behind.
        stop_tree()
        await process.wait()
        if readers:
            await asyncio.gather(*readers)
        else:
            await process.communicate()
    record.update(native_exit_code=process.returncode, native_tree_stopped=True,
                  output_truncated=truncated)
    if process.returncode != 0 and record.get('first_fault') is None:
        executor._first_fault(record, 'PROGRAM_EXIT_NONZERO', str(process.returncode))
    streams = [{'path': str(directory / (channel + '.bin')),
                'sha256': sha_file(directory / (channel + '.bin')),
                'bytes': (directory / (channel + '.bin')).stat().st_size,
                'channel': channel, 'preview_truncated': (directory / (channel + '.bin')).stat().st_size > 131072}
               for channel in ('stdout', 'stderr')]
    return OperationResult(operation_id=operation.id,
        status='failed' if record.get('first_fault') else 'succeeded', effect='confirmed',
        exit_code=process.returncode, stdout=previews.get('stdout', ''), stderr=previews.get('stderr', ''),
        artifacts=streams,
        data={'execution': 'native', 'cwd': args['cwd'], 'output_truncated': truncated,
              'preview_truncated': any(s['preview_truncated'] for s in streams),
              'process_tree_stopped': True, 'capture_complete': not truncated,
              'proof_ceiling': 'Observed current-user program exit; filesystem/network effects require task-specific verification. Read output files with file_read to register verified artifacts.'})
