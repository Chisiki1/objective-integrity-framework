'use strict';
// Read-only projections of saved sources and events. No model calls or record writes.
(() => {
  const sourceOrder=(a,b)=>Number.isInteger(a._index)&&Number.isInteger(b._index)?a._index-b._index:Date.parse(a.created_at)-Date.parse(b.created_at);
  function project(task, input) {
    const events=[...input].sort((a,b)=>a.seq-b.seq), sources=[...(task?.source_history||[])], known=new Set(sources.map(s=>s.id)), receipts=new Map();
    // A source receipt can arrive over SSE before the refreshed task snapshot.
    let nextIndex=sources.reduce((n,s)=>Math.max(n,s._index??-1),sources.length-1);
    for(const event of events)if(event.stage==='source'&&event.detail?.source?.id){
      const source=event.detail.source;if(event.status==='received')receipts.set(source.id,event.seq);
      if(!known.has(source.id)){sources.push({...source,_index:++nextIndex});known.add(source.id);}
    }
    sources.sort(sourceOrder);
    const instructions=sources.filter(s=>s.kind!=='attachment'&&!s.filename);
    if(!instructions.length)instructions.push({id:task?.id+':initial',text:task?.objective||'',kind:'initial',created_at:task?.created_at});
    const groups=instructions.map((source,index)=>({id:source.id,source,index:source._index??index,events:[],final:source._final||null,finalRecord:source._final_record_url,
      start:receipts.get(source.id)??source._event_seq??null}));
    let ownerIndex=0;
    for(const event of events){
      while(ownerIndex+1<groups.length){const next=groups[ownerIndex+1];
        if(next.start!==null?event.seq<next.start:Date.parse(event.created_at)<Date.parse(next.source.created_at))break;
        ownerIndex++;
      }
      const owner=groups[ownerIndex];
      if(owner.index>0&&(owner.start!==null?event.seq<owner.start:Date.parse(event.created_at)<Date.parse(owner.source.created_at)))continue;
      owner.events.push(event);
      if(event.stage==='task'&&event.status==='completed'){owner.final=event.detail;owner.finalRecord=event._record_url;}
    }
    const latest=groups.at(-1);
    if(task?.final&&latest.source.status!=='pending'&&!latest.final)latest.final=task.final;
    return groups;
  }
  function reconcile(previous,groups){
    const latest=groups.at(-1)?.id;
    const view=previous||{latest:null,expanded:new Set(),open:new Map()};
    if(latest!==view.latest){
      view.expanded.clear();view.open.clear();
      if(latest)view.expanded.add(latest);
      view.latest=latest;
      // Both filter views start by following a newly received instruction.
      view.resetFollow=true;
    }
    return view;
  }
  function collapse(view,group){
    view.expanded.delete(group.id);
    for(const event of group.events)view.open.delete(event.seq);
  }
  function artifacts(operations,valid=()=>true){
    const seen=new Set(),latestPaths=new Set(),result=[];
    // Prefer the operation that created/changed the artifact to later readbacks.
    const items=[];
    (operations||[]).forEach((operation,index)=>{
      for(const artifact of operation.result?.artifacts||[])if(!['stdout','stderr'].includes(artifact.channel)&&valid(artifact))items.push({artifact,operation,index});
    });
    const writes=new Map();
    for(const item of items)if(['file_write','file_edit'].includes(item.operation.tool_name||item.operation.operation?.kind))writes.set(item.artifact.relative_path+'|'+item.artifact.sha256,item);
    for(const item of items.reverse()){
      const key=item.artifact.relative_path+'|'+item.artifact.sha256;
      if(seen.has(key))continue;seen.add(key);result.push(writes.get(key)||item);
    }
    result.sort((a,b)=>b.index-a.index);
    for(const item of result){item.latest=!latestPaths.has(item.artifact.relative_path);latestPaths.add(item.artifact.relative_path);}
    return result;
  }
  globalThis.OIFConversation={project,reconcile,collapse,artifacts,sourceOrder};
})();
