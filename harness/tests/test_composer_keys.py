"""Keyboard events reach the same two submission forms, with IME and busy guards."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed for served JavaScript consumer checks')
def test_composer_keyboard_consumer():
    source = Path(__file__).parents[1] / 'src/policy_harness/static/composer.js'
    runner = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
function open(saved,broken=false){
 const storage={...saved}, elements={};
 function element(){return {listeners:{},value:'',disabled:false,readOnly:false,addEventListener(k,f){this.listeners[k]=f;}};}
 for(const id of ['objective','instruction-text','send-key'])elements[id]=element();
 for(const id of ['objective','instruction-text'])elements[id].form={count:0,busy:false,requestSubmit(){this.count++;},querySelector(){return this.busy?{}:null;}};
 const context={document:{getElementById:id=>elements[id]},localStorage:{getItem:k=>{if(broken)throw Error('storage');return storage[k];},setItem:(k,v)=>{if(broken)throw Error('storage');storage[k]=v;}}};
 vm.createContext(context);vm.runInContext(source,context);
 function key(id,values={}){let prevented=false;elements[id].listeners.keydown({key:'Enter',preventDefault(){prevented=true;},...values});return prevented;}
 return {elements,storage,key,context};
}
const a=open({});
for(const id of ['objective','instruction-text']){
 const input=a.elements[id],form=input.form;
 assert.equal(a.key(id),true);assert.equal(form.count,1);
 assert.equal(a.key(id,{shiftKey:true}),false);assert.equal(a.key(id,{altKey:true}),false);
 assert.equal(a.key(id,{isComposing:true}),false);assert.equal(a.key(id,{keyCode:229}),false);
 input.listeners.compositionstart();assert.equal(a.key(id),false);input.listeners.compositionend();
 assert.equal(form.count,1);
 assert.equal(a.key(id,{repeat:true}),true);assert.equal(form.count,1);
 form.busy=true;assert.equal(a.key(id),true);assert.equal(form.count,1);form.busy=false;
 input.disabled=true;assert.equal(a.key(id),false);input.disabled=false;
 input.readOnly=true;assert.equal(a.key(id),false);input.readOnly=false;
 assert.equal(a.key(id,{ctrlKey:true}),true);assert.equal(form.count,2);
}
a.elements['send-key'].value='ctrl-enter';a.elements['send-key'].listeners.change();
assert.equal(a.storage['oif.sendKey'],'ctrl-enter');
for(const id of ['objective','instruction-text']){
 assert.equal(a.key(id),false);assert.equal(a.key(id,{ctrlKey:true}),true);
 assert.equal(a.key(id,{metaKey:true}),true);assert.equal(a.elements[id].form.count,4);
}
const b=open(a.storage);assert.equal(b.elements['send-key'].value,'ctrl-enter');assert.equal(b.key('objective'),false);
b.elements['send-key'].value='enter';b.elements['send-key'].listeners.change();assert.equal(b.key('objective'),true);
assert.equal(open({'oif.sendKey':'invalid'}).key('objective'),true);
assert.equal(open({},true).key('objective'),true);
console.log(JSON.stringify({forms:2,preferences:true,composition:true,busy:true}));
'''
    result = subprocess.run([shutil.which('node'), '-e', runner, str(source)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == dict(forms=2, preferences=True, composition=True, busy=True)
