"""Run the real composer and creation-recovery scripts together with API receipts."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from tests.test_creation_recovery import creation
from tests.test_ui_preferences import DOM_RUNNER, MarkedElements


def full_dom(client, receipt, mode):
    node=shutil.which('node')
    if not node:pytest.skip('Node required for real UI JavaScript consumer')
    runner=DOM_RUNNER.replace("this.classList={add(){},toggle(){}};", "this.style={setProperty(){}};this.options=['workspace','ask','read_only','full'].map(value=>({value}));this.classList={add(){},toggle(){},remove(){}};")
    runner=runner.replace("close(){this.open=false;}","closest(){return this;}querySelector(){return this;}close(){this.open=false;this.listeners.close?.();}")
    runner=runner.replace("window:{addEventListener(name,fn)","window:{matchMedia:()=>({matches:true}),addEventListener(name,fn)")
    runner=runner.replace("const h=context.hooks;", """
 document.addEventListener=()=>{};document.querySelector=()=>document.getElementById('layout');document.getElementById('layout').clientWidth=1200;
 for(const id of ['new-access','task-access'])document.getElementById(id).value='workspace';
 vm.runInContext(input.workspace,context);
 const h=context.hooks;
""")
    runner=runner.replace(" if(input.mode==='creation-loss'||input.mode==='creation-storage'){", """
 if(input.mode.startsWith('full-')){
  h.state.csrf='fixture-csrf';const get=id=>document.getElementById(id);
  async function grant(){get('new-access').value='full';const change=get('new-access').onchange();
   if(!get('full-access-dialog').open||!get('full-access-allow').disabled)throw Error('Missing confirmation');
   get('full-access-text').value='wrong';get('full-access-text').oninput();if(!get('full-access-allow').disabled)throw Error('Wrong text allowed');
   get('full-access-text').value='FULL ACCESS';get('full-access-text').oninput();
   get('full-access-form').onsubmit({preventDefault(){}});await change;}
  await grant();get('objective').value='first full task';
  const send=get('task-form').listeners.submit({preventDefault(){}});await r.posted;
  r.pending.shift()(input.mode==='full-normal'?input.createReceipt:{transportError:true});await send;
  if(input.mode!=='full-normal'){
   const entry=h.pendingSubmissions()[0];if(!entry)throw Error('Original unknown submission lost');
   if(input.mode==='full-newer-draft'){
    get('new-task').onclick();get('new-task').listeners.click();await grant();
   }
   await h.recoverCreation(entry,{open:input.mode!=='full-newer-draft'});
  }
  const next=await r.context.OIFWorkspace.prepareCreate();
  const beforeNew=next.data.access_mode;
  get('new-task').onclick();get('new-task').listeners.click();
  const fresh=await r.context.OIFWorkspace.prepareCreate();
  get('new-access').value='full';const cancel=get('new-access').onchange();
  const needsConfirmation=get('full-access-dialog').open;get('full-access-cancel').onclick();await cancel;
  const afterCancel=await r.context.OIFWorkspace.prepareCreate();
  process.stdout.write(JSON.stringify({calls:r.calls,beforeNew,fresh:fresh.data,needsConfirmation,afterCancel:afterCancel.data,session}));return;
 }
 if(input.mode==='creation-loss'||input.mode==='creation-storage'){
""")
    static={name:client.get('/static/'+name+'.js').text for name in ['app','i18n','presentation','conversation','workspace']}
    data={**static,'records':client.get('/static/record-view.js').text,'marked':MarkedElements(client.get('/').text).items,
          'mode':mode,'initial':receipt,'created':receipt,'createReceipt':receipt}
    actual=subprocess.run([node,'-e',runner],input=json.dumps(data,ensure_ascii=False),capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert actual.returncode==0,actual.stderr
    return json.loads(actual.stdout)


@pytest.mark.parametrize('mode',['full-normal','full-response-loss','full-newer-draft'])
def test_full_confirmation_consumed_by_normal_and_recovered_receipts(creation,mode):
    client,store,_,_=creation
    receipt=client.post('/api/tasks',json={'objective':'first full task','access_mode':'full','confirm_full_access':True}).json()
    # The UI consumes a real accepted task receipt; transport loss is injected only
    # after that actual acceptance, retaining the submission lookup identity.
    actual=full_dom(client,receipt,mode)
    posts=[json.loads(c['options']['body']) for c in actual['calls'] if c['options'].get('method')=='POST']
    assert len(posts)==1 and posts[0]['access_mode']=='full' and posts[0]['confirm_full_access'] is True
    assert actual['beforeNew']==('full' if mode=='full-newer-draft' else 'workspace')
    assert actual['fresh']['access_mode']==actual['afterCancel']['access_mode']=='workspace'
    assert 'confirm_full_access' not in actual['fresh'] and 'confirm_full_access' not in actual['afterCancel']
    assert actual['needsConfirmation']
