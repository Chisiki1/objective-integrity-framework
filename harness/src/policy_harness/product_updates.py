"""Optional desktop releases, separate from evidence-based harness self-improvement."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4
import zipfile

import httpx

from . import __version__
from .product_install import atomic, digest, make_plan, manifest, member, plain

REPOSITORY = 'Chisiki1/objective-integrity-framework'
RELEASES = 'https://api.github.com/repos/' + REPOSITORY + '/releases?per_page=30'
PAGE = 'https://github.com/' + REPOSITORY + '/releases'
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024


def version_key(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:(?:-beta\.|b)(\d+))?', str(value))
    if not match:
        raise ValueError('Unsupported release version')
    a, b, c, beta = match.groups()
    return int(a), int(b), int(c), int(beta is None), int(beta or 0)


def fetch(url, limit):
    """Bounded HTTPS only, including each redirect; never use provider credentials."""
    allowed = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
    with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                      headers={'User-Agent': 'OIF-desktop-updates', 'Accept': 'application/vnd.github+json'}) as client:
        for _ in range(5):
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password or parsed.port not in {None, 443}:
                raise ValueError('Release URL is outside the official download hosts')
            with client.stream('GET', url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers['location'])); continue
                response.raise_for_status()
                if int(response.headers.get('content-length', '0')) > limit:
                    raise ValueError('Release download is too large')
                parts, size = [], 0
                for block in response.iter_bytes():
                    size += len(block)
                    if size > limit: raise ValueError('Release download is too large')
                    parts.append(block)
                return b''.join(parts)
    raise ValueError('Too many release redirects')


def select_release(rows, current):
    chosen = []
    for row in rows:
        tag = row.get('tag_name', '')
        try:
            key = version_key(tag)
        except ValueError:
            continue
        if row.get('draft') or key <= version_key(current):
            continue
        # Stable installations stay on stable releases; a beta also sees betas.
        if version_key(current)[3] and (row.get('prerelease') or not key[3]):
            continue
        version = tag.removeprefix('v'); name = f'OIF-Desktop-{version}-windows-x64.zip'
        matches = [a for a in row.get('assets', []) if a.get('name') == name and a.get('state') == 'uploaded']
        if len(matches) != 1: continue
        asset = matches[0]; expected = PAGE + '/download/' + tag + '/' + name
        if asset.get('browser_download_url') != expected or not 0 < asset.get('size', 0) <= MAX_ARCHIVE:
            continue
        sha = asset.get('digest') or ''
        if not re.fullmatch('sha256:[a-fA-F0-9]{64}', sha): continue
        chosen.append((key, {'version': version, 'tag': tag, 'url': expected, 'bytes': asset['size'],
                            'sha256': sha[7:].upper(), 'page': PAGE + '/tag/' + tag}))
    return max(chosen, key=lambda pair: pair[0])[1] if chosen else None


def extract_package(raw, destination, release):
    destination = plain(destination, missing=True)
    prefix = 'OIF-Desktop-' + release['version'] + '/'
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        rows = archive.infolist()
        if len(rows) > 30001 or sum(r.file_size for r in rows) > MAX_EXPANDED or any(r.file_size > 256 * 1024 * 1024 for r in rows):
            raise ValueError('Package exceeds the installation limit')
        if shutil.disk_usage(destination.parent).free < sum(r.file_size for r in rows) * 2 + len(raw):
            raise ValueError('Not enough free space to stage and back up OIF')
        names, entries, directories = set(), {}, set()
        for row in rows:
            if not row.filename.startswith(prefix) or row.is_dir() or row.flag_bits & 1:
                raise ValueError('Invalid desktop package layout')
            name = row.filename[len(prefix):]
            if row.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError('Redirected package member')
            if name.casefold() in names: raise ValueError('Duplicate package member')
            names.add(name.casefold())
            target = plain(destination / name, missing=True) if name == 'application.json' else member(destination, name, checked_directories=directories)
            entries[name] = (row, target)
        if 'application.json' not in entries: raise ValueError('Package manifest is missing')
        if entries['application.json'][0].file_size > 8 * 1024 * 1024: raise ValueError('Package manifest is too large')
        metadata = json.loads(archive.read(entries['application.json'][0]))
        if metadata.get('version') != release['version']: raise ValueError('Package version changed')
        inventory = {m['path']: m for m in metadata.get('members', [])}
        if set(inventory) | {'application.json'} != set(entries) or len(inventory) != len(metadata['members']):
            raise ValueError('Package inventory differs from the archive')
        destination.mkdir()
        for name, (row, target) in entries.items():
            data = archive.read(row)
            if name != 'application.json' and (len(data) != inventory[name]['bytes'] or
                    hashlib.sha256(data).hexdigest().upper() != inventory[name]['sha256'].upper()):
                raise ValueError('Package member verification failed')
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as handle: handle.write(data)
        manifest(destination)
        for name in ('OIF.exe', 'python/python.exe', 'src/policy_harness/product_install.py'):
            if name not in inventory: raise ValueError('Required updater files are missing')


def owned_record(root, data, launch):
    record = json.loads(plain(data / 'server-process.json').read_bytes())
    if (not launch or record.get('launch_id') != launch or Path(record.get('data_dir', '')).resolve() != data.resolve()
            or Path(record.get('executable', '')).resolve() not in {(root / 'python/python.exe').resolve(), (root / '.venv/Scripts/python.exe').resolve()}):
        raise ValueError('Service ownership changed; reopen OIF')
    return record


def launch_restart(root, data, launch):
    if os.name != 'nt': raise ValueError('Desktop restart requires Windows')
    owned_record(root, data, launch)
    script = plain(root / 'scripts/Restart-Harness.ps1')
    log = plain(data / 'desktop-restart.log', missing=True)
    with log.open('ab') as output:
        subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                          '-File', str(script), '-DataDirectory', str(data), '-LaunchId', launch],
                         cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                         creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


class ProductUpdates:
    def __init__(self, root, data, *, downloader=fetch):
        self.root, self.data = Path(root).resolve(), Path(data).resolve()
        self.directory = plain(self.data / 'product-updates', missing=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cache = self.directory / 'check.json'; self.lock = threading.Lock(); self.downloader = downloader
        self.prepared = None

    def status(self):
        try:
            cached = json.loads(plain(self.cache).read_bytes())
        except (OSError, ValueError):
            cached = {}
        available = cached.get('available')
        if available:
            try:
                newer = version_key(available['version']) > version_key(__version__)
                compatible = not version_key(__version__)[3] or version_key(available['version'])[3]
            except (KeyError, TypeError, ValueError):
                newer = compatible = False
            if not newer or not compatible:
                cached = {**cached, 'available': None, 'state': 'current'}
        installable = (os.name == 'nt' and self.data == self.root / '.runtime'
                       and (self.root / 'application.json').is_file() and (self.root / 'python/python.exe').is_file())
        result = {'current_version': re.sub(r'b(\d+)$', r'-beta.\1', __version__), 'installable': installable, 'release_page': PAGE,
                  'checked_at': cached.get('checked_at'), 'state': cached.get('state', 'not_checked'),
                  'available': cached.get('available'), 'prepared': self.prepared['version'] if self.prepared else None}
        receipt = self.data / 'product-update-result.json'
        if receipt.exists():
            previous = json.loads(plain(receipt).read_bytes())
            result['last_result'] = {k: previous[k] for k in ('status', 'version', 'error', 'time') if k in previous}
        return result

    def check(self, *, force=False):
        with self.lock:
            current = self.status()
            if not force and current.get('checked_at') and time.time() - current['checked_at'] < 6 * 3600:
                return current
            try:
                rows = json.loads(self.downloader(RELEASES, 2 * 1024 * 1024))
                if not isinstance(rows, list): raise ValueError('Invalid release response')
                available = select_release(rows, __version__)
                cached = {'checked_at': time.time(), 'state': 'available' if available else 'current', 'available': available}
            except (OSError, ValueError, httpx.HTTPError):
                cached = {'checked_at': time.time(), 'state': 'unavailable', 'available': None}
            atomic(self.cache, cached)
            return self.status()

    def prepare(self, version):
        with self.lock:
            status = self.status(); release = status.get('available')
            if not status['installable']: raise ValueError('Use the complete Windows desktop package to enable in-app installation.')
            if not release or release['version'] != version: raise ValueError('Check for updates again; the release selection changed.')
            if self.prepared and self.prepared['version'] == version: return self.status()
            manifest(self.root)  # Refuse local modifications before downloading.
            work = plain(self.directory / uuid4().hex, missing=True); work.mkdir()
            try:
                raw = self.downloader(release['url'], MAX_ARCHIVE)
            except httpx.HTTPError:
                raise ValueError('The release could not be downloaded. Application files were retained; try again later.') from None
            if len(raw) != release['bytes'] or hashlib.sha256(raw).hexdigest().upper() != release['sha256']:
                raise ValueError('Release download verification failed; application files were retained.')
            (work / 'download.zip').write_bytes(raw)
            staged = work / 'package'; extract_package(raw, staged, release)
            make_plan(self.root, staged, work)
            self.prepared = {'version': version, 'work': str(work)}
            return self.status()

    def install(self, version, launch):
        # Another window may be preparing a release in a worker thread. Do not
        # wait on that download in the event loop, or change the selected work.
        if not self.lock.acquire(blocking=False):
            raise ValueError('Another update is being prepared. Wait for it to finish, then select the update again.')
        try:
            return self._install_selected(version, launch)
        finally:
            self.lock.release()

    def _install_selected(self, version, launch):
        prepared = dict(self.prepared or {})
        if prepared.get('version') != version or version_key(version) <= version_key(__version__):
            raise ValueError('Prepare a newer update first')
        if version_key(__version__)[3] and not version_key(version)[3]:
            raise ValueError('Stable installations require a stable update')
        owned_record(self.root, self.data, launch)
        work = plain(prepared['work']); staged = plain(work / 'package')
        manifest(self.root); metadata, _ = manifest(staged)
        plan = json.loads(plain(work / 'install.json').read_bytes())
        if (metadata['version'] != version or plan['to_version'] != version
                or Path(plan['root']).resolve() != self.root or Path(plan['staged']).resolve() != staged):
            raise ValueError('Prepared update identity changed')
        runner = plain(staged / 'src/policy_harness/product_install.py')
        pending = plain(self.data / 'product-update-pending.json', missing=True)
        if pending.exists(): raise ValueError('A previous update is still pending')
        atomic(pending, {'schema': 'oif-product-update-pending-v1', 'work_id': work.name,
                         'helper_sha256': digest(runner), 'version': version})
        try:
            with (work / 'install.log').open('ab') as log:
                subprocess.Popen([str(staged / 'python/python.exe'), '-B', '-I', str(runner), '--work', str(work),
                                  '--data-dir', str(self.data)], cwd=staged, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
