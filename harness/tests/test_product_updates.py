"""Optional releases: verification, preservation, rollback and idle service controls."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from policy_harness import product_install as install
from policy_harness import product_updates as updates
from .test_server import harness


def package(root, version, files):
    root.mkdir(parents=True, exist_ok=True)
    rows=[]
    for name, value in files.items():
        data=value.encode() if isinstance(value,str) else value
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        rows.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest().upper()})
    (root/'application.json').write_text(json.dumps({'schema':'oif-desktop-package-v1','version':version,'members':rows}),encoding='utf-8')
    return root


def release(version='0.3.0-beta.3'):
    tag='v'+version;name=f'OIF-Desktop-{version}-windows-x64.zip'
    return {'tag_name':tag,'draft':False,'prerelease':'beta' in version,'assets':[{'state':'uploaded','name':name,'size':100,
        'digest':'sha256:'+'a'*64,'browser_download_url':updates.PAGE+'/download/'+tag+'/'+name}]}


def test_release_selection_understands_beta_numeric_order_and_stable_channel():
    rows=[release('0.3.0-beta.9'),release('0.3.0-beta.10'),release('0.3.0-beta.2')]
    assert updates.select_release(rows,'0.3.0b2')['version']=='0.3.0-beta.10'
    assert updates.select_release(rows,'0.3.0') is None
    rows += [release('0.3.1')]
    assert updates.select_release(rows,'0.3.0')['version']=='0.3.1'
    rows[-1]['assets'][0]['browser_download_url']='https://example.com/update.zip'
    assert updates.select_release(rows,'0.3.0') is None
    rows[1]['assets'][0]['digest']=None
    assert updates.select_release(rows,'0.3.0b2')['version']=='0.3.0-beta.9'


def test_update_check_is_cached_and_network_failure_does_not_touch_tasks(tmp_path):
    calls=[]
    def download(url,limit):calls.append(url);return json.dumps([release()]).encode()
    manager=updates.ProductUpdates(tmp_path/'app',tmp_path/'data',downloader=download)
    assert manager.check()['state']=='available'
    assert manager.check()['state']=='available' and len(calls)==1
    assert manager.check(force=True)['available']['version']=='0.3.0-beta.3' and len(calls)==2
    def offline(*args):raise OSError('offline')
    manager.downloader=offline
    assert manager.check(force=True)['state']=='unavailable'
    assert not (tmp_path/'app').exists()


def test_install_preserves_unknown_files_and_runtime_removes_only_retired_managed_files(tmp_path):
    root=package(tmp_path/'app','0.3.0-beta.1',{'OIF.exe':'old','src/main.py':'old','src/retired.py':'remove'})
    stage=package(tmp_path/'new','0.3.0-beta.2',{'OIF.exe':'new','src/main.py':'new','src/added.py':'add'})
    runtime=root/'.runtime';runtime.mkdir();(runtime/'control.sqlite3').write_bytes(b'original tasks')
    (root/'my-notes.txt').write_text('retain');(root/'src/custom.py').write_text('custom')
    work=tmp_path/'work';work.mkdir()
    install.make_plan(root,stage,work);result=install.apply_plan(work)
    assert result['state']=='installed'
    assert (root/'src/main.py').read_text()=='new' and not (root/'src/retired.py').exists()
    assert (runtime/'control.sqlite3').read_bytes()==b'original tasks'
    assert (root/'my-notes.txt').read_text()=='retain' and (root/'src/custom.py').read_text()=='custom'
    assert (work/'backup/OIF.exe').read_text()=='old'


def test_local_self_improvements_and_unmanaged_collision_are_not_overwritten(tmp_path):
    root=package(tmp_path/'app','0.3.0-beta.1',{'src/main.py':'original'})
    stage=package(tmp_path/'new','0.3.0-beta.2',{'src/main.py':'new','src/added.py':'new'})
    work=tmp_path/'work';work.mkdir();(root/'src/main.py').write_text('local improvement')
    with pytest.raises(ValueError,match='changed'):install.make_plan(root,stage,work)
    assert (root/'src/main.py').read_text()=='local improvement'
    (root/'src/main.py').write_text('original');(root/'src/added.py').write_text('mine')
    with pytest.raises(ValueError,match='local file'):install.make_plan(root,stage,work)
    assert not (work/'install.json').exists()


def test_write_failure_restores_all_changed_files_and_keeps_first_fault(tmp_path,monkeypatch):
    root=package(tmp_path/'app','0.3.0-beta.1',{'a.txt':'old-a','b.txt':'old-b'})
    stage=package(tmp_path/'new','0.3.0-beta.2',{'a.txt':'new-a','b.txt':'new-b'})
    before=(root/'application.json').read_bytes();work=tmp_path/'work';work.mkdir();install.make_plan(root,stage,work)
    original=install.replace_bytes;failed=False
    def write(path,data):
        nonlocal failed
        if Path(path)==root/'b.txt' and not failed:
            failed=True;raise OSError('synthetic disk failure')
        return original(path,data)
    monkeypatch.setattr(install,'replace_bytes',write)
    with pytest.raises(OSError,match='synthetic disk failure'):install.apply_plan(work)
    assert (root/'a.txt').read_text()=='old-a' and (root/'b.txt').read_text()=='old-b'
    assert (root/'application.json').read_bytes()==before
    assert json.loads((work/'install.json').read_bytes())['state']=='rolled_back'


def test_interrupted_update_recovers_from_preimages_without_replaying_install(tmp_path):
    root=package(tmp_path/'app','0.3.0-beta.1',{'a.txt':'old'})
    stage=package(tmp_path/'new','0.3.0-beta.2',{'a.txt':'new'})
    work=tmp_path/'work';work.mkdir();plan=install.make_plan(root,stage,work)
    (work/'backup').mkdir();(work/'backup/a.txt').write_text('old')
    (root/'a.txt').write_text('new');plan['state']='applying';install.atomic(work/'install.json',plan)
    assert install.apply_plan(work,recover=True)['state']=='rolled_back'
    assert (root/'a.txt').read_text()=='old'


@pytest.mark.parametrize('name', ['../escape','.runtime/control.sqlite3','CON.txt','src/a.py:stream','/absolute','src/../escape','src/a.py.','src\\escape'])
def test_archive_rejects_unsafe_and_user_data_paths(tmp_path,name):
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as archive:
        archive.writestr('OIF-Desktop-0.3.0-beta.3/'+name,b'bad')
    with pytest.raises(ValueError):updates.extract_package(raw.getvalue(),tmp_path/'stage',{'version':'0.3.0-beta.3'})
    assert not (tmp_path/'stage').exists()


def test_shutdown_idle_check_includes_queued_engine_handle_and_identity(harness):
    client,store,engine,_,app=harness;called=[];app.state.request_shutdown=lambda:called.append('exit')
    app.state.launch_id='a'*32;engine.updates=SimpleNamespace(source_status=lambda:{'restart_required':True})
    row=client.get('/api/maintenance').json();assert row['restart_required']
    payload={'expected_instance_id':row['instance_id'],'only_if_idle':True,'only_if_source_changed':True}
    assert client.post('/api/shutdown',json={**payload,'expected_instance_id':'wrong'}).status_code==409
    engine.running={'queued':SimpleNamespace(done=lambda:False)}
    assert client.post('/api/shutdown',json=payload).status_code==409
    assert called==[] and not app.state.shutting_down
    engine.running={};task=store.create_task('keep this task',['preserve']);store.update_task(task['id'],status='running')
    assert client.post('/api/shutdown',json=payload).status_code==409
    store.update_task(task['id'],status='held')
    assert client.post('/api/shutdown',json=payload).json()['status']=='shutdown_requested'
    assert called==['exit'] and store.get_task(task['id'])['status']=='held'
    assert client.post('/api/tasks/'+task['id']+'/resume',json={}).status_code==409


def test_unchanged_service_reuses_instance_and_does_not_exit(harness):
    client,_,engine,_,app=harness;called=[];app.state.request_shutdown=lambda:called.append(True)
    engine.updates=SimpleNamespace(source_status=lambda:{'restart_required':False})
    row=client.get('/api/maintenance').json()
    response=client.post('/api/shutdown',json={'expected_instance_id':row['instance_id'],'only_if_idle':True,'only_if_source_changed':True})
    assert response.json()['status']=='unchanged' and not called


def test_install_refuses_running_task_before_dispatching_any_helper(harness):
    client,store,_,_,app=harness;calls=[];app.state.request_shutdown=lambda:calls.append('exit')
    app.state.product_updates=SimpleNamespace(install=lambda *args:calls.append('install'))
    task=store.create_task('running',['preserve']);store.update_task(task['id'],status='running')
    response=client.post('/api/app-updates/install',json={'version':'0.3.0-beta.3','expected_instance_id':app.state.instance_id})
    assert response.status_code==409 and not calls and not app.state.shutting_down


def test_stale_update_manager_keeps_original_loaded_cut(tmp_path):
    from policy_harness.updates import UpdateManager
    # Constructor needs the real store/executor contract; use its existing small
    # _code method binding to exercise source_status without relaxing the guard.
    manager=object.__new__(UpdateManager);source=tmp_path/'source.py';source.write_text('old')
    from policy_harness.updates import _manifest
    manager._code=lambda:{'source.py':source.read_bytes()}
    manager._loaded_manifest=_manifest(manager._code())
    manager.runtime_identity=lambda:{}
    assert not manager.source_status()['restart_required']
    source.write_text('new');assert manager.source_status()['restart_required']
    assert manager._loaded_manifest!=_manifest(manager._code())


def test_conditional_shutdown_reply_survives_windows_oem_decoding(monkeypatch,capsys):
    from policy_harness import cli
    class Client:
        def __init__(self,*args):pass
        def get(self,path):return {'conditional_shutdown':True,'restart_required':True,'active_tasks':0,'instance_id':'test','launch_id':'test'}
        def post(self,path,body):return {'status':'shutdown_requested','detail':'実行中の作用を保存して終了します。'}
        def close(self):pass
    monkeypatch.setattr(cli,'Client',Client)
    assert cli.main(['shutdown','--if-idle','--if-source-changed','--launch-id','test'])==0
    raw=capsys.readouterr().out.encode('utf-8')
    assert all(c<128 for c in raw)
    assert json.loads(raw.decode('cp932'))['status']=='shutdown_requested'


@pytest.mark.parametrize('installed,offered', [('0.3.0b3','0.3.0-beta.3'),('0.3.0b3','0.3.0-beta.2'),('0.3.0','0.3.1-beta.1')])
def test_persisted_cache_cannot_offer_installed_older_or_wrong_channel(tmp_path,monkeypatch,installed,offered):
    import time
    monkeypatch.setattr(updates,'__version__',installed)
    manager=updates.ProductUpdates(tmp_path/'app',tmp_path/'data',downloader=lambda *_:pytest.fail('Fresh cache should not fetch'))
    install.atomic(manager.cache,{'state':'available','checked_at':time.time(),'available':{'version':offered}})
    assert manager.check()['available'] is None
    assert manager.status()['state']=='current'
    # The original check receipt is retained; it is interpreted against the
    # running version rather than silently rewritten as a new network check.
    assert json.loads(manager.cache.read_bytes())['available']['version']==offered


def test_install_rejects_concurrent_preparation_without_waiting_or_substitution(tmp_path,monkeypatch):
    manager=updates.ProductUpdates(tmp_path/'app',tmp_path/'data')
    manager.prepared={'version':'0.3.0-beta.3','work':str(tmp_path/'selected')}
    monkeypatch.setattr(updates,'owned_record',lambda *_:pytest.fail('Busy preparation must be rejected before dispatch'))
    with manager.lock:
        with pytest.raises(ValueError,match='being prepared'):manager.install('0.3.0-beta.3','launch')
    assert manager.prepared['work']==str(tmp_path/'selected')


def test_install_checks_selected_plan_identity_under_shared_lock(tmp_path,monkeypatch):
    root=package(tmp_path/'app','0.3.0-beta.2',{'OIF.exe':'old'})
    manager=updates.ProductUpdates(root,root/'.runtime')
    work=manager.directory/'selected';work.mkdir()
    stage=package(work/'package','0.3.0-beta.4',{'OIF.exe':'different release'})
    install.make_plan(root,stage,work)
    manager.prepared={'version':'0.3.0-beta.3','work':str(work)}
    def owner(*args):assert manager.lock.locked()
    monkeypatch.setattr(updates,'owned_record',owner)
    with pytest.raises(ValueError,match='identity changed'):manager.install('0.3.0-beta.3','launch')
    assert not manager.lock.locked() and not (manager.data/'product-update-pending.json').exists()


@pytest.mark.parametrize('endpoint', ['/api/shutdown','/api/maintenance/restart','/api/app-updates/install'])
def test_shutdown_apis_block_even_a_queued_maintenance_recovery_task(tmp_path,monkeypatch,endpoint):
    from fastapi.testclient import TestClient
    from .test_practical_runtime import runtime
    from .test_server import Settings
    from policy_harness.models import PolicyError
    from policy_harness.server import create_app
    engine,store,gateway,_,task=runtime(tmp_path,[])
    monkeypatch.setattr(engine.maintenance,'recovery_targets',lambda *_:True)
    monkeypatch.setattr(updates,'launch_restart',lambda *_:None)
    app=create_app(tmp_path,engine=engine,settings=Settings(),store=store)
    exits=[];app.state.request_shutdown=lambda:exits.append('exit')
    try:
        with TestClient(app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',41234)) as client:
            assert client.get('/').status_code==200
            client.headers['X-CSRF-Token']=client.get('/api/session').json()['csrf_token']
            app.state.product_updates=SimpleNamespace(install=lambda *_:None)
            payload={'expected_instance_id':app.state.instance_id}
            if endpoint.endswith('/install'):payload['version']='0.3.0-beta.3'
            else:payload['only_if_idle']=True
            assert client.post(endpoint,json=payload).status_code==200
            assert exits==['exit']
            # Represents a start queued before the HTTP middleware closed.
            with pytest.raises(PolicyError,match='再起動'):engine.start_task(task['id'])
            assert not engine.running and not gateway.calls
    finally:store.close()


def test_nested_package_paths_extract_install_and_restore(tmp_path,monkeypatch):
    name='python/Lib/site-packages/'+('nested/'*24)+'schema.py'
    root=package(tmp_path/'app','0.3.0-beta.2',{'OIF.exe':'old','a.txt':'old'})
    stage=tmp_path/'updates'/'selected'/'package';stage.parent.mkdir(parents=True)
    files={'OIF.exe':b'new','a.txt':b'new','python/python.exe':b'fixture',
           'src/policy_harness/product_install.py':b'fixture',name:b'nested content'}
    metadata={'schema':'oif-desktop-package-v1','version':'0.3.0-beta.3','members':[
        {'path':n,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest().upper()} for n,b in files.items()]}
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as archive:
        for n,b in {**files,'application.json':json.dumps(metadata).encode()}.items():
            archive.writestr('OIF-Desktop-0.3.0-beta.3/'+n,b)
    updates.extract_package(raw.getvalue(),stage,{'version':'0.3.0-beta.3'})
    assert install.member(stage,name).read_bytes()==b'nested content'
    work=tmp_path/'work';work.mkdir();install.make_plan(root,stage,work)
    install.apply_plan(work)
    assert install.member(root,name).read_bytes()==b'nested content'
    # A second update backs up and restores the long member on a later failure.
    newer=tmp_path/'newer';updates.extract_package(raw.getvalue(),newer,{'version':'0.3.0-beta.3'})
    install.member(newer,name).write_bytes(b'changed')
    data=json.loads((newer/'application.json').read_bytes())
    for row in data['members']:
        if row['path']==name:row.update(bytes=7,sha256=hashlib.sha256(b'changed').hexdigest().upper())
    install.atomic(newer/'application.json',data)
    retry=tmp_path/'retry';retry.mkdir();install.make_plan(root,newer,retry)
    original=install.replace_bytes;failed=False
    def fail_manifest(path,content):
        nonlocal failed
        if Path(path)==root/'application.json' and not failed:
            failed=True;raise OSError('synthetic final write failure')
        return original(path,content)
    monkeypatch.setattr(install,'replace_bytes',fail_manifest)
    with pytest.raises(OSError,match='synthetic'):install.apply_plan(retry)
    assert install.member(root,name).read_bytes()==b'nested content'
    assert install.member(retry/'backup',name).read_bytes()==b'nested content'
    install.manifest(root)


def test_recovery_waits_for_windows_files_before_restoring(tmp_path,monkeypatch):
    root=package(tmp_path/'app','0.3.0-beta.2',{'OIF.exe':'old'})
    stage=package(tmp_path/'new','0.3.0-beta.3',{'OIF.exe':'new'})
    data=root/'.runtime';data.mkdir();work=tmp_path/'work';work.mkdir()
    install.make_plan(root,stage,work);seen=[]
    original=install.apply_plan
    monkeypatch.setattr(install,'wait_windows_files',lambda *a:seen.append('unmapped'))
    def apply(*a,**kw):
        assert seen==['unmapped'] and kw=={'recover':True}
        return original(*a,**kw)
    monkeypatch.setattr(install,'apply_plan',apply)
    assert install.run_helper(work,data,recover=True,reopen=False)['state']=='prepared'
    assert (root/'OIF.exe').read_text()=='old'


@pytest.mark.skipif(os.name!='nt',reason='Windows extended path identity')
def test_long_installation_root_prepares_and_dispatches_same_location(tmp_path,monkeypatch):
    base=tmp_path/'installation'
    while len(str(base))<205:base=base/'nested-application-folder'
    files={'OIF.exe':b'fixture','python/python.exe':b'fixture',
           'src/policy_harness/product_install.py':b'fixture'}
    package(install.filesystem_path(base,extended=True),'0.3.0-beta.2',files)
    metadata={'schema':'oif-desktop-package-v1','version':'0.3.0-beta.3','members':[
        {'path':n,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest().upper()} for n,b in files.items()]}
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as archive:
        for n,b in {**files,'application.json':json.dumps(metadata).encode()}.items():
            archive.writestr('OIF-Desktop-0.3.0-beta.3/'+n,b)
    data=raw.getvalue();manager=updates.ProductUpdates(base,base/'.runtime',downloader=lambda *_:data)
    offer={'version':'0.3.0-beta.3','url':'fixture','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest().upper()}
    install.atomic(manager.cache,{'state':'available','available':offer})
    assert manager.prepare(offer['version'])['prepared']==offer['version']
    install.atomic(install.plain(base/'.runtime/server-process.json',missing=True),
                   {'launch_id':'owned','data_dir':str(base/'.runtime'),'executable':str(base/'python/python.exe')})
    calls=[];monkeypatch.setattr(updates.subprocess,'Popen',lambda *a,**kw:calls.append(a))
    manager.install(offer['version'],'owned')
    assert len(calls)==1
    pending=install.plain(base/'.runtime/product-update-pending.json')
    assert json.loads(pending.read_bytes())['version']==offer['version']
    pending.unlink()
    work=Path(manager.prepared['work']);plan=json.loads((work/'install.json').read_bytes())
    plan['root']=str(tmp_path/'different-installation');install.atomic(work/'install.json',plan)
    with pytest.raises(ValueError,match='identity changed'):manager.install(offer['version'],'owned')
    assert len(calls)==1 and not pending.exists()
