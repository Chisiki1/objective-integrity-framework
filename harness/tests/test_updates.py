"""Update control fixtures; opt-in real Docker checks use a disposable project."""
from __future__ import annotations

import asyncio
import ast
import base64
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from uuid import uuid4

import pytest
import policy_harness

from policy_harness.capabilities import atomic_json, canonical, digest, process_lock
from policy_harness.models import now
from policy_harness.updates import UpdateManager, UpdateError
from policy_harness import updates


class Policy:
    hash = 'fixed-fixture-policy'
    def verify_current(self):
        pass


@pytest.fixture
def fixture(tmp_path):
    project = tmp_path / 'project'
    (project / 'src/policy_harness').mkdir(parents=True)
    (project / 'src/policy_harness/__init__.py').write_bytes(b'ANSWER = 42\n')
    (project / 'tests').mkdir()
    (project / 'tests/test_baseline.py').write_text('from policy_harness import ANSWER\ndef test_fixed_contract():\n    assert ANSWER == 42\n')
    (project / 'policy').mkdir()
    (project / 'policy/complete-policy-v3.json').write_text('{"fixture":true}')
    actual = Path(__file__).resolve().parents[1]
    shutil.copyfile(actual / 'pyproject.toml', project / 'pyproject.toml')
    # This disposable project has one independently owned fixture contract,
    # not the application's normal-runtime acceptance files.
    config = project / 'pyproject.toml'
    config.write_text(config.read_text(encoding='utf-8').split('[tool.oif.verification]')[0], encoding='utf-8')
    shutil.copyfile(actual / 'uv.lock', project / 'uv.lock')
    manager = UpdateManager(tmp_path / 'data', project, Policy())
    workspace = manager.workspaces / 'test-task'
    workspace.mkdir()
    return manager, workspace, project


def proposal(fixture, content=b'ANSWER = 42\n# reviewed controller update\n', *, target='src/policy_harness/__init__.py'):
    manager, workspace, project = fixture
    (workspace / 'proposal.py').write_bytes(content)
    existing = project / target
    return {'policy_hash': manager.policy.hash, 'rationale': 'Preserve fixed behavior while exercising the exact update route',
        'changes': [{'path': target, 'source': 'proposal.py', 'expected_sha256': digest(existing.read_bytes()) if existing.exists() else None, 'sha256': digest(content)}]}


def stage(fixture, args=None):
    manager, workspace, _ = fixture
    args = args or proposal(fixture)
    description = manager.describe(workspace, args)
    return manager.stage(workspace, {**args, 'expected_candidate_sha256': description['candidate_sha256']})['candidate_id']


class FixtureDocker:
    """A labelled process fixture, never claimed as an observed Docker run."""
    def __init__(self, manager, *, exit_code=0, report=True, cancel=False, transform=None):
        self.manager = manager
        self.exit_code = exit_code
        self.report = report
        self.cancel = cancel
        self.calls = []
        self.info = None
        self.transform = transform

    def response(self):
        # The fixture's expected names come from its protected fixed test source,
        # never from candidate imports. Actual driver integration is tested below.
        ids = []
        baseline = self.manager._baseline()
        for path in sorted((self.manager.baselines / baseline['sha256'] / 'tests').rglob('test_*.py')):
            module = ast.parse(path.read_text(encoding='utf-8'))
            relative = path.relative_to(self.manager.baselines / baseline['sha256']).as_posix()
            ids.extend(relative + '::' + node.name for node in module.body if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'))
        seen = {'schema': 'pytest-observation-v2', 'collection_complete': True, 'session_finished': True,
            'exit_code': 0, 'returned_exit_code': 0, 'collected_node_ids': ids, 'collection_events': [], 'reports': []}
        executed = copy.deepcopy(seen)
        executed['reports'] = [{'node_id': node, 'when': when, 'outcome': 'passed'}
            for node in ids for when in ('setup', 'call', 'teardown')]
        driver = {'schema': 'verifier-driver-v2', 'driver_sha256': digest(updates._VERIFIER_DRIVER),
            'phases': {name: {'exit_code': 0, 'observation': value} for name, value in (('reference', seen), ('candidate', executed))}}
        suite = ET.Element('testsuite', tests=str(len(ids)), failures='0', errors='0', skipped='0')
        for identity in ids:
            case = ET.SubElement(suite, 'testcase', name=identity.split('::')[-1])
            ET.SubElement(ET.SubElement(case, 'properties'), 'property', name='hph_node_id', value=identity)
        if self.transform:
            self.transform(suite, driver)
        suites = ET.Element('testsuites')
        suites.append(suite)
        raw = ET.tostring(suites, encoding='utf-8', xml_declaration=True) if self.report else b''
        return (b'Fixture baseline response, not live evidence\n' + raw + b'\n' +
                updates._DRIVER_MARKER + base64.b64encode(canonical(driver)) + b'\n')

    async def prepare(self, baseline):
        return {'image_id': 'sha256:' + 'a' * 64, 'baseline_sha256': baseline['sha256']}

    async def docker(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ['container', 'create']:
            labels = {}
            for index, value in enumerate(args):
                if value == '--label':
                    key, data = args[index+1].split('=', 1)
                    labels[key] = data
            self.info = {'Id': 'b' * 64, 'Name': '/' + args[args.index('--name')+1], 'Config': {'Labels': labels}, 'State': {'Running': False, 'ExitCode': self.exit_code, 'OOMKilled': False}}
            return 0, self.info['Id'].encode(), b''
        if args[:2] == ['container', 'inspect']:
            return 0, json.dumps([self.info]).encode(), b''
        if args[:2] == ['container', 'start']:
            if self.cancel:
                self.info['State']['Running'] = True
                raise asyncio.CancelledError()
            return self.exit_code, self.response(), b''
        if args[:2] == ['container', 'kill']:
            self.info['State']['Running'] = False
            self.info['State']['ExitCode'] = 137
            return 0, b'', b''
        if args[:2] == ['container', 'cp']:
            if not self.report:
                return 1, b'', b'No report'
            Path(args[-1]).write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="fixture"/></testsuite></testsuites>')
            return 0, b'', b''
        if args[:2] == ['container', 'rm']:
            return 0, b'', b''
        raise AssertionError(args)


def docker_fixture(manager, monkeypatch, **kwargs):
    backend = FixtureDocker(manager, **kwargs)
    monkeypatch.setattr(manager, '_prepare', backend.prepare)
    monkeypatch.setattr(manager, '_docker', backend.docker)
    return backend


def runtime_operation(kind='file_write', *, identity=None, args=None):
    return {'id': identity or uuid4().hex, 'kind': kind, 'args': args or {},
            'purpose': 'Bounded real next-use fixture'}


def runtime_result(operation, *, data=None, status='succeeded', effect='confirmed'):
    return {'operation_id': operation['id'], 'status': status, 'effect': effect,
            'stdout': '', 'stderr': '', 'data': data or {}, 'artifacts': [],
            'started_at': now(), 'finished_at': now(), 'elapsed_seconds': 0.0,
            'exit_code': 0, 'controller_runtime': {}}


def recorded_activation(manager, identity):
    operation = runtime_operation('activate_update', args={'candidate_id': identity})
    manager.before_execution('test-task', operation)
    result = runtime_result(operation, data=manager.activate(identity))
    result['controller_runtime'] = manager.after_actual_result('test-task', operation, result)
    return operation, result


_NEXT_USE_PROCESS = r'''
import asyncio, json, sys, time
from pathlib import Path
from policy_harness.capabilities import digest
from policy_harness.executor import Executor
from policy_harness.models import Operation, OperationResult, now
from policy_harness.updates import UpdateManager
class Policy:
    hash = 'fixed-fixture-policy'
    def verify_current(self): pass
payload = json.loads(sys.stdin.read())
manager = UpdateManager(Path(payload['data']), Path(payload['project']), Policy())
operation = payload['operation']
task_id = payload.get('task_id', 'test-task')
if operation['kind'] in {'history_read', 'knowledge_read'}:
    from policy_harness.store import Store
    from policy_harness.knowledge import Knowledge
    store = Store(manager.data_dir)
    knowledge = Knowledge(store)
manager.before_execution(task_id, operation)
started_at, started = now(), time.monotonic()
target = manager.workspaces / 'test-task' / (operation['id'] + '.txt')
if operation['kind'] in {'file_read', 'file_list'}:
    # Actual executor read results retain effect=none; no output file is created.
    result = asyncio.run(Executor(manager.data_dir).execute(manager.workspaces / 'test-task',
                                                          Operation.model_validate(operation))).model_dump()
    if payload.get('status') != 'succeeded': result['status'] = payload['status']
    if payload.get('effect') != 'none': result['effect'] = payload['effect']
elif operation['kind'] in {'history_read', 'knowledge_read'}:
    if operation['kind'] == 'history_read':
        data = {'operations': [store.get_operation(identity) for identity in operation['args']['operation_ids']]}
        assert all(row['task_id'] == task_id for row in data['operations'])
    else:
        data = knowledge.describe_episode(task_id, operation['args']['episode_id'], operation['args'].get('expected_hash'))
    result = OperationResult(operation_id=operation['id'], status='succeeded', data=data,
                             started_at=started_at, finished_at=now()).model_dump()
    store.close()
else:
    # The normal write fixture performs an actual effect in the new Python process.
    target.write_bytes(b'actual ordinary operation after process restart\n')
    result = {'operation_id':operation['id'], 'status':payload.get('status','succeeded'),
              'effect':payload.get('effect','confirmed'), 'stdout':str(target), 'stderr':'',
              'data':{'sha256':digest(target.read_bytes()), 'bytes':target.stat().st_size},
              'artifacts':[], 'started_at':started_at, 'finished_at':now(),
              'elapsed_seconds':time.monotonic()-started, 'exit_code':0}
result['controller_runtime'] = {'pid':1,'process_id':'caller-forgery','loaded_source_version':'f'*64}
result['controller_runtime'] = manager.after_actual_result(task_id, operation, result)
print(json.dumps({'operation':operation,'result':result,
                  'record':manager.runtime_record(task_id,operation['id']),
                  'identity':manager.runtime_identity()}, ensure_ascii=False))
'''


def next_use_process(manager, project, *, kind='file_write', status='succeeded', effect='confirmed', task_id='test-task', args=None):
    operation = runtime_operation(kind)
    if kind in {'file_read', 'file_list', 'history_read', 'knowledge_read'}:
        from policy_harness.models import Operation
        operation = Operation(kind=kind, args=args or {'path': 'proposal.py' if kind == 'file_read' else '.'},
                              purpose='Read the original source after restart', expected_result='Exact original bytes or directory entries',
                              decisions=[{'id': 'read', 'statement': 'Use existing source', 'rationale': 'No mutation required'}]).model_dump()
    payload = {'data': str(manager.data_dir), 'project': str(project), 'operation': operation,
               'status': status, 'effect': effect, 'task_id': task_id}
    environment = dict(os.environ)
    # Fixed verifier tests live under /baseline, while the reviewed code lives
    # under /candidate/src. Follow the actually imported package in both layouts.
    environment['PYTHONPATH'] = str(Path(policy_harness.__file__).resolve().parent.parent)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    child = subprocess.run([sys.executable, '-B', '-c', _NEXT_USE_PROCESS],
                           input=json.dumps(payload), text=True, encoding='utf-8',
                           capture_output=True, check=False, timeout=30, env=environment)
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


@pytest.mark.parametrize('kind', ['file_read', 'file_list'])
def test_actual_new_process_executor_read_is_use_without_fabricated_modification(fixture, monkeypatch, kind):
    manager, workspace, project = fixture
    identity = stage(fixture)
    docker_fixture(manager, monkeypatch)
    asyncio.run(manager.verify(identity))
    _, activated = recorded_activation(manager, identity)
    before = {p.name: digest(p.read_bytes()) for p in workspace.iterdir() if p.is_file()}
    observed = next_use_process(manager, project, kind=kind, effect='none')
    assert observed['result']['status'] == 'succeeded'
    assert observed['result']['effect'] == 'none'
    assert observed['record']['normal_use']['status'] == 'observed'
    assert observed['identity']['pid'] != activated['controller_runtime']['pid']
    assert {p.name: digest(p.read_bytes()) for p in workspace.iterdir() if p.is_file()} == before


@pytest.mark.parametrize('kind,status,effect', [('file_read', 'unknown', 'none'),
                                             ('file_read', 'failed', 'none'),
                                             ('file_list', 'succeeded', 'unknown')])
def test_read_exception_preserves_unknown_and_failed_observations(fixture, monkeypatch, kind, status, effect):
    manager, _, project = fixture
    identity = stage(fixture)
    docker_fixture(manager, monkeypatch)
    asyncio.run(manager.verify(identity))
    recorded_activation(manager, identity)
    observed = next_use_process(manager, project, kind=kind, status=status, effect=effect)
    assert observed['record']['normal_use']['status'] == 'not-established'


@pytest.mark.parametrize('kind', ['history_read', 'knowledge_read'])
def test_actual_new_process_original_record_read_is_attested(fixture, monkeypatch, kind):
    from policy_harness.executor import Executor
    from policy_harness.knowledge import Knowledge
    from policy_harness.models import Learning, Operation
    from policy_harness.store import Store, digest as record_digest
    manager, _, project = fixture
    store = Store(manager.data_dir)
    knowledge = Knowledge(store)
    task = store.create_task('Fixture source record read', ['Retrieve complete observed source'])
    write = Operation(kind='file_write', args={'path': 'source.txt', 'text': 'Original observed bytes'},
                      purpose='Prepare source fixture', expected_result='Original bytes',
                      decisions=[{'id': 'source', 'statement': 'Prepare exact file source', 'rationale': 'Fixture setup'}])
    actual = asyncio.run(Executor(manager.data_dir).execute(Path(task['workspace']), write)).model_dump()
    assert actual['status'] == 'succeeded'
    store.save_operation(task['id'], write.model_dump())
    store.update_operation(write.id, result=actual, status='post_reviewed')
    learned = Learning(outcome_summary='Observed source fixture write', classifications=['create'],
                       recurrence='unknown', next_use_trigger='Read the original source record', ideas=[], skill_updates=[])
    knowledge.apply(task, write.model_dump(), learned, actual, [])
    episode = next(e for e in store.records('episode') if e['operation_id'] == write.id)
    expected = store.get_operation(write.id) if kind == 'history_read' else knowledge.describe_episode(task, episode['id'])
    args = {'operation_ids': [write.id]} if kind == 'history_read' else {'episode_id': episode['id'], 'expected_hash': record_digest(episode)}
    store.close()
    identity = stage(fixture)
    docker_fixture(manager, monkeypatch)
    asyncio.run(manager.verify(identity))
    _, activated = recorded_activation(manager, identity)
    observed = next_use_process(manager, project, kind=kind, effect='none', task_id=task['id'], args=args)
    data = observed['result']['data']
    assert (data['operations'][0] if kind == 'history_read' else data) == expected
    assert observed['result']['effect'] == 'none'
    assert observed['record']['normal_use']['status'] == 'observed'
    assert observed['record']['task_id'] == task['id']
    assert observed['identity']['pid'] != activated['controller_runtime']['pid']


def test_runtime_start_result_are_exact_and_cannot_be_replayed(fixture):
    manager, workspace, _ = fixture
    operation = runtime_operation()
    before = manager.before_execution('test-task', operation)
    assert before['pid'] == os.getpid()
    assert manager.runtime_record('test-task', operation['id'])['status'] == 'started'
    with pytest.raises(UpdateError, match='ALREADY_STARTED'):
        manager.before_execution('test-task', operation)
    (workspace / 'normal.txt').write_bytes(b'observed write')
    result = runtime_result(operation, data={'sha256': digest((workspace / 'normal.txt').read_bytes()),
                                            'controller_runtime': {'process_id': 'forged'}})
    result['controller_runtime'] = {'process_id': 'forged'}
    runtime = manager.after_actual_result('test-task', operation, result)
    assert runtime['process_id'] == before['process_id'] != 'forged'
    assert runtime['normal_use'] == 'not-established'  # No activated update yet.
    assert runtime['result_sha256'] == digest(canonical({key: value for key, value in result.items() if key != 'controller_runtime'}))
    assert manager.after_actual_result('test-task', operation, result) == runtime
    with pytest.raises(UpdateError, match='RESULT_CHANGED'):
        manager.after_actual_result('test-task', operation, {**result, 'stdout': 'changed'})
    other = runtime_operation()
    with pytest.raises(UpdateError, match='START_OR_RESULT_MISMATCH'):
        manager.after_actual_result('test-task', other, runtime_result(other))
    path = manager.runtime_operations / (before['record_id'] + '.json')
    damaged = json.loads(path.read_text())
    damaged['result_sha256'] = '0' * 64
    atomic_json(path, damaged)
    with pytest.raises(UpdateError, match='IDENTITY_CHANGED'):
        manager.runtime_record('test-task', operation['id'])


@pytest.mark.asyncio
async def test_manager_reconstruction_and_disk_readback_are_not_restart_use(fixture, monkeypatch):
    manager, workspace, project = fixture
    docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    await manager.verify(identity)
    old_runtime = manager.runtime_identity()
    _, activated = recorded_activation(manager, identity)
    stamp = activated['controller_runtime']
    assert stamp['loaded_source_version'] == old_runtime['loaded_source_version']
    assert stamp['expected_source_version'] == activated['data']['after_version']
    assert stamp['activation_candidate_id'] == stamp['candidate_sha256'] == identity
    assert stamp['normal_use'] == 'not-established'
    assert manager.runtime_identity() == old_runtime  # Later disk bytes do not relabel loaded code.
    with pytest.raises(UpdateError, match='LOADED_SOURCE_DIFFERS'):
        manager.before_execution('test-task', runtime_operation())
    rebuilt = UpdateManager(manager.data_dir, project, Policy())
    assert rebuilt.runtime_identity()['loaded_source_version'] == stamp['expected_source_version']
    assert rebuilt.runtime_identity()['process_id'] == old_runtime['process_id']
    operation = runtime_operation()
    rebuilt.before_execution('test-task', operation)
    (workspace / 'same-process.txt').write_bytes(b'actual same-process effect')
    result = runtime_result(operation)
    result['controller_runtime'] = rebuilt.after_actual_result('test-task', operation, result)
    assert result['controller_runtime']['normal_use'] == 'not-established'
    assert rebuilt.status()['normal_use']['count'] == 0


@pytest.mark.asyncio
async def test_new_process_normal_effect_is_bound_and_review_is_not_measured_benefit(fixture, monkeypatch):
    manager, _, project = fixture
    docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    await manager.verify(identity)
    _, activated = recorded_activation(manager, identity)
    actual = next_use_process(manager, project)
    runtime = actual['result']['controller_runtime']
    assert runtime['pid'] != os.getpid()
    assert runtime['process_id'] != manager.runtime_identity()['process_id']
    assert runtime['loaded_source_version'] == activated['controller_runtime']['expected_source_version']
    assert runtime['activation_candidate_id'] == runtime['candidate_sha256'] == identity
    assert runtime['normal_use'] == 'observed'
    stored = manager.runtime_record('test-task', actual['operation']['id'])
    assert stored == actual['record']
    assert stored['operation_sha256'] == digest(canonical(actual['operation']))
    state = manager.status()
    assert state['normal_use']['count'] == 1
    assert state['normal_use']['first']['operation_id'] == actual['operation']['id']
    assert state['normal_use']['empirical_benefit'] == 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'
    assert 'benefit-unverified' in state['effect_measurement']
    # Trusted Engine calls this only after its actual Knowledge post-review.
    resolution = runtime_operation('knowledge_update', args={'decisions': [
        {'id': 'idea-next-use', 'disposition': 'verified', 'candidate_sha256': identity,
         'actual_use_operation_ids': [actual['operation']['id']], 'effect': 'caller says benefit proved'}]})
    manager.before_execution('test-task', resolution)
    resolved = runtime_result(resolution, data={'resolved_idea_ids': ['idea-next-use']})
    resolved['controller_runtime'] = manager.after_actual_result('test-task', resolution, resolved)
    review = manager.consume_use_review('test-task', resolution, resolved)
    assert review['observations'][0]['use_record_sha256'] == stored['record_sha256']
    assert review['empirical_benefit'] == 'UNVERIFIED_UNTIL_MEASURED_COMPARISON'
    assert manager.consume_use_review('test-task', resolution, resolved) == review
    with pytest.raises(UpdateError, match='ACTUAL_RESULT_REQUIRED'):
        manager.consume_use_review('test-task', resolution, {**resolved, 'stdout': 'forged actual result'})
    assert len(manager.status()['normal_use']['reviewed_resolutions']) == 1
    # A rollback keeps prior use evidence but does not certify the restored cut.
    manager.rollback(identity)
    rolled = manager.status()['normal_use']
    assert rolled['count'] == 0 and len(rolled['prior_activation_observations']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,status,effect', [
    ('file_write', 'unknown', 'unknown'), ('file_write', 'failed', 'none'),
    ('verify_update', 'succeeded', 'confirmed'), ('knowledge_update', 'succeeded', 'confirmed'),
    ('finish', 'succeeded', 'confirmed')])
async def test_new_process_unknown_or_bookkeeping_cannot_establish_normal_use(fixture, monkeypatch, kind, status, effect):
    manager, _, project = fixture
    docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    await manager.verify(identity)
    recorded_activation(manager, identity)
    actual = next_use_process(manager, project, kind=kind, status=status, effect=effect)
    assert actual['result']['controller_runtime']['normal_use'] == 'not-established'
    assert manager.status()['normal_use']['count'] == 0
    if effect == 'unknown':
        assert manager.status()['normal_use']['pending'][0]['operation_id'] == actual['operation']['id']


def test_description_has_exact_old_new_bytes_and_review_binding(fixture):
    manager, workspace, project = fixture
    args = proposal(fixture)
    description = manager.describe(workspace, args)
    assert description['changes'][0]['old']['text'] == 'ANSWER = 42\n'
    assert description['changes'][0]['new']['text'].endswith('# reviewed controller update\n')
    assert description['source_conditions'] == ['R15', 'N-06']
    with pytest.raises(UpdateError, match='DESCRIPTION_HASH_REQUIRED'):
        manager.stage(workspace, args)
    (workspace / 'proposal.py').write_text('ANSWER = 1')
    with pytest.raises(UpdateError, match='SOURCE_CHANGED'):
        manager.stage(workspace, {**args, 'expected_candidate_sha256': description['candidate_sha256']})
    assert (project / 'src/policy_harness/__init__.py').read_text() == 'ANSWER = 42\n'


@pytest.mark.parametrize('target', ['policy/complete-policy-v3.json', 'tests/test_baseline.py', '.updates/verification.json', '../escape.py', 'src/policy_harness/../evil.py', 'src/Policy_Harness/a.py', 'src/policy_harness/a.pth', 'src/policy_harness/.secret.py'])
def test_update_target_cannot_change_policy_baseline_or_receipts(fixture, target):
    manager, workspace, _ = fixture
    args = proposal(fixture, target=target)
    with pytest.raises(Exception):
        manager.describe(workspace, args)


def test_arg_bypass_and_foreign_workspace_are_rejected(fixture, tmp_path):
    manager, workspace, _ = fixture
    args = proposal(fixture)
    for name in ('approved', 'verification', 'argv', 'tests', 'mounts', 'environment'):
        with pytest.raises(UpdateError, match='UNSUPPORTED'):
            manager.describe(workspace, {**args, name: True})
    candidate_id = stage(fixture, args)
    with pytest.raises(UpdateError, match='WORKSPACE_NOT_OWNED'):
        manager.describe(tmp_path, {'candidate_id': candidate_id})
    other = manager.workspaces / 'other'
    other.mkdir()
    with pytest.raises(UpdateError, match='WORKSPACE_MISMATCH'):
        manager.describe(other, {'candidate_id': candidate_id})


def test_staged_bytes_and_fixed_baseline_are_immutable_inputs(fixture):
    manager, workspace, project = fixture
    identity = stage(fixture)
    (manager.candidates / identity / 'tree/src/policy_harness/__init__.py').write_text('forged')
    with pytest.raises(UpdateError, match='CANDIDATE_BYTES_CHANGED'):
        manager.describe(workspace, {'candidate_id': identity})
    (project / 'tests/test_baseline.py').write_text('def test_weakened(): pass')
    with pytest.raises(UpdateError, match='TRUSTED_BASELINE_CHANGED'):
        manager.describe(workspace, proposal(fixture))


@pytest.mark.asyncio
async def test_fixture_verify_activate_exact_cas_and_rollback(fixture, monkeypatch):
    manager, workspace, project = fixture
    backend = docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    with pytest.raises(UpdateError, match='VERIFICATION_REQUIRED'):
        manager.activate(identity)
    result = await manager.verify(identity)
    assert result['status'] == 'succeeded' and result['exit_code'] == 0
    args = next(args for args in backend.calls if args[:2] == ['container', 'create'])
    assert args[args.index('--network')+1] == 'none'
    assert args[args.index('--user')+1] == '65534:65534'
    mounts = [args[i+1] for i, value in enumerate(args) if value == '--mount']
    assert len(mounts) == 2
    assert any(',dst=/candidate/src,readonly' in value for value in mounts)
    assert any(',dst=/reference/src,readonly' in value for value in mounts)
    assert '/baseline/tests' in args and '/hph-verifier.py' in args and '--reference-src' in args
    assert args[args.index('--memory')+1] == str(4 * 1024**3)
    assert args[args.index('--tmpfs')+1].endswith('size=' + str(2 * 1024**3))
    assert result['resource_envelope'] == manager.resource_envelope()
    assert result['report_validation']['expected_node_ids'] == ['tests/test_baseline.py::test_fixed_contract']
    assert not any('docker.sock' in arg or 'provider-settings' in arg for arg in args)
    activation = manager.activate(identity)
    assert activation['restart_required'] and activation['effect_measurement'] == 'pending-next-use'
    assert '# reviewed controller update' in (project / 'src/policy_harness/__init__.py').read_text()
    attested = manager.restart_attestation(identity)
    assert attested['source_version'] == activation['after_version']
    assert attested['activation_status'] == 'activated'
    assert manager.activate(identity)['reused']
    rolled = manager.rollback(identity)
    assert rolled['status'] == 'rolled_back' and rolled['restart_required']
    assert (project / 'src/policy_harness/__init__.py').read_text() == 'ANSWER = 42\n'
    assert manager.restart_attestation(identity)['activation_status'] == 'rolled_back'


@pytest.mark.asyncio
async def test_missing_report_or_nonzero_exit_cannot_activate(fixture, monkeypatch):
    manager, _, _ = fixture
    docker_fixture(manager, monkeypatch, report=False)
    identity = stage(fixture)
    result = await manager.verify(identity)
    assert result['status'] == 'failed'
    assert result['first_fault']['reason'].startswith('UPDATE_TEST_REPORT_MISSING')
    with pytest.raises(UpdateError, match='VERIFICATION_REQUIRED'):
        manager.activate(identity)
    backend = docker_fixture(manager, monkeypatch, exit_code=2)
    result2 = await manager.verify(identity)
    assert result2['status'] == 'failed' and result2['exit_code'] == 2
    assert result2['previous_run_id'] == result['run_id']
    assert (manager.runs / (result['run_id'] + '.json')).exists()


@pytest.mark.asyncio
async def test_concurrent_source_change_prevents_activation_and_foreign_bytes_prevent_rollback(fixture, monkeypatch):
    manager, _, project = fixture
    docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    await manager.verify(identity)
    source = project / 'src/policy_harness/__init__.py'
    original = source.read_bytes()
    source.write_bytes(b'foreign change')
    with pytest.raises(UpdateError, match='SOURCE_CAS_CONFLICT'):
        manager.activate(identity)
    source.write_bytes(original)
    manager.activate(identity)
    source.write_bytes(b'foreign change after activation')
    with pytest.raises(UpdateError, match='ROLLBACK_CAS_CONFLICT'):
        manager.rollback(identity)
    assert source.read_bytes() == b'foreign change after activation'


@pytest.mark.asyncio
async def test_unknown_and_cancelled_verification_are_not_replayed_or_passed(fixture, monkeypatch):
    manager, _, _ = fixture
    backend = docker_fixture(manager, monkeypatch, cancel=True)
    identity = stage(fixture)
    result = await manager.verify(identity)
    assert result['status'] == 'failed' and result['exit_code'] == 137
    assert result['first_fault']['type'] == 'CancelledError'
    assert any(call[:2] == ['container', 'kill'] for call in backend.calls)
    result.update(status='unknown', effect='unknown')
    manager._save_verification(result)
    calls = len(backend.calls)
    again = await manager.verify(identity)
    assert again['reconciliation_required'] and len(backend.calls) == calls


@pytest.mark.asyncio
async def test_foreign_container_is_not_stopped_or_removed(fixture, monkeypatch):
    manager, _, _ = fixture
    backend = docker_fixture(manager, monkeypatch)
    row = {'run_id': 'a'*32, 'candidate_id': 'b'*64, 'container_name': 'our-name', 'container_id': 'c'*64}
    backend.info = {'Id': 'c'*64, 'Name': '/our-name', 'Config': {'Labels': {'policy-harness.update-owner': 'foreign'}}, 'State': {'Running': True}}
    with pytest.raises(UpdateError, match='OWNERSHIP_MISMATCH'):
        await manager._stop(row)
    assert not any(call[:2] in (['container','kill'], ['container','rm']) for call in backend.calls)


@pytest.mark.asyncio
async def test_verification_artifact_tamper_is_not_an_activation_permission(fixture, monkeypatch):
    manager, _, _ = fixture
    docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    result = await manager.verify(identity)
    Path(result['test_report']['path']).write_text('changed')
    with pytest.raises(UpdateError, match='ARTIFACT_CHANGED'):
        manager.activate(identity)


@pytest.mark.asyncio
async def test_partial_multifile_activation_recovers_exact_preimages(fixture, monkeypatch):
    manager, workspace, project = fixture
    docker_fixture(manager, monkeypatch)
    args = proposal(fixture)
    (workspace / 'added.py').write_text('ADDED = True\n')
    args['changes'].append({'path':'src/policy_harness/added.py','source':'added.py','expected_sha256':None,'sha256':digest((workspace/'added.py').read_bytes())})
    identity = stage(fixture, args)
    await manager.verify(identity)
    def partial(candidate, **kwargs):
        (project/'src/policy_harness/__init__.py').write_bytes(b'ANSWER = 42\n# reviewed controller update\n')
        raise OSError('injected interrupted second file')
    monkeypatch.setattr(manager, '_apply_files', partial)
    with pytest.raises(UpdateError, match='ACTIVATION_INTERRUPTED'):
        manager.activate(identity)
    assert manager._activation(identity)['effect'] == 'unknown'
    rolled = manager.rollback(identity)
    assert rolled['status'] == 'rolled_back'
    assert (project/'src/policy_harness/__init__.py').read_text() == 'ANSWER = 42\n'
    assert not (project/'src/policy_harness/added.py').exists()


def test_os_lease_does_not_poll_or_admit_a_second_writer(fixture):
    manager, _, _ = fixture
    with process_lock(manager.control/'update.lock'):
        with pytest.raises(Exception, match='BUSY'):
            stage(fixture)


@pytest.mark.asyncio
async def test_baseline_epoch_preserves_history_and_invalidates_old_candidates(fixture, monkeypatch):
    manager, workspace, project = fixture
    docker_fixture(manager, monkeypatch)
    old_id = stage(fixture)
    before = manager._baseline()
    (project/'tests/test_added.py').write_bytes(b'def test_additional_contract():\n    assert True\n')
    with pytest.raises(UpdateError, match='BASELINE_CHANGED'):
        await manager.verify(old_id)
    after = manager.enroll_baseline_epoch(reason='Trusted test owner froze an additional baseline check')
    assert after['previous_epoch_id'] == before['epoch_id']
    assert (manager.baselines/before['sha256']/'tests/test_baseline.py').exists()
    assert (manager.baselines/('epoch-'+before['epoch_id']+'.json')).exists()
    with pytest.raises(UpdateError, match='STALE'):
        await manager.verify(old_id)
    new_id = stage(fixture)
    assert new_id != old_id
    assert (await manager.verify(new_id))['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_reconcile_stage_and_partial_activation_never_reexecutes(fixture, monkeypatch):
    manager, workspace, project = fixture
    backend = docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    staged = await manager.reconcile(identity)
    assert staged['candidate_state'] == 'complete'
    assert staged['stage']['candidate_id'] == identity
    assert staged['verification'] is None and staged['original_operation_success'] == 'not-inferred'
    assert not backend.calls
    await manager.verify(identity)
    manager.activate(identity)
    observation = await manager.reconcile(identity)
    assert observation['activation']['status'] == 'activated'
    assert observation['current_source_state'] == 'after'
    assert observation['replayed'] is False
    assert (await manager.reconcile('0'*64))['candidate_state'] == 'absent'


@pytest.mark.parametrize('override', [
    {'memory_bytes': 0}, {'tmpfs_bytes': True}, {'pids_limit': -1}, {'cpus': float('nan')},
    {'verification_timeout': float('inf')}, {'tmpfs_bytes': 2048, 'memory_bytes': 1024}])
def test_trusted_resource_envelope_rejects_invalid_limits(fixture, override):
    manager, _, project = fixture
    with pytest.raises(UpdateError, match='RESOURCE_INVALID|TMPFS_EXCEEDS'):
        UpdateManager(manager.data_dir, project, manager.policy, **override)


def test_trusted_resource_envelope_explicit_override(fixture):
    manager, _, project = fixture
    configured = UpdateManager(manager.data_dir, project, manager.policy,
        memory_bytes=3 * 1024**3, tmpfs_bytes=1024**3, cpus=1.5, pids_limit=100, verification_timeout=1200)
    assert configured.resource_envelope()['memory_bytes'] == 3 * 1024**3
    assert configured.resource_envelope()['tmpfs_bytes'] == 1024**3
    assert configured.resource_envelope()['verification_timeout_seconds'] == 1200
    assert configured.resource_envelope()['cpus'] == 1.5


@pytest.mark.asyncio
async def test_failed_process_retains_complete_junit_collection_and_first_fault(fixture, monkeypatch):
    manager, _, _ = fixture
    def failure(suite, driver):
        suite.set('failures', '1')
        ET.SubElement(suite.find('testcase'), 'failure', message='first actual assertion').text = 'full source trace remains here'
        driver['phases']['candidate']['exit_code'] = 1
        observation = driver['phases']['candidate']['observation']
        observation.update(exit_code=1, returned_exit_code=1)
        observation['reports'][1].update(outcome='failed', detail='first actual assertion')
        observation['collection_events'].append({'node_id': 'source', 'outcome': 'failed', 'detail': 'collection trace'})
    docker_fixture(manager, monkeypatch, exit_code=1, transform=failure)
    identity = stage(fixture)
    row = await manager.verify(identity)
    assert row['status'] == 'failed' and row['capture_complete']
    assert row['first_fault']['reason'] == 'UPDATE_BASELINE_PROCESS_FAILED'
    assert row['test_report']['failures'] == 1
    assert 'full source trace' in row['test_report']['cases'][0]['detail']
    assert row['driver_report']['observation']['phases']['candidate']['observation']['collection_events'][0]['detail'] == 'collection trace'
    assert Path(row['test_report']['path']).read_bytes() in Path(row['streams'][0]['path']).read_bytes()
    assert not row['report_validation']['valid']
    with pytest.raises(UpdateError, match='VERIFICATION_REQUIRED'):
        manager.activate(identity)


@pytest.mark.parametrize('damage', ['omitted', 'extra', 'duplicate', 'counts', 'skip_reason',
    'unfinished', 'wrong_driver', 'missing_call', 'collection_changed', 'collection_error'])
@pytest.mark.asyncio
async def test_incomplete_or_mismatched_reports_cannot_activate(fixture, monkeypatch, damage):
    manager, _, _ = fixture
    def change(suite, driver):
        observation = driver['phases']['candidate']['observation']
        case = suite.find('testcase')
        if damage == 'omitted':
            suite.remove(case); suite.set('tests', '0')
        elif damage in {'extra', 'duplicate'}:
            added = copy.deepcopy(case)
            if damage == 'extra':
                added.find('./properties/property').set('value', 'tests/other.py::test_other')
            suite.append(added); suite.set('tests', '2')
        elif damage == 'counts':
            suite.set('tests', '999')
        elif damage == 'skip_reason':
            suite.set('skipped', '1'); ET.SubElement(case, 'skipped')
        elif damage == 'unfinished':
            observation['session_finished'] = False
        elif damage == 'wrong_driver':
            driver['driver_sha256'] = 'f' * 64
        elif damage == 'missing_call':
            observation['reports'] = [r for r in observation['reports'] if r['when'] != 'call']
        elif damage == 'collection_changed':
            observation['collected_node_ids'].append('tests/extra.py::test_extra')
        elif damage == 'collection_error':
            observation['collection_events'] = [{'node_id': 'module', 'outcome': 'failed', 'detail': 'original cause'}]
    docker_fixture(manager, monkeypatch, transform=change)
    identity = stage(fixture)
    row = await manager.verify(identity)
    assert row['status'] == 'failed' and not row['report_validation']['valid']
    assert row['test_report'] and row['driver_report']
    with pytest.raises(UpdateError, match='VERIFICATION_REQUIRED'):
        manager.activate(identity)


@pytest.mark.asyncio
async def test_successful_report_readback_rejects_changed_driver_evidence(fixture, monkeypatch):
    manager, _, _ = fixture
    docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    row = await manager.verify(identity)
    assert row['status'] == 'succeeded'
    Path(row['driver_report']['path']).write_text('{}')
    with pytest.raises(UpdateError, match='ARTIFACT_CHANGED'):
        manager.activate(identity)


@pytest.mark.asyncio
async def test_required_case_set_preserves_explicit_skip_alongside_normal_pass(fixture, monkeypatch):
    manager, _, project = fixture
    (project / 'tests/test_baseline.py').write_text(
        'from policy_harness import ANSWER\n'
        'def test_fixed_contract(): assert ANSWER == 42\n'
        'def test_requires_optional_platform(): pass\n')
    def skip(suite, driver):
        case = suite.findall('testcase')[1]
        identity = case.find('./properties/property').get('value')
        suite.set('skipped', '1')
        ET.SubElement(case, 'skipped', message='Optional platform unavailable').text = 'explicit platform boundary'
        reports = driver['phases']['candidate']['observation']['reports']
        reports[:] = [r for r in reports if r['node_id'] != identity or r['when'] != 'call']
        for item in reports:
            if item['node_id'] == identity and item['when'] == 'setup':
                item.update(outcome='skipped', detail='Optional platform unavailable')
    docker_fixture(manager, monkeypatch, transform=skip)
    identity = stage(fixture)
    row = await manager.verify(identity)
    assert row['status'] == 'succeeded' and row['test_report']['tests'] == 2 and row['test_report']['skipped'] == 1
    assert len(row['report_validation']['expected_node_ids']) == 2
    assert manager.activate(identity)['status'] == 'activated'


@pytest.mark.asyncio
async def test_recovery_attestation_exact_readonly_and_stale_rejected(fixture, monkeypatch):
    manager, _, _ = fixture
    docker_fixture(manager, monkeypatch)
    identity = stage(fixture)
    await manager.verify(identity)
    manager.activate(identity)
    expected = manager.restart_attestation(identity)
    before = {p: digest(p.read_bytes()) for p in manager.control.rglob('*') if p.is_file()}
    assert manager.validate_recovery_attestation(identity, expected) == expected
    assert {p: digest(p.read_bytes()) for p in manager.control.rglob('*') if p.is_file()} == before
    for changed in ({**expected, 'source_version': '0' * 64}, {**expected, 'extra': True}):
        with pytest.raises(UpdateError, match='RECOVERY_ATTESTATION_CHANGED'):
            manager.validate_recovery_attestation(identity, changed)
    manager.rollback(identity)
    with pytest.raises(UpdateError, match='RECOVERY_ATTESTATION_CHANGED'):
        manager.validate_recovery_attestation(identity, expected)


@pytest.mark.parametrize('answer,expected_code', [(42, 0), (41, 1)])
def test_actual_trusted_driver_collects_before_then_runs_benign_fixture(fixture, tmp_path, answer, expected_code):
    """Ordinary authored native fixture, not host execution of an agent proposal."""
    manager, _, project = fixture
    from policy_harness import verification_driver
    candidate = tmp_path / 'benign-fixed-fixture/src/policy_harness'
    candidate.mkdir(parents=True)
    candidate.joinpath('__init__.py').write_text(f'ANSWER = {answer}\n')
    completed = subprocess.run([sys.executable, '-B', str(Path(verification_driver.__file__)),
        '--reference-src', str(project / 'src'), '--candidate-src', str(candidate.parent),
        '--tests', str(project / 'tests'), '--config', str(project / 'pyproject.toml')],
        cwd=tmp_path, capture_output=True, timeout=30)
    assert completed.returncode == expected_code, completed.stderr.decode(errors='replace')
    row = {'run_id': uuid4().hex}
    manager._capture(row, completed.stdout, completed.stderr)
    manager._capture_reports(row, completed.stdout)
    assert row['report_capture_errors'] == []
    assert row['driver_report']['observation']['phases']['reference']['observation']['reports'] == []
    assert row['report_validation']['valid'] is (expected_code == 0)
    assert row['test_report']['tests'] == 1
    if expected_code:
        assert row['test_report']['failures'] == 1 and '41 == 42' in row['test_report']['cases'][0]['detail']


@pytest.mark.asyncio
async def test_fixed_image_recipe_contains_offline_tokenizer_and_own_driver(fixture, monkeypatch):
    import io
    import tarfile
    manager, _, _ = fixture
    captured = {}
    baseline = manager._baseline()
    async def image(ref):
        return {'Id': 'sha256:' + 'a' * 64, 'Os': 'linux',
            'RepoDigests': ['python@sha256:' + 'b' * 64],
            'Config': {'Labels': {'policy-harness.update-baseline': baseline['sha256']}}}
    async def process(*args, **kwargs):
        return 0, b'pytest==9.0.2 --hash=sha256:fixture\n', b''
    async def docker(args, **kwargs):
        captured['argv'] = args
        with tarfile.open(fileobj=io.BytesIO(kwargs['input_bytes'])) as archive:
            captured['inputs'] = {item.name: archive.extractfile(item).read() for item in archive.getmembers()}
        return 0, b'fixture trusted image build', b''
    monkeypatch.setattr(manager, '_image', image)
    monkeypatch.setattr(manager, '_process', process)
    monkeypatch.setattr(manager, '_docker', docker)
    prepared = await manager._prepare(baseline)
    files = captured['inputs']
    assert files['verifier.py'] == updates._VERIFIER_DRIVER
    assert updates._TOKENIZER_SHA256.encode() in files['tokenizer_setup.py']
    assert b'tiktoken.load.read_file = no_download' in files['tokenizer_setup.py']
    assert b'TIKTOKEN_CACHE_DIR=/opt/hph-tiktoken' in files['Dockerfile']
    assert b'COPY baseline/ /reference/' in files['Dockerfile']
    assert b'AS hph_node' in files['Dockerfile']
    assert b'COPY --from=hph_node /usr/local/bin/node' in files['Dockerfile']
    assert '@sha256:' in prepared['node_digest']
    assert prepared['tokenizer']['offline_checked_during_build']
    previous = prepared['recipe_sha256']
    monkeypatch.setattr(updates, '_VERIFIER_DRIVER', updates._VERIFIER_DRIVER + b'\n# trusted driver revision\n')
    revised = await manager._prepare(baseline)
    assert revised['recipe_sha256'] != previous
    assert revised['driver_sha256'] == digest(updates._VERIFIER_DRIVER)


@pytest.mark.asyncio
async def test_real_docker_whole_candidate_update_failure_and_rollback(fixture):
    if os.getenv('HARNESS_RUN_UPDATE_DOCKER_TESTS') != '1':
        pytest.skip('Explicit real Docker opt-in required; fixtures are not Docker evidence')
    manager, workspace, project = fixture
    tests = project/'tests/test_baseline.py'
    tests.write_text('''import os, pathlib, socket
from policy_harness import ANSWER
def test_fixed_contract():
    assert ANSWER == 42
def test_isolation():
    assert os.geteuid() == 65534
    assert not pathlib.Path('/var/run/docker.sock').exists()
    assert not pathlib.Path('/candidate/.runtime/provider-settings.json').exists()
    assert pathlib.Path('/candidate/policy/complete-policy-v3.json').is_file()
    assert pathlib.Path('/candidate/pyproject.toml').is_file()
    assert pathlib.Path('/candidate/uv.lock').is_file()
    assert pathlib.Path('/candidate/tests/test_baseline.py').is_file()
    assert not any('API_KEY' in k.upper() for k in os.environ)
    s=socket.socket(); s.settimeout(0.2)
    assert s.connect_ex(('1.1.1.1',443)) != 0
    s.close()
''')
    identity = stage(fixture)
    result = await manager.verify(identity)
    assert result['status'] == 'succeeded', result
    assert result['test_report']['tests'] == 2
    activated = manager.activate(identity)
    assert activated['status'] == 'activated' and activated['restart_required']
    assert manager.rollback(identity)['status'] == 'rolled_back'
    failed_id = stage(fixture, proposal(fixture, b'ANSWER = 41\n'))
    failed = await manager.verify(failed_id)
    assert failed['status'] == 'failed' and failed['exit_code'] != 0
    with pytest.raises(UpdateError):
        manager.activate(failed_id)
    missing_id = stage(fixture, proposal(fixture, b'import os\nos._exit(0)\n'))
    missing = await manager.verify(missing_id)
    assert missing['status'] == 'failed'
    assert 'REPORT_MISSING' in missing['first_fault']['reason']
