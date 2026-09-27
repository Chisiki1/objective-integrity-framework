from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from starlette.background import BackgroundTask

from . import __version__
from .models import ConfigurationRequired, PolicyError, now
from .settings import SettingsError
from .capabilities import atomic_json, canonical, check_node, confined, sha_file

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / 'static'
COOKIE = 'hermes_local_session'
SECRET_FIELDS = {'model_api_key', 'web_api_key', 'api_key', 'authorization', 'password', 'secret'}
RESTART_EXIT = 75
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
CREDENTIAL_RECOVERY = '保存したキーを読み出せません。接続設定でキーを再入力してください。記録の表示はキーを確認できるまで保留しています。'
HISTORICAL_CREDENTIAL_RECOVERY = '以前のキーを読み出せないため、保存済み記録の表示と作業の開始・再開を保留しています。新しい接続キーの保存だけでは解決しません。元のWindows利用者と保護データを復旧してから再確認してください。元の記録と暗号化した設定は保持しています。停止と接続設定は利用できます。'
SETTINGS_RECOVERY = '接続設定またはキーの履歴を読み取れません。元の設定と設定履歴を保持したまま復旧してください。停止と復旧用ID・状態の確認は利用できます。'


def windows_opened_path(value: str) -> str:
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + value[8:]
    return value[4:] if value.startswith('\\\\?\\') else value


def restart_directory(data_dir: Path) -> Path:
    path = Path(data_dir) / '.controller-restarts'
    path.mkdir(parents=True, exist_ok=True)
    check_node(path)
    return path


def source_manifest(project_root: Path) -> dict[str, str]:
    root = confined(project_root, 'src/policy_harness')
    result = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            if path.name in {'__pycache__', '.pytest_cache'}:
                continue
            check_node(path)
            if path.is_dir():
                pending.append(path)
            elif path.suffix not in {'.pyc', '.pyo'}:
                result[path.relative_to(project_root).as_posix()] = sha_file(path)
    return result


def validate_restart_marker(data_dir: Path, context: dict, *, reference=None,
                            project_root: Path = ROOT, allow_consumed=False) -> tuple[dict, Path]:
    directory = restart_directory(data_dir)
    if reference:
        if not isinstance(reference, dict):
            raise PolicyError('RESTART_REFERENCE_INVALID')
        name = reference.get('name', '')
        if not re.fullmatch(r'accepted-[a-f0-9]{32}\.json', name):
            raise PolicyError('RESTART_REFERENCE_INVALID')
        path = directory / name
    else:
        path = directory / 'pending.json'
    check_node(path)
    if reference and sha_file(path) != reference.get('sha256'):
        raise PolicyError('RESTART_MARKER_CHANGED')
    marker = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(marker, dict) or marker.get('schema') != 'controller-restart-v1':
        raise PolicyError('RESTART_SCHEMA_INVALID')
    for name in ('launch_id', 'supervisor_id'):
        if marker.get(name) != context.get(name):
            raise PolicyError('RESTART_OWNER_CHANGED')
    generation = reference.get('generation_id') if reference else context.get('generation_id')
    if (not isinstance(generation, str) or not re.fullmatch('[a-f0-9]{32}', generation) or
            marker.get('generation_id') != generation):
        raise PolicyError('RESTART_GENERATION_CHANGED')
    identity = marker.get('restart_id', '')
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
        raise PolicyError('RESTART_ID_INVALID')
    tasks = marker.get('resume_task_ids')
    if not _valid_resume_ids(tasks):
        raise PolicyError('RESTART_TASKS_INVALID')
    attestation = marker.get('attestation', {})
    if (not isinstance(attestation, dict) or attestation.get('activation_status') not in {'activated', 'rolled_back'} or
            attestation.get('restart_required') is not True or not attestation.get('activation_hash')):
        raise PolicyError('RESTART_ACTIVATION_INVALID')
    sources = source_manifest(Path(project_root))
    version = hashlib.sha256(canonical(sources)).hexdigest()
    if (attestation.get('source_hashes') != sources or attestation.get('source_version') != version or
            marker.get('candidate_id') != attestation.get('candidate_id')):
        raise PolicyError('RESTART_SOURCE_CHANGED')
    worker_path = directory / ('worker-' + generation + '.json')
    check_node(worker_path)
    worker = json.loads(worker_path.read_text(encoding='utf-8'))
    if any(worker.get(k) != marker.get(k) for k in ('launch_id','supervisor_id','generation_id','worker_pid')):
        raise PolicyError('RESTART_WORKER_CHANGED')
    if not allow_consumed and (directory / ('consumed-' + identity + '.json')).exists():
        raise PolicyError('RESTART_ALREADY_CONSUMED')
    return marker, path


def _valid_resume_ids(value) -> bool:
    return (isinstance(value, list) and bool(value) and
            all(isinstance(t, str) and re.fullmatch('[a-zA-Z0-9_-]{1,128}', t) for t in value) and
            len(value) == len(set(value)))


async def bind_restart_consumer(app: FastAPI, engine, store, data_dir: Path):
    context = getattr(app.state, 'worker_context', None)
    exit_callback = getattr(app.state, 'request_restart_exit', None)
    if not context or not exit_callback:
        return
    project_root = Path(getattr(app.state, 'project_root', ROOT))
    directory = restart_directory(data_dir)
    worker = {k: context[k] for k in ('launch_id','supervisor_id','generation_id')}
    worker.update(worker_pid=os.getpid(), started_at=now())
    atomic_json(directory / ('worker-' + context['generation_id'] + '.json'), worker, exclusive=True)

    async def on_restart(payload):
        if app.state.shutting_down:
            raise PolicyError('RESTART_WHILE_SHUTTING_DOWN')
        manager = getattr(engine, 'updates', None)
        if manager is None:
            raise PolicyError('RESTART_UPDATE_MANAGER_UNAVAILABLE')
        attestation = await _maybe_await(manager.restart_attestation(payload['candidate_id']))
        for key in ('source_version','source_hashes','activation_hash'):
            if key in payload and payload[key] != attestation.get(key):
                raise PolicyError('RESTART_ATTESTATION_CHANGED')
        ids = payload.get('resume_task_ids', [])
        if not _valid_resume_ids(ids):
            raise PolicyError('RESTART_TASKS_INVALID')
        for identity in ids:
            task = store.get_task(identity)
            if task['status'] == 'completed' or any(op.get('status') == 'executing' for op in store.operations(identity)):
                raise PolicyError('RESTART_TASK_NOT_AT_BOUNDARY')
        marker = dict(worker, schema='controller-restart-v1', restart_id=uuid4().hex,
                      candidate_id=payload['candidate_id'], resume_task_ids=ids,
                      attestation=attestation, requested_at=now())
        pending = directory / 'pending.json'
        atomic_json(pending, marker, exclusive=True)
        # Read back the actual disk cut before asking this exact worker to exit.
        validate_restart_marker(data_dir, context, project_root=project_root)
        for identity in ids:
            store.event(identity, 'controller_restart', 'requested', {
                'restart_id':marker['restart_id'], 'candidate_id':marker['candidate_id'],
                'source_version':attestation['source_version'], 'worker_pid':os.getpid(),
                'message':'審査済み更新を、新しい実行プロセスへ引き継ぎます。'})
        app.state.shutting_down = True
        app.state.engine.service_shutdown_pending = True
        app.state.shutdown_event.set()
        exit_callback()

    engine.on_restart = on_restart
    reference = context.get('resume')
    if reference:
        marker, _ = validate_restart_marker(data_dir, context, reference=reference, project_root=project_root)
        attestation = await _maybe_await(engine.updates.restart_attestation(marker['candidate_id']))
        if attestation != marker['attestation']:
            raise PolicyError('RESTART_ACTIVATION_CHANGED')
        for identity in marker['resume_task_ids']:
            if store.get_task(identity)['status'] == 'completed':
                raise PolicyError('RESTART_TASK_ALREADY_COMPLETED')
        receipt = {'restart_id':marker['restart_id'], 'candidate_id':marker['candidate_id'],
                   'previous_worker_pid':marker['worker_pid'], 'worker_pid':os.getpid(),
                   'generation_id':context['generation_id'], 'source_version':attestation['source_version'],
                   'resume_task_ids':marker['resume_task_ids'], 'claimed_at':now(), 'status':'claimed',
                   'proof_ceiling':'Fresh process and requested resumption; next cycle outcome unobserved.'}
        receipt_path = directory / ('consumed-' + marker['restart_id'] + '.json')
        atomic_json(receipt_path, receipt, exclusive=True)
        for identity in marker['resume_task_ids']:
            store.event(identity, 'controller_restart', 'resuming', receipt)
            if inspect.iscoroutinefunction(engine.resume_task):
                await engine.resume_task(identity)
            else:
                engine.resume_task(identity)
        receipt.update(status='resume_dispatched', dispatched_at=now())
        atomic_json(receipt_path, receipt)


class AttachmentPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filename: str = Field(min_length=1, max_length=255)
    base64: str = Field(max_length=((MAX_ATTACHMENT_BYTES + 2) // 3) * 4)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    objective: str = Field(min_length=1, max_length=100_000)
    acceptance: list[str] = Field(default_factory=list, max_length=100)
    submission_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    attachments: list[AttachmentPayload] = Field(default_factory=list, max_length=12)
    access_mode: Literal['workspace', 'ask', 'read_only', 'full'] | None = None
    confirm_full_access: StrictBool = False
    folder_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')

    @field_validator('objective')
    @classmethod
    def nonblank_objective(cls, value):
        if not value.strip():
            raise ValueError('空白だけの目的は送信できません。')
        return value

    @field_validator('acceptance')
    @classmethod
    def nonblank_acceptance(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError('完成条件には空白以外の内容を入力してください。')
        return values


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    decision: Literal['approve', 'reject']
    expected_hash: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class InstructionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=100_000)
    expected_source_hash: str = Field(pattern=r'^[a-fA-F0-9]{64}$')

    @field_validator('text')
    @classmethod
    def nonblank_original(cls, value):
        if not value.strip():
            raise ValueError('空白だけの指示は送信できません。')
        return value  # Keep the user's original whitespace and line breaks.


class AttachmentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filename: str = Field(min_length=1, max_length=255)
    base64: str = Field(max_length=((MAX_ATTACHMENT_BYTES + 2) // 3) * 4)
    expected_source_hash: str = Field(pattern=r'^[a-fA-F0-9]{64}$')

    @field_validator('filename')
    @classmethod
    def original_basename(cls, value):
        if (not value.strip() or value in {'.','..'} or any(c in value for c in '/\\:')
                or any(ord(c) < 32 for c in value)):
            raise ValueError('添付ファイル名には名前だけを指定してください。')
        return value


class ShutdownInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_instance_id: str
    only_if_idle: StrictBool = False
    only_if_source_changed: StrictBool = False


class ReleaseVersionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+(?:-beta\.\d+)?$')


class ReleaseInstallInput(ReleaseVersionInput):
    expected_instance_id: str


class MessageInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(default='', max_length=100_000)
    attachments: list[AttachmentPayload] = Field(default_factory=list, max_length=12)
    expected_source_hash: str = Field(pattern=r'^[a-fA-F0-9]{64}$')
    submission_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class AccessInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['workspace', 'ask', 'read_only', 'full']
    confirm_full_access: StrictBool = False
    expected_revision: int = Field(ge=0)


class TaskMetadataInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    pinned: bool | None = None
    archived: bool | None = None
    deleted: bool | None = None
    folder_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


class FolderInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    parent_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


def default_data_dir() -> Path:
    return Path(os.environ.get('HERMES_HARNESS_DATA', str(ROOT / '.runtime'))).expanduser().resolve()


def create_runtime(data_dir: Path) -> tuple[Any, Any, Any, Any]:
    """The UI owns no execution implementation; assemble the guarded services."""
    from .practical_engine import PracticalEngine
    from .executor import Executor
    from .knowledge import Knowledge
    from .practical_policy import PracticalPolicy
    from .providers import ModelGateway, WebCollector
    from .settings import SettingsManager
    from .store import Store
    from .updates import UpdateManager

    settings = SettingsManager(data_dir)
    store = Store(data_dir)
    policy = PracticalPolicy(ROOT / 'policy' / 'complete-policy-v3.json')
    engine = PracticalEngine(store, policy, Executor(data_dir), ModelGateway(settings, policy),
                    WebCollector(settings), Knowledge(store), updates=UpdateManager(data_dir, ROOT, policy))
    return store, settings, policy, engine


def _loopback(value: str | None) -> bool:
    try:
        return bool(value and ipaddress.ip_address(value).is_loopback)
    except ValueError:
        return False


def _authority(value: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlsplit('http://' + value)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return None
        hostname = parsed.hostname or ''
        if hostname.lower() != 'localhost' and not _loopback(hostname):
            return None
        return hostname.lower(), parsed.port
    except ValueError:
        return None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def create_app(data_dir: Path | None = None, engine=None, settings=None, store=None) -> FastAPI:
    data_dir = Path(data_dir or default_data_dir()).resolve()
    owns_runtime = engine is None
    sessions: dict[str, str] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if engine is None:
            runtime_store, runtime_settings, policy, runtime_engine = create_runtime(data_dir)
        else:
            if store is None or settings is None:
                raise ValueError('Injected engine requires store and settings.')
            runtime_store, runtime_settings, runtime_engine = store, settings, engine
            policy = getattr(engine, 'policy', None)
        app.state.store = runtime_store
        app.state.settings = runtime_settings
        from .provider_login import ProviderLogin
        app.state.provider_login = ProviderLogin(runtime_settings)
        app.state.policy = policy
        app.state.engine = runtime_engine
        app.state.data_dir = data_dir
        app.state.instance_id = secrets.token_hex(16)
        from .explanations import ExplanationService
        app.state.explanations = ExplanationService(runtime_store, getattr(runtime_engine, 'gateway', None))
        app.state.shutting_down = False
        app.state.shutdown_event = asyncio.Event()
        from .product_updates import ProductUpdates
        app.state.product_updates = ProductUpdates(ROOT, data_dir)
        try:
            await bind_restart_consumer(app, runtime_engine, runtime_store, data_dir)
            yield
        finally:
            app.state.shutting_down = True
            if owns_runtime:
                close = getattr(runtime_engine, 'close', None)
                if close:
                    await _maybe_await(close())
                else:
                    # Only operations owned by this runtime are cancelled by its engine.
                    for task in runtime_store.list_tasks():
                        if task.get('status') in {'running', 'stopping'}:
                            await _maybe_await(runtime_engine.stop_task(task['id']))
                await _maybe_await(runtime_store.close())

    app = FastAPI(title='OIF', version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def redaction_context():
        manager = getattr(app.state, 'settings', None)
        secret_values, unavailable = [], []
        if manager:
            reader = getattr(manager, 'redaction_secrets', None)
            if reader is not None:
                try:
                    return reader()
                except SettingsError:
                    raise ConfigurationRequired(SETTINGS_RECOVERY) from None
            # Injected settings fixtures retain the same secret() contract.
            for name in ('model_api_key', 'web_api_key'):
                try:
                    secret = manager.secret(name)
                except ConfigurationRequired:
                    unavailable.append(name)
                    continue
                if secret:
                    secret_values.append(secret)
        return tuple(secret_values), tuple(unavailable)

    def public(value: Any, *, context=None, configuration_only=False) -> Any:
        secret_values, unavailable = context if context is not None else redaction_context()
        if unavailable and not configuration_only:
            raise ConfigurationRequired(recovery_message(unavailable))

        def scrub(item):
            if isinstance(item, dict):
                return {str(k): ('[非表示]' if str(k).lower() in SECRET_FIELDS else scrub(v))
                        for k, v in item.items()}
            if isinstance(item, (list, tuple)):
                return [scrub(v) for v in item]
            if isinstance(item, str):
                for secret in secret_values:
                    item = item.replace(secret, '[非表示]')
            return item
        from .store import redact
        return scrub(redact(value))

    def recovery_message(unavailable):
        return HISTORICAL_CREDENTIAL_RECOVERY if any(name.startswith('history:') for name in unavailable) else CREDENTIAL_RECOVERY

    def control_task(task):
        # IDs are generated by Store, not model/user text. Never echo arbitrary
        # objective, error, source, parent ID or state in this recovery projection.
        identity = task.get('id')
        if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise PolicyError('The saved task identity needs local recovery.')
        statuses = {'created', 'running', 'stopping', 'stopped', 'completed', 'retired',
                    'configuration_required', 'attention_required', 'recovery_required',
                    'source_update_required', 'source_preparation_required', 'failed', 'held'}
        return {'id': identity, 'status': task['status'] if task.get('status') in statuses else 'unknown'}

    def accepted_response(value, *, action, task_id=None, approval_id=None):
        try:
            return public(value)
        except ConfigurationRequired as exc:
            # The precondition can change while an awaited action is in flight.
            # The action is already accepted; never turn this into a bare409.
            result = {'accepted': True, 'action': action, 'records_withheld': True,
                      'detail': '操作は受理済みです。重ねて送信しないでください。' + str(exc)}
            if task_id is not None:
                result['task'] = control_task(app.state.store.get_task(task_id))
            if approval_id is not None:
                result['approval_identity_sha256'] = hashlib.sha256(approval_id.encode('utf-8')).hexdigest()
            return result

    def configuration_view():
        # Only the settings schema is eligible for recovery without all keys.
        # Task bodies, events, health error text and provider replies are not.
        from .settings import DEFAULTS
        context = redaction_context()
        raw = app.state.settings.public()
        allowed = set(DEFAULTS) | {'model_api_key_configured', 'web_api_key_configured',
                                  'secret_storage', 'cost_limit'}
        result = {key: value for key, value in raw.items() if key in allowed}
        result['credential_status'] = {
            label: 'unavailable' if name in context[1] else
                  'available' if raw.get(name + '_configured') else 'not_configured'
            for label, name in (('model', 'model_api_key'), ('web', 'web_api_key'))}
        result['redaction_status'] = ('historical_recovery_required' if any(name.startswith('history:') for name in context[1])
                                      else 'credential_recovery_required' if context[1] else 'ready')
        result['recovery_detail'] = recovery_message(context[1]) if context[1] else ''
        return public(result, context=context, configuration_only=True), context

    @app.middleware('http')
    async def local_security(request: Request, call_next):
        host = _authority(request.headers.get('host', ''))
        if not _loopback(request.client.host if request.client else None) or host is None:
            return JSONResponse({'detail': 'この画面はローカル接続で使用してください。'}, status_code=403)
        callback = request.method == 'GET' and request.url.path.startswith('/oauth/openrouter/')
        origin = request.headers.get('origin')
        if origin and not callback:
            try:
                parsed = urlsplit(origin)
                origin_authority = _authority(parsed.netloc)
                if (parsed.scheme != request.url.scheme or origin_authority != host or
                        parsed.path or parsed.query or parsed.fragment):
                    raise ValueError('origin')
            except ValueError:
                return JSONResponse({'detail': '接続元がこの画面と一致しません。'}, status_code=403)
        if not callback and request.headers.get('sec-fetch-site') in {'cross-site', 'same-site'}:
            return JSONResponse({'detail': 'この画面から直接操作してください。'}, status_code=403)
        sid = request.cookies.get(COOKIE)
        if request.url.path.startswith('/api/'):
            if not sid or sid not in sessions:
                return JSONResponse({'detail': '画面を開き直して接続してください。'}, status_code=401)
            if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                candidate = request.headers.get('x-csrf-token', '')
                if not secrets.compare_digest(candidate, sessions[sid]):
                    return JSONResponse({'detail': '操作用の接続情報が一致しません。'}, status_code=403)
                if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                    return JSONResponse({'detail': 'JSON形式の操作が必要です。'}, status_code=415)
                if app.state.shutting_down:
                    return JSONResponse({'detail': 'OIFを再起動しています。再接続後に操作してください。'}, status_code=409)
        response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'",
        })
        if request.url.path == '/' and request.method == 'GET' and sid not in sessions:
            sid = secrets.token_urlsafe(32)
            sessions[sid] = secrets.token_urlsafe(32)
            response.set_cookie(COOKIE, sid, httponly=True, samesite='strict', secure=False, path='/')
        return response

    @app.exception_handler(PolicyError)
    async def policy_error(request: Request, exc: PolicyError):
        try:
            detail = public(str(exc))
        except ConfigurationRequired as withheld:
            return JSONResponse({'detail': str(withheld), 'kind': 'ConfigurationRequired'}, status_code=409)
        return JSONResponse({'detail': detail, 'kind': type(exc).__name__},
                            status_code=409 if isinstance(exc, ConfigurationRequired) or
                            str(exc).startswith('SOURCE_HASH_CONFLICT:') else 422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default errors include the submitted input, which can be a key.
        return JSONResponse({'detail': [{'loc': list(e['loc']), 'msg': e['msg'], 'type': e['type']}
                                        for e in exc.errors()]}, status_code=422)

    def task_or_404(task_id: str) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
            raise HTTPException(404, 'タスクが見つかりません。')
        try:
            task = app.state.store.get_task(task_id)
        except (KeyError, LookupError):
            raise HTTPException(404, 'タスクが見つかりません。') from None
        if not task:
            raise HTTPException(404, 'タスクが見つかりません。')
        return task

    def current_source_task(task_id: str, expected_source_hash: str) -> dict:
        task = task_or_404(task_id)
        if app.state.shutting_down:
            raise HTTPException(409, 'サービスを終了処理中です。指示はまだ受け付けていません。')
        if task.get('source_hash') != expected_source_hash:
            raise HTTPException(409, 'SOURCE_HASH_CONFLICT: 指示の版が更新されています。最新の履歴を確認してください。')
        return task

    def artifact_location(task: dict, relative_path: str) -> tuple[Path, Path]:
        from .access import Access
        from .access_paths import operation_path
        if Path(relative_path).is_absolute() and Access(app.state.store).get(task['id'])['mode'] == 'full':
            try:
                resolved = operation_path(Path(task['workspace']), relative_path, full_access=True)
            except PolicyError as error:
                raise ValueError(str(error)) from error
            if not resolved.is_file():
                raise ValueError('not a plain file')
            return resolved.parent, resolved
        path = PurePosixPath(relative_path)
        if (not relative_path or '\\' in relative_path or ':' in relative_path or path.is_absolute()
                or any(part in {'', '.', '..'} for part in relative_path.split('/'))):
            raise HTTPException(400, '成果物のパスが正しくありません。')
        workspace = Path(task['workspace'])
        root = data_dir / 'workspaces'
        root_info = root.lstat()
        if root.is_symlink() or getattr(root_info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked root')
        workspace.resolve().relative_to(root.resolve())
        current = root
        for part in workspace.relative_to(root).parts + path.parts:
            current /= part
            info = current.lstat()
            if current.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('linked path')
        resolved = current.resolve(strict=True)
        resolved.relative_to(workspace.resolve())
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('not a plain file')
        return workspace, resolved

    def artifact_name(task, value):
        from .access import Access
        from .access_paths import artifact_path
        return artifact_path(Path(task['workspace']), value,
                             full_access=Access(app.state.store).get(task['id'])['mode'] == 'full')

    def project_snapshot(task_id: str, snapshot: Any, store=None) -> dict:
        from .organization import Organization
        from .access import Access
        store = store or app.state.store
        result = dict(snapshot) if isinstance(snapshot, dict) else {}
        task = result.get('task') or task_or_404(task_id)
        result['task'] = Organization(store).project(task)
        result['access'] = Access(store).get(task_id)
        operations = result.get('operations')
        if operations is None:
            operations = store.operations(task_id)
        projected = []
        for row in operations:
            updated = dict(row)
            actual = row.get('result')
            if isinstance(actual, dict) and isinstance(actual.get('artifacts'), list):
                artifacts = []
                for original in actual['artifacts']:
                    if not isinstance(original, dict):
                        artifacts.append(original)
                        continue
                    item = dict(original)
                    # Only this projection assigns usable links. Original records
                    # and the original path remain unchanged in protected storage.
                    item.pop('download_url', None)
                    item.pop('relative_path', None)
                    item.pop('download_operation_id', None)
                    value = original.get('path', original.get('relative_path'))
                    if isinstance(value, str):
                        try:
                            operation_id = row['operation']['id']
                            artifact_hash = original.get('sha256', '')
                            if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id) or
                                    not re.fullmatch(r'[a-fA-F0-9]{64}', artifact_hash)):
                                raise ValueError('unbound artifact identity')
                            path = Path(value)
                            relative = artifact_name(task, value)
                            artifact_location(task, relative)
                        except (KeyError, TypeError, ValueError, OSError, HTTPException):
                            pass
                        else:
                            item['relative_path'] = relative
                            item['download_operation_id'] = operation_id
                            item['download_url'] = ('/api/tasks/' + quote(task_id, safe='') + '/artifacts/' + quote(relative, safe='/') +
                                '?operation_id=' + quote(operation_id, safe='') + '&sha256=' + artifact_hash.lower())
                    artifacts.append(item)
                updated['result'] = dict(actual, artifacts=artifacts)
            projected.append(updated)
        result['operations'] = projected
        if 'events' not in result:
            result['events'] = store.events(task_id)
        if 'children' not in result:
            result['children'] = [t for t in store.list_tasks() if t.get('parent_id') == task_id]
        return result

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'index.html', media_type='text/html')

    @app.get('/api/session')
    async def session(request: Request):
        return {'csrf_token': sessions[request.cookies[COOKIE]], 'instance_id': app.state.instance_id,
                'attachment_max_bytes': MAX_ATTACHMENT_BYTES}

    @app.get('/api/status')
    async def status():
        config, context = configuration_view()
        executor = getattr(app.state.engine, 'executor', None)
        gateway = getattr(app.state.engine, 'gateway', None)
        async def health(service):
            method = getattr(service, 'health', None)
            if not method:
                return {'status': 'not-observed'}
            try:
                return await _maybe_await(method())
            except Exception as exc:
                return {'status': 'error', 'error': public(str(exc))}
        if context[1]:
            # Arbitrary health payloads cannot be screened against unreadable keys.
            executor_health = {'status': 'not-observed'}
            model_health = {'status': 'configuration_required', 'message': recovery_message(context[1])}
        else:
            executor_health, model_health = await asyncio.gather(health(executor), health(gateway))
        return public({'version': __version__, 'service': 'available',
                       'instance_id': app.state.instance_id,
                       'launch_id': getattr(app.state, 'launch_id', None),
                       'worker_pid': os.getpid(),
                       'generation_id': (getattr(app.state, 'worker_context', None) or {}).get('generation_id'),
                       'supervised': bool(getattr(app.state, 'worker_context', None)),
                       'policy_hash': getattr(app.state.policy, 'hash', None),
                       'settings': config, 'executor': executor_health, 'model': model_health,
                       'active_tasks': len(app.state.store.active_task_ids(statuses=('running',))),
                       'evidence_limit': '接続中という表示は、方針の全条件や実作業の成功を保証するものではありません。'},
                      context=context, configuration_only=bool(context[1]))

    @app.get('/api/policy')
    async def policy():
        if app.state.policy is None:
            return {'status': 'unavailable'}
        return public({'policy_hash': app.state.policy.hash, **app.state.policy.summary()})

    @app.get('/api/tasks')
    async def tasks(cursor: str = '', limit: int = 60, view: str = 'active', folder: str = ''):
        from .organization import Organization
        organization = Organization(app.state.store)
        safe = public({**app.state.store.task_page(cursor=cursor, limit=limit, view=view, folder=folder), 'folders': organization.folders()})
        for task in safe['tasks']:task['objective'] = (task.get('objective') or '')[:640]
        return safe

    @app.get('/api/task-list/events')
    async def task_list_events():
        public(None)
        async def stream():
            iterator = app.state.store.subscribe_task_list()
            stopping = asyncio.create_task(app.state.shutdown_event.wait())
            pending = None
            try:
                while True:
                    pending = asyncio.create_task(anext(iterator))
                    done, _ = await asyncio.wait((pending, stopping), return_when=asyncio.FIRST_COMPLETED)
                    if stopping in done:break
                    revision = pending.result()
                    public(None)
                    yield f'event: changed\ndata: {revision}\n\n'
            finally:
                handles = [h for h in (pending, stopping) if h is not None]
                for handle in handles:handle.cancel()
                await asyncio.gather(*handles, return_exceptions=True)
                await iterator.aclose()
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/api/folders', status_code=201)
    async def create_folder(payload: FolderInput):
        public(None)
        from .organization import Organization
        return public(Organization(app.state.store).create_folder(payload.name, payload.parent_id))

    @app.patch('/api/tasks/{task_id}/organization')
    async def organize_task(task_id: str, payload: TaskMetadataInput):
        public(None)
        task_or_404(task_id)
        from .organization import Organization
        changes = payload.model_dump(exclude_unset=True, exclude={'expected_revision'})
        value = Organization(app.state.store).update(task_id, changes, payload.expected_revision)
        return public({'task_id': task_id, 'ui': value})

    @app.post('/api/tasks/{task_id}/access')
    async def task_access(task_id: str, payload: AccessInput):
        public(None)
        task = task_or_404(task_id)
        from .access import Access
        if task.get('state', {}).get('runtime') != 'practical-v1':
            raise HTTPException(409, '過去の実行方式の権限は変更できません。新しいタスクで利用してください。')
        value = Access(app.state.store).set(task_id, payload.mode, payload.expected_revision,
                                           confirm_full_access=payload.confirm_full_access)
        if task['status'] == 'awaiting_user':
            if inspect.iscoroutinefunction(app.state.engine.resume_task):
                await app.state.engine.resume_task(task_id)
            else:
                app.state.engine.resume_task(task_id)
        return public(value)

    @app.post('/api/clipboard/files')
    async def paste_files(payload: dict):
        public(None)
        if payload != {'gesture': 'paste'}:
            raise HTTPException(422, '入力欄の貼り付け操作から利用してください。')
        from .attachments import clipboard_files
        return {'files': await asyncio.to_thread(clipboard_files)}

    @app.get('/api/recovery/tasks')
    async def recovery_tasks(cursor: str = '', limit: int = 60):
        page = app.state.store.task_page(cursor=cursor, limit=limit, view='all')
        return {'tasks': [control_task(task) for task in page['tasks']], 'next_cursor': page['next_cursor'],
                'records_withheld': True,
                'detail': '復旧用のIDと状態だけを表示しています。本文・履歴・未確定作用の内容は表示していません。'}

    @app.post('/api/tasks', status_code=201)
    async def new_task(payload: TaskInput):
        public(None)
        # Ordinary UI/CLI callers retain this identity before the request. An
        # omitted identity remains an explicit new submission for older callers.
        submission_id=payload.submission_id or uuid4().hex
        from .attachments import decode_files
        files = decode_files([a.model_dump() for a in payload.attachments])
        options = {k: v for k, v in {'access_mode': payload.access_mode, 'folder_id': payload.folder_id}.items() if v is not None}
        if payload.confirm_full_access:
            options['confirm_full_access'] = True
        try:
            extra = {'attachments': files, 'options': options} if files or options else {}
            task=app.state.store.create_task(payload.objective,payload.acceptance,submission_id=submission_id, **extra)
        except PolicyError as error:
            if str(error).startswith('SUBMISSION_PAYLOAD_CONFLICT:'):
                raise HTTPException(409,'SUBMISSION_PAYLOAD_CONFLICT: 同じ送信IDに別の依頼は指定できません。') from None
            raise
        if app.state.store.claim_submission_start(submission_id):
            try:
                if inspect.iscoroutinefunction(app.state.engine.start_task):
                    await app.state.engine.start_task(task['id'])
                else:
                    app.state.engine.start_task(task['id'])
            except BaseException as error:
                app.state.store.event(task['id'],'creation','dispatch_unknown',
                    {'error_type':type(error).__name__,'automatic_replay':False})
                app.state.store.update_task(task['id'],status='recovery_required')
                raise
        response=accepted_response(app.state.store.get_submission(submission_id),action='create',task_id=task['id'])
        response['submission_id']=submission_id
        return response

    @app.get('/api/submissions/{submission_id}')
    async def submission_view(submission_id: str):
        # Receipt lookup never dispatches or resumes work, including after a
        # refresh or when protected task records cannot currently be displayed.
        try:
            receipt=app.state.store.get_submission(submission_id)
        except KeyError:
            raise HTTPException(404,'送信の受領記録はまだ見つかりません。') from None
        response=accepted_response(receipt,action='create',task_id=receipt['task']['id'])
        response['submission_id']=submission_id
        return response

    def busy_tasks():
        ids = set(app.state.store.active_task_ids(statuses=('running', 'stopping')))
        ids.update(key for key, handle in getattr(app.state.engine, 'running', {}).items() if not handle.done())
        return len(ids)

    def loaded_source():
        manager = getattr(app.state.engine, 'updates', None)
        return manager.source_status() if manager and hasattr(manager, 'source_status') else {'restart_required': False}

    @app.get('/api/maintenance')
    async def maintenance():
        return {'version': __version__, 'instance_id': app.state.instance_id,
                'launch_id': getattr(app.state, 'launch_id', None),
                'conditional_shutdown': True, 'active_tasks': busy_tasks(), **loaded_source()}

    def shutdown_ready(payload):
        callback = getattr(app.state, 'request_shutdown', None)
        if callback is None:
            raise HTTPException(409, 'この起動方法は画面からのサービス終了に対応していません。')
        if payload.expected_instance_id != app.state.instance_id:
            raise HTTPException(409, '終了対象のサービスが入れ替わりました。')
        if payload.only_if_idle and busy_tasks():
            raise HTTPException(409, '実行中の作業があります。作業が終わってから更新・再起動してください。')
        return callback

    def request_exit(callback):
        app.state.shutting_down = True
        # Block a queued practical start even if its HTTP request entered before
        # this atomic idle check. Existing task handles were checked above.
        app.state.engine.maintenance_restart_pending = True
        app.state.shutdown_event.set()
        callback()

    @app.post('/api/shutdown')
    async def shutdown(payload: ShutdownInput):
        callback = shutdown_ready(payload)
        if payload.only_if_source_changed and not loaded_source()['restart_required']:
            return {'status': 'unchanged'}
        request_exit(callback)
        return {'status': 'shutdown_requested', 'detail': '実行中の作用を保存して終了します。'}

    @app.post('/api/maintenance/restart')
    async def restart_service(payload: ShutdownInput):
        if not payload.only_if_idle:
            raise HTTPException(422, '作業の完了を待って再起動してください。')
        callback = shutdown_ready(payload)
        from .product_updates import launch_restart
        launch_restart(ROOT, data_dir, getattr(app.state, 'launch_id', None))
        request_exit(callback)
        return {'status': 'restart_requested'}

    @app.get('/api/app-updates')
    async def product_update_status():
        return app.state.product_updates.status()

    @app.post('/api/app-updates/check')
    async def product_update_check(payload: dict):
        if set(payload) - {'force'} or not isinstance(payload.get('force', False), bool):
            raise HTTPException(422, 'Invalid update check')
        return await asyncio.to_thread(app.state.product_updates.check, force=payload.get('force', False))

    @app.post('/api/app-updates/prepare')
    async def product_update_prepare(payload: ReleaseVersionInput):
        try:
            return await asyncio.to_thread(app.state.product_updates.prepare, payload.version)
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from None

    @app.post('/api/app-updates/install')
    async def product_update_install(payload: ReleaseInstallInput):
        callback = shutdown_ready(ShutdownInput(expected_instance_id=payload.expected_instance_id, only_if_idle=True))
        try:
            # No await between the final idle check, helper dispatch and exit.
            app.state.product_updates.install(payload.version, getattr(app.state, 'launch_id', None))
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from None
        request_exit(callback)
        return {'status': 'installing', 'close_window': True}

    async def read_snapshot(task_id):
        if inspect.iscoroutinefunction(app.state.engine.snapshot):
            return await app.state.engine.snapshot(task_id)
        return await asyncio.to_thread(app.state.engine.snapshot, task_id)

    @app.get('/api/tasks/{task_id}')
    async def task_view(task_id: str, view: str = '', after: int = 0, before: int = 0, source_before: int = 0, operation_before: int = 0, artifact_page: int = 0):
        task_or_404(task_id)
        if view == 'conversation':
            from .record_reader import snapshot_reader
            from .ui_records import conversation_view, event_view
            context = redaction_context()
            public(None, context=context)
            def prepare():
                with snapshot_reader(app.state.store, app.state.engine, task_id, display=True,
                                     after=max(0,after), before=max(0,before), source_before=max(0,source_before), operation_before=max(0,operation_before), artifact_page=max(0,artifact_page)) as (reader, engine):
                    reader.transform_event = lambda row: event_view(public(row, context=context))
                    snapshot = engine.snapshot(task_id)
                    safe = public(project_snapshot(task_id, snapshot, reader), context=context)
                    result = conversation_view(safe)
                    paths = {}
                    def artifact_eligible(value, digest, operation_id):
                        if (not isinstance(value, str) or not isinstance(digest, str) or
                                not isinstance(operation_id, str) or
                                not re.fullmatch(r'[a-fA-F0-9]{64}', digest) or
                                not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id)):
                            return False
                        if public([value, digest, operation_id], context=context) != [value, digest, operation_id]:
                            return False
                        if value not in paths:
                            try:
                                path = Path(value)
                                relative = artifact_name(snapshot['task'], value)
                                artifact_location(snapshot['task'], relative)
                                paths[value] = True
                            except (KeyError, TypeError, ValueError, OSError, HTTPException):
                                paths[value] = False
                        return paths[value]
                    # Eligibility precedes SQL version/path ranking; an invalid
                    # later record must not hide the last usable artifact.
                    result['artifact_operations'] = public(project_snapshot(task_id,{'task':snapshot['task'],'operations':reader.latest_artifacts(artifact_eligible),'events':[],'children':[]},reader)['operations'],context=context)
                    result['record_page'] = reader.page
                    return result
            return await asyncio.to_thread(prepare)
        snapshot = await read_snapshot(task_id)
        return await asyncio.to_thread(lambda: public(project_snapshot(task_id, snapshot)))

    @app.get('/api/tasks/{task_id}/records')
    @app.get('/api/tasks/{task_id}/records/{section}/{identity}')
    async def record_view(task_id: str, section: str = '', identity: str = '', download: bool = False):
        from .record_reader import snapshot_reader
        task_or_404(task_id)
        context = redaction_context()
        public(None, context=context)
        value = None
        if section == 'operations':
            try:
                value = app.state.store.get_operation(identity)
            except KeyError:
                raise HTTPException(404, 'Record not found') from None
            if value['task_id'] != task_id:
                raise HTTPException(404, 'Record not found')
        elif section == 'events' and identity.isdecimal():
            with app.state.store.lock:
                row = app.state.store.db.execute('SELECT * FROM events WHERE task_id=? AND seq=?', (task_id,int(identity))).fetchone()
            if row is None:
                raise HTTPException(404, 'Record not found')
            value = dict(row, detail=json.loads(row['detail']))
        elif section == 'sources':
            with app.state.store.lock:
                row = app.state.store.db.execute("SELECT j.value FROM tasks,json_each(body,'$.source_history') j WHERE tasks.id=? AND json_extract(j.value,'$.id')=?",(task_id,identity)).fetchone()
            if row is None:
                raise HTTPException(404, 'Record not found')
            value = json.loads(row['value'])
        elif section:
            raise HTTPException(404, 'Record not found')

        encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
        def encode(item):
            # Redact a whole original record before slicing its text. A secret
            # split at a display/network boundary must never become visible.
            yield from encoder.iterencode(public(item, context=context))

        def tokens():
            if section:
                yield from encode(value)
                return
            with snapshot_reader(app.state.store, app.state.engine, task_id) as (reader, engine):
                reader.metadata_only = True
                snapshot = engine.snapshot(task_id)  # operations/events are lazy below
                reader.metadata_only = False
                snapshot = project_snapshot(task_id, snapshot, reader)
                yield '{'
                for index, (key, item) in enumerate(snapshot.items()):
                    yield (',' if index else '')+'\n'+json.dumps(key)+': '
                    if key=='knowledge':
                        yield '{'
                        for n,(name,records) in enumerate(reader.knowledge_records(engine).items()):
                            yield (',' if n else '')+json.dumps(name)+':['
                            for count,record in enumerate(records):
                                if count:yield ','
                                yield from encode(record)
                            yield ']'
                        yield '}'
                        continue
                    if key not in ('events','operations'):
                        yield from encode(item)
                        continue
                    yield '['
                    records = reader.iter_events() if key=='events' else reader.iter_operations()
                    for count, record in enumerate(records):
                        if count:
                            yield ','
                        if key=='operations':
                            record=project_snapshot(task_id, {'task':snapshot['task'],'operations':[record],'events':[],'children':[]},reader)['operations'][0]
                        yield from encode(record)
                    yield ']'
                yield '\n}'

        def chunks():
            pending = ''
            for token in tokens():
                for offset in range(0,len(token),16384):
                    pending += token[offset:offset+16384]
                    if len(pending)>=16384:
                        yield pending.encode('utf-8')
                        pending=''
            if pending:
                yield pending.encode('utf-8')
        headers = {'Cache-Control':'no-store'}
        if download:
            headers['Content-Disposition'] = 'attachment; filename="oif-record.json"'
        async def stream_chunks():
            iterator=chunks()
            try:
                while True:
                    pending=asyncio.create_task(asyncio.to_thread(next,iterator,None))
                    try:
                        chunk=await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        await pending
                        raise
                    if chunk is None:
                        break
                    yield chunk
            finally:
                iterator.close()
        return StreamingResponse(stream_chunks(), media_type='application/json', headers=headers)

    @app.post('/api/tasks/{task_id}/instructions')
    async def submit_instruction(task_id: str, payload: InstructionInput):
        public(None)
        current_source_task(task_id, payload.expected_source_hash)
        snapshot = await app.state.engine.submit_instruction(task_id, payload.text, payload.expected_source_hash)
        if snapshot.get('refresh_conversation'):
            snapshot = await task_view(task_id, view='conversation')
        return accepted_response(project_snapshot(task_id, snapshot), action='instruction', task_id=task_id)

    @app.post('/api/tasks/{task_id}/attachments')
    async def submit_attachment(task_id: str, payload: AttachmentInput):
        public(None)
        current_source_task(task_id, payload.expected_source_hash)
        try:
            raw = base64.b64decode(payload.base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, '添付内容のbase64形式が正しくありません。') from None
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, '添付ファイルは1件10 MiB以下にしてください。')
        snapshot = await app.state.engine.submit_attachment(task_id, payload.filename, raw, payload.expected_source_hash)
        if snapshot.get('refresh_conversation'):
            snapshot = await task_view(task_id, view='conversation')
        return accepted_response(project_snapshot(task_id, snapshot), action='attachment', task_id=task_id)

    @app.post('/api/tasks/{task_id}/messages')
    async def submit_message(task_id: str, payload: MessageInput):
        public(None)
        task_or_404(task_id)
        from .attachments import decode_files
        files = decode_files([a.model_dump() for a in payload.attachments])
        method = getattr(app.state.engine, 'submit_message', None)
        if method is None:
            raise HTTPException(409, 'この作業の実行方式は一括添付に対応していません。')
        snapshot = await method(task_id, payload.text, files, payload.expected_source_hash, payload.submission_id)
        if snapshot.get('refresh_conversation'):
            snapshot = await task_view(task_id, view='conversation')
        return accepted_response(project_snapshot(task_id, snapshot), action='message', task_id=task_id)

    @app.post('/api/tasks/{task_id}/stop')
    async def stop_task(task_id: str):
        task_or_404(task_id)
        await _maybe_await(app.state.engine.stop_task(task_id))
        return accepted_response({'task': app.state.store.get_task(task_id)}, action='stop', task_id=task_id)

    @app.post('/api/tasks/{task_id}/resume')
    async def resume_task(task_id: str):
        public(None)
        task_or_404(task_id)
        if inspect.iscoroutinefunction(app.state.engine.resume_task):
            await app.state.engine.resume_task(task_id)
        else:
            app.state.engine.resume_task(task_id)
        return accepted_response({'task': app.state.store.get_task(task_id)}, action='resume', task_id=task_id)

    @app.post('/api/tasks/{task_id}/events/{seq}/explanation')
    async def event_explanation(task_id: str, seq: int, payload: dict):
        task_or_404(task_id)
        if set(payload)-{'language'}:
            raise HTTPException(422, 'Only language is accepted')
        try:
            result=await app.state.explanations.explain(task_id, seq, payload.get('language','ja'), public)
        except KeyError:
            raise HTTPException(404, 'Event not found') from None
        return public(result)

    @app.get('/api/tasks/{task_id}/events')
    async def task_events(request: Request, task_id: str, after: int = 0, view: str = ''):
        task_or_404(task_id)
        public(None)  # Check before stream headers; never emit unscreened events.
        try:
            cursor = max(0, after, int(request.headers.get('last-event-id', '0')))
        except ValueError:
            raise HTTPException(400, 'イベント番号が正しくありません。') from None

        async def stream():
            iterator = app.state.store.subscribe(task_id, after=cursor)
            stopping = asyncio.create_task(app.state.shutdown_event.wait())
            pending = None
            try:
                while True:
                    pending = asyncio.create_task(anext(iterator))
                    done, _ = await asyncio.wait((pending, stopping), return_when=asyncio.FIRST_COMPLETED)
                    if stopping in done:
                        break
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        break
                    safe = public(event)
                    if view == 'conversation':
                        from .ui_records import event_view
                        safe = event_view(safe)
                    yield f"id: {int(event['seq'])}\nevent: stage\ndata: {json.dumps(safe, ensure_ascii=False)}\n\n"
            finally:
                for handle in (pending, stopping):
                    if handle is not None:
                        handle.cancel()
                await asyncio.gather(*[h for h in (pending, stopping) if h is not None], return_exceptions=True)
                await iterator.aclose()

        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/settings')
    async def settings_view():
        return configuration_view()[0]

    def settings_idle():
        if app.state.store.active_task_ids():
            raise HTTPException(409, '作業中の接続を保持しています。作業の終了後に切り替えてください。')

    @app.get('/api/settings/profiles')
    async def provider_profiles():
        return public(app.state.settings.profiles())

    @app.post('/api/settings/profiles')
    async def provider_profile_action(payload: dict):
        try:
            action = payload.get('action')
            if action == 'save' and set(payload) == {'action', 'name'}:
                app.state.settings.save_profile(payload['name'])
            elif action == 'activate' and set(payload) == {'action', 'id'}:
                settings_idle()
                app.state.settings.activate_profile(payload['id'])
            elif action == 'delete' and set(payload) == {'action', 'id'}:
                app.state.settings.delete_profile(payload['id'])
            else:
                raise SettingsError('接続設定の操作を確認してください。')
        except (SettingsError, TypeError):
            raise HTTPException(422, '接続設定を変更できませんでした。名前と選択内容を確認してください。') from None
        return public(app.state.settings.profiles())

    @app.post('/api/settings/login/openrouter')
    async def provider_login(request: Request):
        return app.state.provider_login.begin(str(request.base_url).rstrip('/'))

    @app.get('/oauth/openrouter/{state}')
    async def provider_callback(state: str, code: str = ''):
        import html
        try:
            await app.state.provider_login.finish(state, code)
            message = 'OpenRouterの接続を保存しました。この画面を閉じ、OIFの接続設定を開き直して「OpenRouter」を選び、使うモデルを設定してください。'
            status = 200
        except (SettingsError, ConfigurationRequired) as error:
            message, status = str(error), 400
        return HTMLResponse('<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>OIF 接続設定</title><h1>OIF 接続設定</h1><p>' + html.escape(message) + '</p></html>', status_code=status)

    @app.post('/api/settings')
    async def settings_update(payload: dict):
        # SettingsManager owns validation and encrypted persistence. No update echo.
        try:
            settings_idle()
            app.state.settings.update(payload)
        except (ValueError, TypeError) as exc:
            try:
                detail = public(str(exc))
            except ConfigurationRequired:
                detail = '接続設定を保存できませんでした。入力項目を確認してください。'
            raise HTTPException(422, detail) from None
        return configuration_view()[0]

    @app.get('/api/approvals')
    async def approvals():
        method = getattr(app.state.engine, 'pending_approvals', None)
        return public({'approvals': await _maybe_await(method()) if method else []})

    @app.post('/api/approvals/{approval_id}')
    async def approval(approval_id: str, payload: ApprovalInput):
        public(None)
        method = getattr(app.state.engine, 'resolve_approval', None)
        if method is None:
            raise HTTPException(409, 'この実行環境には処理できる方針変更の申請がありません。')
        result = await _maybe_await(method(approval_id, decision=payload.decision,
                                        expected_hash=payload.expected_hash, reason=payload.reason))
        return accepted_response(result, action='approval', approval_id=approval_id)

    @app.get('/api/tasks/{task_id}/artifacts/{relative_path:path}')
    async def artifact(task_id: str, relative_path: str, operation_id: str | None = None, sha256: str | None = None):
        task = task_or_404(task_id)
        try:
            workspace, resolved = artifact_location(task, relative_path)
        except (ValueError, OSError):
            raise HTTPException(404, '読み出せる成果物が見つかりません。') from None
        if (not operation_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id) or
                not sha256 or not re.fullmatch(r'[a-fA-F0-9]{64}', sha256)):
            raise HTTPException(409, '操作と成果物の版が必要です。操作履歴のリンクから開いてください。')
        try:
            row = app.state.store.get_operation(operation_id)
        except (KeyError, LookupError):
            raise HTTPException(404, '成果物を記録した操作が見つかりません。') from None
        if row['task_id'] != task_id:
            raise HTTPException(404, '成果物を記録した操作が見つかりません。')
        bound = False
        for item in (row.get('result') or {}).get('artifacts', []):
            if not isinstance(item, dict) or str(item.get('sha256', '')).lower() != sha256.lower():
                continue
            value = item.get('path', item.get('relative_path'))
            if not isinstance(value, str):
                continue
            try:
                path = Path(value)
                recorded = artifact_name(task, value)
            except ValueError:
                continue
            if recorded == relative_path:
                bound = True
                break
        if not bound:
            raise HTTPException(409, '成果物の操作・パス・SHA-256が保存結果と一致しません。')

        def verified_snapshot():
            # Hash and serve one private snapshot made from the validated open
            # handle. A later replacement or in-place write cannot change it.
            fd = os.open(resolved, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                os.close(fd)
                raise ValueError('not a plain file')
            try:
                if os.name == 'nt':
                    import ctypes
                    import msvcrt
                    from ctypes import wintypes
                    function = ctypes.WinDLL('kernel32', use_last_error=True).GetFinalPathNameByHandleW
                    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
                    function.restype = wintypes.DWORD
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = function(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
                    if not size or size >= len(buffer):
                        raise OSError('Cannot resolve open artifact')
                    opened = windows_opened_path(buffer.value)
                    Path(opened).resolve().relative_to(workspace.resolve())
                elif Path('/proc/self/fd').is_dir():
                    Path(os.readlink(f'/proc/self/fd/{fd}')).resolve().relative_to(workspace.resolve())
            except (ValueError, OSError):
                os.close(fd)
                raise
            snapshot = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, 'rb') as handle:
                    while data := handle.read(64 * 1024):
                        digest.update(data)
                        snapshot.write(data)
                        size += len(data)
                if digest.hexdigest() != sha256.lower():
                    raise HTTPException(409, 'この操作で記録した版と現在のファイルが異なります。以前の成果物としてダウンロードできません。')
                snapshot.seek(0)
                return snapshot, size
            except BaseException:
                snapshot.close()
                raise

        try:
            handle, size = await asyncio.to_thread(verified_snapshot)
        except (ValueError, OSError):
            raise HTTPException(404, '読み出せる成果物が見つかりません。') from None

        def chunks():
            try:
                while data := handle.read(64 * 1024):
                    yield data
            finally:
                handle.close()
        return StreamingResponse(chunks(), media_type='application/octet-stream',
                                 headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(resolved.name)}",
                                          'Content-Length': str(size), 'X-Artifact-SHA256': sha256.lower(),
                                          'X-Artifact-Operation': operation_id},
                                 background=BackgroundTask(handle.close))

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app
