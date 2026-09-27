"""Actual local CLI/Store/process recovery; model and activation are explicit fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

from policy_harness.capabilities import atomic_json
from policy_harness.cli import _assert_restart_clear, _recover_restart, _runtime_lock, _supervise
from policy_harness.models import Operation, OperationResult, PolicyError
from policy_harness.server import restart_directory
from policy_harness.store import Store


SUPERVISOR = r'''
import argparse,json,os,sys
from pathlib import Path
from policy_harness.cli import _supervise
import policy_harness.capabilities as cap
project,data,mode,worker=map(str,sys.argv[1:])
original=cap.atomic_json
def write(path,value,*args,**kwargs):
    result=original(path,value,*args,**kwargs)
    if Path(path).name.startswith('accepted-') and mode=='accepted_pending':
        os._exit(17)  # Explicit bounded interruption after durable acceptance.
    if Path(path).name=='supervisor.json' and value.get('resume') and mode=='new_generation':
        os._exit(18)  # Before launching the replacement worker.
    return result
cap.atomic_json=write
unlink=Path.unlink
def remove(path,*args,**kwargs):
    result=unlink(path,*args,**kwargs)
    if path.name=='pending.json' and mode=='accepted_only':
        os._exit(19)
    return result
Path.unlink=remove
args=argparse.Namespace(data_dir=data,host='127.0.0.1',port=8876,launch_id=None)
code=_supervise(args,worker_command=lambda c:[sys.executable,'-B',worker,project,data,mode],project_root=Path(project))
sys.exit(code)
'''


WORKER = r'''
import asyncio,hashlib,json,os,sys
from pathlib import Path
from fastapi import FastAPI
from policy_harness.capabilities import canonical
from policy_harness.server import bind_restart_consumer,source_manifest
from policy_harness.store import Store
project,data,mode=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
store=Store(data)
task=store.list_tasks()[0]
context=json.loads(os.environ['HERMES_POLICY_WORKER'])
sources=source_manifest(project)
attestation={'candidate_id':'fixture-reviewed-candidate','source_version':hashlib.sha256(canonical(sources)).hexdigest(),
 'source_hashes':sources,'activation_hash':'d'*64,'activation_status':'activated','policy_hash':'fixture-policy',
 'restart_required':True,'effect_measurement':'pending-next-use'}
class Updates:
 def restart_attestation(self,candidate_id):
  assert candidate_id==attestation['candidate_id']
  return attestation
class Engine:
 updates=Updates()
engine=Engine();app=FastAPI()
app.state.worker_context=context;app.state.project_root=project
app.state.shutting_down=False;app.state.shutdown_event=asyncio.Event()
app.state.request_restart_exit=lambda:None
async def run():
 await bind_restart_consumer(app,engine,store,data)
 await engine.on_restart({'candidate_id':attestation['candidate_id'],'resume_task_ids':[task['id']]})
asyncio.run(run())
store.close()
sys.exit(0 if mode=='pending_stop' else 75)
'''


class AttestationFixture:
    """No activation is fabricated as live evidence; exact API contract only."""
    def __init__(self, value):
        self.value = value
        self.calls = []

    def validate_recovery_attestation(self, candidate_id, expected):
        self.calls.append((candidate_id, expected))
        if candidate_id != self.value['candidate_id'] or expected != self.value:
            raise PolicyError('UPDATE_RECOVERY_ATTESTATION_CHANGED')
        return self.value


def args(data, *, apply=False, marker=None, reason='Explicit local test recovery'):
    return argparse.Namespace(data_dir=str(data), restart_id=None, apply=apply,
        expected_marker_sha256=marker, reason=reason)


def setup_interruption(tmp_path, mode):
    project=tmp_path/'project'
    source=project/'src/policy_harness'
    source.mkdir(parents=True)
    (source/'fixture.py').write_text('version = 1\n', encoding='utf-8')
    data=tmp_path/'data'
    store=Store(data)
    task=store.create_task('Keep exact task after controlled stop',[' Preserve source '])
    operation=Operation(kind='file_write',args={'path':'unknown.txt','text':'fixture'},
        purpose='Preserve prior unknown fixture',expected_result='Never replay from recovery CLI',
        decisions=[{'id':'owned','statement':'Keep unknown effect','rationale':'Recovery test'}])
    store.save_operation(task['id'],operation.model_dump())
    store.update_operation(operation.id,status='result_recorded',result=OperationResult(
        operation_id=operation.id,status='unknown',effect='unknown',stderr='Explicit prior unknown fixture').model_dump())
    store.update_task(task['id'],status='stopped')
    before={'task':store.get_task(task['id']),'operations':store.operations(task['id'])}
    store.close()
    worker=tmp_path/'worker.py';worker.write_text(WORKER,encoding='utf-8')
    supervisor=tmp_path/'supervisor.py';supervisor.write_text(SUPERVISOR,encoding='utf-8')
    finished=subprocess.run([sys.executable,'-B',str(supervisor),str(project),str(data),mode,str(worker)],
        capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert finished.returncode=={'pending_stop':0,'accepted_pending':17,'accepted_only':19,'new_generation':18}[mode],finished.stderr
    directory=restart_directory(data)
    marker_path=directory/'pending.json'
    if not marker_path.exists():
        marker_path=next(directory.glob('accepted-*.json'))
    original=marker_path.read_bytes();marker=json.loads(original)
    return project,data,marker,original,before


@pytest.mark.parametrize('mode',['pending_stop','accepted_pending','accepted_only','new_generation'])
def test_trusted_cli_parks_actual_interrupted_handoff_without_resuming(tmp_path,mode):
    project,data,marker,original,before=setup_interruption(tmp_path,mode)
    with pytest.raises(PolicyError,match='RECONCILIATION'):
        _assert_restart_clear(data)
    store=Store(data);manager=AttestationFixture(marker['attestation'])
    try:
        view=_recover_restart(args(data),project_root=project,store=store,manager=manager)
        assert view['status']=='ready_to_park' and view['tasks_resumed'] is False
        assert view['tasks'][0]['unresolved_operation_ids']==[before['operations'][0]['operation']['id']]
        assert all(p['state']=='not_running' for p in view['process_states'])
        assert not list(restart_directory(data).glob('resolved-*'))
        final=_recover_restart(args(data,apply=True,marker=view['expected_marker_sha256']),
            project_root=project,store=store,manager=manager)
        assert final['status']=='parked_without_resume' and not final['tasks_resumed']
        directory=restart_directory(data)
        receipt=json.loads((directory/final['disposition']).read_bytes())
        assert all((directory/r['name']).read_bytes()==original for r in receipt['marker_refs'])
        owner_ref=receipt['supervisor_ref']
        assert hashlib.sha256((directory/owner_ref['name']).read_bytes()).hexdigest()==owner_ref['sha256']
        assert not (directory/'pending.json').exists()
        assert store.get_task(before['task']['id'])==before['task']
        assert store.operations(before['task']['id'])==before['operations']
        _assert_restart_clear(data)
        assert _recover_restart(args(data),project_root=project,store=store,manager=manager)['status']=='no_pending_restart'
        # Actual next ordinary supervisor start is now possible. The worker is
        # a no-model exit fixture; no stopped task is automatically resumed.
        launch=argparse.Namespace(data_dir=str(data),host='127.0.0.1',port=8876,launch_id=None)
        assert _supervise(launch,worker_command=lambda c:[sys.executable,'-B','-c','raise SystemExit(0)'],project_root=project)==0
        assert store.get_task(before['task']['id'])==before['task']
    finally:
        store.close()


def test_recovery_cli_uses_effective_policy_for_practical_marker(tmp_path,monkeypatch):
    from policy_harness.practical_policy import PracticalPolicy
    import policy_harness.updates as updates
    project,data,marker,original,before=setup_interruption(tmp_path,'pending_stop')
    shutil.copytree(Path(__file__).resolve().parents[1]/'policy',project/'policy')
    policy=PracticalPolicy(project/'policy/complete-policy-v3.json')
    marker['attestation']['policy_hash']=policy.hash
    atomic_json(restart_directory(data)/'pending.json',marker)
    seen=[]
    def manager_factory(actual_data,actual_root,actual_policy):
        seen.append(actual_policy.hash)
        assert actual_policy.hash==marker['attestation']['policy_hash']
        return AttestationFixture(marker['attestation'])
    monkeypatch.setattr(updates,'UpdateManager',manager_factory)
    view=_recover_restart(args(data),project_root=project)
    assert view['status']=='ready_to_park' and not view['tasks_resumed']
    assert seen==[policy.hash]


@pytest.mark.parametrize('fault',['stale_hash','foreign_owner','source_changed','activation_changed','live_owner','missing_task'])
def test_recovery_rejects_unbound_or_live_state_and_keeps_original(tmp_path,fault):
    project,data,marker,original,before=setup_interruption(tmp_path,'pending_stop')
    directory=restart_directory(data)
    store=Store(data);manager=AttestationFixture(marker['attestation'])
    value=args(data,apply=True,marker=hashlib.sha256(original).hexdigest())
    if fault=='stale_hash':value.expected_marker_sha256='0'*64
    if fault in {'foreign_owner','live_owner'}:
        context=json.loads((directory/'supervisor.json').read_bytes())
        context['supervisor_id' if fault=='foreign_owner' else 'supervisor_pid']='f'*32 if fault=='foreign_owner' else os.getpid()
        atomic_json(directory/'supervisor.json',context)
    if fault=='source_changed':(project/'src/policy_harness/fixture.py').write_text('version = 2\n')
    if fault=='activation_changed':manager.value=dict(marker['attestation'],activation_hash='e'*64)
    if fault=='missing_task':
        original_get=store.get_task
        store.get_task=lambda identity: (_ for _ in ()).throw(KeyError(identity))
    try:
        with pytest.raises((PolicyError,KeyError)):
            _recover_restart(value,project_root=project,store=store,manager=manager)
        assert (directory/'pending.json').read_bytes()==original
        assert not list(directory.glob('resolved-*'))
    finally:
        store.close()


def test_recovery_holds_both_runtime_ownership_locks(tmp_path):
    data=tmp_path/'data'
    code=('import argparse; from policy_harness.cli import _recover_restart; import sys\n'
          'a=argparse.Namespace(data_dir=sys.argv[1],restart_id=None,apply=False,expected_marker_sha256=None,reason=None)\n'
          'try: _recover_restart(a)\nexcept ValueError: print("ownership-held"); sys.exit(3)\n')
    for name in ('service-owner.lock','worker-owner.lock'):
        with _runtime_lock(data,name):
            completed=subprocess.run([sys.executable,'-B','-c',code,str(data)],capture_output=True,timeout=15)
            assert completed.returncode==3 and b'ownership-held' in completed.stdout


def test_recovery_reenters_after_disposition_before_pending_cleanup(tmp_path,monkeypatch):
    project,data,marker,original,before=setup_interruption(tmp_path,'accepted_pending')
    store=Store(data);manager=AttestationFixture(marker['attestation'])
    value=args(data,apply=True,marker=hashlib.sha256(original).hexdigest())
    unlink=Path.unlink
    def interrupt(path,*a,**kw):
        if path.name=='pending.json':raise OSError('Explicit interruption after recovery record')
        return unlink(path,*a,**kw)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path,'unlink',interrupt)
            with pytest.raises(OSError,match='Explicit interruption'):
                _recover_restart(value,project_root=project,store=store,manager=manager)
        directory=restart_directory(data)
        path=directory/('resolved-'+marker['restart_id']+'.json')
        recorded=path.read_bytes()
        assert (directory/'pending.json').read_bytes()==original
        final=_recover_restart(value,project_root=project,store=store,manager=manager)
        assert final['status']=='parked_without_resume' and path.read_bytes()==recorded
        assert store.operations(before['task']['id'])==before['operations']
        _assert_restart_clear(data)
    finally:store.close()


@pytest.mark.parametrize('status',['claimed','resume_dispatched'])
def test_already_consumed_pending_is_preserved_and_parked_without_replay(tmp_path,status):
    project,data,marker,original,before=setup_interruption(tmp_path,'accepted_pending')
    directory=restart_directory(data)
    atomic_json(directory/('consumed-'+marker['restart_id']+'.json'),{
        'restart_id':marker['restart_id'],'candidate_id':marker['candidate_id'],
        'previous_worker_pid':marker['worker_pid'],'source_version':marker['attestation']['source_version'],
        'resume_task_ids':marker['resume_task_ids'],'status':status})
    store=Store(data)
    try:
        final=_recover_restart(args(data,apply=True,marker=hashlib.sha256(original).hexdigest()),
            project_root=project,store=store,manager=AttestationFixture(marker['attestation']))
        assert final['status']=='parked_without_resume'
        assert store.get_task(before['task']['id'])==before['task']
        assert (directory/('consumed-'+marker['restart_id']+'.json')).exists()
        _assert_restart_clear(data)
    finally:store.close()


def test_cli_parser_keeps_recovery_local_and_no_automatic_apply():
    from policy_harness.cli import parser
    parsed=parser().parse_args(['recover-restart','--data-dir','local-runtime'])
    assert parsed.command=='recover-restart' and parsed.apply is False
    assert not hasattr(parsed,'url')
