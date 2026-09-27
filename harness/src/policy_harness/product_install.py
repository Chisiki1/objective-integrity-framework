"""Small standalone, staged Windows package installer. Uses only the standard library.

The running service never overwrites its loaded files. This helper runs from the
verified new package, waits for the old service and desktop file locks, then
replaces only package-owned files. Runtime data and unknown files stay in place.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
from uuid import uuid4


PROTECTED = {'.runtime', '.work', '.venv', '.git', '.env', 'settings.json', 'credentials.json'}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest().upper()


def plain(path, *, missing=False):
    path = Path(path).absolute()
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Redirected update path')
    return path


def member(root, name, *, checked_directories=None):
    if not isinstance(name, str) or '\\' in name or ':' in name or any(ord(c) < 32 for c in name):
        raise ValueError('Invalid package path')
    parts = PurePosixPath(name).parts
    if (not parts or PurePosixPath(name).is_absolute() or '/'.join(parts) != name
            or any(p in {'.', '..'} or p.endswith(('.', ' ')) for p in parts)
            or parts[0].lower() in PROTECTED or name.lower() == 'application.json'
            or any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p) for p in parts)):
        raise ValueError('Protected or invalid package path')
    target = Path(root).joinpath(*parts)
    if checked_directories is None:
        return plain(target, missing=True)
    # A package has thousands of files sharing the same directories. Check each
    # directory once in this bounded read, and each leaf separately; later write
    # operations still perform their own fresh path checks.
    for path in [*reversed(target.parents), target]:
        if path != target and path in checked_directories:
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Redirected update path')
        if path != target:
            checked_directories.add(path)
    return target


def atomic(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    replace_bytes(path, raw)


def replace_bytes(path, raw):
    path = plain(path, missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.oif-' + uuid4().hex + '.tmp')
    plain(temporary, missing=True)
    with temporary.open('xb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def manifest(root, *, verify=True):
    root = plain(root)
    path = plain(root / 'application.json')
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Package manifest is too large')
    value = json.loads(path.read_bytes())
    if value.get('schema') != 'oif-desktop-package-v1' or not isinstance(value.get('version'), str):
        raise ValueError('A complete OIF desktop package is required')
    rows = value.get('members')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 30000:
        raise ValueError('Invalid package inventory')
    result, seen, directories = {}, set(), set()
    for row in rows:
        name = row['path']; target = member(root, name, checked_directories=directories)
        if name.casefold() in seen or not re.fullmatch('[A-Fa-f0-9]{64}', row['sha256']):
            raise ValueError('Duplicate or invalid package member')
        seen.add(name.casefold()); result[name] = row
        if verify and (not target.is_file() or target.stat().st_size != row['bytes'] or digest(target) != row['sha256'].upper()):
            raise ValueError('Installed application files have changed: ' + name)
    return value, result


def make_plan(root, staged, work):
    """Read every managed input before writing a rollback plan."""
    root, staged, work = plain(root), plain(staged), plain(work)
    before, old = manifest(root); after, new = manifest(staged)
    entries, directories = [], set()
    names = set(old) | set(new)
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError('Package paths changed letter case; use a fresh installation')
    for name in sorted(names):
        target = member(root, name, checked_directories=directories)
        previous, following = old.get(name), new.get(name)
        if previous and following and previous['sha256'].upper() == following['sha256'].upper():
            continue
        if not previous and target.exists():
            raise ValueError('A local file would be overwritten: ' + name)
        entries.append({'path': name, 'before': previous['sha256'].upper() if previous else None,
                        'after': following['sha256'].upper() if following else None})
    entries.append({'path': 'application.json', 'before': digest(root / 'application.json'),
                    'after': digest(staged / 'application.json')})
    plan = {'schema': 'oif-product-install-v1', 'root': str(root), 'staged': str(staged),
            'from_version': before['version'], 'to_version': after['version'],
            'entries': entries, 'state': 'prepared'}
    atomic(work / 'install.json', plan)
    return plan


def plan_path(root, name):
    return plain(root / name, missing=True) if name == 'application.json' else member(root, name)


@contextmanager
def update_lock(work, name='install.lock'):
    handle = plain(work / name, missing=True).open('a+b')
    if handle.tell() == 0:
        handle.write(b'0'); handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def rollback(work, plan):
    root = plain(plan['root']); backup = work / 'backup'
    for entry in reversed(plan['entries']):
        target = plan_path(root, entry['path'])
        current = digest(target) if target.exists() else None
        if current == entry['before']:
            continue
        if current != entry['after']:
            raise ValueError('A changed file needs manual recovery: ' + entry['path'])
        if entry['before'] is None:
            target.unlink()
        else:
            saved = plain(backup / entry['path'])
            if digest(saved) != entry['before']:
                raise ValueError('Update backup changed')
            replace_bytes(target, saved.read_bytes())
    plan['state'] = 'rolled_back'; atomic(work / 'install.json', plan)


def apply_plan(work, *, recover=False):
    work = plain(work)
    with update_lock(work):
        plan = json.loads(plain(work / 'install.json').read_bytes())
        if plan.get('schema') != 'oif-product-install-v1':
            raise ValueError('Invalid update plan')
        root, staged = plain(plan['root']), plain(plan['staged'])
        if plan['state'] == 'installed':
            manifest(root)
            return plan
        if recover or plan['state'] == 'applying':
            if plan['state'] == 'applying':
                rollback(work, plan)
            return plan
        if plan['state'] != 'prepared':
            raise ValueError('This update attempt is already closed')
        manifest(root); manifest(staged)
        # All preimages are durable before the first live application write.
        for entry in plan['entries']:
            target = plan_path(root, entry['path'])
            current = digest(target) if target.exists() else None
            if current != entry['before']:
                raise ValueError('Application changed after update preparation')
            if entry['before']:
                saved = plain(work / 'backup' / entry['path'], missing=True)
                saved.parent.mkdir(parents=True, exist_ok=True)
                with saved.open('xb') as stream:
                    stream.write(target.read_bytes()); stream.flush(); os.fsync(stream.fileno())
        plan['state'] = 'applying'; atomic(work / 'install.json', plan)
        try:
            for entry in plan['entries']:
                target = plan_path(root, entry['path'])
                if entry['after'] is None:
                    target.unlink()
                else:
                    source = plan_path(staged, entry['path'])
                    if digest(source) != entry['after']:
                        raise ValueError('Staged update changed')
                    replace_bytes(target, source.read_bytes())
            manifest(root)
            plan['state'] = 'installed'; atomic(work / 'install.json', plan)
        except BaseException:
            rollback(work, plan)
            raise
        return plan


def wait_windows_files(root, entries, timeout=90):
    """Windows denies exclusive write handles while executables/DLLs are loaded."""
    if os.name != 'nt':
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    until = time.monotonic() + timeout
    # OIF.exe is always included, even if a future update reuses its bytes.
    names = sorted({'OIF.exe'} | {e['path'] for e in entries})
    while True:
        locked = False
        for name in names:
            path = plan_path(root, name)
            if not path.exists():
                continue
            handle = kernel.CreateFileW(str(path), 0x40000000, 0, None, 3, 0, None)
            if handle == wintypes.HANDLE(-1).value:
                if ctypes.get_last_error() not in {5, 32, 33}:
                    raise OSError(ctypes.get_last_error(), 'Cannot access application file')
                locked = True; break
            kernel.CloseHandle(handle)
        if not locked:
            return
        if time.monotonic() >= until:
            raise TimeoutError('Close the OIF window before installing. Application files were retained.')
        time.sleep(.4)


def _run_helper(work, data_dir, *, recover=False, reopen=True):
    work, data_dir = plain(work), plain(data_dir)
    plan = json.loads(plain(work / 'install.json').read_bytes())
    root = plain(plan['root']); pending = data_dir / 'product-update-pending.json'
    try:
        # Recovery has the same ownership and file-lock boundaries as install.
        # The native launcher releases its own EXE before asking us to recover.
        from contextlib import ExitStack
        with ExitStack() as stack:
            until = time.monotonic() + 90
            while True:
                handles = []
                try:
                    if os.name == 'nt':
                        import msvcrt
                        for name in ('service-owner.lock', 'worker-owner.lock'):
                            h = (data_dir / name).open('a+b'); handles.append(h)
                            h.seek(0); msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    for h in handles: h.close()
                    if time.monotonic() >= until: raise TimeoutError('OIF is still stopping')
                    time.sleep(.4)
            for h in handles: stack.callback(h.close)
            wait_windows_files(root, plan['entries'])
            result = apply_plan(work, recover=recover)
        atomic(data_dir / 'product-update-result.json', {'status': result['state'], 'version': result['to_version'],
               'backup': str(work), 'time': time.time()})
        pending.unlink(missing_ok=True)
    except BaseException as error:
        atomic(data_dir / 'product-update-result.json', {'status': 'failed', 'error': str(error), 'backup': str(work), 'time': time.time()})
        # Only terminal/no-write failures clear the launch hold. An interrupted
        # applying plan keeps it, so normal startup can recover the preimages.
        state = json.loads((work / 'install.json').read_bytes())['state']
        if state in {'prepared', 'rolled_back'}:
            pending.unlink(missing_ok=True)
        raise
    finally:
        if reopen and not pending.exists() and os.name == 'nt':
            subprocess.Popen([str(root / 'OIF.exe')], cwd=root, close_fds=True,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    return result


def run_helper(work, data_dir, *, recover=False, reopen=True):
    with update_lock(plain(work), 'helper.lock'):
        return _run_helper(work, data_dir, recover=recover, reopen=reopen)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--no-reopen', action='store_true')
    args = parser.parse_args()
    run_helper(args.work, args.data_dir, recover=args.recover, reopen=not args.no_reopen)
