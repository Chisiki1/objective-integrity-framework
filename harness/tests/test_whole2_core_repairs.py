"""Whole-2 counterexamples through real effects; model/Web judgments are fixtures."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from policy_harness.engine import CycleHeld
from policy_harness.models import PolicyError
from tests.test_cleanup_recovery import frozen_cleanup
from tests.test_core_recovery_closure import cycle
from tests.test_grouped_repairs import op
from tests.test_source_preparation import actual_runtime,attach,preparation
from tests.test_source_integration import proposed
from tests.fixture_preparation import prepare_operation


@pytest.mark.asyncio
@pytest.mark.parametrize('saved_status',['executing','superseded'])
async def test_policy_adoption_preserves_real_effect_without_saved_result(tmp_path,monkeypatch,saved_status):
    async with actual_runtime(tmp_path/'data') as (store,engine,executor,gateway):
        task=store.create_task('Preserve the original file',['Original file survives recovery'])
        action=op('file_write',path='effect.txt',text='original effect')
        state=await prepare_operation(engine,task,action)
        store.update_task(task['id'],state={'operation_id':action.id,'cycle_id':'result-gap-fixture'})
        await engine._pre(state)
        update=store.update_operation;execute=executor.execute;calls=[]
        async def counted(workspace,operation):
            calls.append(operation.id)
            return await execute(workspace,operation)
        def fail_result(identity,**fields):
            if identity==action.id and fields.get('status')=='result_recorded':
                raise sqlite3.OperationalError('FIRST_FAULT_RESULT_SAVE_AFTER_REAL_WRITE')
            return update(identity,**fields)
        monkeypatch.setattr(executor,'execute',counted)
        monkeypatch.setattr(store,'update_operation',fail_result)
        with pytest.raises(sqlite3.OperationalError,match='FIRST_FAULT_RESULT_SAVE'):
            await engine._execute(state)
        monkeypatch.setattr(store,'update_operation',update)
        if saved_status=='superseded':
            # Retained legacy row from the old bug; its consumed permit remains.
            update(action.id,status='superseded')
        original=store.get_operation(action.id)
        assert original['result'] is None and calls==[action.id]
        assert (Path(task['workspace'])/'effect.txt').read_bytes()==b'original effect'
        assert not engine._allowed_with_unknown(store.get_task(task['id']),op('file_write',path='other.txt',text='new'))
        with pytest.raises(CycleHeld,match='Unknown effects'):
            await engine._finalize(store.get_task(task['id']),original)
        # Explicit old-policy fixture: the real adoption cycle consumes the same
        # started operation, without pretending a real user amendment occurred.
        store.update_task(task['id'],policy_hash='OLD_POLICY_FIXTURE')
        await engine._adopt_current_policy(store.get_task(task['id']))
        adoption=next(r for r in store.operations(task['id']) if r['operation']['kind']=='adopt_policy')
        assert adoption['status']=='cycle_complete'
        assert adoption['operation']['args']['suspended_operation_id']==action.id
        pending=store.get_operation(action.id)
        assert pending['result']['effect']=='unknown'
        assert pending['operation']==original['operation']
        assert store.record_get('operation_result_gap',action.id)['original']==original
        await engine._post(state);await engine._learn(state);await engine._close_cycle(state)
        recovery=op('reconcile',operation_id=action.id,reason='Read the original executor receipt without replay')
        result=await cycle(engine,store.get_task(task['id']),recovery)
        assert result['status']=='cycle_complete' and result['result']['status']=='succeeded'
        assert store.get_operation(action.id)['result']['effect']=='confirmed'
        assert store.record_get('effect_reconciliation',recovery.id)['original']['result']['effect']=='unknown'
        assert calls==[action.id] and (Path(task['workspace'])/'effect.txt').read_bytes()==b'original effect'


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition',['withdraw','retain','retain_after_update'])
async def test_decoded_attachment_pending_confirmation_reaches_reviewed_disposition(tmp_path,disposition):
    async with actual_runtime(tmp_path/'data') as (store,engine,executor,gateway):
        task,source=attach(store,store.create_task('Write hello in answer.txt',['answer.txt contains hello']),
                           raw=b'Complete plain attachment.\n',filename='plain.txt')
        await cycle(engine,task,proposed('source_read',source_id=source['id'],expected_hash=source['sha256'],offset=0))
        progress=store.record_get('source_read_progress',source['id'])
        assert progress['complete'] and progress['text_decoded']
        task=store.append_instruction(task['id'],'Withdraw plain.txt; keep the file task.',task['source_hash'])
        instruction=task['source_history'][-1]
        action=preparation(task,source,'withdraw',instruction_id=instruction['id'],source_quote=instruction['text'])
        store.save_operation(task['id'],action.model_dump(),policy_hash=engine.policy.hash)
        state={'task_id':task['id'],'operation_id':action.id}
        await engine._pre(state);await engine._execute(state)
        original=deepcopy(store.get_operation(action.id))
        text='Keep the withdrawal and the file task.' if disposition=='withdraw' else 'Retain plain.txt and use its full contents; keep the file task.'
        task=store.append_instruction(task['id'],text,task['source_hash'])
        await engine._post(state);await engine._learn(state);await engine._close_cycle(state)
        task=store.get_task(task['id'])
        pending=engine.source_preparation.pending(task)['pending']
        assert len(pending)==1 and pending[0]['confirmation_pending'][0]['id']==action.id
        with pytest.raises(PolicyError,match='confirmation requires'):
            store.apply_source_plan(task['id'],task['source_hash'],{},'NO_ADOPTION_PERMISSION')
        latest=task['source_history'][-1]
        mode='withdraw' if disposition=='withdraw' else 'retain'
        fresh=preparation(task,source,mode,instruction_id=latest['id'],source_quote=latest['text'])
        if mode=='retain':
            stale=preparation(task,source,'retain',instruction_id=instruction['id'],source_quote=instruction['text'])
            with pytest.raises(PolicyError,match='later than'):
                engine.source_preparation.describe(task,stale.args)
        if disposition=='retain_after_update':
            store.save_operation(task['id'],fresh.model_dump(),policy_hash=engine.policy.hash)
            retention_state={'task_id':task['id'],'operation_id':fresh.id}
            await engine._pre(retention_state);await engine._execute(retention_state)
            task=store.append_instruction(task['id'],'Keep the requested output text unchanged.',task['source_hash'])
            await engine._post(retention_state);await engine._learn(retention_state);await engine._close_cycle(retention_state)
            assert len(engine.source_preparation.pending(store.get_task(task['id']))['pending'][0]['confirmation_pending'])==2
            # The later unrelated instruction changes the source set, but does
            # not erase the still-valid prior user instruction to retain input.
            fresh=preparation(task,source,'retain',instruction_id=latest['id'],source_quote=latest['text'])
        observed=await cycle(engine,task,fresh)
        assert observed['status']=='cycle_complete' and observed['pre_review'] and observed['post_review'] and observed['knowledge']
        settled=store.record_get('source_preparation_confirmation_pending',action.id)
        assert settled['status']==('superseded_by_reviewed_confirmation' if disposition=='withdraw' else 'superseded_by_reviewed_retention')
        assert settled['confirmation_operation_id']==fresh.id
        assert not engine.source_preparation.pending(store.get_task(task['id']))['pending']
        assert store.get_operation(action.id)['operation']==original['operation']
        assert store.get_operation(action.id)['result']==original['result']
        completed=await asyncio.wait_for(engine.run_task(task['id']),120)
        assert completed['task']['status']=='completed',completed['events'][-3:]
        assert (Path(task['workspace'])/'answer.txt').read_bytes()==b'hello'
        current_source=next(s for s in completed['task']['source_history'] if s['id']==source['id'])
        assert current_source['status']==('withdrawn' if disposition=='withdraw' else 'applied')
        assert store.source_bytes(task['id'],source['id'])[0]==b'Complete plain attachment.\n'


class CommitAckFault:
    def __init__(self,connection,cleanup_id):
        self.connection=connection;self.cleanup_id=cleanup_id;self.armed=True;self.commits=0
        assert connection.execute('SELECT 1 FROM records WHERE kind=? AND id=?',
                                  ('cleanup_result',cleanup_id)).fetchone() is None
    def __getattr__(self,name):return getattr(self.connection,name)
    def execute(self,sql,*args):
        result=self.connection.execute(sql,*args)
        # Earlier intent/metadata commits are not the injected cleanup effect.
        # Fail only after the exact cleanup result has actually committed.
        if sql=='COMMIT' and self.armed and self.connection.execute(
                'SELECT 1 FROM records WHERE kind=? AND id=?',
                ('cleanup_result',self.cleanup_id)).fetchone() is not None:
            self.armed=False;self.commits+=1
            raise sqlite3.OperationalError('FIRST_FAULT_AFTER_ACTUAL_COMMIT')
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize('hide_readback',[False,True])
async def test_cleanup_commit_ack_fault_keeps_actual_effect_and_after_review(tmp_path,monkeypatch,hide_readback):
    store,engine,task,row,skills=frozen_cleanup(tmp_path)
    identity=task['id']+':'+row['operation']['id'];get=store.record_get
    connection=store.db;proxy=CommitAckFault(connection,identity)
    finish=engine.knowledge.finish;mutations=[];reviews=[]
    def counted(*args):mutations.append(args);return finish(*args)
    def readback(kind,key):
        if hide_readback and proxy.commits and kind=='cleanup_result':raise OSError('READBACK_UNAVAILABLE')
        return get(kind,key)
    async def assessments(*args,**kwargs):return []
    async def review(*args,**kwargs):
        reviews.append(args)
        return row['finalization']['pre_review']
    draft=deepcopy(row['finalization']);draft.pop('post_review');draft.pop('post_assessments')
    store.update_operation(row['operation']['id'],finalization=draft)
    row=store.get_operation(row['operation']['id'])
    monkeypatch.setattr(engine.knowledge,'finish',counted)
    monkeypatch.setattr(store,'record_get',readback)
    monkeypatch.setattr(engine,'_assess_targets',assessments)
    monkeypatch.setattr(engine,'_review_bundle',review)
    store.db=proxy
    try:
        if hide_readback:
            with pytest.raises(CycleHeld,match='cleanup has necessary work'):await engine._finalize(task,row)
        else:await engine._finalize(task,row)
        monkeypatch.setattr(store,'record_get',get)
        failure=get('cleanup_failure',identity)
        assert failure['first_fault']['message']=='FIRST_FAULT_AFTER_ACTUAL_COMMIT'
        assert failure['transaction_rolled_back'] is False
        assert failure['effect']==('unknown' if hide_readback else 'confirmed')
        assert get('cleanup_result',identity) is not None and proxy.commits==1
        assert all(store.record_get('skill',s['id'])['status']=='retired' for s in skills)
        assert reviews and reviews[0][2]=='completion_outcome'
        if not hide_readback:
            assert reviews[0][3]['actual_cleanup_result']['commit_observation']['first_fault']==failure['first_fault']
            assert store.get_task(task['id'])['status']=='completed'
            await engine._finalize(store.get_task(task['id']),store.get_operation(row['operation']['id']))
        else:
            assert store.get_task(task['id'])['status']!='completed'
            with pytest.raises(CycleHeld):await engine._finalize(store.get_task(task['id']),store.get_operation(row['operation']['id']))
        assert len(mutations)==1 and get('cleanup_failure',identity)==failure
    finally:
        store.db=connection
        await engine.close();store.close()
