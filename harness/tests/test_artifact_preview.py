"""Exercise the real preview module with response bytes from the protected endpoint.

The small DOM fixture verifies inert rendering and request isolation; the installed
browser supplies actual layout, image decoding and dialog evidence separately.
"""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import policy_harness

from tests.test_whole_ui_repairs import harness, record_artifact  # noqa: F401


MODULE = Path(policy_harness.__file__).parent / 'static/artifact-preview.js'
NODE = r'''
const fs=require('fs'),vm=require('vm'),{webcrypto}=require('crypto');
const input=JSON.parse(fs.readFileSync(0,'utf8')),nodes=new Map(),calls=[];
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.listeners={};this.hidden=false;this.isConnected=true;}
 set textContent(value){this.children=[];this.value=String(value);}
 get textContent(){return (this.value||'')+this.children.map(x=>x.textContent).join('');}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.value='';this.children=items;}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(k,v){this.listeners[k]=v;}
 showModal(){this.open=true;}
 close(){this.open=false;this.listeners.close?.();}
}
const document={createElement:t=>new Element(t),createTextNode:t=>{const n=new Element('#text');n.textContent=t;return n;},
 getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);}};
const $=document.getElementById;
class FileReader {
 async readAsDataURL(blob){this.result='data:'+blob.type+';base64,'+Buffer.from(await blob.arrayBuffer()).toString('base64');this.onload();}
}
let pending=null, reads=0,cancels=0;
function response(record){
 const bytes=Buffer.from(record.bytes||'','base64');let offset=0;
 return {ok:record.status>=200&&record.status<300,status:record.status,headers:{get:k=>record.headers[k]??null},
 body:{cancel:async()=>{cancels++;},getReader:()=>({read:async()=>{reads++;if(offset>=bytes.length)return {done:true};
 const value=bytes.subarray(offset,offset+65536);offset+=value.length;return {done:false,value};},cancel:async()=>{cancels++;},releaseLock(){}})}};
}
const context={document,URL,TextDecoder,Uint8Array,AbortController,Blob,FileReader,crypto:webcrypto,OIFI18n:{locale:'ja'},
 fetch:async(url,options)=>{calls.push({url,options});if(input.mode==='race'&&calls.length===1)return await new Promise(resolve=>pending=resolve);
 return response(input.responses?.[calls.length-1]||input.response);}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
function tree(n){return {tag:n.tag,text:n.textContent,href:n.href,src:n.src,rel:n.rel,attrs:n.attrs,children:n.children.map(tree)};}
(async()=>{
 if(input.mode==='race'){
  const old=context.OIFArtifacts.open(input.artifact);$('artifact-dialog').close();
  await context.OIFArtifacts.open({...input.artifact,...input.second});pending(response(input.response));await old;
 }else await context.OIFArtifacts.open(input.artifact);
 const rendered=tree($('artifact-preview-body'));
 if(input.mode==='broken-image')$('artifact-preview-body').children[0].onerror();
 if(input.mode==='toggle')$('artifact-view-source').onclick();
 const source=tree($('artifact-preview-body'));
 if(input.mode==='toggle')$('artifact-view-rendered').onclick();
 const back=tree($('artifact-preview-body')),open=$('artifact-dialog').open;
 if(input.mode==='close')$('artifact-close').onclick();
 console.log(JSON.stringify({rendered,source,back,open,closed:!$('artifact-dialog').open,remaining:tree($('artifact-preview-body')),
 switchHidden:$('artifact-view-switch').hidden,download:$('artifact-download').href,reads,cancels,
 calls:calls.map(c=>({url:c.url,credentials:c.options.credentials,cache:c.options.cache,aborted:c.options.signal.aborted}))}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def node_preview(body, path='report.md', *, mode='normal', status=200, headers=None, **extra):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the JavaScript consumer')
    digest = hashlib.sha256(body).hexdigest()
    url = f'/api/tasks/fixture/artifacts/{path}?operation_id=operation&sha256={digest}'
    payload = {
        'mode': mode,
        'artifact': {'url': url, 'path': path, 'operation': 'operation', 'sha256': digest},
        'response': {'status': status, 'bytes': base64.b64encode(body).decode(),
                     'headers': headers or {'X-Artifact-Operation': 'operation', 'X-Artifact-SHA256': digest,
                                           'Content-Length': str(len(body))}},
        **extra,
    }
    proc = subprocess.run([node, '-e', NODE, str(MODULE)], input=json.dumps(payload),
                          text=True, encoding='utf-8', capture_output=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def descendants(tree):
    yield tree
    for child in tree['children']:
        yield from descendants(child)


def test_real_checked_artifact_markdown_and_original(harness):
    client, store, *_ = harness
    task = store.create_task('Preview fixture', [])
    body = '# 結果\n\n| 項目 | 結果 |\n| --- | --- |\n| 内容 | **確認済み** |\n\n- ひとつ\n- ふたつ\n\n```python\nprint("hello")\n```\n'.encode()
    operation = record_artifact(store, task, '日本語 report.md', body)
    digest = hashlib.sha256(body).hexdigest()
    response = client.get(f'/api/tasks/{task["id"]}/artifacts/日本語 report.md',
                          params={'operation_id': operation, 'sha256': digest})
    assert response.status_code == 200
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['x-artifact-operation'] == operation
    assert response.headers['x-artifact-sha256'] == digest
    preview = node_preview(response.content, mode='toggle')
    tags = [node['tag'] for node in descendants(preview['rendered'])]
    assert {'h1', 'table', 'th', 'td', 'strong', 'ul', 'li', 'pre', 'code'} <= set(tags)
    assert preview['source']['text'] == body.decode()
    assert preview['rendered'] == preview['back']
    assert preview['switchHidden'] is False
    assert preview['calls'][0]['credentials'] == 'same-origin'
    assert preview['calls'][0]['cache'] == 'no-store'
    assert preview['download'] == preview['calls'][0]['url']


def test_untrusted_markdown_stays_inert_and_remote_images_are_links():
    text = '<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n\n[bad](javascript:alert) [data](data:text/html,bad) [file](file:///C:/private) ![remote](https://example.com/pixel.png) [safe](https://example.com/read)'
    result = node_preview(text.encode())
    nodes = list(descendants(result['rendered']))
    assert not {'script', 'img', 'iframe', 'object', 'style'} & {n['tag'] for n in nodes}
    assert '<script>alert(1)</script>' in result['rendered']['text']
    assert [n['href'] for n in nodes if n['tag'] == 'a'] == ['https://example.com/pixel.png', 'https://example.com/read']
    assert all(n['rel'] == 'noopener noreferrer' for n in nodes if n['tag'] == 'a')
    assert len(result['calls']) == 1


@pytest.mark.parametrize('body,path,expected', [
    (b'hello', 'answer.txt', 'hello'),
    ('日本語\n'.encode('utf-16'), 'memo.txt', '日本語\n'),
    ('日本語\n'.encode('shift_jis'), 'memo.txt', '日本語\n'),
    (b'<html><script>alert(1)</script></html>', 'page.html', '<html><script>alert(1)</script></html>'),
    (b'{"ok": true}', 'result.json', '{"ok": true}'),
])
def test_text_encodings_and_code_are_plain_text(body, path, expected):
    result = node_preview(body, path)
    assert result['rendered']['children'][0]['tag'] == 'pre'
    assert result['rendered']['text'] == expected
    assert result['switchHidden'] is True


@pytest.mark.parametrize('path,body', [('report.pdf', b'%PDF-1.7'), ('doc.docx', b'PK'), ('unknown.bin', b'\x00\x01')])
def test_unsupported_formats_have_a_download_fallback(path, body):
    result = node_preview(body, path)
    assert 'ダウンロードして開いて' in result['rendered']['text']
    assert result['download'] == result['calls'][0]['url']


@pytest.mark.parametrize('status,expected', [(401, '再読み込み'), (409, '内容が変わって'), (404, '読み込めません')])
def test_endpoint_failures_do_not_render_content(status, expected):
    result = node_preview(b'# secret content', status=status)
    assert expected in result['rendered']['text']
    assert 'secret content' not in result['rendered']['text']
    assert result['reads'] == 0


@pytest.mark.parametrize('operation,hash_value', [('different', None), ('operation', '0' * 64)])
def test_wrong_record_identity_is_rejected_before_read(operation, hash_value):
    body = b'correct'
    result = node_preview(body, headers={'X-Artifact-Operation': operation,
                                         'X-Artifact-SHA256': hash_value or hashlib.sha256(body).hexdigest()})
    assert '対応を確認できません' in result['rendered']['text']
    assert result['reads'] == 0


def test_content_digest_is_checked_before_rendering():
    body = b'changed'; digest = hashlib.sha256(b'original').hexdigest()
    result = node_preview(body, artifact={'url': '/fixture', 'path': 'report.md', 'operation': 'operation', 'sha256': digest},
                          headers={'X-Artifact-Operation': 'operation', 'X-Artifact-SHA256': digest})
    assert '内容が一致しません' in result['rendered']['text']
    assert 'changed' not in result['rendered']['text']


@pytest.mark.parametrize('declared', [True, False])
def test_large_response_is_bounded_even_without_content_length(declared):
    body = b'a' * (4 * 1024 * 1024 + 1)
    headers = {'X-Artifact-Operation': 'operation', 'X-Artifact-SHA256': hashlib.sha256(body).hexdigest()}
    if declared:
        headers['Content-Length'] = str(len(body))
    result = node_preview(body, headers=headers)
    assert 'この大きさ' in result['rendered']['text']
    assert result['cancels'] == 1
    if declared:
        assert result['reads'] == 0


def test_long_text_is_honestly_truncated_and_close_clears_it():
    result = node_preview(b'a' * 200001, 'long.txt', mode='close')
    assert len(result['rendered']['children'][0]['text']) == 200000
    assert '先頭20万文字' in result['rendered']['text']
    assert result['closed'] and not result['remaining']['text']
    assert result['calls'][0]['aborted']


def test_old_request_cannot_replace_new_preview_after_close():
    newer = b'# New report'; digest = hashlib.sha256(newer).hexdigest()
    record = {'status': 200, 'bytes': base64.b64encode(newer).decode(),
              'headers': {'X-Artifact-Operation': 'second', 'X-Artifact-SHA256': digest}}
    result = node_preview(b'# Old report', mode='race', second={'url': '/second', 'operation': 'second', 'sha256': digest},
                          responses=[None, record])
    assert 'New report' in result['rendered']['text']
    assert 'Old report' not in result['rendered']['text']
    assert result['calls'][0]['aborted']
    assert not result['calls'][1]['aborted']


def test_raster_image_and_decode_failure_fallback():
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG9sAAAAASUVORK5CYII=')
    result = node_preview(png, 'image.png', mode='broken-image')
    image = result['rendered']['children'][0]
    assert image['tag'] == 'img' and image['src'] == 'data:image/png;base64,' + base64.b64encode(png).decode()
    assert '画像を表示できません' in result['source']['text']
