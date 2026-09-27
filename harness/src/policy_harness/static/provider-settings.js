/* Provider secrets remain on the local server. This view receives only labels. */
'use strict';
async function loadProviderProfiles(){
  const result=await api('/api/settings/profiles'), select=$('provider-profile');
  select.replaceChildren(new Option(tr('現在の設定'),''));
  for(const profile of result.profiles)select.add(new Option(profile.name+' · '+(profile.model||tr('モデルを選択')),profile.id));
  select.value=result.active||'';
  $('profile-use').disabled=$('profile-delete').disabled=!select.value;
}
function collectProviderSettings(){
  const form=$('settings-form'),values={};
  for(const name of ['base_url','model','review_model','api_mode','web_provider'])values[name]=form.elements[name].value;
  for(const name of ['reasoning_effort','web_base_url'])values[name]=form.elements[name].value||null;
  for(const name of ['model_context_tokens','max_output_tokens'])values[name]=form.elements[name].value?Number(form.elements[name].value):null;
  for(const name of ['model_api_key','web_api_key'])if(form.elements[name].value)values[name]=form.elements[name].value;
  if(state.settingsView?.auth_method==='openrouter_oauth'&&values.base_url!=='https://openrouter.ai/api/v1')values.auth_method='api_key';
  return values;
}
function providerError(error){state.settingsError=error.message;$('settings-error').textContent=error.message;}
$('provider-profile').onchange=()=>{$('profile-use').disabled=$('profile-delete').disabled=!$('provider-profile').value;};
$('profile-use').onclick=async()=>{try{
  await api('/api/settings/profiles',{method:'POST',body:JSON.stringify({action:'activate',id:$('provider-profile').value})});
  await showSettings();
}catch(error){providerError(error);}};
$('profile-delete').onclick=async()=>{try{
  await api('/api/settings/profiles',{method:'POST',body:JSON.stringify({action:'delete',id:$('provider-profile').value})});
  await loadProviderProfiles();
}catch(error){providerError(error);}};
$('profile-save').onclick=async()=>{const button=$('profile-save');button.disabled=true;try{
  const name=$('profile-name').value.trim();if(!name)throw new Error(tr('接続名を入力してください。'));
  await api('/api/settings',{method:'POST',body:JSON.stringify(collectProviderSettings())});
  await api('/api/settings/profiles',{method:'POST',body:JSON.stringify({action:'save',name})});
  $('profile-name').value='';await showSettings();
}catch(error){providerError(error);}finally{button.disabled=false;}};
$('openrouter-login').onclick=async()=>{
  const popup=window.open('about:blank','_blank');if(popup&&!popup.closed)popup.opener=null;
  try{
    const result=await api('/api/settings/login/openrouter',{method:'POST',body:'{}'});
    if(popup&&!popup.closed){
      try{popup.location.replace(result.url);return;}catch(error){/* Retain the explicit link if the reserved window can no longer navigate. */}
    }
    const link=node('a','',tr('OpenRouterのログイン画面を開く'));link.href=result.url;link.target='_blank';link.rel='noopener noreferrer';$('settings-error').replaceChildren(link);
  }catch(error){if(popup&&!popup.closed)popup.close();providerError(error);}
};
