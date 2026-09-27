'use strict';
// Presentation only. Stored events and the raw-record dialog stay unchanged.
globalThis.OIFPresentation = (() => {
  const en = () => globalThis.OIFI18n?.locale === 'en';
  const say = (ja, english) => en() ? english : ja;
  const tools = {
    file_read:['ファイルを読む','Read a file'], file_write:['ファイルを保存する','Save a file'],
    file_list:['ファイルを探す','List files'], exec:['プログラムを実行する','Run a program'],
    run_command:['PCでコマンドを実行する','Run a command on this PC'], run_python:['Pythonを実行する','Run Python'], run_tests:['テストする','Run tests'],
    web_fetch:['公開情報を調べる','Read public information'], capability_request:['実行環境を準備する','Prepare execution'],
    history_read:['これまでの結果を確認する','Read previous results'], knowledge_read:['過去の知識を確認する','Read task knowledge'],
    attachment_read:['添付ファイルを読む','Read an attachment'],
    harness_info:['OIFの設定を確認する','Inspect OIF settings'], harness_read:['OIFの実装を読む','Read OIF source'],
    harness_propose:['OIFの改善案を準備する','Prepare an OIF improvement'],
    harness_verify:['OIFの改善案を検証する','Verify an OIF improvement'],
    harness_apply:['OIFの改善を反映する','Apply an OIF improvement'],
    harness_rollback:['OIFの更新を元に戻す','Restore OIF before this update']
  };
  function toolName(name) { const names=tools[name]; return names?say(...names):say('操作','Operation'); }
  function friendly(value) {
    return String(value ?? '')
      .replace(/The same response error recurred; original replies are preserved: 教訓には実結果・適用条件・手順・限界・次回使う場面が必要です。/g,say('旧版の学習処理がメモの不備で作業を保留しました。修正済みのため「再開」で保存した結果から続けられます。','An older learning handler held the task because a lesson was incomplete. The handler is fixed; Resume continues from the saved results.'))
      .replace(/(?:SHA[-_ ]?256|[a-z_]*_hash)\s*[:：=]?\s*[`"']?[a-f0-9]{4,64}(?:…|\.{3})[a-f0-9]{0,64}[`"']?/gi,say('ファイルの照合情報（詳細に保存）','File fingerprint (saved in details)'))
      .replace(/(?:SHA[-_ ]?256|[a-z_]*_hash)\s*[:：=]?\s*[`"']?[a-f0-9]{64}[`"']?/gi,say('ファイルの照合情報（詳細に保存）','File fingerprint (saved in details)'))
      .replace(/SHA[-_ ]?256/gi,say('ファイルの照合情報','File fingerprint'))
      .replace(/\b(?:operation|task|call|candidate|run|source)[_-]?id\s*[:：=]?\s*[`"']?[a-f0-9]{32,64}[`"']?/gi,say('関連する作業の記録','Related work record'));
  }
  function important(event) {
    const d=event.detail||{};
    if(['failed','held','unknown','pending','error','interrupted','stopped','stop_requested','recovery_required','attention_required','configuration_required','awaiting_user','required','rejected','rejected_format','revision_required'].includes(event.status))return true;
    function adverse(value,depth=0){
      if(!value||typeof value!=='object'||depth>8)return false;
      if(['revise','hold'].includes(value.verdict)||['failed','unknown'].includes(value.success)||value.effect==='unknown'||value.achieved===false||value.unresolved?.length)return true;
      if(value.cleanup&&value.cleanup.removed!==true&&value.cleanup.reason)return true;
      return Object.values(value).some(x=>adverse(x,depth+1));
    }
    if(adverse(d))return true;
    return ['task','source','final','finalize','completion','controller_restart','recovery','policy_amendment','creation','learning','permission','context'].includes(event.stage);
  }
  function describe(event, snapshot, rawSummary='') {
    const d=event.detail||{};
    const row=(snapshot?.operations||[]).find(x=>x.operation?.id===(d.operation_id||d.operation?.id));
    const op=d.operation||row?.operation;
    const result=d.result||row?.result||(['operation','execute','execution'].includes(event.stage)?d:null);
    const status=event.status;
    let overview='',purpose='',outcome='',next='';
    if(event.stage==='context'||(event.stage==='model'&&d.phase==='context_compaction')) {
      overview=status==='started'?say('長い作業の要点を整理しています','Summarizing earlier work'):
        status==='failed'?say('要点の整理に失敗しました。元の記録は保持しています','Summary failed; original records are retained'):
        status==='interrupted'?say('要点の整理を中断しました','Summary interrupted'):
        status==='completed'?say('要点を保存し、作業を続けます','Working memory saved; continuing'):
        status==='deferred'?say('元の記録を保持して続けます','Keeping the original context'):
        say('作業の要点を受け取りました','Received the working summary');
      purpose=say('以前の結果を要約し、必要な記憶と直近の結果をモデルへ渡します。','Summarize earlier results while keeping useful memory and recent evidence.');
      outcome=say('元の指示、達成条件、権限、原記録は保持しています。','Original instructions, requirements, permissions and evidence are retained.');
      if(d.input_tokens_after!=null)outcome+=' '+say('今回の入力量の目安：','Estimated input size: ')+d.input_tokens_before+' → '+d.input_tokens_after;
    } else if(event.stage==='model') {
      const phase=d.phase||'';
      const review=phase.includes('review');
      const source=phase.includes('source');
      purpose=source?say('追加された指示を、これまでの条件と照らし合わせています。','Compare the new instruction with the existing requirements.'):
        review?say('依頼の条件を満たしているか、成果物と実際の結果を別の観点から確認します。','Check the deliverable and observed results against the request.'):
        say('依頼とこれまでの結果を読み、次に必要な操作を選びます。','Read the request and previous results to choose the next action.');
      overview=status==='started'?(source?say('追加の指示を確認しています','Checking the new instruction'):review?say('成果と条件を照合しています','Checking the result against the requirements'):say('次に行うことを検討しています','Deciding what to do next')):
        status==='interrupted'?say('判断の受信を中断しました。結果は未確認です','Response interrupted; the result is unconfirmed'):
        status==='failed'&&d.failure_kind==='response_format'?say('モデルの返答が指定の形式に合いませんでした','The model response did not match the required format'):
        d.message?friendly(d.message):status==='responded'||status==='succeeded'?say('判断の応答を受け取りました','Received the proposed next step'):say('判断の応答を確認してください','Check the model response');
      outcome=status==='started'?say('この時点では応答待ちでした。結果は後続の記録で確認できます。','At this point, a response was pending. Later records show the result.'):
        status==='responded'||status==='succeeded'?say('モデルの応答を受信しました。操作の成功や作業の完了は、後続の記録で確認します。','The model has responded. Later records show whether the action succeeded and the task finished.'):friendly(rawSummary)||say('応答を正常に確認できませんでした。原記録を保持しています。','A successful response was not confirmed. The original record is retained.');
      if(Array.isArray(d.tools)&&d.tools.length)next=d.tools.map(t=>friendly(t.purpose||toolName(t.name))).join(' / ');
      if(status==='responded'||status==='succeeded') {
        if(d.message)outcome=say('モデルが返した内容：','Model response:')+'\n'+friendly(d.message).slice(0,6000)+'\n'+outcome;
        if(d.review) {
          overview=d.review.verdict==='accept'?say('依頼の条件を満たすと判断しました','The reviewer judged the request satisfied'):say('修正が必要な点を見つけました','The reviewer found required corrections');
          outcome=overview+'。\n'+friendly(d.review.rationale)+'\n'+(d.review.findings||[]).map(x=>'・'+friendly(x)).join('\n')+'\n'+say('これはレビューの判断です。操作の実行結果は別の記録で確認できます。','This is a review judgment; execution results are recorded separately.');
        }
        if((d.message||'').length>6000||d.review?.truncated)outcome+='\n'+say('全文は「根拠と結果の全文」で確認できます。','See the original record for the complete text.');
      }
    } else if(event.stage==='learning') {
      overview=friendly(d.title||d.message||rawSummary);
      purpose=friendly(d.message||'');
      outcome=status==='used'?say('この教訓を実際の操作に使い、結果を保存しました。','Applied the lesson to a real operation and saved its result.'):
        status==='deferred'&&d.task_continues?say('学習メモは未採用で保存しました。本来の作業は続けます。次の必要な判断で、根拠を補って再検討できます。','The learning proposal was retained without adoption. Work continues; it can be corrected during the next necessary decision.'):
        status==='assessed'?say('実際の結果を踏まえて評価しました。','Assessed the observed result.'):
        status==='use_unconfirmed'?(d.use_status==='not_executed'?say('対象の操作は実行されていません。知識の使用回数には含めません。','The target action did not execute; no use is credited.'):say('対象の操作結果は未確認です。知識の効果はまだ評価できません。','The action result is unknown; its benefit cannot yet be assessed.')):
        say('次に使う場面と手順を知識として保存しました。','Saved the procedure and when to use it.');
      if(d.action==='retire')outcome=say('通常の候補から外し、過去の内容は保存しました。','Retired from selection; history is retained.');
      if(d.result_status)outcome+=' '+say('操作の結果：','Action result: ')+d.result_status;
      if(d.judgment)outcome+='\n'+purpose;
    } else if(event.stage==='permission') {
      overview=friendly(d.purpose||d.message||rawSummary);
      purpose=overview;outcome=status==='awaiting_user'?say('入力欄の上で、この操作を許可するか選べます。','Approve or deny this action above the composer.'):overview;
    } else if(op||['operation','execute','execution'].includes(event.stage)) {
      purpose=friendly(d.purpose||op?.purpose||'');
      const name=toolName(d.kind||row?.tool_name||op?.kind);
      const path=op?.args?.path;
      overview=(['failed','unknown','error','pending'].includes(status)&&rawSummary)?friendly(rawSummary):purpose||name+(path?' · '+path:'');
      if(status==='started')outcome=say('操作を開始しました。結果はまだ確認中です。','The action has started; its result is pending.');
      else if(result?.effect==='unknown'||status==='unknown')outcome=say('操作がどこまで反映されたか未確認です。再実行の前に状態の確認が必要です。','Its effects are not yet known. Check the state before repeating the action.')+'\n'+friendly(rawSummary);
      else if(status==='failed'||result?.status==='failed')outcome=friendly(result?.stderr||result?.error||rawSummary)||say('操作に失敗しました。原因の確認が必要です。','The action failed and needs investigation.');
      else if(status==='succeeded') {
        const data=result?.data||{};
        outcome=friendly(data.summary||result?.stdout||'')||say('操作が正常に終了し、結果を保存しました。','The action finished successfully and its result was saved.');
        if(data.bytes!==undefined)outcome+=say(` 対象は${data.bytes}バイトです。`,` The file contains ${data.bytes} bytes.`);
      } else outcome=friendly(rawSummary);
      const cleanup=d.cleanup||result?.data?.cleanup;
      if(cleanup&&cleanup.removed!==true&&cleanup.reason){
        const warning=say('後片付けの完了を確認できていません','Cleanup has not been confirmed');
        overview+=' · '+warning;outcome+='\n'+warning+'。'+friendly(cleanup.reason);
      }
    } else {
      overview=(event.stage==='source'?rawSummary:friendly(rawSummary))||({created:say('依頼を受け付け、作業を保存しました','Received and saved the request'),start_claimed:say('実行の準備をしています','Preparing to start the task'),started:say('この工程の確認を始めました','Started checking this step'),completed:say('依頼された作業が完了しました','The requested task is complete'),running:say('作業を進めています','Work is in progress'),stopped:say('作業を停止しました。記録は保存されています','Work stopped; records are retained'),resume_requested:say('保存した結果から作業を再開します','Resuming from saved results')}[status])||say('作業の状態を記録しました','Recorded the task state');
      outcome=overview;
    }
    return {overview,purpose,outcome,next};
  }
  return {say, toolName, friendly, important, describe};
})();
