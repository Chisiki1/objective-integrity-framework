"""Durable user controls and learning consumers, using scripted model decisions."""
import asyncio
import base64
import io
import json
from pathlib import Path
from uuid import uuid4
import zipfile

import pytest
from fastapi.testclient import TestClient

from policy_harness.access import Access, PermissionRequired
from policy_harness.attachments import decode_files, source_view
from policy_harness.capabilities import describe
from policy_harness.models import PolicyError
from policy_harness.organization import Organization
from policy_harness.practical_models import PracticalStep, ToolRequest
from policy_harness.server import create_app
from policy_harness.store import Store
from tests.test_practical_runtime import ROOT, runtime, steps, tool, finish, run
from tests.test_server import Settings


def lesson(**changes):
    return dict(action='create', title='Exact short UTF-8 files', reason='Actual bytes were read back successfully',
                operation_indices=[0], applies_when='Writing short literal UTF-8 text',
                procedure='Use file_write with the exact text; consume its actual byte readback.',
                limits='Does not prove behavior of executable code.', next_trigger='Next required text file write', **changes)


@pytest.mark.asyncio
async def test_learning_reaches_next_tool_and_observed_assessment(tmp_path):
    second=steps(tool('file_write',path='second.txt',text='world'))
    second.update(learning=[lesson()],skill_uses=[{'new_lesson':0,'tool_index':0,'adaptation':'Reuse exact-text write with readback for second.txt'}])
    done=finish('first.txt','second.txt')
    done['learning_assessments']=[{'operation_index':1,'judgment':'helpful','reason':'The second exact literal was saved and read back in one operation.'}]
    engine,store,gateway,web,task=runtime(tmp_path,[steps(tool('file_write',path='first.txt',text='hello')),second,done])
    result=await run(engine,store,task)
    assert result['status']=='completed'
    knowledge=engine.snapshot(task['id'])['knowledge']
    skill=knowledge['skills'][0]
    assert skill['use_count']==1 and skill['status']=='provisional'
    use=skill['uses'][0]
    assert use['result_status']=='succeeded' and use['assessment']['judgment']=='helpful'
    assert use['operation_id']==store.operations(task['id'])[1]['operation']['id']
    assert len(gateway.calls)==3 and not web.calls
    assert result['state']['metrics']['learning_model_calls']==0
    assert gateway.calls[2][2]['learning_context']['skills'][0]['title']==skill['title']
    assert gateway.calls[2][2]['learning_context']['pending_use_results'][0]['operation_index']==1
    store.close()


@pytest.mark.asyncio
async def test_saved_lesson_stays_unused_and_replays_idempotently(tmp_path):
    done=finish('answer.txt');done['learning']=[lesson()]
    engine,store,gateway,web,task=runtime(tmp_path,[steps(tool('file_write',path='answer.txt',text='hello')),done])
    await run(engine,store,task)
    skill=engine.learning.snapshot(task['id'])['skills'][0]
    assert skill['use_count']==0 and skill['display_status']=='暫定・未使用'
    call=store.records('practical_call')[-1]
    engine.learning.apply(store.get_task(task['id']),PracticalStep.model_validate(done),call['id'],[])
    assert len(store.records('practical_skill'))==1
    store.close()


@pytest.mark.asyncio
async def test_refinement_and_next_use_share_the_new_version_without_false_staleness(tmp_path):
    create=steps(tool('file_read',path='first.txt'));create['learning']=[lesson()]
    improve=steps(tool('file_write',path='second.txt',text='world'))
    improved=lesson();improved.update(action='improve',skill=0,procedure='Use verified whole_file text facts without a counting program.')
    improve.update(learning=[improved],skill_uses=[{'skill':0,'tool_index':0,'adaptation':'Use the refined measured readback procedure'}])
    done=finish('first.txt','second.txt');done['learning_assessments']=[{'operation_index':2,'judgment':'helpful','reason':'Measured text facts met the expected criteria'}]
    engine,store,gateway,_,task=runtime(tmp_path,[steps(tool('file_write',path='first.txt',text='hello')),create,improve,done])
    result=await run(engine,store,task)
    assert result['status']=='completed' and len(gateway.calls)==4
    skill=engine.learning.snapshot(task['id'])['skills'][0]
    assert skill['revision']==2 and skill['use_count']==1
    assert skill['uses'][0]['skill_revision']==2 and skill['uses'][0]['assessment']['judgment']=='helpful'
    assert store.records('practical_skill_history')[0]['revision']==1
    # A later response against an actually old catalog still fails.
    old=engine.learning.context(store.get_task(task['id']))['skills']
    old[0]['revision']=1
    with pytest.raises(PolicyError,match='知識が更新'):
        engine.learning.apply(store.get_task(task['id']),PracticalStep.model_validate(improve),'stale-call',old)
    store.close()


@pytest.mark.asyncio
async def test_learning_cannot_cite_unexecuted_or_foreign_evidence(tmp_path):
    engine,store,gateway,web,task=runtime(tmp_path,[])
    with pytest.raises(PolicyError,match='実際の操作結果'):
        engine.learning.apply(task,PracticalStep.model_validate(dict(finish(),learning=[lesson()])),'unknown-call',[])
    assert not store.records('practical_idea') and not store.records('practical_skill')
    store.close()


@pytest.mark.asyncio
async def test_folder_shares_lessons_but_not_original_other_task_sources(tmp_path):
    done=finish('answer.txt');done['learning']=[lesson(share_scope='folder', sharing_reason='Generic exact-text verification for related folder tasks')]
    engine,store,gateway,web,task=runtime(tmp_path,[steps(tool('file_write',path='answer.txt',text='hello')),done])
    folder=Organization(store).create_folder('Project')
    Organization(store).update(task['id'],{'folder_id':folder['id']},0)
    await run(engine,store,task)
    other=store.create_task('Private different task',[],options={'folder_id':folder['id']})
    outside=store.create_task('Outside',[])
    assert len(engine.learning.available(other))==1
    assert engine.learning.available(outside)==[]
    assert 'Private different task' not in json.dumps(engine.learning.context(store.get_task(task['id'])))
    store.close()


@pytest.mark.asyncio
async def test_permission_confirmation_resumes_exact_saved_call(tmp_path):
    engine,store,gateway,web,task=runtime(tmp_path,[steps(tool('file_write',path='answer.txt',text='hello')),finish('answer.txt')])
    Access(store).set(task['id'],'ask',0)
    result=await run(engine,store,task)
    assert result['status']=='awaiting_user'
    assert not Path(task['workspace'],'answer.txt').exists()
    approval=engine.pending_approvals()[0]
    engine.resolve_approval(approval['id'],'approve',approval['proposal_hash'],'User allowed this exact operation')
    await engine.running[task['id']]
    assert store.get_task(task['id'])['status']=='completed'
    assert Path(task['workspace'],'answer.txt').read_bytes()==b'hello'
    assert len(gateway.calls)==2 and len(store.operations(task['id']))==1
    store.close()


@pytest.mark.asyncio
async def test_readonly_rejects_write_without_effect(tmp_path):
    engine,store,gateway,web,task=runtime(tmp_path,[steps(tool('file_write',path='answer.txt',text='hello')),{'action':'blocked','message':'The current task is read only'}])
    Access(store).set(task['id'],'read_only',0)
    result=await run(engine,store,task)
    assert result['status']=='held' and not Path(task['workspace'],'answer.txt').exists()
    store.close()


@pytest.mark.asyncio
async def test_admission_denial_is_not_skill_use_or_effect(tmp_path):
    async def denied_next(payload):
        Access(store).set(task['id'], 'read_only', 0)
        answer=steps(tool('file_write',path='second.txt',text='world'))
        return dict(answer,learning=[lesson()],skill_uses=[{'new_lesson':0,'tool_index':0,'adaptation':'Write the next exact text'}])
    engine,store,_,_,task=runtime(tmp_path,[steps(tool('file_write',path='first.txt',text='hello')),denied_next,
                                          {'action':'blocked','message':'Write denied by current access'}])
    await run(engine,store,task)
    skill=engine.learning.snapshot(task['id'])['skills'][0]
    assert skill['use_count']==0 and skill['uses'][0]['status']=='not_executed'
    assert not Path(task['workspace'],'second.txt').exists()
    assert engine.learning.context(store.get_task(task['id']))['pending_use_results']==[]
    assessment=dict(finish(),learning_assessments=[{'operation_index':1,'judgment':'no_change','reason':'Nothing ran'}])
    with pytest.raises(PolicyError,match='作用が確認'):
        engine.learning.apply(store.get_task(task['id']),PracticalStep.model_validate(assessment),'assessment',[])
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['READ_NOT_FILE','WRITE_CONFLICT'])
async def test_executor_pre_target_rejection_is_not_skill_use(tmp_path,monkeypatch,failure):
    request=(tool('file_read',path='directory') if failure=='READ_NOT_FILE'
             else tool('file_write',path='second.txt',text='world'))
    second=dict(steps(request),learning=[lesson()],skill_uses=[{'new_lesson':0,'tool_index':0,'adaptation':'Apply to the next needed file operation'}])
    engine,store,_,_,task=runtime(tmp_path,[steps(tool('file_write',path='first.txt',text='hello')),second,
                                          {'action':'blocked','message':'Preserve failed operation'}])
    if failure=='READ_NOT_FILE':
        Path(task['workspace'], 'directory').mkdir()
    if failure=='WRITE_CONFLICT':
        execute=engine.executor.execute
        async def changed_before_target(workspace,operation,**options):
            if operation.args.get('path')=='second.txt':
                (workspace/'second.txt').write_bytes(b'concurrent external change')
            return await execute(workspace,operation,**options)
        monkeypatch.setattr(engine.executor,'execute',changed_before_target)
    await run(engine,store,task)
    row=store.operations(task['id'])[1]
    assert row['started_at'] and failure in row['result']['stderr']
    assert engine.executor._read_record(row['operation']['id'])['target_started'] is False
    skill=engine.learning.snapshot(task['id'])['skills'][0]
    assert skill['use_count']==0 and skill['uses'][0]['status']=='not_executed'
    assessment=dict(finish(),learning_assessments=[{'operation_index':1,'judgment':'no_change','reason':'No target execution'}])
    with pytest.raises(PolicyError,match='作用が確認'):
        engine.learning.apply(store.get_task(task['id']),PracticalStep.model_validate(assessment),'rejected-assessment',[])
    store.close()


def test_python_entrypoint_is_bound_with_supporting_files(tmp_path):
    engine,store,_,_,task=runtime(tmp_path,[])
    workspace=Path(task['workspace'])
    (workspace/'check.py').write_text('print("ok")',encoding='utf-8')
    (workspace/'input.txt').write_text('input',encoding='utf-8')
    request=ToolRequest.model_validate(tool('run_python',entrypoint='check.py',files=['input.txt']))
    recipe=engine._recipe(task,request)
    assert set(recipe['files'])=={'check.py','input.txt'}
    described=describe(workspace,recipe,{'image_id':'sha256:'+'a'*64})
    assert described['candidate']['entrypoint']=='check.py'
    with pytest.raises(PolicyError):
        engine._recipe(task,ToolRequest.model_validate(tool('run_python',entrypoint='../outside.py',files=['input.txt'])))
    store.close()


def test_permissions_are_rechecked_and_stale_approval_cannot_transfer(tmp_path):
    store=Store(tmp_path);task=store.create_task('Write',[]);access=Access(store)
    access.set(task['id'],'ask',0)
    with pytest.raises(PermissionRequired):access.check(task,'file_write',{'path':'x','text':'a'},'op','Save x')
    approval=access.pending()[0]
    changed=store.append_instruction(task['id'],'Changed user instruction',task['source_hash'])
    assert access.pending()==[]
    with pytest.raises(PolicyError,match='指示または権限'):access.resolve(approval['id'],'approve',approval['proposal_hash'],'ok')
    access.set(task['id'],'read_only',1)
    with pytest.raises(PolicyError,match='読み取り専用'):access.check(changed,'run_python',{},'run','Execute')
    access.check(changed,'file_read',{'path':'x'},'read','Read')
    store.close()


def test_organization_preserves_source_and_rejects_active_deletion(tmp_path):
    store=Store(tmp_path);task=store.create_task('Original instruction',[]);org=Organization(store)
    folder=org.create_folder('Project');nested=org.create_folder('Subfolder',folder['id'])
    meta=org.update(task['id'],{'title':'New title','pinned':True,'folder_id':nested['id']},0)
    current=store.get_task(task['id'])
    assert current==task and org.project(current)['ui']['title']=='New title'
    with pytest.raises(PolicyError,match='CONFLICT'):org.update(task['id'],{'pinned':False},0)
    store.update_task(task['id'],status='running')
    with pytest.raises(PolicyError,match='作業中'):org.update(task['id'],{'deleted':True},meta['revision'])
    store.update_task(task['id'],status='stopped')
    meta=org.update(task['id'],{'deleted':True,'archived':True},meta['revision'])
    org.update(task['id'],{'deleted':False,'archived':False},meta['revision'])
    assert store.source_bytes(task['id'],task['id']+':initial')[0]
    assert Path(task['workspace']).is_dir()
    store.close()


def test_submission_keeps_all_files_before_start_and_replay_is_exact(tmp_path):
    store=Store(tmp_path);identity=uuid4().hex
    files=decode_files([{'filename':'a.txt','base64':base64.b64encode(b'hello').decode()}, {'filename':'b.txt','base64':'eA=='}])
    task=store.create_task('Read attachments',[],submission_id=identity,attachments=files,options={'access_mode':'read_only'})
    assert len(task['source_history'])==3 and task['status']=='created'
    assert Access(store).get(task['id'])['mode']=='read_only'
    assert store.create_task('Read attachments',[],submission_id=identity,attachments=files,options={'access_mode':'read_only'})['id']==task['id']
    with pytest.raises(PolicyError,match='PAYLOAD_CONFLICT'):store.create_task('Read attachments',[],submission_id=identity,attachments=files[:1])
    assert store.claim_submission_start(identity) and not store.claim_submission_start(identity)
    store.close()


def test_docx_text_and_invalid_file_paths(tmp_path):
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>添付の本文</w:t></w:r></w:p></w:body></w:document>')
    parsed=source_view(raw.getvalue(),'document.docx')
    assert parsed['text']=='添付の本文' and parsed['format']=='docx'
    for filename in ['../secret','C:\\private','a/b','..']:
        with pytest.raises(PolicyError):decode_files([{'filename':filename,'base64':'eA=='}])


@pytest.mark.asyncio
async def test_attachment_reference_is_classified_without_faking_read_or_authority(tmp_path):
    engine,store,gateway,web,task=runtime(tmp_path,[])
    task=store.append_instruction(task['id'],'Reference attachment: binary.bin',task['source_hash'],attachment={'filename':'binary.bin','bytes':b'\x00\xffbinary'})
    criterion=task['acceptance'][0]
    gateway.replies=[{'objective':task['objective'],'sources':[{'source':1,'classification':'clarify','reason':'Reference is present but unsupported'}],
                      'criteria':[{'original':0,'disposition':'retain','criterion':criterion,'sources':[],'quote':'','reason':'Keep user requirement'}]},
                     {'action':'blocked','message':'This attached format cannot yet be read.'}]
    result=await run(engine,store,task)
    assert result['status']=='held'
    assert result['source_history'][1]['reference_only'] and result['source_history'][1]['status']=='applied'
    assert not store.record_get('source_read_progress',task['source_history'][1]['id'])['complete']
    assert result['acceptance']==[criterion]
    store.close()


@pytest.mark.asyncio
async def test_attachment_read_obtains_later_text_range(tmp_path):
    engine,store,gateway,web,task=runtime(tmp_path,[])
    text='A'*25000+'tail'
    task=store.append_instruction(task['id'],'Reference attachment',task['source_hash'],attachment={'filename':'large.txt','bytes':text.encode()})
    views=engine._source_views(task)
    assert views[1]['truncated'] and 'tail' not in views[1]['text']
    result=await engine._perform(task,PracticalStep.model_validate(steps(tool('attachment_read',source=1,offset=25000))).tools[0],uuid4().hex)
    assert result.status=='succeeded' and result.data['text']=='tail'
    assert result.data['next_offset'] is None
    store.close()


def test_api_organization_and_clipboard_are_authenticated_and_csrf_bound(tmp_path,monkeypatch):
    engine,store,gateway,web,task=runtime(tmp_path,[])
    app=create_app(data_dir=tmp_path,store=store,engine=engine,settings=Settings())
    calls=[]
    monkeypatch.setattr('policy_harness.attachments.clipboard_files',lambda:calls.append('paste') or [{'filename':'x.txt','base64':'eA=='}])
    with TestClient(app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',41234)) as client:
        assert client.post('/api/clipboard/files',json={'gesture':'paste'}).status_code in {401,403}
        assert calls==[]
        client.get('/');session=client.get('/api/session').json()
        client.headers['X-CSRF-Token']=session['csrf_token']
        assert client.post('/api/clipboard/files',json={}).status_code==422
        assert client.post('/api/clipboard/files',json={'gesture':'paste'}).json()['files'][0]['filename']=='x.txt'
        reply=client.patch('/api/tasks/'+task['id']+'/organization',json={'expected_revision':0,'pinned':True})
        assert reply.status_code==200 and reply.json()['ui']['pinned']
        listing=client.get('/api/tasks').json()
        assert listing['tasks'][0]['objective']==task['objective'] and listing['tasks'][0]['ui']['pinned']
    store.close()
