// Server-backed local history. Never replaces or clears the live transcript.
async function historyRequest(url, options = {}) {
  const response = await fetch(url, { ...options, signal: AbortSignal.timeout(15000) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

(() => {
  const $ = id => document.getElementById(id);
  const dialog = $('historyDialog');
  let current = null, request = 0, selected = null, deleteArmed = false;
  function armDelete(on) {
    deleteArmed = on;
    $('historyDelete').textContent = on ? '确认删除' : '删除记录';
    $('historyDelete').dataset.armed = String(on);
    $('historyDelete').setAttribute('aria-expanded', String(on));
    $('historyDeleteCancel').hidden = !on;
  }
  const date = t => new Date(t).toLocaleString('zh-CN', {hour12:false});
  const clock = t => new Date(t).toLocaleTimeString('zh-CN', {hour12:false});
  const duration = ms => {
    const seconds = Math.max(0, Math.round(ms / 1000));
    return `${Math.floor(seconds / 3600)}小时${Math.floor(seconds % 3600 / 60)}分${seconds % 60}秒`;
  };
  function text() {
    return current.segments.map(s => `${clock(s.at)}  ${s.channel === 'mic' ? '我' : '对方'}：${s.text}`).join('\n');
  }
  function mode(detail) {
    armDelete(false);
    $('historyBack').hidden = !detail;
    $('historyList').hidden = detail;
    $('historyDetail').hidden = !detail;
  }
  async function list() {
    selected = null;
    const token = ++request;
    mode(false);$('historyTitle').textContent = '历史转录';
    $('historyList').replaceChildren();$('historyMessage').textContent = '正在读取本地记录…';
    try {
      const rows = await historyRequest('/api/history');
      if (token !== request) return;
      $('historyMessage').textContent = rows.length
        ? '全部记录按开始时间倒序显示。本机磁盘保存，无自动过期；清理浏览器缓存不会删除历史。重要记录请另行导出备份。'
        : '还没有历史记录。从本次更新后的下一次录音开始自动保存；旧字幕仍保留在主页。';
      for (const row of rows) {
        const item = document.createElement('article'); item.className = 'history-item';
        const info = document.createElement('div');
        const title = document.createElement('h3');title.textContent = date(row.started);
        const note = document.createElement('p');
        note.textContent = row.ended
          ? `录音时长 ${duration(row.ended - row.started)} · ${row.segments} 个已保存片段`
          : `未结束或异常中断 · ${row.segments} 个已保存片段${row.last_audio_end ? ' · 已保存音频覆盖至 '+clock(row.last_audio_end) : ''}`;
        const button = document.createElement('button');button.className='tbtn';button.textContent='查看历史转录结果';
        button.onclick = () => detail(row.id);
        info.append(title,note);item.append(info,button);$('historyList').append(item);
      }
    } catch(err) { if(token===request)$('historyMessage').textContent = `读取失败：${err.message}。请确认已重启新版 server.py，再点刷新。`; }
  }
  async function detail(id) {
    selected=id;const token=++request;
    mode(true);current=null;$('historyCopy').disabled=true;$('historyExport').disabled=true;$('historyDelete').disabled=true;
    $('historyContent').replaceChildren();$('historyMessage').textContent='正在读取转写结果…';
    try {
      const row=await historyRequest('/api/history/'+encodeURIComponent(id));
      if(token!==request)return;
      row.segments = row.segments.filter(s => typeof s.text === 'string' && s.text.trim());
      current=row;$('historyTitle').textContent=date(row.started);
      $('historyDelete').disabled = typeof state !== 'undefined' && state.sessionId === row.id;
      $('historyDelete').title = $('historyDelete').disabled ? '请先停止这条记录的录音' : '删除本地历史记录，无法在界面撤销';
      $('historyMessage').textContent=`${row.ended ? '录音时长 '+duration(row.ended-row.started) : '录音未结束或异常中断'}。按原始切片时间显示；停止录音后，排队结果仍会继续写入，可点刷新查看。`;
      for(const s of row.segments){
        const item=document.createElement('article');item.className='history-segment';
        const meta=document.createElement('small');meta.textContent=`${clock(s.at)}  ${s.channel==='mic'?'我':'对方'}`;
        const p=document.createElement('p');p.textContent=s.text;
        item.append(meta,p);$('historyContent').append(item);
      }
      if(!row.segments.length)$('historyContent').textContent='暂无已保存的转写结果。';
      $('historyCopy').disabled=$('historyExport').disabled=!row.segments.length;
    }catch(err){if(token===request)$('historyMessage').textContent=`读取失败：${err.message}`;}
  }
  $('historyDeleteCancel').onclick=()=>{armDelete(false);$('historyDelete').focus();};
  dialog.addEventListener('cancel',e=>{if(deleteArmed){e.preventDefault();armDelete(false);$('historyDelete').focus();}});
  $('historyDelete').onclick=async()=>{
    if(!current)return;
    if(typeof state !== 'undefined' && state.sessionId === current.id){
      $('historyMessage').textContent='请先停止这条记录的录音，再删除。';return;
    }
    if(!deleteArmed){armDelete(true);$('historyMessage').textContent='确认删除整条本地历史？此操作无法在界面撤销，已导出的 TXT 不受影响。';return;}
    const id=current.id;
    $('historyDelete').disabled=true;
    $('historyDeleteCancel').disabled=true;
    try {
      await historyRequest('/api/history/'+encodeURIComponent(id),{method:'DELETE'});
      if(selected===id){current=null;await list();}
    }catch(err){if(selected===id)$('historyMessage').textContent=`删除失败：${err.message}。记录未确认删除，请刷新检查。`;}
    finally {armDelete(false);$('historyDelete').disabled=!current;$('historyDeleteCancel').disabled=false;}
  };
  $('historyBtn').onclick=()=>{dialog.showModal();list();};
  $('historyClose').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>{request++;$('historyBtn').focus();});
  $('historyBack').onclick=list;
  $('historyRefresh').onclick=()=>selected?detail(selected):list();
  $('historyCopy').onclick=async()=>{if(!current)return;try{await navigator.clipboard.writeText(text());$('historyMessage').textContent='已复制全文。';}catch{$('historyMessage').textContent='复制被浏览器拒绝，请使用「保存为 TXT」。';}};
  $('historyExport').onclick=()=>{
    if(!current)return;
    const url=URL.createObjectURL(new Blob([`转录记录 ${date(current.started)}\n\n${text()}\n`],{type:'text/plain;charset=utf-8'}));
    const a=document.createElement('a');a.href=url;a.download=`转录-${current.id}.txt`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
})();
