"""Real native/filesystem consumers in owned temporary directories; scripted AI."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from pathlib import PureWindowsPath
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from policy_harness.access import Access
from policy_harness.models import OperationResult, PolicyError
from policy_harness.practical_models import ToolRequest
from policy_harness.server import create_app, windows_opened_path
from policy_harness.updates import UpdateError
from tests.test_practical_runtime import runtime, steps, tool, finish, run
from tests.test_server import Settings, Engine


def select_full(store, task):
    access = Access(store)
    return access.set(task['id'], 'full', access.get(task['id'])['revision'], confirm_full_access=True)


def test_full_access_requires_exact_confirmation_and_revision(tmp_path):
    engine, store, _, _, task = runtime(tmp_path, [])
    access = Access(store)
    for confirm in (False, None, 'true', 1):
        with pytest.raises(PolicyError, match='CONFIRMATION_REQUIRED'):
            access.set(task['id'], 'full', 0, confirm_full_access=confirm)
    assert access.get(task['id'])['mode'] == 'workspace'
    full = select_full(store, task)
    with pytest.raises(PolicyError, match='ACCESS_CONFLICT'):
        access.set(task['id'], 'read_only', 0)
    access.set(task['id'], 'read_only', full['revision'])
    with pytest.raises(PolicyError, match='CONFIRMATION_REQUIRED'):
        access.set(task['id'], 'full', full['revision'] + 1)
    with pytest.raises(PolicyError, match='CONFIRMATION_REQUIRED'):
        store.create_task('new', [], options={'access_mode': 'full'})
    assert len(store.list_tasks()) == 1


@pytest.mark.asyncio
async def test_absolute_file_delivery_and_read_before_replace(tmp_path):
    outside = tmp_path / 'outside' / '日本語 note.txt'
    engine, store, gateway, _, task = runtime(tmp_path / 'data', [
        steps(tool('file_write', path=str(outside), text='hello')), finish(outside.as_posix())])
    select_full(store, task)
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert outside.read_bytes() == b'hello'
    assert result['final']['artifacts'][0]['relative_path'] == outside.as_posix()
    assert 'run_command' in gateway.calls[0][2]['tools']
    assert len(gateway.calls) == 2
    other = store.create_task('replace existing', [])
    select_full(store, other)
    engine._state(other['id'], runtime='practical-v1')
    request = ToolRequest.model_validate(tool('file_write', path=str(outside), text='changed'))
    refused = await engine._perform(other, request, uuid4().hex)
    assert refused.status == 'failed' and 'READ_BEFORE_WRITE' in refused.stderr
    read = await engine._perform(other, ToolRequest.model_validate(tool('file_read', path=str(outside))), uuid4().hex)
    assert read.stdout == 'hello'
    written = await engine._perform(other, request, uuid4().hex)
    assert written.status == 'succeeded' and outside.read_bytes() == b'changed'


@pytest.mark.asyncio
async def test_non_full_stays_confined_and_downgrade_prevents_saved_host_run(tmp_path):
    engine, store, _, _, task = runtime(tmp_path / 'data', [])
    outside = tmp_path / 'private.txt'; outside.write_text('private', encoding='utf-8')
    engine._state(task['id'], runtime='practical-v1')
    for name, args in [('file_read', {'path': str(outside)}),
                       ('run_command', {'command': [sys.executable, '-c', 'print(1)']})]:
        result = await engine._perform(task, ToolRequest.model_validate(tool(name, **args)), uuid4().hex)
        assert result.status == 'failed'
    full = select_full(store, task)
    marker = tmp_path / 'must-not-run'
    op = engine._operation(uuid4().hex, 'exec', {'command': [sys.executable, '-c', f'open({str(marker)!r},"w").write("bad")']}, 'check changed access')
    store.save_operation(task['id'], op.model_dump(), tool_name='run_command', execution_access_mode='full')
    Access(store).set(task['id'], 'workspace', full['revision'])
    with pytest.raises(PolicyError, match='ACCESS_CHANGED'):
        await engine._execute_saved(task, op, 'run_command')
    assert not marker.exists()
    assert not engine.executor._read_record(op.id)


@pytest.mark.asyncio
async def test_native_command_real_cwd_output_files_no_docker_and_no_replay(tmp_path, monkeypatch):
    engine, store, _, _, task = runtime(tmp_path / 'data', [])
    select_full(store, task); engine._state(task['id'], runtime='practical-v1')
    async def no_docker(*args, **kwargs): raise AssertionError('Native path must not invoke Docker')
    monkeypatch.setattr(engine.executor, '_docker', no_docker)
    outside = tmp_path / 'native'; outside.mkdir()
    request = ToolRequest.model_validate(tool('run_command', command=[sys.executable, '-c',
        'from pathlib import Path; p=Path("count.txt");p.write_text(str(int(p.read_text())+1) if p.exists() else "1");print("native-ok")'], cwd=str(outside)))
    identity = uuid4().hex
    result = await engine._perform(task, request, identity)
    assert result.status == 'succeeded' and 'native-ok' in result.stdout
    assert result.data['execution'] == 'native' and result.data['process_tree_stopped']
    again = await engine._perform(task, request, identity)
    assert again.status == 'succeeded' and (outside / 'count.txt').read_text() == '1'
    read = await engine._perform(task, ToolRequest.model_validate(tool('file_read', path=str(outside / 'count.txt'))), uuid4().hex)
    assert read.stdout == '1'
    assert engine._artifact_evidence(task, [str(outside / 'count.txt')])


@pytest.mark.asyncio
async def test_native_output_is_bounded_and_timeout_stops_descendants(tmp_path):
    engine, store, _, _, task = runtime(tmp_path / 'data', [])
    select_full(store, task); engine._state(task['id'], runtime='practical-v1')
    request = ToolRequest.model_validate(tool('run_command', command=[sys.executable, '-c', 'print("x"*5000000)']))
    result = await engine._perform(task, request, uuid4().hex)
    assert result.status == 'succeeded' and len(result.stdout) <= 131072
    assert result.data['output_truncated']
    marker = tmp_path / 'escaped.txt'
    child = f'import time;time.sleep(2);open({str(marker)!r},"w").write("escaped")'
    parent = f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",{child!r}]);time.sleep(10)'
    request = ToolRequest.model_validate(tool('run_command', command=[sys.executable, '-c', parent], timeout_seconds=1))
    result = await engine._perform(task, request, uuid4().hex)
    assert result.status == 'failed' and result.data['first_fault']['reason'] == 'PROGRAM_TIMEOUT'
    assert result.data['process_tree_stopped']
    await asyncio.sleep(1.3)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_native_cancel_records_stop_without_replay(tmp_path):
    engine, store, _, _, task = runtime(tmp_path / 'data', [])
    select_full(store, task); engine._state(task['id'], runtime='practical-v1')
    operation = engine._operation(uuid4().hex, 'exec', {'command': [sys.executable, '-c', 'import time;time.sleep(20)']}, 'cancel test')
    running = asyncio.create_task(engine.executor.execute(Path(task['workspace']), operation, full_access=True))
    for _ in range(100):
        record = engine.executor._read_record(operation.id)
        if record and record.get('target_started'): break
        await asyncio.sleep(.02)
    assert record.get('target_started')
    running.cancel(); result = await running
    assert result.status == 'failed' and result.data['first_fault']['reason'] == 'CANCELLED'
    assert result.data['process_tree_stopped']
    recovered = await engine.executor.reconcile(operation.id)
    assert recovered.status == 'failed' and recovered.exit_code == result.exit_code


def test_api_confirmation_and_external_artifact_binding(tmp_path):
    engine, store, _, _, task = runtime(tmp_path / 'data', [])
    api_engine = Engine(store)
    app = create_app(engine=api_engine, store=store, settings=Settings(), data_dir=tmp_path / 'data')
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1',41234)) as client:
        client.get('/')
        headers = {'X-CSRF-Token': client.get('/api/session').json()['csrf_token']}
        store.update_task(task['id'], state={'runtime': 'practical-v1'})
        url = '/api/tasks/' + task['id'] + '/access'
        assert client.post(url, json={'mode':'full','expected_revision':0}, headers=headers).status_code != 200
        assert client.post(url, json={'mode':'full','expected_revision':0,'confirm_full_access':True}, headers=headers).status_code == 200
        assert client.post('/api/tasks', json={'objective':'native','access_mode':'full'}, headers=headers).status_code != 201
        response = client.post('/api/tasks', json={'objective':'native','access_mode':'full','confirm_full_access':True}, headers=headers)
        assert response.status_code == 201
        outside = tmp_path / 'download.txt'; outside.write_bytes(b'hello')
        operation = engine._operation(uuid4().hex, 'file_read', {'path':str(outside)}, 'registered read')
        store.save_operation(task['id'], operation.model_dump())
        store.update_operation(operation.id, result=OperationResult(operation_id=operation.id,status='succeeded',
            artifacts=[{'path':str(outside),'sha256':hashlib.sha256(b'hello').hexdigest(),'bytes':5}]).model_dump())
        snapshot = client.get('/api/tasks/' + task['id']).json()
        artifact = snapshot['operations'][0]['result']['artifacts'][0]
        from tests.test_whole_ui_repairs import node_ui
        rendered = node_ui(client, tmp_path, mode='artifacts', snapshot=snapshot,
                           invalid=[dict(artifact, download_url='https://invalid.example/file'),
                                    dict(artifact, sha256='0' * 64),
                                    dict(artifact, relative_path='../private.txt')])
        assert all(rendered['rejected'])
        assert [link['href'] for link in rendered['links']] == [artifact['download_url']]
        assert client.get(artifact['download_url']).content == b'hello'
        capture=[]
        for i in range(40):
            path=tmp_path/f'capture-{i}.bin';path.write_bytes(b'log')
            capture.append({'path':str(path),'sha256':hashlib.sha256(b'log').hexdigest(),'bytes':3,'channel':'stdout'})
        stream_op=engine._operation(uuid4().hex,'exec',{'command':['fixture']},'preserved stream record')
        store.save_operation(task['id'],stream_op.model_dump(),tool_name='run_command')
        store.update_operation(stream_op.id,result=OperationResult(operation_id=stream_op.id,status='succeeded',artifacts=capture).model_dump())
        paged=client.get('/api/tasks/'+task['id']+'?view=conversation').json()
        assert paged['record_page']['artifact_more'] is False
        assert len(paged['artifact_operations'])==1
        rendered=node_ui(client,tmp_path,mode='artifacts',snapshot=paged,invalid=[])
        assert [link['href'] for link in rendered['mainLinks']]==[artifact['download_url']]
        assert len(store.get_operation(stream_op.id)['result']['artifacts'])==40
        assert any('capture-' in link['text'] for link in rendered['links'])
        outside.write_bytes(b'changed')
        assert client.get(artifact['download_url']).status_code == 409
        Access(store).set(task['id'], 'workspace', 1)
        assert client.get(artifact['download_url']).status_code != 200


@pytest.mark.asyncio
async def test_native_external_result_reaches_independent_completion_review(tmp_path):
    outside = tmp_path / 'outside.txt'
    async def review(payload):
        assert payload['artifact_contents'][0]['path'] == outside.as_posix()
        assert payload['artifact_contents'][0]['text'] == 'hello'
        return {'verdict':'accept','findings':[],'rationale':'Actual execution and requested output verified'}
    engine, store, gateway, _, task = runtime(tmp_path / 'data', [
        steps(tool('run_command', command=[sys.executable, '-c', f'from pathlib import Path;Path({str(outside)!r}).write_text("hello")']),
              tool('file_read', path=str(outside))), finish(outside.as_posix()), review])
    select_full(store, task)
    result = await run(engine, store, task)
    assert result['status'] == 'completed'
    assert [c[1] for c in gateway.calls] == ['next_action','next_action','final_review']
    assert result['final']['artifacts'][0]['relative_path'] == outside.as_posix()


@pytest.mark.asyncio
async def test_normal_exit_closes_child_with_inherited_output_pipes(tmp_path):
    engine, store, _, _, task = runtime(tmp_path / 'data', [])
    select_full(store, task);engine._state(task['id'],runtime='practical-v1')
    child='import time;time.sleep(30)'
    parent=f'import subprocess,sys;p=subprocess.Popen([sys.executable,"-c",{child!r}]);print(p.pid,flush=True)'
    start=time.monotonic()
    result=await engine._perform(task,ToolRequest.model_validate(tool('run_command',command=[sys.executable,'-c',parent],timeout_seconds=5)),uuid4().hex)
    assert result.status=='succeeded' and result.exit_code==0,result
    assert time.monotonic()-start<3
    assert result.data['process_tree_stopped']
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
        api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];api.WaitForSingleObject.restype=wintypes.DWORD
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x00100000,False,int(result.stdout.strip()))
        if handle:
            try:assert api.WaitForSingleObject(handle,1000)==0
            finally:api.CloseHandle(handle)
        else:assert ctypes.get_last_error()==87  # Process ID already gone.


@pytest.mark.asyncio
async def test_changed_loaded_source_holds_once_without_repeated_model_calls(tmp_path):
    engine, store, gateway, _, task = runtime(tmp_path, [steps(tool('file_write',path='answer.txt',text='hello'))])
    def changed(*_):raise UpdateError('UPDATE_LOADED_SOURCE_DIFFERS_FROM_DISK: restart required before ordinary execution')
    engine.updates=SimpleNamespace(before_execution=changed)
    result=await run(engine,store,task)
    assert result['status']=='held' and '再起動' in result['state']['last_hold']['reason']
    assert len(gateway.calls)==1
    saved=store.operations(task['id'])[0]['result']
    assert 'UPDATE_LOADED_SOURCE_DIFFERS_FROM_DISK' in saved['stderr']
    assert not Path(task['workspace'],'answer.txt').exists()


@pytest.mark.parametrize('extended,expected',[
    (('\\\\?\\C' + ':' + '\\folder\\file.txt'),('C' + ':' + '\\folder\\file.txt')),
    (r'\\?\UNC\server\share\folder\file.txt',r'\\server\share\folder\file.txt'),
    (('C' + ':' + '\\folder\\file.txt'),('C' + ':' + '\\folder\\file.txt')),
])
def test_windows_handle_paths_keep_their_absolute_identity(extended,expected):
    normalized=PureWindowsPath(windows_opened_path(extended))
    assert normalized.is_absolute() and normalized==PureWindowsPath(expected)
    assert normalized.parent==PureWindowsPath(expected).parent
