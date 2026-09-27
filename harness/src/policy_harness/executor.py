"""Trusted, journalled filesystem and Docker execution boundary.

Only the controller imports this object. Untrusted programs run in a container
which receives one task directory, not this object, its journal or credentials.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from .capabilities import (CapabilityRegistry, Recipe, atomic_json, canonical, check_node,
                           confined, describe, digest, process_lock, sha_file)
from .models import Operation, OperationResult, PolicyError, now
from .operation_contracts import file_write_input


def utf8_text_facts(data: bytes, *, complete: bool = True) -> dict:
    """Facts about verified bytes, never about an unread remainder."""
    facts = {'scope': 'whole_file' if complete else 'returned_range', 'byte_count': len(data)}
    try:
        text = data.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        return dict(facts, valid_utf8=False)
    return dict(facts, valid_utf8=True, character_count=len(text), character_unit='Unicode code points',
                has_newline=('\n' in text or '\r' in text), ends_with_newline=text.endswith(('\n', '\r')),
                newline_count=text.count('\n') + text.count('\r') - text.count('\r\n'),
                has_utf8_bom=text.startswith('\ufeff'))


class Executor:
    def __init__(self, data_dir: Path, *, image: str = 'python:3.12-slim',
                 docker_bin: str = 'docker'):
        self.data_dir = Path(data_dir).absolute()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces = self.data_dir / 'workspaces'
        self.workspaces.mkdir(exist_ok=True)
        self.control = self.data_dir / '.executor'
        self.control.mkdir(exist_ok=True)
        for directory in (self.data_dir, self.workspaces, self.control):
            self._check_ancestors(directory)
        self.journal = self.control / 'operations'
        self.journal.mkdir(exist_ok=True)
        self.registry = CapabilityRegistry(self.control / 'capabilities')
        self.image = image
        self.docker_bin = shutil.which(docker_bin) or docker_bin
        self._host: str | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._setup_lock = asyncio.Lock()
        self.unresolved_lookup = None  # Optional trusted Store integration.
        owner_path = self.control / 'owner.json'
        try:
            atomic_json(owner_path, {'id': uuid4().hex}, exclusive=True)
        except FileExistsError:
            pass
        check_node(owner_path)
        self.owner = json.loads(owner_path.read_text())['id']
        if not re.fullmatch('[0-9a-f]{32}', self.owner):
            raise PolicyError('EXECUTOR_OWNER_CORRUPT')

    @staticmethod
    def _check_ancestors(path: Path) -> None:
        for node in reversed([path, *path.parents]):
            check_node(node)

    def _workspace(self, workspace: Path) -> Path:
        path = Path(workspace).absolute()
        if path.parent != self.workspaces or path.name in ('', '.', '..'):
            raise PolicyError('WORKSPACE_ESCAPE: only server-owned task workspaces are accepted')
        self._check_ancestors(path)
        if not path.is_dir():
            raise PolicyError('WORKSPACE_NOT_DIRECTORY')
        return path

    def _environment(self) -> dict:
        path = self.control / 'environment.json'
        if not path.exists():
            return {}
        check_node(path)
        data = json.loads(path.read_text(encoding='utf-8'))
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', data.get('image_id', '')):
            raise PolicyError('ENVIRONMENT_CORRUPT')
        return data

    def describe_capability(self, workspace: Path, args: dict) -> dict:
        return describe(self._workspace(workspace), args, self._environment())

    def catalog(self, workspace: Path | None = None) -> dict:
        """Self-describing preparation route, not permission or success flags."""
        environment = self._environment()
        registered = []
        target = str(self._workspace(workspace)) if workspace is not None else None
        for path in self.registry.directory.glob('*.json'):
            row = self.registry.get(path.stem)
            if target is None or row['candidate']['workspace'] == target:
                registered.append({'id': row['id'], 'name': row['candidate']['name'],
                    'recipe': row['candidate']['recipe'], 'files': row['candidate']['files']})
        return {'prepared': bool(environment), 'environment': environment,
            'recipes': ['python', 'pytest'], 'registered': registered,
            'capability_request_schema': Recipe.model_json_schema(),
            'exec_args': {'capability_id': 'ID returned by a successful capability_request'},
            'file_read_args': {'path': 'task-relative file', 'offset': 0, 'max_bytes': 131072, 'encoding': 'utf-8 or base64'},
            'file_write_args': {'path': 'task-relative file', 'text': 'UTF8 contents', 'expected_sha256': 'optional current SHA256, or null for absent'},
            'file_list_args': {'path': '.', 'recursive': False, 'max_entries': 1000},
            'preparation': 'Create required source with file_write, use returned SHA256 in files, submit capability_request, then exec. Unknown capabilities require preparation, not host execution.',
            'setup': 'Trusted local controller calls prepare_environment(include_pytest=True) when unprepared.',
            'recovery': 'Trusted controller calls reconcile(operation_id, stop=False) to observe, or stop=True to cancel the exact owned container. Neither route replays execution.'}

    def describe_execution(self, workspace: Path, args: dict) -> dict:
        self._args(args, {'capability_id'})
        row = self.registry.get(args.get('capability_id'))
        candidate = row['candidate']
        workspace = self._workspace(workspace)
        if candidate['workspace'] != str(workspace):
            raise PolicyError('CAPABILITY_WORKSPACE_MISMATCH')
        source_args = {key: value for key, value in candidate.items()
                       if key not in ('workspace', 'image_id', 'executable', 'argv')}
        description = describe(workspace, source_args,
            {'image_id': candidate['image_id'], 'pytest': candidate['recipe'] == 'pytest'})
        return {**description, 'capability_id': row['id'],
                'workspace_manifest': self._manifest(workspace)}

    @asynccontextmanager
    async def _lease(self, workspace: Path):
        async with self._locks.setdefault(str(workspace), asyncio.Lock()):
            with process_lock(self.control / 'locks' / (digest(str(workspace).encode()) + '.lock')):
                yield

    def _record_path(self, operation_id: str) -> Path:
        return self.journal / (digest(operation_id.encode()) + '.json')

    def _read_record(self, operation_id: str) -> dict | None:
        path = self._record_path(operation_id)
        if not path.exists():
            return None
        check_node(path)
        record = json.loads(path.read_text(encoding='utf-8'))
        if record['operation']['id'] != operation_id or record['owner'] != self.owner:
            raise PolicyError('OPERATION_IDENTITY_MISMATCH')
        return record

    def _save(self, record: dict) -> None:
        record['updated_at'] = now()
        atomic_json(self._record_path(record['operation']['id']), record)

    def _first_fault(self, record: dict, reason: str, detail: str) -> None:
        event = {'reason': reason, 'detail': detail, 'at': now()}
        if record.get('first_fault') is None:
            record['first_fault'] = event
        else:
            record.setdefault('contributing_faults', []).append(event)

    def _unresolved(self, workspace: Path, *, exclude: str) -> list[str]:
        if self.unresolved_lookup is not None:
            return [identity for identity in self.unresolved_lookup(workspace) if identity != exclude]
        pending = []
        for path in self.journal.glob('*.json'):
            check_node(path)
            row = json.loads(path.read_text(encoding='utf-8'))
            if row.get('workspace') != str(workspace) or row['operation']['id'] == exclude:
                continue
            result = row.get('result', {})
            if row.get('phase') != 'finished' or result.get('effect') == 'unknown':
                pending.append(row['operation']['id'])
        return pending

    async def execute(self, workspace: Path, operation: Operation, *, full_access=False, before_start=None) -> OperationResult:
        started = time.monotonic()
        try:
            workspace = self._workspace(workspace)
        except (OSError, ValueError, PolicyError) as error:
            return OperationResult(operation_id=operation.id, status='failed',
                                   stderr=str(error), data={'reason': 'WORKSPACE_REJECTED'})
        async with self._lease(workspace):
            if before_start is not None:
                before_start()
            payload = {'workspace': str(workspace), 'operation': operation.model_dump()}
            if full_access:
                payload['full_access'] = True
            payload_hash = digest(canonical(payload))
            existing = self._read_record(operation.id)
            if existing is not None:
                if existing['payload_sha256'] != payload_hash:
                    return OperationResult(operation_id=operation.id, status='failed',
                                           stderr='OPERATION_ID_REUSE: payload changed')
                if existing.get('phase') == 'finished' and existing.get('result'):
                    result = OperationResult.model_validate(existing['result'])
                    result.data = {**result.data, 'replayed': True}
                    return result
                return await self.reconcile(operation.id)
            blockers = self._unresolved(workspace, exclude=operation.id)
            if blockers:
                return OperationResult(operation_id=operation.id, status='pending',
                                       data={'reason': 'RECONCILIATION_REQUIRED',
                                             'operation_ids': blockers})
            record = {'owner': self.owner, 'workspace': str(workspace),
                      'operation': operation.model_dump(), 'payload_sha256': payload_hash,
                      'phase': 'prepared', 'created_at': now(), 'first_fault': None,
                      'contributing_faults': [], 'target_started': False}
            if full_access:
                record['full_access'] = True
            # Cross-process collision cannot start a second operation with this ID.
            try:
                atomic_json(self._record_path(operation.id), record, exclusive=True)
            except FileExistsError:
                return OperationResult(operation_id=operation.id, status='pending',
                                       data={'reason': 'OPERATION_ALREADY_STARTED'})
            try:
                if operation.kind in ('file_read', 'file_write', 'file_list'):
                    if full_access:
                        work = asyncio.create_task(asyncio.to_thread(self._file_operation, workspace, operation, record))
                        try:
                            result = await asyncio.shield(work)
                        except asyncio.CancelledError:
                            # Atomic file operations already started must finish
                            # recording their actual result before cancellation.
                            result = await work
                    else:
                        result = self._file_operation(workspace, operation, record)
                elif operation.kind == 'capability_request':
                    description = self.describe_capability(workspace, operation.args)
                    capability = self.registry.register(description, operation.id)
                    result = OperationResult(operation_id=operation.id, status='succeeded',
                        effect='confirmed', data={'capability_id': capability['id'],
                        'candidate_sha256': description['candidate_sha256'],
                        'registration_operation_id': capability['registration_operation_id'],
                        'prepared': True, 'executed': False})
                elif operation.kind == 'exec':
                    if full_access:
                        from .native_execution import execute_native
                        result = await execute_native(self, workspace, operation, record)
                    else:
                        result = await self._execute_container(workspace, operation, record)
                else:
                    raise PolicyError('EXECUTOR_ROUTE_UNSUPPORTED: ' + operation.kind)
            except asyncio.CancelledError:
                # _execute_container retains an owned-container cleanup attempt.
                self._first_fault(record, 'CANCELLED', 'Controller cancellation received')
                if record.get('target_started') or record.get('container_name'):
                    result = self._unknown(record, 'CANCELLED')
                else:
                    result = OperationResult(operation_id=operation.id, status='failed',
                        data={'reason': 'CANCELLED_BEFORE_TARGET_START'})
            except (OSError, ValueError, PolicyError) as error:
                self._first_fault(record, str(error).split(':', 1)[0], str(error))
                result = OperationResult(operation_id=operation.id,
                    status='unknown' if record.get('target_started') else 'failed',
                    effect='unknown' if record.get('target_started') else 'none',
                    stderr=str(error), data={'reason': record['first_fault']['reason']})
            result.started_at = record['created_at']
            result.finished_at = now()
            result.elapsed_seconds = time.monotonic() - started
            result.data = {**result.data, 'first_fault': record.get('first_fault'),
                           'contributing_faults': record.get('contributing_faults', []),
                           'journal': str(self._record_path(operation.id))}
            record['result'] = result.model_dump()
            record['phase'] = 'finished' if result.effect != 'unknown' and result.status != 'pending' else 'unresolved'
            self._save(record)
            if operation.kind == 'exec' and record['phase'] == 'finished' and record.get('container_id'):
                try:
                    result.data['cleanup'] = await self.cleanup(operation.id)
                except asyncio.CancelledError:
                    # The original terminal result has already been saved. A
                    # cleanup interruption cannot turn it into a replay request.
                    result.data['cleanup'] = {'removed': None, 'reason': 'CLEANUP_INTERRUPTED'}
                except (OSError, ValueError, PolicyError, asyncio.TimeoutError) as error:
                    result.data['cleanup'] = {'removed': None, 'reason': str(error)}
                # cleanup() owns its updated journal fields; do not overwrite
                # removal identity with the caller's earlier in-memory record.
                latest = self._read_record(operation.id)
                latest['result'] = result.model_dump()
                self._save(latest)
            return result

    @staticmethod
    def _args(args: dict, accepted: set[str]) -> None:
        extra = set(args) - accepted
        if extra:
            raise PolicyError('ARGUMENT_UNSUPPORTED: ' + ','.join(sorted(extra)))

    def _file_operation(self, workspace: Path, operation: Operation, record: dict) -> OperationResult:
        from .access_paths import operation_path, display_path
        full_access = record.get('full_access', False)
        def locate(value, **options):
            return operation_path(workspace, value, full_access=full_access, **options)
        args = operation.args
        if operation.kind == 'file_read':
            self._args(args, {'path', 'offset', 'max_bytes', 'encoding'})
            path = locate(args.get('path', ''))
            if not path.is_file():
                raise PolicyError('READ_NOT_FILE')
            offset, maximum = args.get('offset', 0), args.get('max_bytes', 131072)
            if type(offset) is not int or offset < 0 or type(maximum) is not int or maximum < 1:
                raise PolicyError('READ_RANGE_INVALID')
            size, sha = path.stat().st_size, sha_file(path)
            with path.open('rb') as source:
                source.seek(offset)
                data = source.read(maximum)
            if sha_file(path) != sha:
                raise PolicyError('FILE_CHANGED_DURING_READ')
            encoding = args.get('encoding', 'utf-8')
            if encoding not in ('utf-8', 'base64'):
                raise PolicyError('READ_ENCODING_UNSUPPORTED')
            return OperationResult(operation_id=operation.id, status='succeeded',
                stdout=data.decode('utf-8', errors='replace') if encoding == 'utf-8' else '',
                data={'path': display_path(workspace, path), 'sha256': sha, 'total_bytes': size,
                      'offset': offset, 'returned_bytes': len(data),
                      'truncated': offset > 0 or offset + len(data) < size,
                      'next_offset': offset + len(data) if offset + len(data) < size else None,
                      'encoding': encoding, 'base64': base64.b64encode(data).decode() if encoding == 'base64' else None,
                      'text_facts': utf8_text_facts(data, complete=offset == 0 and len(data) == size),
                      'utf8_lossy': encoding == 'utf-8' and data.decode('utf-8', errors='replace').encode() != data})
        if operation.kind == 'file_list':
            self._args(args, {'path', 'recursive', 'max_entries'})
            directory = locate(args.get('path', '.'), allow_root=True)
            if not directory.is_dir():
                raise PolicyError('LIST_NOT_DIRECTORY')
            maximum = args.get('max_entries', 1000)
            if type(maximum) is not int or maximum < 1:
                raise PolicyError('LIST_LIMIT_INVALID')
            entries: list[dict] = []
            queue, truncated = [directory], False
            while queue:
                parent = queue.pop()
                children = parent.iterdir() if full_access else sorted(parent.iterdir(), key=lambda p: p.name)
                for path in children:
                    if len(entries) >= maximum:
                        truncated = True
                        break
                    entry = {'path': display_path(workspace, path)}
                    try:
                        check_node(path)
                        entry.update(type='directory' if path.is_dir() else 'file', bytes=path.stat().st_size)
                        if args.get('recursive') and path.is_dir():
                            queue.append(path)
                    except PolicyError as error:
                        entry.update(type='blocked', reason=str(error))
                    entries.append(entry)
                if truncated:
                    break
            return OperationResult(operation_id=operation.id, status='succeeded',
                data={'entries': entries, 'returned_entries': len(entries), 'truncated': truncated,
                      'continuation': 'list a narrower subdirectory or raise max_entries' if truncated else None})
        path, data = file_write_input(workspace, args, full_access=full_access)
        before = sha_file(path) if path.exists() else None
        if 'expected_sha256' in args and args['expected_sha256'] != before:
            raise PolicyError('WRITE_CONFLICT: expected_sha256 does not match current bytes')
        temporary = path.with_name('.' + uuid4().hex + '.tmp')
        parents = []
        parent = path.parent
        while parent != workspace and parent != parent.parent:
            parents.append({'path': display_path(workspace, parent), 'existed': parent.exists()})
            if full_access and parent.exists():
                break
            parent = parent.parent
        record.update(phase='file_write_started', target_started=True,
                      file_before_sha256=before, file_expected_sha256=digest(data),
                      file_parents_before=parents,
                      temporary_path=display_path(workspace, temporary))
        self._save(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        locate(args['path'], allow_missing=True)
        try:
            with temporary.open('xb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            locate(args['path'], allow_missing=True)
            if (sha_file(path) if path.exists() else None) != before:
                raise PolicyError('WRITE_CONFLICT: target changed before atomic replacement')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        after = sha_file(locate(args['path']))
        if after != digest(data):
            raise PolicyError('WRITE_READBACK_MISMATCH')
        return OperationResult(operation_id=operation.id, status='succeeded', effect='confirmed',
            data={'path': display_path(workspace, path), 'before_sha256': before,
                  'sha256': after, 'bytes': len(data), 'text_facts': utf8_text_facts(data)},
            artifacts=[{'path': str(path), 'sha256': after, 'bytes': len(data)}])

    @staticmethod
    def _docker_environment() -> dict:
        environment = os.environ.copy()
        for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
            environment.pop(key, None)
        return environment

    async def _process(self, args: list[str], *, input_bytes: bytes | None = None,
                       timeout: float = 60) -> tuple[int, bytes, bytes]:
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        process = await asyncio.create_subprocess_exec(self.docker_bin, *args,
            cwd=self.control, env=self._docker_environment(),
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)
        async def read_bounded(stream: asyncio.StreamReader) -> bytes:
            chunks, count = [], 0
            while chunk := await stream.read(65536):
                count += len(chunk)
                if count > 8 * 1024 * 1024:
                    raise PolicyError('DOCKER_CONTROL_OUTPUT_LIMIT')
                chunks.append(chunk)
            return b''.join(chunks)
        async def communicate():
            if input_bytes is not None:
                process.stdin.write(input_bytes)
                await process.stdin.drain()
                process.stdin.close()
            stdout, stderr = await asyncio.gather(read_bounded(process.stdout), read_bounded(process.stderr))
            return await process.wait(), stdout, stderr
        try:
            return await asyncio.wait_for(communicate(), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise

    async def _local_host(self) -> str:
        if self._host is None:
            code, out, err = await self._process(['context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'])
            if code:
                raise PolicyError('DOCKER_CONTEXT_UNAVAILABLE: ' + err.decode(errors='replace'))
            try:
                host = json.loads(out)
            except (ValueError, TypeError) as error:
                raise PolicyError('DOCKER_CONTEXT_INVALID') from error
            if not isinstance(host, str) or not host.startswith(('npipe://', 'unix://')):
                raise PolicyError('DOCKER_REMOTE_FORBIDDEN: a local Docker engine is required')
            self._host = host
        return self._host

    async def _docker(self, args: list[str], **kwargs) -> tuple[int, bytes, bytes]:
        return await self._process(['--host', await self._local_host(), *args], **kwargs)

    async def _image(self, reference: str) -> dict:
        code, output, error = await self._docker(['image', 'inspect', reference])
        if code:
            raise PolicyError('IMAGE_UNAVAILABLE: ' + error.decode(errors='replace'))
        image = json.loads(output)[0]
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image['Id']):
            raise PolicyError('IMAGE_ID_INVALID')
        if image.get('Os') != 'linux':
            raise PolicyError('LINUX_SANDBOX_REQUIRED')
        return image

    async def health(self) -> dict:
        """Read only: no pulls, builds, probes, cleanup or paid API requests."""
        result = {'available': False, 'prepared': False, 'backend': 'docker',
                  'image': self.image, 'network': 'none'}
        try:
            code, out, err = await self._docker(['version', '--format', '{{json .Server}}'])
            if code:
                raise PolicyError(err.decode(errors='replace'))
            result.update(available=True, server=json.loads(out), local_host=self._host)
            environment = self._environment()
            if environment:
                image = await self._image(environment['image_id'])
                result.update(prepared=True, image_id=image['Id'], pytest=environment.get('pytest', False))
            else:
                result['next_step'] = 'prepare_environment'
        except (OSError, ValueError, PolicyError, asyncio.TimeoutError) as error:
            result['error'] = str(error)
        return result

    async def prepare_environment(self, include_pytest: bool = True) -> dict:
        """Trusted setup action. Its image pull/build effects are returned honestly.

        No Dockerfile, shell command, package URL or build argument is accepted
        from an agent. Running the resulting image still requires a gated recipe.
        """
        async with self._setup_lock:
            setup_id = uuid4().hex
            log = {'id': setup_id, 'started_at': now(), 'image': self.image,
                   'include_pytest': include_pytest, 'steps': [], 'status': 'started'}
            path = self.control / ('setup-' + setup_id + '.json')
            atomic_json(path, log)
            try:
                try:
                    base = await self._image(self.image)
                except PolicyError as error:
                    if not str(error).startswith('IMAGE_UNAVAILABLE:'):
                        raise
                    code, out, err = await self._docker(['pull', self.image], timeout=600)
                    log['steps'].append({'action': 'pull', 'exit_code': code,
                        'stdout': out.decode(errors='replace'), 'stderr': err.decode(errors='replace')})
                    atomic_json(path, log)
                    if code:
                        raise PolicyError('IMAGE_PULL_FAILED')
                    base = await self._image(self.image)
                selected = base
                if include_pytest:
                    digests = base.get('RepoDigests', [])
                    if not digests:
                        raise PolicyError('BASE_DIGEST_REQUIRED: trusted package builds require a repository digest')
                    base_ref = digests[0]
                    if not re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}', base_ref):
                        raise PolicyError('BASE_DIGEST_INVALID')
                    dockerfile = (f'FROM {base_ref}\n'
                        'RUN python -m pip install --no-cache-dir --disable-pip-version-check pytest==9.1.1\n').encode()
                    tag = 'policy-harness-runtime:' + digest(dockerfile)[:24]
                    code, out, err = await self._docker(['build', '--label',
                        'policy-harness.setup=' + self.owner, '--tag', tag, '-'],
                        input_bytes=dockerfile, timeout=600)
                    log['steps'].append({'action': 'build', 'dockerfile_sha256': digest(dockerfile),
                        'base_digest': base_ref, 'exit_code': code,
                        'stdout': out.decode(errors='replace'), 'stderr': err.decode(errors='replace')})
                    atomic_json(path, log)
                    if code:
                        raise PolicyError('SANDBOX_BUILD_FAILED')
                    selected = await self._image(tag)
                environment = {'image_id': selected['Id'], 'base_image_id': base['Id'],
                               'pytest': include_pytest, 'prepared_at': now(), 'setup_id': setup_id}
                atomic_json(self.control / 'environment.json', environment)
                log.update(status='succeeded', environment=environment)
            except asyncio.CancelledError:
                log.update(status='unknown', error='CANCELLED: image build/pull effects may remain')
                raise
            except (OSError, ValueError, PolicyError, asyncio.TimeoutError) as error:
                log.update(status='failed', error=str(error))
            finally:
                log['finished_at'] = now()
                atomic_json(path, log)
            return {**log, 'record': str(path),
                    'proof_ceiling': 'prepared image identity; execution and policy conformance remain untested'}

    def _manifest(self, workspace: Path, *, observe_unsafe: bool = False) -> dict:
        entries: dict[str, dict] = {}
        queue = [workspace]
        while queue:
            directory = queue.pop()
            for path in sorted(directory.iterdir()):
                rel = path.relative_to(workspace).as_posix()
                try:
                    check_node(path)
                except PolicyError as error:
                    if not observe_unsafe:
                        raise
                    node = path.lstat()
                    entries[rel] = {'type': 'blocked', 'reason': str(error),
                                    'mode': node.st_mode, 'links': node.st_nlink,
                                    'link_target': os.readlink(path) if path.is_symlink() else None}
                    continue
                if path.is_dir():
                    entries[rel] = {'type': 'directory'}
                    queue.append(path)
                else:
                    entries[rel] = {'type': 'file', 'bytes': path.stat().st_size, 'sha256': sha_file(path)}
        return entries

    async def _owned_container(self, record: dict) -> dict | None:
        identity = record.get('container_id') or record.get('container_name')
        if not identity:
            return None
        code, output, error = await self._docker(['container', 'inspect', identity])
        if code:
            raise PolicyError('CONTAINER_OBSERVATION_FAILED: ' + error.decode(errors='replace'))
        info = json.loads(output)[0]
        labels = info.get('Config', {}).get('Labels', {}) or {}
        if (labels.get('policy-harness.owner') != self.owner or
                labels.get('policy-harness.operation') != record['payload_sha256'] or
                info.get('Name') != '/' + record['container_name'] or
                not re.fullmatch('[0-9a-f]{64}', info.get('Id', '')) or
                record.get('container_id', info['Id']) != info['Id']):
            raise PolicyError('CONTAINER_OWNERSHIP_MISMATCH')
        record['container_id'] = info['Id']
        return info

    async def _stop_owned(self, record: dict) -> dict | None:
        info = await self._owned_container(record)
        if info and info['State'].get('Running'):
            code, out, err = await self._docker(['container', 'kill', info['Id']])
            record.setdefault('cleanup', []).append({'action': 'kill', 'id': info['Id'],
                'exit_code': code, 'stdout': out.decode(errors='replace'), 'stderr': err.decode(errors='replace')})
            self._save(record)
            info = await self._owned_container(record)
        return info

    def _unknown(self, record: dict, reason: str) -> OperationResult:
        return OperationResult(operation_id=record['operation']['id'], status='unknown', effect='unknown',
            data={'reason': reason, 'container_id': record.get('container_id'),
                  'container_name': record.get('container_name'), 'reconciliation_required': True,
                  'first_fault': record.get('first_fault')})

    async def _execute_container(self, workspace: Path, operation: Operation, record: dict) -> OperationResult:
        self._args(operation.args, {'capability_id'})
        capability = self.registry.get(operation.args.get('capability_id'))
        candidate = capability['candidate']
        if candidate['workspace'] != str(workspace):
            raise PolicyError('CAPABILITY_WORKSPACE_MISMATCH')
        for rel, expected in candidate['files'].items():
            if sha_file(confined(workspace, rel)) != expected:
                raise PolicyError('SOURCE_CHANGED: prepare and review the current program ' + rel)
        # Pin execution to the image reviewed at capability registration.
        await self._image(candidate['image_id'])
        before = self._manifest(workspace)
        resources = candidate['resources']
        name = 'policy-harness-' + self.owner[:12] + '-' + uuid4().hex
        if ',' in str(workspace) or '"' in str(workspace):
            raise PolicyError('WORKSPACE_MOUNT_ENCODING: comma/quote paths require a separate workspace')
        argv = ['container', 'create', '--name', name,
            '--label', 'policy-harness.owner=' + self.owner,
            '--label', 'policy-harness.operation=' + record['payload_sha256'],
            '--network', 'none', '--user', '65534:65534', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--pids-limit', str(resources['pids_limit']), '--cpus', str(resources['cpus']),
            '--memory', str(resources['memory_mb']) + 'm',
            '--memory-swap', str(resources['memory_mb']) + 'm', '--init',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=' + str(resources['tmp_mb']) + 'm',
            '--workdir', '/workspace', '--env', 'HOME=/tmp', '--env', 'PYTHONDONTWRITEBYTECODE=1',
            '--log-driver', 'none', '--mount', 'type=bind,src=' + str(workspace) + ',dst=/workspace',
            '--entrypoint', candidate['executable'], candidate['image_id'], *candidate['argv']]
        record.update(phase='creating', container_name=name, image_id=candidate['image_id'],
                      capability_id=capability['id'], docker_create_argv=argv, before_manifest=before)
        self._save(record)
        try:
            code, output, error = await self._docker(argv)
        except BaseException:
            # create may have reached the engine even when the CLI did not reply.
            record['target_started'] = True
            self._save(record)
            raise
        record['create_result'] = {'exit_code': code, 'stdout': output.decode(errors='replace'),
                                   'stderr': error.decode(errors='replace')}
        if code:
            self._first_fault(record, 'CONTAINER_CREATE_FAILED', error.decode(errors='replace'))
            self._save(record)
            return OperationResult(operation_id=operation.id, status='failed', stderr=error.decode(errors='replace'),
                                   exit_code=code, data={'reason': 'CONTAINER_CREATE_FAILED'})
        identity = output.decode().strip()
        if not re.fullmatch('[0-9a-f]{64}', identity):
            record['target_started'] = True
            raise PolicyError('CONTAINER_CREATE_ID_UNKNOWN')
        record.update(container_id=identity, phase='created')
        self._save(record)
        await self._owned_container(record)
        # Recheck after preparation and immediately before the only start call.
        if before != self._manifest(workspace):
            raise PolicyError('WORKSPACE_CHANGED_BEFORE_START')
        record.update(phase='starting', target_started=True)
        self._save(record)
        return await self._attach(record, candidate)

    async def _attach(self, record: dict, candidate: dict) -> OperationResult:
        identity = record['container_id']
        operation_id = record['operation']['id']
        maximum = candidate['resources']['output_bytes']
        artifact_root = self.control / 'streams' / digest(operation_id.encode())
        artifact_root.mkdir(parents=True, exist_ok=True)
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        process = await asyncio.create_subprocess_exec(self.docker_bin, '--host', await self._local_host(),
            'container', 'start', '--attach', identity, cwd=self.control, env=self._docker_environment(),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, **options)
        record.update(phase='attached', cli_pid=process.pid, streams=str(artifact_root))
        self._save(record)
        count, exceeded = [0], asyncio.Event()
        async def capture(stream: asyncio.StreamReader, path: Path) -> None:
            with path.open('xb') as sink:
                while chunk := await stream.read(65536):
                    remaining = max(0, maximum - count[0])
                    kept = chunk[:remaining]
                    sink.write(kept)
                    sink.flush()
                    count[0] += len(kept)
                    if len(kept) != len(chunk):
                        exceeded.set()
                os.fsync(sink.fileno())
        readers = [asyncio.create_task(capture(process.stdout, artifact_root / 'stdout.bin')),
                   asyncio.create_task(capture(process.stderr, artifact_root / 'stderr.bin'))]
        waiter = asyncio.create_task(process.wait())
        overflow = asyncio.create_task(exceeded.wait())
        try:
            done, _ = await asyncio.wait([waiter, overflow],
                timeout=candidate['resources']['timeout_seconds'], return_when=asyncio.FIRST_COMPLETED)
            if not done:
                self._first_fault(record, 'EXECUTION_TIMEOUT', 'Declared operation time envelope exhausted')
                await self._stop_owned(record)
            elif overflow in done and exceeded.is_set():
                self._first_fault(record, 'OUTPUT_LIMIT', 'Declared capture capacity exhausted; remaining output is unobserved')
                record['output_truncated'] = True
                await self._stop_owned(record)
            await asyncio.wait_for(asyncio.gather(waiter, *readers), 30)
            if exceeded.is_set():
                if not record.get('output_truncated'):
                    self._first_fault(record, 'OUTPUT_LIMIT', 'Output exceeded declared capture capacity')
                record['output_truncated'] = True
        except asyncio.CancelledError:
            self._first_fault(record, 'CANCELLED', 'Controller requested cancellation')
            record['capture_interrupted'] = True
            try:
                await asyncio.shield(self._stop_owned(record))
            except Exception as error:
                self._first_fault(record, 'CLEANUP_FAILED', str(error))
        except (OSError, PolicyError, asyncio.TimeoutError) as error:
            self._first_fault(record, 'ATTACH_FAILED', str(error))
            record['capture_interrupted'] = True
            try:
                await self._stop_owned(record)
            except Exception as cleanup_error:
                self._first_fault(record, 'CLEANUP_FAILED', str(cleanup_error))
        finally:
            overflow.cancel()
            if process.returncode is None:
                process.kill()
            await process.wait()
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(overflow, waiter, *readers, return_exceptions=True)
            record['attach_exit_code'] = process.returncode
            if process.returncode and record.get('first_fault') is None:
                self._first_fault(record, 'ATTACH_EXIT_NONZERO', str(process.returncode))
            self._save(record)
        try:
            info = await self._owned_container(record)
        except (OSError, ValueError, PolicyError, asyncio.TimeoutError) as error:
            self._first_fault(record, 'FINAL_STATE_UNOBSERVED', str(error))
            return self._unknown(record, 'FINAL_STATE_UNOBSERVED')
        if info['State'].get('Running'):
            return self._unknown(record, 'CONTAINER_STILL_RUNNING')
        return self._container_result(record, info)

    def _container_result(self, record: dict, info: dict, *, recovered: bool = False) -> OperationResult:
        state = info['State']
        record['final_container_state'] = state
        operation_id = record['operation']['id']
        stdout, stderr, artifacts = '', '', []
        for channel in ('stdout', 'stderr'):
            path = Path(record['streams']) / (channel + '.bin') if record.get('streams') else None
            if path and path.exists():
                check_node(path)
                with path.open('rb') as source:
                    data = source.read(131072)
                text = data.decode('utf-8', errors='replace')
                if channel == 'stdout':
                    stdout = text
                else:
                    stderr = text
                artifacts.append({'path': str(path), 'sha256': sha_file(path),
                    'bytes': path.stat().st_size, 'channel': channel,
                    'preview_truncated': path.stat().st_size > len(data)})
        try:
            after = self._manifest(self._workspace(Path(record['workspace'])), observe_unsafe=True)
            changed = [name for name in sorted(set(record.get('before_manifest', {})) | set(after))
                       if record.get('before_manifest', {}).get(name) != after.get(name)]
            record['after_manifest'] = after
        except (OSError, PolicyError) as error:
            self._first_fault(record, 'WORKSPACE_READBACK_FAILED', str(error))
            return self._unknown(record, 'WORKSPACE_READBACK_FAILED')
        actually_started = bool(state.get('StartedAt') and not state['StartedAt'].startswith('0001-'))
        code = state.get('ExitCode')
        if state.get('OOMKilled'):
            self._first_fault(record, 'OUT_OF_MEMORY', 'Docker reported OOMKilled')
        if state.get('Error'):
            self._first_fault(record, 'CONTAINER_STATE_ERROR', state['Error'])
        if code != 0 and record.get('first_fault') is None:
            self._first_fault(record, 'PROGRAM_EXIT_NONZERO', str(code))
        success = actually_started and code == 0 and record.get('first_fault') is None and not recovered
        return OperationResult(operation_id=operation_id,
            status='succeeded' if success else 'failed', effect='confirmed' if actually_started or changed else 'none',
            stdout=stdout, stderr=stderr, exit_code=code, artifacts=artifacts,
            data={'container_id': info['Id'], 'container_name': record['container_name'],
                  'image_id': record['image_id'], 'container_state': state,
                  'changed_paths': changed, 'manifest_sha256': digest(canonical(after)),
                  'output_truncated': bool(record.get('output_truncated')),
                  'recovered': recovered, 'capture_complete': not recovered and not record.get('output_truncated', False) and not record.get('capture_interrupted', False),
                  'first_fault': record.get('first_fault'),
                  'proof_ceiling': 'observed isolated program exit and workspace effects; task acceptance is separately evaluated'})

    async def reconcile(self, operation_id: str, *, stop: bool = False) -> OperationResult:
        """Observe a lost result; never start or replay its program.

        stop=True is a trusted controller cancellation action, restricted to the
        exact owner labels and recorded immutable container ID.
        """
        record = self._read_record(operation_id)
        if record is None:
            return OperationResult(operation_id=operation_id, status='failed',
                                   data={'reason': 'OPERATION_NOT_FOUND'})
        if record.get('phase') == 'finished' and record.get('result'):
            return OperationResult.model_validate(record['result'])
        if record.get('native'):
            # Windows closes the owned job on controller death. A lost exit and
            # effects are still unknown, and are never replayed as a fresh run.
            self._first_fault(record, 'NATIVE_EXECUTION_INTERRUPTED', 'Original native command result was not recorded; inspect effects before continuing')
            result = self._unknown(record, 'NATIVE_EXECUTION_INTERRUPTED')
            if not record.get('target_started'):
                result = OperationResult(operation_id=operation_id, status='failed',
                    data={'reason': 'NATIVE_NOT_STARTED', 'replayed': False, 'first_fault': record['first_fault']})
            record.update(result=result.model_dump(), phase='unresolved' if result.effect == 'unknown' else 'finished')
            self._save(record)
            return result
        if not record.get('container_name'):
            return self._reconcile_file_or_registration(record)
        try:
            info = await (self._stop_owned(record) if stop else self._owned_container(record))
            if info['State'].get('Running'):
                result = OperationResult(operation_id=operation_id, status='pending', effect='unknown',
                    data={'reason': 'OWNED_CONTAINER_RUNNING', 'container_id': info['Id'],
                          'container_state': info['State'], 'replayed': False})
            else:
                self._first_fault(record, 'RECOVERED_AFTER_INTERRUPTION',
                                  'Terminal state recovered; complete original output is not established')
                result = self._container_result(record, info, recovered=True)
        except (OSError, ValueError, PolicyError, asyncio.TimeoutError) as error:
            self._first_fault(record, 'RECONCILIATION_FAILED', str(error))
            result = self._unknown(record, 'RECONCILIATION_FAILED')
        record['result'] = result.model_dump()
        record['phase'] = 'finished' if result.effect != 'unknown' else 'unresolved'
        self._save(record)
        return result

    def _reconcile_file_or_registration(self, record: dict) -> OperationResult:
        """Resolve observable state without promoting a lost operation to PASS."""
        operation = record['operation']
        kind = operation['kind']
        data = {'recovered': True, 'replayed': False,
                'reason': 'INTERRUPTED_NONCONTAINER_OPERATION'}
        effect = 'none'
        try:
            workspace = self._workspace(Path(record['workspace']))
            from .access_paths import operation_path
            def locate(value, **options):
                return operation_path(workspace, value, full_access=record.get('full_access', False), **options)
            if kind == 'file_write' and record.get('target_started'):
                path = locate(operation['args']['path'], allow_missing=True)
                current = sha_file(path) if path.exists() else None
                data.update(path=operation['args']['path'], current_sha256=current,
                            before_sha256=record.get('file_before_sha256'),
                            intended_sha256=record.get('file_expected_sha256'))
                # The current filesystem is observed, but its acceptance and
                # original success remain lost. Intermediate directory/temp
                # effects are reported separately below, never replayed.
                parent_changes = [entry['path'] for entry in record.get('file_parents_before', [])
                                  if not entry['existed'] and locate(entry['path'], allow_missing=True).exists()]
                temporary = (locate(record['temporary_path'], allow_missing=True)
                             if record.get('temporary_path') else None)
                temporary_present = temporary is not None and temporary.exists()
                if current == record.get('file_before_sha256') and 'file_parents_before' not in record:
                    return self._unknown(record, 'LEGACY_WRITE_PARENT_EFFECTS_UNOBSERVED')
                effect = 'confirmed' if current != record.get('file_before_sha256') or parent_changes or temporary_present else 'none'
                data['current_matches_intended'] = current == record.get('file_expected_sha256')
                data.update(created_parents=parent_changes,
                            temporary_path=record.get('temporary_path'), temporary_present=temporary_present)
                data['workspace_manifest'] = self._manifest(workspace, observe_unsafe=True)
            elif kind == 'capability_request':
                registrations = []
                for path in self.registry.directory.glob('*.json'):
                    row = self.registry.get(path.stem)
                    if row['registration_operation_id'] == operation['id']:
                        registrations.append(row['id'])
                data['registrations'] = registrations
                effect = 'confirmed' if registrations else 'none'
            elif kind not in ('file_read', 'file_list', 'file_write'):
                return self._unknown(record, 'INTERRUPTED_OPERATION_NEEDS_SPECIFIC_RECOVERY')
            self._first_fault(record, 'RECOVERED_AFTER_INTERRUPTION',
                'Current state observed; lost original result is not successful completion')
            result = OperationResult(operation_id=operation['id'], status='failed', effect=effect,
                                     data={**data, 'first_fault': record['first_fault']})
            record.update(phase='finished', result=result.model_dump())
            self._save(record)
            return result
        except (OSError, ValueError, PolicyError) as error:
            self._first_fault(record, 'FILE_RECONCILIATION_FAILED', str(error))
            self._save(record)
            return self._unknown(record, 'FILE_RECONCILIATION_FAILED')

    async def cleanup(self, operation_id: str) -> dict:
        """Remove only a terminal, proven-owned container after saving its result."""
        record = self._read_record(operation_id)
        if not record or record.get('phase') != 'finished' or not record.get('result'):
            raise PolicyError('CLEANUP_REQUIRES_RECONCILED_RESULT')
        if record.get('container_removed'):
            return {'removed': True, 'id': record.get('container_id'), 'replayed': True}
        info = await self._owned_container(record)
        if not info or info['State'].get('Running'):
            raise PolicyError('CLEANUP_REQUIRES_TERMINAL_CONTAINER')
        self._save(record)
        code, out, err = await self._docker(['container', 'rm', info['Id']])
        result = {'removed': code == 0, 'id': info['Id'], 'exit_code': code,
                  'stdout': out.decode(errors='replace'), 'stderr': err.decode(errors='replace')}
        record.setdefault('cleanup', []).append(result)
        record['container_removed'] = code == 0
        self._save(record)
        return result
