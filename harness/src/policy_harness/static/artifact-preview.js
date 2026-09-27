'use strict';
// Checked artifact bytes become inert DOM, never document HTML or auto-fetched resources.
globalThis.OIFArtifacts = (() => {
  const $=id=>document.getElementById(id), say=(ja,en)=>globalThis.OIFI18n?.locale==='en'?en:ja;
  const element=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;};
  const MAX_BYTES=4*1024*1024, MAX_TEXT=200000;
  let serial=0,controller=null,current=null;
  function safeLink(value){try{const u=new URL(value);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:null;}catch{return null;}}
  function inline(target,text,depth=0){
    if(depth>3){target.append(document.createTextNode(text));return;}
    const pattern=/(`{1,3})([^`\n]+)\1|(!?)\[([^\[\]\n]{1,2048})\]\(([^\s)]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*/g;
    let start=0,match;
    while((match=pattern.exec(text))){
      target.append(document.createTextNode(text.slice(start,match.index)));
      if(match[1])target.append(element('code',match[2]));
      else if(match[4]){const href=safeLink(match[5]);if(href){const a=element('a',match[3]?say('画像: ','Image: ')+match[4]:match[4]);a.href=href;a.target='_blank';a.rel='noopener noreferrer';target.append(a);}else target.append(document.createTextNode(match[0]));}
      else{const e=element(match[6]||match[7]?'strong':'em');inline(e,match[6]||match[7]||match[8],depth+1);target.append(e);}
      start=pattern.lastIndex;
    }
    target.append(document.createTextNode(text.slice(start)));
  }
  const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split(/(?<!\\)\|/).map(x=>x.trim().replace(/\\\|/g,'|'));
  const tableRule=line=>line.includes('|')&&cells(line).every(x=>/^:?-{3,}:?$/.test(x));
  function markdown(text){
    const article=element('article');article.className='artifact-markdown';const lines=text.replace(/\r\n?/g,'\n').split('\n');
    for(let i=0;i<lines.length;){
      const line=lines[i],fence=line.match(/^\s*(`{3,}|~{3,})/),heading=line.match(/^ {0,3}(#{1,6})\s+(.+)/);
      if(!line.trim()){i++;continue;}
      if(fence){const body=[];i++;while(i<lines.length&&!lines[i].trim().startsWith(fence[1]))body.push(lines[i++]);if(i<lines.length)i++;const pre=element('pre');pre.append(element('code',body.join('\n')));article.append(pre);continue;}
      if(heading){const h=element('h'+heading[1].length);inline(h,heading[2]);article.append(h);i++;continue;}
      if(i+1<lines.length&&line.includes('|')&&tableRule(lines[i+1])){
        const wrap=element('div'),table=element('table'),head=element('thead'),body=element('tbody'),row=element('tr');wrap.className='artifact-table';
        for(const value of cells(line)){const c=element('th');inline(c,value);row.append(c);}head.append(row);i+=2;
        while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){const r=element('tr');for(const value of cells(lines[i++])){const c=element('td');inline(c,value);r.append(c);}body.append(r);}
        table.append(head,body);wrap.append(table);article.append(wrap);continue;
      }
      if(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)){article.append(element('hr'));i++;continue;}
      const list=line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)/);
      if(list){const ordered=Boolean(list[2]),ul=element(ordered?'ol':'ul');if(ordered)ul.setAttribute('start',list[2]);
        while(i<lines.length){const m=lines[i].match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)/);if(!m||Boolean(m[2])!==ordered)break;const li=element('li');inline(li,m[3]);ul.append(li);i++;}article.append(ul);continue;}
      if(/^\s*>/.test(line)){const quote=element('blockquote'),body=[];while(i<lines.length&&/^\s*>/.test(lines[i]))body.push(lines[i++].replace(/^\s*> ?/,''));inline(quote,body.join('\n'));article.append(quote);continue;}
      const body=[line];i++;
      while(i<lines.length&&lines[i].trim()&&!/^\s*(?:#{1,6}\s|`{3,}|~{3,}|>|[-+*]\s|\d+[.)]\s|---+$)/.test(lines[i])&&!(i+1<lines.length&&tableRule(lines[i+1])))body.push(lines[i++]);
      const p=element('p');inline(p,body.join('\n'));article.append(p);
    }
    return article;
  }
  function imageType(bytes){
    if(bytes.length>=8&&[137,80,78,71,13,10,26,10].every((n,i)=>bytes[i]===n))return 'image/png';
    if(bytes.length>=3&&bytes[0]===255&&bytes[1]===216&&bytes[2]===255)return 'image/jpeg';
    const head=String.fromCharCode(...bytes.slice(0,12));if(/^GIF8[79]a/.test(head))return 'image/gif';if(head.startsWith('RIFF')&&head.slice(8)==='WEBP')return 'image/webp';return null;
  }
  function decode(bytes){const encoding=bytes[0]===255&&bytes[1]===254?'utf-16le':bytes[0]===254&&bytes[1]===255?'utf-16be':'utf-8';try{return new TextDecoder(encoding,{fatal:true}).decode(bytes);}catch{return new TextDecoder('shift-jis',{fatal:true}).decode(bytes);}}
  function cleanup(){serial++;controller?.abort();controller=null;current=null;$('artifact-preview-body').replaceChildren();}
  function display(raw=false){
    if(!current)return;const body=$('artifact-preview-body');body.replaceChildren();
    $('artifact-view-rendered').setAttribute('aria-pressed',String(!raw));$('artifact-view-source').setAttribute('aria-pressed',String(raw));
    if(current.image){const img=element('img');img.src=current.image;img.alt=current.path;img.className='artifact-image';img.onerror=()=>{if(img.isConnected)body.textContent=say('画像を表示できませんでした。ダウンロードして確認してください。','The image could not be displayed. Download it to inspect the file.');};body.append(img);}
    else if(current.markdown&&!raw)body.append(markdown(current.text));else{const pre=element('pre',current.text);pre.className='artifact-text';body.append(pre);}
    if(current.truncated){const note=element('p',say('先頭20万文字を表示しています。全文はダウンロードできます。','Showing the first 200,000 characters. Download the file to read it all.'));note.className='muted';body.append(note);}body.scrollTop=0;
  }
  async function open(artifact){
    cleanup();const token=serial,dialog=$('artifact-dialog'),body=$('artifact-preview-body');controller=new AbortController();const signal=controller.signal;
    $('artifact-title').textContent=artifact.path;$('artifact-preview-label').textContent=say('成果物のプレビュー','Artifact preview');$('artifact-close').setAttribute('aria-label',say('閉じる','Close'));
    $('artifact-download').textContent=say('ダウンロード','Download');$('artifact-download').href=artifact.url;
    $('artifact-view-rendered').textContent=say('プレビュー','Preview');$('artifact-view-source').textContent=say('原文','Source');$('artifact-view-switch').hidden=true;$('artifact-meta').textContent='';
    body.textContent=say('読み込んでいます…','Loading…');if(!dialog.open)dialog.showModal();
    try{
      const response=await fetch(artifact.url,{credentials:'same-origin',cache:'no-store',signal});
      if(!response.ok)throw new Error(response.status===401?say('画面を再読み込みしてください。','Reload the page.'):response.status===409?say('作成後に内容が変わっています。最新の成果物を開いてください。','The file changed after it was recorded. Open the latest artifact.'):say('成果物を読み込めませんでした。','The artifact could not be loaded.'));
      if(response.headers.get('X-Artifact-Operation')!==artifact.operation||response.headers.get('X-Artifact-SHA256')!==artifact.sha256.toLowerCase())throw new Error(say('保存記録と内容の対応を確認できませんでした。','The file could not be matched to its saved record.'));
      const tooLarge=()=>new Error(say('この大きさのファイルはダウンロードして開いてください。','Download this larger file to open it.'));
      if(Number(response.headers.get('Content-Length'))>MAX_BYTES){await response.body?.cancel();throw tooLarge();}
      const reader=response.body.getReader(),chunks=[];let size=0;
      try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>MAX_BYTES)throw tooLarge();chunks.push(value);}}finally{await reader.cancel();reader.releaseLock();}
      if(token!==serial||signal.aborted)return;const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
      const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');
      if(token!==serial||signal.aborted)return;if(hash!==artifact.sha256.toLowerCase())throw new Error(say('保存記録とファイルの内容が一致しません。','The file does not match its saved content.'));
      $('artifact-meta').textContent=size<1024?size+' B':(size/1024).toLocaleString(undefined,{maximumFractionDigits:1})+' KB';const mime=imageType(bytes);
      if(mime){const image=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error(say('画像を読み込めませんでした。','The image could not be loaded.')));reader.readAsDataURL(new Blob([bytes],{type:mime}));});if(token!==serial||signal.aborted)return;current={path:artifact.path,image};}
      else{
        const unsupported=()=>new Error(say('この形式はダウンロードして開いてください。','Download this format to open it.'));
        if(/\.(?:pdf|docx?|xlsx?|pptx?|zip|exe|dll)$/i.test(artifact.path))throw unsupported();
        let text;try{text=decode(bytes);}catch{throw unsupported();}if(/[\x00-\x08\x0e-\x1f]/.test(text))throw unsupported();
        current={path:artifact.path,text:text.slice(0,MAX_TEXT),truncated:text.length>MAX_TEXT,markdown:/\.(?:md|markdown)$/i.test(artifact.path)};$('artifact-view-switch').hidden=!current.markdown;
      }
      display();
    }catch(error){if(token===serial&&!signal.aborted){controller.abort();body.textContent=error.message||say('プレビューを開けませんでした。','The preview could not be opened.');}}
  }
  $('artifact-close').onclick=()=>$('artifact-dialog').close();$('artifact-dialog').addEventListener('close',cleanup);
  $('artifact-view-rendered').onclick=()=>display();$('artifact-view-source').onclick=()=>display(true);
  return {open,markdown};
})();
