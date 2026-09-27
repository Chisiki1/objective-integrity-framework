"""Real UI script/CLI main consumers with explicit ASGI, cipher and DOM fixtures.

These are not real model decisions, Windows DPAPI or browser-rendering evidence.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import policy_harness

from policy_harness import cli
from policy_harness.models import Completion
from tests.test_credential_recovery_continuity import harness_at
from tests.test_whole_ui_repairs import CipherFixture, NODE_UI, record_artifact


@pytest.mark.parametrize('legacy', [False, True])
def test_actual_completion_shape_is_visible_with_records_and_artifact(tmp_path, legacy):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node needed for explicit full-script DOM fixture')
    with harness_at(tmp_path/'data', CipherFixture(), True) as (client, store, engine, settings):
        task = store.create_task('Synthetic output consumer', ['Exact artifact'])
        record_artifact(store, task, 'answer.txt', b'hello')
        completion = Completion(achieved=True, acceptance=[], evidence_refs=['fixture-result'],
                                unresolved=[], summary='結果の本文 <em>文字列として表示</em>')
        final = {'summary': completion.summary} if legacy else {'completion': completion.model_dump(), 'cleanup': {'decisions': []}, 'review': {'fixture': True}}
        store.update_task(task['id'], status='completed', final=final)
        snapshot = client.get('/api/tasks/'+task['id']).json()
        responses = {'GET '+path: {'status': (r:=client.get(path)).status_code, 'body': r.json()}
                     for path in ['/api/session','/api/tasks','/api/settings','/api/status']}
        script = NODE_UI.split('(async()=>{',1)[0] + r'''
(async()=>{
 await bootReady;const h=context.hooks;h.state.taskId=input.snapshot.task.id;h.renderSnapshot(input.snapshot);
 function all(el){return [el,...el.children.filter(x=>x instanceof Element).flatMap(all)];}
 const content=all(document.getElementById('final-content'));
 const links=all(document.getElementById('operations')).filter(x=>x.tag==='a').map(x=>x.href);
 process.stdout.write(JSON.stringify({visible:!document.getElementById('final-result').hidden,
   paragraphs:content.filter(x=>x.tag==='p').map(x=>x.textContent),
   detail:content.some(x=>x.tag==='button'&&x.textContent==='終了判断・全結果'),links}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        payload={'script':str(Path(policy_harness.__file__).parent/'static/app.js'), 'responses':responses, 'snapshot':snapshot}
        result=subprocess.run([node,'-e',script],input=json.dumps(payload,ensure_ascii=False),capture_output=True,text=True,encoding='utf-8',timeout=15)
        (tmp_path/'node.stdout').write_text(result.stdout,encoding='utf-8')
        (tmp_path/'node.stderr').write_text(result.stderr,encoding='utf-8')
        assert result.returncode == 0, result.stderr
        view=json.loads(result.stdout)
        assert view['visible'] and view['paragraphs']==[completion.summary] and view['detail']
        assert len(view['links'])==1
        assert client.get(view['links'][0]).content==b'hello'
        assert store.get_task(task['id'])['final']==final


@pytest.mark.parametrize('withheld', [False, True])
@pytest.mark.parametrize('follow', [False, True])
def test_cli_main_preserves_real_asgi_admission_and_normal_follow(tmp_path, monkeypatch, capsys, withheld, follow):
    actual_client=cli.Client
    with harness_at(tmp_path, CipherFixture(), True) as (client, store, engine, settings):
        if withheld:
            engine.break_during='create'
        calls={'post':0,'stream':0,'close':0}
        receipts=[]
        class Transport:
            @contextmanager
            def stream(self, method, path):
                calls['stream']+=1
                assert not withheld
                identity=receipts[-1]['task']['id']
                assert path=='/api/tasks/'+identity+'/events'
                # Explicit finite SSE fixture; actual CLI _follow consumes it.
                class Response:
                    status_code=200
                    def iter_lines(self):
                        yield 'data: '+json.dumps({'stage':'task','status':'stopped','task_id':identity})
                yield Response()
        class Adapter:
            def __init__(self,url):
                assert url=='http://127.0.0.1:8765'
                self.client=Transport()
            def post(self,path,body):
                calls['post']+=1
                result=actual_client._result(client.post(path,json=body))
                receipts.append(result)
                return result
            def close(self):
                calls['close']+=1
        monkeypatch.setattr(cli,'Client',Adapter)
        args=['run','A single ordinary request']+(['--follow'] if follow else [])
        assert cli.main(args)==0
        captured=capsys.readouterr()
        output,_=json.JSONDecoder().raw_decode(captured.out.lstrip())
        task=receipts[0]['task']
        assert output['task_id']==task['id'] and output['status']==task['status']
        assert len(store.list_tasks())==1 and calls['post']==1 and calls['close']==1
        assert calls['stream']==int(follow and not withheld)
        assert captured.err==''
        if withheld:
            assert output['accepted'] is True and output['records_withheld'] is True
            assert output['detail']==receipts[0]['detail'] and output['action']=='create'
            assert '再送せず' in output['next_step'] and 'stop' in output['next_step']
        else:
            assert 'records_withheld' not in output and 'next_step' not in output
