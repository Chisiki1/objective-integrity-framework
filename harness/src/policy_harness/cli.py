"""Local operator entrypoint. Task work always enters through the guarded API."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, ExitStack
import ipaddress
import hashlib
import json
import os
import re
import subprocess
import threading
import secrets
from pathlib import Path
import sys
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from .models import PolicyError, now


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _local_url(value):
    parsed = urlsplit(value)
    try:
        local = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname or '').is_loopback
    except ValueError:
        local = False
    if (parsed.scheme != 'http' or not local or parsed.username or parsed.password or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('このCLIはローカルの http://127.0.0.1:ポート に接続してください。')
    return value.rstrip('/')


@contextmanager
def _runtime_lock(data_dir: Path, name='service-owner.lock'):
    """A second supported controller must not recover the first one's live tasks."""
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = (data_dir / name).open('a+b')
    acquired = False
    try:
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise ValueError(f'保存先の所有ロックを取得できません（OSコード {exc.errno}）。同じ保存先のサービスを確認してください。') from None
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _read_record(path):
    from .capabilities import check_node
    check_node(path)
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise PolicyError('RESTART_RECORD_INVALID')
    return value, raw


def _archive_record(path, raw):
    """Publish exact bytes from a flushed temporary, without overwriting history."""
    if path.exists():
        if _read_record(path)[1] != raw:
            raise PolicyError('RESTART_ARCHIVE_CHANGED')
        return
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        if os.name == 'nt':
            os.rename(temporary, path)  # Windows rename rejects an existing target.
        else:
            os.link(temporary, path)  # Exclusive publication on POSIX.
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    if _read_record(path)[1] != raw:
        raise PolicyError('RESTART_ARCHIVE_CHANGED')


def _restart_resolved(directory, marker, raw):
    """Validate a previous disposition; file existence alone never resolves it."""
    from .capabilities import sha_file
    identity = marker.get('restart_id', '')
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
        raise PolicyError('RESTART_ID_INVALID')
    consumed = directory / ('consumed-' + identity + '.json')
    if consumed.exists():
        receipt, _ = _read_record(consumed)
        if (receipt.get('restart_id') != identity or receipt.get('candidate_id') != marker.get('candidate_id') or
                receipt.get('previous_worker_pid') != marker.get('worker_pid') or
                receipt.get('source_version') != marker.get('attestation', {}).get('source_version') or
                receipt.get('resume_task_ids') != marker.get('resume_task_ids') or
                receipt.get('status') not in {'claimed', 'resume_dispatched'}):
            raise PolicyError('RESTART_CONSUMPTION_CHANGED')
        return True
    path = directory / ('resolved-' + identity + '.json')
    if not path.exists():
        return False
    record, _ = _read_record(path)
    digest = hashlib.sha256(raw).hexdigest()
    if (record.get('schema') != 'controller-restart-recovery-v1' or record.get('restart_id') != identity or
            record.get('status') != 'parked_without_resume' or record.get('candidate_id') != marker.get('candidate_id')):
        raise PolicyError('RESTART_DISPOSITION_CHANGED')
    name = 'marker-' + digest + '.json'
    if not any(r.get('name') == name and r.get('sha256') == digest for r in record.get('marker_refs', [])):
        raise PolicyError('RESTART_DISPOSITION_MARKER_CHANGED')
    archived, original = _read_record(directory / name)
    if original != raw or sha_file(directory / name) != digest:
        raise PolicyError('RESTART_DISPOSITION_ARCHIVE_CHANGED')
    return True


def _restart_frontier(directory):
    """Pending and accepted-but-unconsumed handoffs need an exact disposition."""
    paths = ([directory / 'pending.json'] if (directory / 'pending.json').exists() else [])
    paths += sorted(directory.glob('accepted-*.json'))
    found = {}
    for path in paths:
        marker, raw = _read_record(path)
        if marker.get('schema') != 'controller-restart-v1':
            raise PolicyError('RESTART_SCHEMA_INVALID')
        resolved = _restart_resolved(directory, marker, raw)
        if resolved and path.name != 'pending.json':
            continue
        identity = marker['restart_id']
        if path.name != 'pending.json' and path.name != 'accepted-' + identity + '.json':
            raise PolicyError('RESTART_REFERENCE_INVALID')
        previous = found.setdefault(identity, {'marker':marker, 'paths':[]})
        if previous['marker'] != marker:
            raise PolicyError('RESTART_PENDING_ACCEPTED_DIFFER')
        previous['paths'].append(path)
    return list(found.values())


def _assert_restart_clear(data_dir):
    from .server import restart_directory
    if _restart_frontier(restart_directory(data_dir)):
        raise PolicyError('RESTART_PENDING_REQUIRES_RECONCILIATION: recover-restart --data-dir で記録を照合してください。自動再試行は行いません。')


def _process_end_state(pid):
    """Read process state without signalling or terminating another process."""
    if type(pid) is not int or pid <= 0:
        raise PolicyError('RESTART_PROCESS_ID_INVALID')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return {'pid':pid, 'state':'not_running'}
            raise PolicyError('RESTART_PROCESS_STATE_UNOBSERVED')
        try:
            state = kernel.WaitForSingleObject(handle, 0)
            if state not in (0, 258):
                raise PolicyError('RESTART_PROCESS_STATE_UNOBSERVED')
            return {'pid':pid, 'state':'not_running' if state == 0 else 'running'}
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {'pid':pid, 'state':'not_running'}
    except PermissionError:
        raise PolicyError('RESTART_PROCESS_STATE_UNOBSERVED') from None
    return {'pid':pid, 'state':'running'}


def _recover_restart(args, *, project_root=None, store=None, manager=None):
    """Trusted local operator transition. Never starts a model, task or worker."""
    from .server import default_data_dir, restart_directory, validate_restart_marker, ROOT
    from .capabilities import atomic_json, canonical, check_node
    data = (Path(args.data_dir) if args.data_dir else default_data_dir()).resolve()
    root = Path(project_root or ROOT).resolve()
    with _runtime_lock(data), _runtime_lock(data, 'worker-owner.lock'):
        directory = restart_directory(data)
        frontier = _restart_frontier(directory)
        if args.restart_id:
            frontier = [x for x in frontier if x['marker']['restart_id'] == args.restart_id]
        if len(frontier) != 1:
            if args.apply:
                raise PolicyError('RESTART_SELECT_EXACT_PENDING_ID')
            return {'status':'no_pending_restart' if not frontier else 'select_restart_id',
                    'restart_ids':[x['marker']['restart_id'] for x in frontier], 'tasks_resumed':False}
        selected = frontier[0]
        marker = selected['marker']
        primary = selected['paths'][0]
        _, raw = _read_record(primary)
        expected = hashlib.sha256(raw).hexdigest()
        if args.apply and (args.expected_marker_sha256 != expected or not args.reason or not args.reason.strip()):
            raise PolicyError('RESTART_EXACT_HASH_AND_REASON_REQUIRED')
        context, context_raw = _read_record(directory / 'supervisor.json')
        if context.get('schema') != 'supervised-worker-v1' or context.get('data_dir') != str(data):
            raise PolicyError('RESTART_OWNER_DATA_CHANGED')
        old_generation = marker.get('generation_id')
        if context.get('generation_id') != old_generation:
            reference = context.get('resume') or {}
            accepted = directory / ('accepted-' + marker['restart_id'] + '.json')
            if (reference.get('name') != accepted.name or reference.get('generation_id') != old_generation or
                    reference.get('sha256') != hashlib.sha256(accepted.read_bytes()).hexdigest()):
                raise PolicyError('RESTART_CURRENT_GENERATION_UNRELATED')
        validation_context = dict(context, generation_id=old_generation)
        reference = None if primary.name == 'pending.json' else {
            'name':primary.name, 'sha256':expected, 'generation_id':old_generation}
        validated, _ = validate_restart_marker(data, validation_context, reference=reference,
                                               project_root=root, allow_consumed=True)
        if validated != marker:
            raise PolicyError('RESTART_MARKER_CHANGED')
        process_refs = []
        pids = {context.get('supervisor_pid'), marker.get('worker_pid')}
        generations = {old_generation, context.get('generation_id')}
        if any(not isinstance(g, str) or not re.fullmatch('[a-f0-9]{32}', g) for g in generations):
            raise PolicyError('RESTART_GENERATION_CHANGED')
        for generation in generations:
            for prefix in ('worker-', 'process-'):
                path = directory / (prefix + generation + '.json')
                if not path.exists():
                    if generation == old_generation:
                        raise PolicyError('RESTART_PROCESS_RECORD_MISSING')
                    continue  # The owner can die after writing context but before launch.
                record, record_raw = _read_record(path)
                if (record.get('generation_id') != generation or record.get('supervisor_id') != context['supervisor_id']):
                    raise PolicyError('RESTART_PROCESS_OWNER_CHANGED')
                pids.add(record.get('worker_pid' if prefix == 'worker-' else 'launcher_pid'))
                process_refs.append({'name':path.name, 'sha256':hashlib.sha256(record_raw).hexdigest(), 'record':record})
        if any(type(pid) is not int or pid <= 0 for pid in pids):
            raise PolicyError('RESTART_PROCESS_ID_INVALID')
        process_states = [_process_end_state(pid) for pid in sorted(pids)]
        if any(p['state'] != 'not_running' for p in process_states):
            raise PolicyError('RESTART_OWNER_OR_WORKER_STILL_RUNNING')
        owns_store = store is None
        if store is None:
            from .store import Store
            store = Store(data)
        try:
            if manager is None:
                from .policy import PolicyCatalog
                from .updates import UpdateManager
                policy = PolicyCatalog(root / 'policy/complete-policy-v3.json')
                for amendment in store.records('policy_amendment'):
                    if amendment.get('status') == 'applied':
                        policy.apply_amendment(amendment)
                # Preserve recovery of explicitly bound legacy updates while
                # using the same effective policy as the practical server.
                if policy.hash != marker['attestation']['policy_hash']:
                    from .practical_policy import PracticalPolicy
                    policy = PracticalPolicy(root / 'policy/complete-policy-v3.json')
                manager = UpdateManager(data, root, policy)
            attestation = manager.validate_recovery_attestation(marker['candidate_id'], marker['attestation'])
            tasks = []
            for identity in marker['resume_task_ids']:
                task = store.get_task(identity)
                operations = store.operations(identity)
                tasks.append({'task_id':identity, 'status':task['status'], 'source_hash':task['source_hash'],
                    'task_sha256':hashlib.sha256(canonical(task)).hexdigest(),
                    'operations_sha256':hashlib.sha256(canonical(operations)).hexdigest(),
                    'unresolved_operation_ids':[o['operation']['id'] for o in operations
                        if o.get('status') == 'executing' or (o.get('result') or {}).get('effect') == 'unknown']})
            result = {'status':'ready_to_park', 'restart_id':marker['restart_id'], 'candidate_id':marker['candidate_id'],
                'expected_marker_sha256':expected, 'source_version':attestation['source_version'],
                'process_states':process_states, 'tasks':tasks, 'tasks_resumed':False,
                'next_action':'指定したhashでapplyすると記録を保全して通常起動へ戻せます。タスク再開は起動後に明示操作してください。'}
            if not args.apply:
                return result
            # Preserve the original bytes before recording a finite disposition.
            marker_refs = []
            for path in selected['paths']:
                original, original_raw = _read_record(path)
                if original != marker:
                    raise PolicyError('RESTART_MARKER_CHANGED_DURING_RECOVERY')
                digest = hashlib.sha256(original_raw).hexdigest()
                name = 'marker-' + digest + '.json'
                archive = directory / name
                _archive_record(archive, original_raw)
                marker_refs.append({'name':name, 'sha256':digest})
            record_path = directory / ('resolved-' + marker['restart_id'] + '.json')
            owner_name = 'owner-' + hashlib.sha256(context_raw).hexdigest() + '.json'
            owner_archive = directory / owner_name
            _archive_record(owner_archive, context_raw)
            disposition = {'schema':'controller-restart-recovery-v1', 'status':'parked_without_resume',
                'restart_id':marker['restart_id'], 'candidate_id':marker['candidate_id'],
                'marker_refs':marker_refs, 'attestation':attestation, 'tasks':tasks,
                'process_states':process_states, 'process_records':process_refs,
                'supervisor_ref':{'name':owner_name, 'sha256':hashlib.sha256(context_raw).hexdigest()},
                'reason':args.reason, 'recorded_at':now(), 'tasks_resumed':False}
            # Close every old launch generation before publishing the recovery.
            # This survives a stop between disposition and pending cleanup, and
            # prevents a late orphan from using a previously checked context.
            for generation in generations:
                closed = directory / ('closed-' + generation + '.json')
                closure = {'generation_id':generation, 'supervisor_id':context['supervisor_id'],
                           'restart_id':marker['restart_id'], 'disposition':record_path.name}
                if closed.exists():
                    if _read_record(closed)[0] != closure:
                        raise PolicyError('RESTART_GENERATION_DISPOSITION_CHANGED')
                else:
                    atomic_json(closed, closure, exclusive=True)
            if record_path.exists():
                # A previous apply may have ended after recording but before cleanup.
                if not _restart_resolved(directory, marker, raw):
                    raise PolicyError('RESTART_DISPOSITION_CHANGED')
            else:
                atomic_json(record_path, disposition, exclusive=True)
            if _read_record(directory / 'supervisor.json')[1] != context_raw:
                raise PolicyError('RESTART_OWNER_CHANGED_DURING_RECOVERY')
            pending = directory / 'pending.json'
            if pending in selected['paths']:
                check_node(pending)
                if _read_record(pending)[1] != raw:
                    raise PolicyError('RESTART_MARKER_CHANGED_DURING_RECOVERY')
                pending.unlink()
            return dict(result, status='parked_without_resume', disposition=record_path.name,
                        next_action='通常のsuperviseで起動できます。停止済みタスクと未確定作用は保持されています。')
        finally:
            if owns_store:
                store.close()


class Client:
    def __init__(self, url):
        self.client = httpx.Client(base_url=_local_url(url), trust_env=False,
                                   follow_redirects=False, timeout=None)
        response = self.client.get('/')
        response.raise_for_status()
        self.csrf = self.get('/api/session')['csrf_token']

    def get(self, path):
        return self._result(self.client.get(path))

    def post(self, path, body):
        return self._result(self.client.post(path, json=body, headers={'X-CSRF-Token': self.csrf}))

    @staticmethod
    def _result(response):
        if response.status_code >= 400:
            try:
                detail = response.json().get('detail', '操作を処理できませんでした。')
            except ValueError:
                detail = 'ローカルサービスから有効な応答がありません。'
            raise ValueError(detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
        return response.json()

    def close(self):
        self.client.close()


async def _serve(args):
    import uvicorn
    from .server import create_app, default_data_dir, restart_directory, RESTART_EXIT
    from .capabilities import check_node
    _local_url(f'http://[{args.host}]:{args.port}' if ':' in args.host else f'http://{args.host}:{args.port}')
    data_dir = (Path(args.data_dir) if args.data_dir else default_data_dir()).resolve()
    context = None
    if getattr(args, 'internal_worker', False):
        try:
            context = json.loads(os.environ['HERMES_POLICY_WORKER'])
            state_path = restart_directory(data_dir) / 'supervisor.json'
            check_node(state_path)
            expected = json.loads(state_path.read_text(encoding='utf-8'))
            if (context != expected or context['data_dir'] != str(data_dir) or
                    context['launch_id'] != args.launch_id or not context.get('generation_id')):
                raise ValueError()
        except (KeyError, ValueError):
            raise PolicyError('SUPERVISED_WORKER_CONTEXT_INVALID') from None
    with ExitStack() as locks:
        if context is None:
            locks.enter_context(_runtime_lock(data_dir))
        # A worker outliving its supervisor still blocks another controller.
        locks.enter_context(_runtime_lock(data_dir, 'worker-owner.lock'))
        if context is None:
            _assert_restart_clear(data_dir)
        else:
            expected, _ = _read_record(restart_directory(data_dir) / 'supervisor.json')
            if context != expected or (restart_directory(data_dir) / ('closed-' + context['generation_id'] + '.json')).exists():
                raise PolicyError('SUPERVISED_WORKER_CONTEXT_CLOSED_OR_CHANGED')
        app = create_app(data_dir)
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port,
                                             access_log=False, log_level='info', proxy_headers=False))
        app.state.launch_id = args.launch_id
        app.state.worker_context = context
        exit_code = 0
        def request_shutdown():
            nonlocal exit_code
            # A user stop or lost owner wins over a restart already scheduled.
            exit_code = 0
            server.should_exit = True
        app.state.request_shutdown = request_shutdown
        if context:
            def request_restart_exit():
                nonlocal exit_code
                exit_code = RESTART_EXIT
                server.should_exit = True
            app.state.request_restart_exit = request_restart_exit
            loop = asyncio.get_running_loop()
            def watch_supervisor():
                # Anonymous pipe EOF signals owner death; this thread is not a poller.
                try:
                    # A daemon must not hold BufferedReader's lock while Python
                    # finalizes. Read the owned OS descriptor without that buffer.
                    os.read(sys.stdin.fileno(), 1)
                except OSError:
                    pass
                finally:
                    if not loop.is_closed():
                        try:
                            loop.call_soon_threadsafe(request_shutdown)
                        except RuntimeError:
                            pass  # Loop completed while the owner pipe closed.
            threading.Thread(target=watch_supervisor, daemon=True).start()
        await server.serve()
        return exit_code if server.started else 1


def _supervise(args, *, worker_command=None, project_root=None):
    """Wait on owned process exit; only an exact reviewed marker permits replacement."""
    from .server import default_data_dir, restart_directory, validate_restart_marker, ROOT, RESTART_EXIT
    from .capabilities import atomic_json, sha_file
    data_dir = (Path(args.data_dir) if args.data_dir else default_data_dir()).resolve()
    _local_url(f'http://[{args.host}]:{args.port}' if ':' in args.host else f'http://{args.host}:{args.port}')
    root = Path(project_root or ROOT)
    with _runtime_lock(data_dir):
        directory = restart_directory(data_dir)
        _assert_restart_clear(data_dir)
        supervisor_id = uuid4().hex
        launch_id = args.launch_id or uuid4().hex
        resume = None
        while True:
            context = {'schema':'supervised-worker-v1', 'supervisor_id':supervisor_id,
                       'supervisor_pid':os.getpid(), 'launch_id':launch_id,
                       'generation_id':uuid4().hex, 'worker_token':secrets.token_hex(32),
                       'data_dir':str(data_dir), 'resume':resume}
            atomic_json(directory / 'supervisor.json', context)
            argv = ([sys.executable,'-B','-m','policy_harness.cli','serve','--host',args.host,
                     '--port',str(args.port),'--data-dir',str(data_dir),'--launch-id',launch_id,
                     '--internal-worker'] if worker_command is None else worker_command(context))
            environment = os.environ.copy()
            environment['HERMES_POLICY_WORKER'] = json.dumps(context)
            # -B prevents writes but still permits reading old timestamp-based
            # bytecode. An unused generation prefix forces source loading.
            cache_prefix = directory / ('bytecode-' + context['generation_id'])
            if cache_prefix.exists():
                raise PolicyError('RESTART_BYTECODE_PREFIX_ALREADY_EXISTS')
            environment['PYTHONPYCACHEPREFIX'] = str(cache_prefix)
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            process = subprocess.Popen(argv, env=environment, cwd=str(root), stdin=subprocess.PIPE,
                                       close_fds=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            entry = {'supervisor_id':supervisor_id, 'generation_id':context['generation_id'],
                     'launcher_pid':process.pid, 'started_at':now(), 'status':'running'}
            path = directory / ('process-' + context['generation_id'] + '.json')
            atomic_json(path, entry, exclusive=True)
            try:
                code = process.wait()
            except BaseException:
                process.stdin.close()
                code = process.wait()
                entry.update(status='owner_interrupted', exit_code=code, ended_at=now())
                atomic_json(path, entry)
                raise
            finally:
                process.stdin.close()
            entry.update(status='exited', exit_code=code, ended_at=now())
            atomic_json(path, entry)
            if code != RESTART_EXIT:
                return code
            try:
                marker, pending = validate_restart_marker(data_dir, context, project_root=root)
                accepted = directory / ('accepted-' + marker['restart_id'] + '.json')
                # Exclusive immutable accepted record and exact source checks precede restart.
                atomic_json(accepted, marker, exclusive=True)
                resume = {'name':accepted.name, 'sha256':sha_file(accepted),
                          'generation_id':context['generation_id']}
                if json.loads(pending.read_text(encoding='utf-8')) != marker:
                    raise PolicyError('RESTART_MARKER_CHANGED_DURING_ACCEPTANCE')
                pending.unlink()
                entry.update(status='restart_accepted', restart_id=marker['restart_id'])
                atomic_json(path, entry)
            except Exception as error:
                entry.update(status='restart_rejected', error_type=type(error).__name__,
                             error=str(error), effect='unknown')
                atomic_json(path, entry)
                raise


@contextmanager
def _task_submission(client, args):
    """Retain only an opaque pending identity and payload digest across retries.

    The original prompt stays in this invocation and in the trusted server's
    source store. A receipt is acknowledged only after the caller prints it.
    """
    from .capabilities import atomic_json, canonical, check_node
    payload={'objective':args.objective,'acceptance':args.acceptance}
    payload_hash=hashlib.sha256(canonical(payload)).hexdigest()
    origin_hash=hashlib.sha256(_local_url(args.url).encode('utf-8')).hexdigest()
    key=hashlib.sha256((origin_hash+payload_hash).encode('ascii')).hexdigest()
    directory=Path(os.environ.get('HERMES_HARNESS_CLIENT_STATE',str(Path.home()/'.oif'/'submissions'))).expanduser().absolute()
    directory.mkdir(parents=True,exist_ok=True)
    check_node(directory)
    path=directory/(key+'.json')
    with _runtime_lock(directory,key+'.lock'):
        if path.exists():
            check_node(path)
            record=json.loads(path.read_text(encoding='utf-8'))
            if (record.get('schema')!='task-submission-v1' or record.get('payload_sha256')!=payload_hash or
                    record.get('origin_sha256')!=origin_hash or not re.fullmatch('[a-f0-9]{32}',str(record.get('id','')))):
                raise PolicyError('SUBMISSION_RECORD_CHANGED: 保存した送信記録の照合が必要です。')
        else:
            record={'schema':'task-submission-v1','id':uuid4().hex,
                    'payload_sha256':payload_hash,'origin_sha256':origin_hash}
            atomic_json(path,record,exclusive=True)
        response=client.post('/api/tasks',dict(payload,submission_id=record['id']))
        if (response.get('submission_id')!=record['id'] or
                not re.fullmatch('[a-f0-9]{32}',str(response.get('task',{}).get('id',''))) or
                (response.get('records_withheld') and (not response.get('accepted') or response.get('action')!='create'))):
            raise PolicyError('SUBMISSION_RECEIPT_MISMATCH: 送信結果は未確認です。同じコマンドで照合できます。')
        yield response
        check_node(path)
        if json.loads(path.read_text(encoding='utf-8'))!=record:
            raise PolicyError('SUBMISSION_RECORD_CHANGED: 受領後の送信記録が変わっています。')
        path.unlink()


def _follow(client, identity):
    # Server-side event subscription has durable replay. No timed polling loop.
    with client.client.stream('GET', f'/api/tasks/{identity}/events') as response:
        if response.status_code != 200:
            raise ValueError('工程の受信を開始できませんでした。記録は export で確認できます。')
        for line in response.iter_lines():
            if not line.startswith('data: '):
                continue
            event = json.loads(line[6:])
            _print(event)
            if event.get('stage') in {'task','final'} and event.get('status') in {
                'completed', 'stopped', 'held', 'waiting_configuration', 'configuration_required',
                'attention_required', 'recovery_required', 'failed'}:
                return


def parser():
    cli = argparse.ArgumentParser(description='Hermes 作業方針ハーネス — ローカル操作')
    commands = cli.add_subparsers(dest='command', required=True)
    for name, help_text in [('serve','ローカル画面を起動'),('supervise','審査済み更新のプロセス引継ぎ付きで起動')]:
        serve = commands.add_parser(name, help=help_text)
        serve.add_argument('--host', default='127.0.0.1')
        serve.add_argument('--port', type=int, default=8765)
        serve.add_argument('--data-dir')
        serve.add_argument('--launch-id', help=argparse.SUPPRESS)
        if name == 'serve':
            serve.add_argument('--internal-worker', action='store_true', help=argparse.SUPPRESS)
    for name, help_text in [('status','実際の状態を表示'),('run','作業を開始'),('export','全記録を保存'),
                            ('stop','指定タスクを停止'),('resume','指定タスクを再開'),
                            ('instruction','同じタスクへ指示を追加'),('shutdown','ローカルサービスを終了')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--url', default='http://127.0.0.1:8765')
        if name == 'run':
            command.add_argument('objective')
            command.add_argument('--acceptance', action='append', default=[])
            command.add_argument('--follow', action='store_true', help='工程をイベント受信する')
        if name in {'export','stop','resume','instruction'}:
            command.add_argument('task_id')
        if name == 'instruction':
            command.add_argument('text')
            command.add_argument('--expected-source-hash', help='画面や記録で確認した指示の版。省略時は現在の版を一度だけ取得')
        if name == 'export':
            command.add_argument('--output', type=Path)
        if name == 'shutdown':
            command.add_argument('--launch-id', help='一致する起動IDのサービスだけを終了')
    setup = commands.add_parser('setup', help='管理者が実行用の隔離環境を準備する')
    setup.add_argument('--data-dir')
    setup.add_argument('--without-pytest', action='store_true')
    recovery = commands.add_parser('recover-restart', help='停止済みの更新引継ぎを照合し、タスクを再開せず通常起動へ戻す')
    recovery.add_argument('--data-dir')
    recovery.add_argument('--restart-id')
    recovery.add_argument('--apply', action='store_true', help='照合した記録を保全して保留を解消')
    recovery.add_argument('--expected-marker-sha256')
    recovery.add_argument('--reason')
    return cli


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    client = None
    try:
        if args.command == 'serve':
            return asyncio.run(_serve(args))
        if args.command == 'supervise':
            return _supervise(args)
        if args.command == 'recover-restart':
            _print(_recover_restart(args))
            return 0
        if args.command == 'setup':
            from .executor import Executor
            from .server import default_data_dir
            data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
            with _runtime_lock(data_dir):
                with _runtime_lock(data_dir, 'worker-owner.lock'):
                    result = asyncio.run(Executor(data_dir).prepare_environment(include_pytest=not args.without_pytest))
            _print(result)
            return 0 if result.get('status') in {'ready','succeeded','prepared'} or result.get('ready') else 1
        client = Client(args.url)
        if args.command == 'status':
            _print(client.get('/api/status'))
        elif args.command == 'run':
            with _task_submission(client,args) as response:
                task = response['task']
                output = {'task_id': task['id'], 'status': task['status'], 'url': args.url+'/#task='+task['id']}
                for key in ('accepted', 'action', 'records_withheld', 'detail'):
                    if key in response:
                        output[key] = response[key]
                if response.get('records_withheld'):
                    output['next_step'] = '依頼は受理されています。同じ依頼を再送せず、このタスクIDで状態を確認してください。停止は stop を使えます。記録の表示には暗号化設定の復旧が必要です。'
                _print(output)
            if args.follow and not response.get('records_withheld'):
                _follow(client, task['id'])
        elif args.command == 'export':
            snapshot = client.get('/api/tasks/'+args.task_id)
            if args.output:
                # Exclusive creation avoids overwriting an existing evidence file.
                with args.output.open('x', encoding='utf-8') as handle:
                    json.dump(snapshot, handle, ensure_ascii=False, indent=2)
                _print({'saved': str(args.output.resolve())})
            else:
                _print(snapshot)
        elif args.command in {'stop','resume'}:
            _print(client.post(f'/api/tasks/{args.task_id}/{args.command}', {}))
        elif args.command == 'instruction':
            path = '/api/tasks/' + quote(args.task_id, safe='')
            expected = args.expected_source_hash
            if expected is None:
                expected = client.get(path)['task'].get('source_hash')
            if not expected:
                raise ValueError('現在の指示の版を取得できません。タスクの状態を確認してください。')
            _print(client.post(path+'/instructions', {'text':args.text, 'expected_source_hash':expected}))
        elif args.command == 'shutdown':
            current = client.get('/api/status')
            if args.launch_id and current.get('launch_id') != args.launch_id:
                raise ValueError('対象の起動IDが一致しないため、終了しませんでした。')
            _print(client.post('/api/shutdown', {'expected_instance_id':current['instance_id']}))
        return 0
    except KeyboardInterrupt:
        print('受信を中断しました。タスクの停止は stop を使用してください。', file=sys.stderr)
        return 130
    except (ValueError, OSError, httpx.HTTPError, PolicyError) as exc:
        # HTTP error representations can contain request details. Keep those out.
        message = ('ローカルサービスとの通信結果は未確認です。送信IDを保持しています。同じ run コマンドは同じ依頼を照合します。'
                   if args.command=='run' else 'ローカルサービスに接続できません。起動状態とURLを確認してください。') if isinstance(exc,httpx.HTTPError) else str(exc)
        print(message, file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
