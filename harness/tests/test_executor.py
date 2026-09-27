"""Filesystem/recipe fixtures and explicitly opted-in real Docker boundaries."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from policy_harness.capabilities import atomic_json, canonical, digest, process_lock
from policy_harness.executor import Executor
from policy_harness.models import Operation, PolicyError


def operation(kind, args=None, **kwargs):
    return Operation(kind=kind, args=args or {}, purpose='Bounded executor test',
        decisions=[{'id': 'test-decision', 'statement': 'Exercise the declared boundary',
                    'rationale': 'The test directly observes this condition'}],
        expected_result='The asserted observed result', **kwargs)


def recipe(source: bytes, *, timeout=10, output_bytes=65536, entrypoint='program.py'):
    return {'name': 'test-program', 'recipe': 'python', 'entrypoint': entrypoint,
        'files': {entrypoint: digest(source)}, 'arguments': [],
        'declared_effects': ['Writes only declared test output inside its task workspace'],
        'resources': {'timeout_seconds': timeout, 'cpus': 1, 'memory_mb': 128,
                      'pids_limit': 32, 'output_bytes': output_bytes, 'tmp_mb': 16,
                      'rationale': 'A tiny standard-library program with bounded output'}}


@pytest.fixture
def sandbox(tmp_path):
    executor = Executor(tmp_path / 'data')
    workspace = executor.workspaces / 'test-task'
    workspace.mkdir()
    atomic_json(executor.control / 'environment.json',
                {'image_id': 'sha256:' + 'a' * 64, 'pytest': True})
    return executor, workspace


@pytest.mark.asyncio
async def test_atomic_write_read_and_cas(sandbox):
    executor, workspace = sandbox
    first = await executor.execute(workspace, operation('file_write',
        {'path': 'sub/a.txt', 'text': 'あいうabc', 'expected_sha256': None}))
    assert first.status == 'succeeded' and first.effect == 'confirmed'
    assert first.data['text_facts']['character_count'] == 6
    assert first.data['text_facts']['byte_count'] == 12
    assert first.data['text_facts']['scope'] == 'whole_file'
    assert first.data['text_facts']['has_newline'] is False
    rejected = await executor.execute(workspace, operation('file_write',
        {'path': 'sub/a.txt', 'text': 'wrong', 'expected_sha256': '0' * 64}))
    assert rejected.status == 'failed' and rejected.effect == 'none'
    assert (workspace / 'sub/a.txt').read_text(encoding='utf-8') == 'あいうabc'
    read = await executor.execute(workspace, operation('file_read',
        {'path': 'sub/a.txt', 'max_bytes': 3}))
    assert read.stdout == 'あ'
    assert read.data['truncated'] and read.data['returned_bytes'] == 3
    assert read.data['sha256'] == first.data['sha256']
    assert read.data['text_facts']['scope'] == 'returned_range'
    assert read.data['text_facts']['character_count'] == 1


@pytest.mark.asyncio
async def test_readback_text_facts_preserve_newlines_and_invalid_utf8(sandbox):
    executor,workspace=sandbox
    (workspace/'lines.txt').write_bytes('あ\r\nい\n'.encode())
    read=await executor.execute(workspace,operation('file_read',{'path':'lines.txt'}))
    facts=read.data['text_facts']
    assert facts['scope']=='whole_file' and facts['character_count']==5
    assert facts['newline_count']==2 and facts['ends_with_newline']
    (workspace/'binary.txt').write_bytes(b'\xff\xfe')
    read=await executor.execute(workspace,operation('file_read',{'path':'binary.txt'}))
    assert read.data['text_facts']=={'scope':'whole_file','byte_count':2,'valid_utf8':False}


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['../outside', '/etc/passwd', 'C:/Windows/a',
                                  'a/../../b', 'a:secret', 'NUL', 'a./b', 'a//b'])
async def test_path_rejection_never_writes_outside(sandbox, path):
    executor, workspace = sandbox
    result = await executor.execute(workspace, operation('file_write', {'path': path, 'text': 'bad'}))
    assert result.status == 'failed' and result.effect == 'none'
    assert list(workspace.iterdir()) == []


@pytest.mark.asyncio
async def test_workspace_itself_must_be_owned(sandbox, tmp_path):
    executor, _ = sandbox
    result = await executor.execute(tmp_path, operation('file_write', {'path': 'escape.txt', 'text': 'bad'}))
    assert result.status == 'failed'
    assert not (tmp_path / 'escape.txt').exists()


@pytest.mark.asyncio
async def test_links_are_not_followed(sandbox, tmp_path):
    executor, workspace = sandbox
    outside = tmp_path / 'private.txt'
    outside.write_text('PRIVATE')
    link = workspace / 'link.txt'
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip('This host does not allow unprivileged symlink creation')
    result = await executor.execute(workspace, operation('file_read', {'path': 'link.txt'}))
    assert result.status == 'failed' and 'PRIVATE' not in result.stdout
    result = await executor.execute(workspace, operation('file_write', {'path': 'link.txt', 'text': 'overwrite'}))
    assert result.status == 'failed' and outside.read_text() == 'PRIVATE'


@pytest.mark.asyncio
async def test_hardlinks_are_rejected(sandbox, tmp_path):
    executor, workspace = sandbox
    outside = tmp_path / 'private.txt'
    outside.write_text('PRIVATE')
    os.link(outside, workspace / 'hardlink.txt')
    result = await executor.execute(workspace, operation('file_read', {'path': 'hardlink.txt'}))
    assert result.status == 'failed' and 'HARDLINK' in result.stderr


@pytest.mark.asyncio
async def test_operation_id_is_not_a_replay_permission(sandbox):
    executor, workspace = sandbox
    op = operation('file_write', {'path': 'a.txt', 'text': 'first'}, id='same-operation')
    first = await executor.execute(workspace, op)
    (workspace / 'a.txt').write_text('later external contents')
    again = await executor.execute(workspace, op)
    assert again.data['replayed'] and again.data['sha256'] == first.data['sha256']
    assert (workspace / 'a.txt').read_text() == 'later external contents'
    changed = await executor.execute(workspace,
        operation('file_write', {'path': 'a.txt', 'text': 'different'}, id=op.id))
    assert changed.status == 'failed' and 'OPERATION_ID_REUSE' in changed.stderr


@pytest.mark.asyncio
async def test_bound_recipe_cannot_use_foreign_workspace_or_changed_source(sandbox):
    executor, workspace = sandbox
    source = b'print("ok")\n'
    (workspace / 'program.py').write_bytes(source)
    description = executor.describe_capability(workspace, recipe(source))
    assert description['sources'][0]['text'] == source.decode()
    registered = await executor.execute(workspace, operation('capability_request', recipe(source)))
    assert registered.status == 'succeeded' and registered.data['executed'] is False
    args = {'capability_id': registered.data['capability_id']}
    (workspace / 'program.py').write_text('print("changed")')
    result = await executor.execute(workspace, operation('exec', args))
    assert result.status == 'failed' and 'SOURCE_CHANGED' in result.stderr
    other = executor.workspaces / 'other'
    other.mkdir()
    result = await executor.execute(other, operation('exec', args))
    assert result.status == 'failed' and 'WORKSPACE_MISMATCH' in result.stderr


@pytest.mark.asyncio
async def test_shell_eval_and_unreviewed_arguments_have_no_route(sandbox):
    executor, workspace = sandbox
    for args in ({'argv': ['sh', '-c', 'echo bypass']},
                 {'capability_id': 'unknown', 'command': 'cmd /c echo bypass'},
                 {'capability_id': 'a' * 64}):
        result = await executor.execute(workspace, operation('exec', args))
        assert result.status == 'failed' and result.effect == 'none'
    with pytest.raises(PolicyError, match='CAPABILITY_SCHEMA'):
        executor.describe_capability(workspace, {'recipe': 'shell', 'command': 'echo bad'})


def test_registry_tamper_is_detected(sandbox):
    executor, workspace = sandbox
    source = b'print("ok")'
    (workspace / 'program.py').write_bytes(source)
    description = executor.describe_capability(workspace, recipe(source))
    row = executor.registry.register(description, 'registration')
    path = executor.registry.directory / (row['id'] + '.json')
    row['candidate']['argv'] = ['-c', 'evil']
    atomic_json(path, row)
    with pytest.raises(PolicyError, match='CAPABILITY_CORRUPT'):
        executor.registry.get(row['id'])


@pytest.mark.asyncio
async def test_unknown_effect_blocks_only_its_workspace_and_never_replays(sandbox):
    executor, workspace = sandbox
    op = operation('exec', {'capability_id': 'a' * 64})
    record = {'owner': executor.owner, 'workspace': str(workspace),
        'operation': op.model_dump(), 'payload_sha256': digest(canonical({'workspace': str(workspace),
        'operation': op.model_dump()})), 'phase': 'creating',
        'target_started': True, 'first_fault': None, 'created_at': 'test'}
    atomic_json(executor._record_path(op.id), record)
    result = await executor.execute(workspace, op)
    assert result.status == 'unknown' and not (workspace / 'a.txt').exists()
    result = await executor.execute(workspace, operation('file_list'))
    assert result.status == 'pending' and result.data['operation_ids'] == [op.id]
    other = executor.workspaces / 'other'
    other.mkdir()
    result = await executor.execute(other, operation('file_list'))
    assert result.status == 'succeeded'


@pytest.mark.asyncio
async def test_interrupted_write_reconciles_state_without_success_or_replay(sandbox):
    executor, workspace = sandbox
    op = operation('file_write', {'path': 'a.txt', 'text': 'intended'})
    record = {'owner': executor.owner, 'workspace': str(workspace),
        'operation': op.model_dump(), 'phase': 'file_write_started',
        'target_started': True, 'first_fault': None, 'created_at': 'test',
        'file_before_sha256': None, 'file_expected_sha256': digest(b'intended'),
        'file_parents_before': [], 'temporary_path': '.a.txt.temporary'}
    atomic_json(executor._record_path(op.id), record)
    (workspace / 'a.txt').write_text('intended')
    result = await executor.reconcile(op.id)
    assert result.status == 'failed' and result.effect == 'confirmed'
    assert result.data['recovered'] and result.data['current_matches_intended']
    assert result.data['replayed'] is False
    assert (await executor.execute(workspace, operation('file_list'))).status == 'succeeded'


def test_cross_process_lock_has_no_fixed_polling(sandbox):
    executor, _ = sandbox
    path = executor.control / 'test.lock'
    with process_lock(path):
        with pytest.raises(PolicyError, match='WORKSPACE_BUSY'):
            with process_lock(path):
                pytest.fail('second lease entered')


@pytest.mark.asyncio
async def test_foreign_container_cannot_be_killed_or_removed(sandbox, monkeypatch):
    executor, workspace = sandbox
    record = {'owner': executor.owner, 'operation': {'id': 'x'}, 'workspace': str(workspace),
              'container_name': 'owned-name', 'container_id': 'a' * 64, 'payload_sha256': 'b' * 64}
    calls = []
    async def fake_docker(args, **kwargs):
        calls.append(args)
        return 0, json.dumps([{'Id': 'a' * 64, 'Name': '/owned-name',
                              'Config': {'Labels': {'policy-harness.owner': 'FOREIGN'}},
                              'State': {'Running': True}}]).encode(), b''
    monkeypatch.setattr(executor, '_docker', fake_docker)
    with pytest.raises(PolicyError, match='OWNERSHIP_MISMATCH'):
        await executor._stop_owned(record)
    assert all('kill' not in call and 'rm' not in call for call in calls)


@pytest.fixture
async def real_sandbox(tmp_path):
    if os.getenv('HARNESS_RUN_DOCKER_TESTS') != '1':
        pytest.skip('Real Docker tests require HARNESS_RUN_DOCKER_TESTS=1; fixtures are not Docker proof')
    executor = Executor(tmp_path / 'real-data')
    health = await executor.health()
    if not health['available']:
        pytest.skip('Real Docker unavailable: ' + health.get('error', 'unknown'))
    setup = await executor.prepare_environment(include_pytest=False)
    assert setup['status'] == 'succeeded', setup
    workspace = executor.workspaces / 'real-task'
    workspace.mkdir(mode=0o777)
    workspace.chmod(0o777)
    yield executor, workspace
    for path in executor.journal.glob('*.json'):
        row = json.loads(path.read_text())
        if row.get('container_id'):
            result = await executor.reconcile(row['operation']['id'], stop=True)
            if result.effect != 'unknown':
                await executor.cleanup(row['operation']['id'])


async def run_program(executor, workspace, source, **options):
    write = await executor.execute(workspace, operation('file_write',
        {'path': 'program.py', 'text': source}))
    assert write.status == 'succeeded'
    registered = await executor.execute(workspace, operation('capability_request', recipe(source.encode(), **options)))
    assert registered.status == 'succeeded', registered
    op = operation('exec', {'capability_id': registered.data['capability_id']})
    return op, await executor.execute(workspace, op)


@pytest.mark.asyncio
async def test_real_normal_and_isolation(real_sandbox):
    executor, workspace = real_sandbox
    source = '''import os, pathlib, socket
assert os.geteuid() == 65534
assert not pathlib.Path('/var/run/docker.sock').exists()
try:
    pathlib.Path('/root-write-probe').write_text('bad')
    raise AssertionError('root filesystem writable')
except OSError:
    pass
s = socket.socket()
s.settimeout(1)
assert s.connect_ex(('1.1.1.1', 443)) != 0
s.close()
pathlib.Path('result.txt').write_text('isolated-normal-result')
print('ISOLATION_OBSERVED')
'''
    op, result = await run_program(executor, workspace, source)
    assert result.status == 'succeeded', result
    assert 'ISOLATION_OBSERVED' in result.stdout
    assert (workspace / 'result.txt').read_text() == 'isolated-normal-result'
    assert 'result.txt' in result.data['changed_paths']
    assert result.data['capture_complete']
    assert (await executor.execute(workspace, op)).data['replayed']


@pytest.mark.asyncio
async def test_real_failure_keeps_partial_effect_and_first_fault(real_sandbox):
    executor, workspace = real_sandbox
    _, result = await run_program(executor, workspace,
        'from pathlib import Path\nPath("partial.txt").write_text("before failure")\nraise RuntimeError("FIRST_FAULT_PROBE")\n')
    assert result.status == 'failed' and result.effect == 'confirmed'
    assert result.exit_code != 0 and 'FIRST_FAULT_PROBE' in result.stderr
    assert 'partial.txt' in result.data['changed_paths']


@pytest.mark.asyncio
async def test_real_pytest_registered_recipe(real_sandbox):
    executor, workspace = real_sandbox
    setup = await executor.prepare_environment(include_pytest=True)
    assert setup['status'] == 'succeeded', setup
    source = 'def test_normal_result():\n    assert sum([2, 3, 5]) == 10\n'
    await executor.execute(workspace, operation('file_write', {'path': 'test_normal.py', 'text': source}))
    args = recipe(source.encode(), entrypoint='test_normal.py')
    args.update(recipe='pytest', entrypoint=None, arguments=['-q', 'test_normal.py'])
    registered = await executor.execute(workspace, operation('capability_request', args))
    assert registered.status == 'succeeded'
    result = await executor.execute(workspace,
        operation('exec', {'capability_id': registered.data['capability_id']}))
    assert result.status == 'succeeded', result
    assert '1 passed' in result.stdout


@pytest.mark.asyncio
async def test_real_timeout_is_not_no_effect(real_sandbox):
    executor, workspace = real_sandbox
    _, result = await run_program(executor, workspace,
        'from pathlib import Path\nimport time\nPath("before-timeout").write_text("retained")\ntime.sleep(300)\n', timeout=2)
    assert result.status == 'failed' and result.effect == 'confirmed'
    assert result.data['first_fault']['reason'] == 'EXECUTION_TIMEOUT'
    assert not result.data['container_state']['Running']


@pytest.mark.asyncio
async def test_real_output_limit_is_explicit(real_sandbox):
    executor, workspace = real_sandbox
    _, result = await run_program(executor, workspace,
        'while True:\n    print("x" * 65536, flush=True)\n', output_bytes=4096)
    assert result.status == 'failed' and result.data['output_truncated']
    assert result.data['first_fault']['reason'] == 'OUTPUT_LIMIT'
    assert sum(artifact['bytes'] for artifact in result.artifacts) <= 4096
    assert not result.data['container_state']['Running']


@pytest.mark.asyncio
async def test_real_terminal_recovery_does_not_replay(real_sandbox, monkeypatch):
    executor, workspace = real_sandbox
    async def retain_for_crash_simulation(operation_id):
        return {'removed': False, 'reason': 'Explicit test retains container for a lost-result simulation'}
    monkeypatch.setattr(executor, 'cleanup', retain_for_crash_simulation)
    op, original = await run_program(executor, workspace,
        'from pathlib import Path\np=Path("count.txt")\np.write_text(p.read_text()+"x" if p.exists() else "x")\n')
    assert original.status == 'succeeded'
    record = executor._read_record(op.id)
    record.update(phase='unresolved', result=None)
    executor._save(record)
    restarted = Executor(executor.data_dir)
    recovered = await restarted.reconcile(op.id)
    assert recovered.status == 'failed' and recovered.effect == 'confirmed'
    assert recovered.data['recovered'] and not recovered.data['capture_complete']
    assert (workspace / 'count.txt').read_text() == 'x'
    assert (await restarted.cleanup(op.id))['removed']


@pytest.mark.asyncio
async def test_real_cancellation_preserves_owned_result(real_sandbox):
    executor, workspace = real_sandbox
    source = 'import time\nfrom pathlib import Path\nPath("started").write_text("yes")\nprint("BEFORE_CANCEL",flush=True)\ntime.sleep(300)\n'
    await executor.execute(workspace, operation('file_write', {'path': 'program.py', 'text': source}))
    registration = await executor.execute(workspace, operation('capability_request', recipe(source.encode(), timeout=300)))
    op = operation('exec', {'capability_id': registration.data['capability_id']})
    started = asyncio.Event()
    original = executor._owned_container
    async def observed(record):
        info = await original(record)
        if record.get('phase') == 'created':
            started.set()
        return info
    executor._owned_container = observed
    task = asyncio.create_task(executor.execute(workspace, op))
    await asyncio.wait_for(started.wait(), 30)
    # This deliberately cancels at a create/start boundary; it may have started,
    # so a truthful unknown outcome is allowed until explicit reconciliation.
    task.cancel()
    result = await task
    assert result.status in ('failed', 'unknown')
    recovered = await executor.reconcile(op.id, stop=True)
    assert recovered.status in ('failed', 'unknown')
    if recovered.status == 'failed':
        assert not recovered.data['container_state']['Running']
