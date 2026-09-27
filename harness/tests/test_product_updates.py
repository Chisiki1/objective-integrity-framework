"""Optional releases: verification, preservation, rollback and idle service controls."""
import hashlib
import io
import json
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
