'use strict';
(() => {
  const PAGE=24000,CHUNK=2048;
  // Incremental JSON formatting also bounds long individual strings. It never
  // stringifies an entire task on the UI thread just to open the first page.
  function* json(value,level=0){
    if(typeof value==='string'){
      yield '"';
      for(let i=0;i<value.length;){let end=Math.min(i+CHUNK,value.length);if(end<value.length&&/[\uD800-\uDBFF]/.test(value[end-1]))end--;
        yield JSON.stringify(value.slice(i,end)).slice(1,-1);i=end;}
      yield '"';return;
    }
    if(!value||typeof value!=='object'){yield JSON.stringify(value)??'null';return;}
    const array=Array.isArray(value),indent='  '.repeat(Math.min(level,20));yield array?'[':'{';let count=0;
    for(const key in value){if(!Object.prototype.hasOwnProperty.call(value,key))continue;
      yield (count++?',':'')+'\n'+indent+'  ';if(!array){yield* json(key);yield ': ';}yield* json(value[key],level+1);}
    if(count)yield '\n'+indent;yield array?']':'}';
  }
  function preview(value,limit=12000){
    if(typeof value==='string')return value.length<=limit?value:value.slice(0,limit)+'…';
    let text='';for(const part of json(value)){text+=part;if(text.length>limit)return text.slice(0,limit)+'…';}return text;
  }
  let serial=0,controller,reader,iterator,pages=[],page=0,remainder='',ended=false,downloadUrl=null;
  const $=id=>document.getElementById(id);
  const say=(ja,en)=>globalThis.OIFI18n?.locale==='en'?en:ja;
  function close(){serial++;controller?.abort();reader?.cancel().catch(()=>{});reader=null;iterator=null;pages=[];remainder='';if(downloadUrl)URL.revokeObjectURL(downloadUrl);downloadUrl=null;}
  function paint(){
    $('evidence-content').textContent=pages[page]||'';
    $('record-page').textContent=say('ページ ','Page ')+(page+1);
    $('record-previous').disabled=page===0;$('record-next').disabled=ended&&page===pages.length-1;
  }
  async function next(){
    if(page<pages.length-1){page++;paint();return;}
    const mine=serial;$('record-next').disabled=true;
    try{
      let text=remainder;remainder='';
      while(text.length<PAGE&&!ended){
        const part=reader?await reader.read():iterator.next();
        if(mine!==serial)return;
        ended=Boolean(part.done);if(!ended)text+=part.value;
      }
      if(text.length>PAGE){remainder=text.slice(PAGE);text=text.slice(0,PAGE);}
      if(text||!pages.length){pages.push(text);page=pages.length-1;}paint();
    }catch(error){if(mine===serial){$('evidence-content').textContent=say('記録を読み込めませんでした。','Could not load the record.')+' '+error.message;$('record-next').disabled=false;}}
  }
  async function open(value){
    close();const mine=serial;ended=false;page=0;const url=value?._record_url;
    $('record-download').hidden=true;$('evidence-content').textContent=say('記録を読み込んでいます…','Loading the record…');
    $('record-page').textContent='';$('record-previous').disabled=true;$('record-next').disabled=true;
    if(typeof url==='string'&&/^\/api\/tasks\/[a-f0-9]{32}\/records(?:\/(?:events|operations|sources)\/[A-Za-z0-9_-]+)?$/.test(url)){
      controller=new AbortController();
      try{
        const response=await fetch(url,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
        if(mine!==serial)return;if(!response.ok)throw new Error(String(response.status));
        reader=response.body.pipeThrough(new TextDecoderStream()).getReader();
        $('record-download').href=url+'?download=true';$('record-download').download='oif-record.json';$('record-download').hidden=false;
      }catch(error){if(mine===serial)$('evidence-content').textContent=say('記録を読み込めませんでした。','Could not load the record.')+' '+error.message;return;}
    }else iterator=json(value);
    if(mine===serial)await next();
  }
  function bind(){
    $('record-previous').onclick=()=>{if(page>0){page--;paint();}};$('record-next').onclick=()=>next();
    $('evidence-dialog').addEventListener('close',close);
  }
  globalThis.OIFRecords={open,close,preview,json,bind,PAGE};
})();
