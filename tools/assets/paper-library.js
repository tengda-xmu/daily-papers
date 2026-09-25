(() => {
  'use strict';
  if (document.documentElement.classList.contains('mobile-public')) return;
  const $ = s => document.querySelector(s), base = 'http://127.0.0.1:43127';
  const library = $('#paper-library'), backup = $('#library-backup-controls');
  const cards = [...document.querySelectorAll('.paper[data-paper-id]')].filter(c=>/^[a-f0-9]{12}$/.test(c.dataset.paperId));
  if (!library && !backup && !cards.length) return;
  const storage = location.origin === base ? 'localStorage' : 'sessionStorage';
  const sessionKey = 'daily-papers-codex-session', browserKey = 'daily-papers-codex-browser';
  const labels = ['尚未评价','留作参考','一般关注','值得阅读','重点阅读','必读／关键参考'];
  const read = (kind,key) => {try{return window[kind].getItem(key)||'';}catch{return '';}};
  const write = (kind,key,value) => {try{window[kind].setItem(key,value);}catch{}};
  let token = read(storage,sessionKey), connected = false, page = 1, totalPages = 1, restorePromise, refreshTimer, generation = 0, transfer = '';
  const ratings = new Map(), controls = new Map();
  const connection = document.createElement('div'); connection.className='library-connection';
  connection.innerHTML='<p class="library-connection-label">连接本机后可读取文献库和保存星级</p><button type="button" class="text-button library-connect">连接本机</button><form class="library-pair" hidden><label>配对码 <input type="password" autocomplete="off" required></label><label><input type="checkbox" checked>记住浏览器</label><button type="submit" class="button-link">连接</button><a href="http://127.0.0.1:43127/" target="_blank" rel="noopener">打开本机助手</a></form>';
  const notice = document.createElement('p');notice.className='library-status';notice.setAttribute('role','status');notice.setAttribute('aria-live','polite');
  (library || backup || $('#reading')).prepend(connection,notice);
  function status(text,error=false){notice.textContent=text;notice.dataset.error=String(error);}
  function connectionState(ok){connected=ok;connection.querySelector('.library-connection-label').textContent=ok?'已连接本机 · 星级与 PDF 长期保存':'尚未连接本机 · 保存操作需要连接';connection.querySelector('.library-connect').textContent=ok?'刷新':'连接本机';if(ok)connection.querySelector('form').hidden=true;}
  async function api(path,options={},retry=true){
    token=read(storage,sessionKey)||token;
    let response;
    try{response=await fetch(base+path,{...options,headers:{Authorization:'Bearer '+token,...(options.body && !(options.body instanceof FormData)?{'Content-Type':'application/json'}:{})},signal:AbortSignal.timeout(path.includes('backup')||path.includes('restore')?180000:20000)});}
    catch{connectionState(false);throw new Error('无法连接本机助手，请启动后重试。');}
    if(response.status===401 && retry && !path.startsWith('/api/session/') && path!=='/api/pair'){
      if(!restorePromise)restorePromise=(async()=>{const credential=read('localStorage',browserKey);if(!credential)throw new Error('首次使用请粘贴本机助手的配对码。');const data=await api('/api/session/restore',{method:'POST',body:JSON.stringify({device_token:credential})},false);token=data.token;write(storage,sessionKey,token);})().finally(()=>restorePromise=null);
      try{await restorePromise;return await api(path,options,false);}catch(error){connectionState(false);connection.querySelector('form').hidden=false;throw error;}
    }
    if(!response.ok){const data=await response.json().catch(()=>({}));const error=new Error(data.message || (typeof data.detail==='string'?data.detail:'操作未完成，请重试。'));error.code=response.status;throw error;}
    connectionState(true);
    return options.blob ? response.blob() : response.json();
  }
  function notifyRatings(){document.dispatchEvent(new CustomEvent('paper-ratings-updated'));}
  function drawRating(pid){
    const value=ratings.get(pid);
    for(const root of controls.get(pid)||[]){
      root.querySelectorAll('[data-rating]').forEach(b=>{const n=Number(b.dataset.rating);b.classList.toggle('filled',n<=(value?.rating||0));b.setAttribute('aria-pressed',String(n===value?.rating));});
      root.querySelector('.rating-label').textContent=value?labels[value.rating]:'连接后标星';
      root.querySelector('.rating-clear').hidden=!value?.rating;
      const card=root.closest('[data-paper-id]');if(card)card.dataset.rating=String(value?.rating || 0);
    }
  }
  function addRating(parent,pid){
    const root=document.createElement('div');root.className='paper-rating';
    const stars=document.createElement('div');stars.className='rating-stars';stars.setAttribute('role','group');stars.setAttribute('aria-label','论文重要性');
    for(let n=1;n<=5;n++){const button=document.createElement('button');button.type='button';button.textContent='★';button.dataset.rating=n;button.setAttribute('aria-label',`${n} 星：${labels[n]}`);button.title=`${n} 星 · ${labels[n]}`;stars.append(button);}
    stars.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const nodes=[...stars.children],i=nodes.indexOf(document.activeElement);nodes[e.key==='Home'?0:e.key==='End'?4:Math.max(0,Math.min(4,i+(e.key==='ArrowRight'?1:-1)))].focus();}});
    const label=document.createElement('span');label.className='rating-label';
    const clear=document.createElement('button');clear.type='button';clear.className='text-button rating-clear';clear.textContent='取消标星';clear.dataset.rating='0';
    root.append(stars,label,clear);parent.append(root);controls.set(pid,[...(controls.get(pid)||[]),root]);drawRating(pid);
    root.onclick=async e=>{const button=e.target.closest('[data-rating]');if(!button||button.disabled)return;root.querySelectorAll('button').forEach(b=>b.disabled=true);
      try{if(!ratings.has(pid)){const data=await api('/api/library/ratings?ids='+pid);ratings.set(pid,data.ratings[pid]);}
        const data=await api(`/api/library/papers/${pid}/rating`,{method:'POST',body:JSON.stringify({rating:Number(button.dataset.rating),revision:ratings.get(pid).revision})});
        ratings.set(pid,data);drawRating(pid);notifyRatings();status(data.rating?`${data.rating} 星 · ${labels[data.rating]}，已保存到本机`:'已取消标星，已保存的 PDF 保留');
        if(library)await refresh();
      }catch(error){status(error.message,true);if(error.code===409)await refreshRatings();}finally{root.querySelectorAll('button').forEach(b=>b.disabled=false);}};
  }
  async function refreshRatings(){
    const ids=[...controls.keys()];if(!ids.length)return;
    const data=await api('/api/library/ratings?ids='+ids.join(','));for(const[id,value]of Object.entries(data.ratings)){ratings.set(id,value);drawRating(id);}notifyRatings();
  }
  cards.forEach(card=>addRating(card.querySelector('.paper-rating-slot'),card.dataset.paperId));
  function element(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
  async function refresh(){
    if(!library){await refreshRatings();if(backup && !cards.length)await api('/api/library/ratings');return;}
    const current=++generation, params=new URLSearchParams({q:$('#library-query').value,min_rating:$('#library-rating').value,topic:$('#library-topic').value,sort:$('#library-sort').value,translated:$('#library-translated').checked,page,page_size:$('#library-page-size').value});
    const data=await api('/api/library?'+params);if(current!==generation)return;
    const topics=$('#library-topic'),chosen=topics.value;topics.replaceChildren(new Option('全部方向',''));for(const t of data.topics)topics.add(new Option(data.topic_labels?.[t]||t,t));topics.value=chosen;
    page=data.page;totalPages=Math.max(1,Math.ceil(data.total/data.page_size));$('#library-count').textContent=`${data.total} 篇文献`;$('#library-page').textContent=`第 ${page} / ${totalPages} 页`;$('#library-previous').disabled=page<=1;$('#library-next').disabled=page>=totalPages;
    $('#library-empty').hidden=Boolean(data.total);$('#library-list').replaceChildren();controls.clear();
    for(const item of data.papers){
      ratings.set(item.id,{rating:item.rating,revision:item.revision});
      const card=element('article','library-paper');card.dataset.paperId=item.id;
      const title=element('h2','',item.title_zh||item.title),meta=element('p','bibliography',[...(Array.isArray(item.authors)?item.authors.slice(0,3):[]),item.venue,item.published_at?.slice(0,10)].filter(Boolean).join(' · '));
      const description=[item.has_translation?'译文已保存':item.pdf_count?'原文已保存':'尚未载入 PDF',item.incomplete_translation?'有未完成翻译，可继续':'',item.missing_files?`${item.missing_files} 个文件缺失，可从备份恢复`:'',...item.topics.map(t=>data.topic_labels?.[t]||t)].filter(Boolean).join(' · ');
      const actions=element('div','paper-tools'),open=element('button','text-button codex-entry',item.pdf_count?'继续阅读':'打开论文对话');open.type='button';open.dataset.paperId=item.id;open.dataset.paperTitle=item.title_zh||item.title;open.dataset.openReader=String(Boolean(item.pdf_count));actions.append(open);
      const remove=element('button','text-button library-delete','删除本机资料');remove.type='button';remove.onclick=async()=>{if(!confirm('删除这篇论文的本机星级、所有 PDF、批注和对话？此操作无法撤销，建议先备份。'))return;remove.disabled=true;try{await api('/api/library/papers/'+item.id,{method:'DELETE'});await refresh();status('本机资料已删除');}catch(error){status(error.message,true);remove.disabled=false;}};
      const heading=element('div','paper-heading'),main=element('div','paper-heading-main'),rating=element('div','paper-rating-slot');
      main.append(title,meta);heading.append(main,rating);actions.append(remove);card.append(heading,element('p','library-paper-status',description),actions);addRating(rating,item.id);$('#library-list').append(card);
    }
  }
  connection.querySelector('.library-connect').onclick=()=>refresh().catch(error=>{connection.querySelector('form').hidden=false;status(error.message,true);});
  connection.querySelector('form').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,button=form.querySelector('button');button.disabled=true;
    try{const data=await api('/api/pair',{method:'POST',body:JSON.stringify({code:form.querySelector('input[type=password]').value.trim(),remember:form.querySelector('input[type=checkbox]').checked})});token=data.token;write(storage,sessionKey,token);if(data.device_token)write('localStorage',browserKey,data.device_token);form.querySelector('input[type=password]').value='';await refresh();status('已连接本机');document.dispatchEvent(new CustomEvent('paper-library-connected'));}catch(error){status(error.message,true);}finally{button.disabled=false;}};
  if(library){
    const filters=FilterPanels.create({button:$('#library-filter-toggle'),panel:$('#library-filter-panel'),chips:$('#library-filter-chips')});
    function filterState(){
      filters.refresh();
      $('#library-reset').hidden=!$('#library-query').value && !$('#library-topic').value && $('#library-rating').value==='0' && $('#library-sort').value==='recent' && !$('#library-translated').checked && $('#library-page-size').value==='20';
    }
    $('#library-filters').onsubmit=e=>e.preventDefault();
    $('#library-reset').onclick=()=>{
      $('#library-query').value='';$('#library-topic').value='';$('#library-rating').value='0';
      $('#library-sort').value='recent';$('#library-translated').checked=false;$('#library-page-size').value='20';
      clearTimeout(refreshTimer);page=1;filterState();refresh().catch(e=>status(e.message,true));
    };
    for(const id of ['library-topic','library-rating','library-sort','library-translated','library-page-size'])$('#'+id).onchange=()=>{page=1;filterState();refresh().catch(e=>status(e.message,true));};
    $('#library-query').oninput=()=>{clearTimeout(refreshTimer);page=1;filterState();refreshTimer=setTimeout(()=>refresh().catch(e=>status(e.message,true)),250);};
    filterState();
    $('#library-previous').onclick=()=>{page=Math.max(1,page-1);refresh().catch(e=>status(e.message,true));};$('#library-next').onclick=()=>{page=Math.min(totalPages,page+1);refresh().catch(e=>status(e.message,true));};
  }
  if(backup){
    const message=backup.querySelector('[data-backup-status]');
    backup.querySelector('[data-backup-export]').onclick=async e=>{const button=e.currentTarget;button.disabled=true;message.textContent='正在整理本机文献库…';try{const blob=await api('/api/library/backup',{blob:true}),a=document.createElement('a'),url=URL.createObjectURL(blob);a.href=url;a.download=`文献库备份-${new Date().toISOString().slice(0,10)}.zip`;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);message.textContent='备份已下载，包含星级、PDF、批注与阅读进度。';}catch(error){message.textContent=error.message;}finally{button.disabled=false;}};
    backup.querySelector('[data-backup-file]').onchange=async e=>{const file=e.target.files[0];transfer='';backup.querySelector('[data-backup-preview]').hidden=true;if(!file)return;const data=new FormData();data.append('file',file);message.textContent='正在校验备份文件…';try{const result=await api('/api/library/restore/preview',{method:'POST',body:data});transfer=result.transfer_id;backup.querySelector('[data-backup-summary]').textContent=`${result.papers} 篇文献、${result.files} 个 PDF；新增 ${result.new_papers} 篇、补入 ${result.new_files} 个文件。${result.existing_papers} 篇已存在，将保留本机评分、批注和进度。${result.missing_files?`备份中缺少 ${result.missing_files} 个文件。`:''}`;backup.querySelector('[data-backup-preview]').hidden=false;message.textContent='校验通过，请确认恢复。';}catch(error){message.textContent=error.message;}};
    backup.querySelector('[data-backup-restore]').onclick=async e=>{if(!transfer)return;const button=e.currentTarget;button.disabled=true;try{const result=await api('/api/library/restore',{method:'POST',body:JSON.stringify({transfer_id:transfer})});message.textContent=result.message;transfer='';backup.querySelector('[data-backup-preview]').hidden=true;await refresh();}catch(error){message.textContent=error.message;}finally{button.disabled=false;}};
  }
  document.addEventListener('paper-chat-connected',()=>refresh().catch(e=>status(e.message,true)));
  window.addEventListener('focus',()=>{if(connected)refresh().catch(e=>status(e.message,true));});
  if(token || read('localStorage',browserKey))refresh().catch(e=>status(e.message,true));
})();
