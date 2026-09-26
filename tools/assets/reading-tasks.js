(() => {
  'use strict';
  if (document.documentElement.classList.contains('mobile-public')) return;
  const cards = [...document.querySelectorAll('[data-reading-status]')];
  const panels = [...document.querySelectorAll('#automatic-reading, #automatic-ai-reading')];
  if (cards.length && !panels.some(p => p.id === 'automatic-reading')) panels.push(null);
  for (const panel of panels) {
  const ai = panel?.id === 'automatic-ai-reading';
  const endpoint = ai ? '/api/ai/reading-tasks' : '/api/recommendations/reading-tasks';
  const base = 'http://127.0.0.1:43127';
  const storage = location.origin === base ? 'localStorage' : 'sessionStorage';
  const message = panel?.querySelector('[role=status]'), list = panel?.querySelector('ul');
  const work = ai ? '导读' : '精读', unit = ai ? '条' : '篇';
  const labels = {pending:`等待本机${work}`, fetching:'资料获取中', generating:`正在${work}`, ready:`${work}完成，等待发布`, published:ai?'已发布':'全文精读已完成', awaiting_fulltext:'摘要解读已完成，全文待补充', retry:'等待重试', failed:'未完成，可重试', missing_evidence:ai?'官方依据不足':'暂时无法获取资料，将自动重试'};
  function read(kind, key) { try {return window[kind].getItem(key) || '';} catch {return '';} }
  function hasSession() { return read(storage, 'daily-papers-codex-session') || read('localStorage', 'daily-papers-codex-browser'); }
  let publishedPage;
  async function updateCard(task) {
    const node = cards.find(c => c.dataset.readingStatus === task.paper_id);
    if (!node) return;
    node.textContent = task.label || labels[task.state] || task.state;
    node.title = task.error || '';
    const card = node.closest('.paper');
    if (!card) return;
    let details = card.querySelector('.reading-issues');
    const issues = (task.issues || []).filter(i => i.kind !== 'extraction_error');
    if (issues.length) {
      if (!details) {
        details = document.createElement('details'); details.className = 'reading-issues';
        const summary = document.createElement('summary'); summary.textContent = '资料核对说明';
        details.append(summary, document.createElement('ul')); node.after(details);
      }
      details.querySelector('ul').replaceChildren(...issues.map(i => {
        const li = document.createElement('li'); li.textContent = `${i.page} · ${i.detail}`; return li;
      }));
    } else if (details && task.state === 'published') details.remove();
    if (task.state !== 'published' || !task.completed_at || node.dataset.readingResult === task.completed_at) return;
    try {
      publishedPage ||= fetch(location.href, {cache:'no-store'}).then(r => {if (!r.ok) throw new Error(); return r.text();})
        .then(html => new DOMParser().parseFromString(html, 'text/html'));
      const fresh = (await publishedPage).querySelector(`.paper[data-paper-id="${task.paper_id}"]`);
      if (!fresh || fresh.querySelector('[data-reading-status]')?.dataset.readingResult !== task.completed_at) return;
      for (const selector of ['h3', '.abstract', '.paper-detail-panel', '.paper-toggle']) {
        const old = card.querySelector(selector), next = fresh.querySelector(selector);
        if (old && next) { old.innerHTML = next.innerHTML; if (next.dataset.label) old.dataset.label = next.dataset.label; }
      }
      node.dataset.readingResult = task.completed_at;
    } catch (_) { /* Existing notes remain visible until publication is reachable. */ }
    finally { publishedPage = null; }
  }
  async function api(path, method = 'GET', retry = true) {
    let response = await fetch(base + path, {method, headers:{Authorization:'Bearer ' + read(storage, 'daily-papers-codex-session')}, signal:AbortSignal.timeout(15000)});
    if (response.status === 401 && retry) {
      const device = read('localStorage', 'daily-papers-codex-browser');
      if (device) {
        const restore = await fetch(base + '/api/session/restore', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({device_token:device}), signal:AbortSignal.timeout(15000)});
        if (restore.ok) {
          const data = await restore.json(); window[storage].setItem('daily-papers-codex-session', data.token);
          return api(path, method, false);
        }
      }
    }
    if (!response.ok) throw new Error(response.status === 401 ? '请先连接本机助手，再查看精读任务。' : '任务状态暂不可用，请更新或重新连接本机助手。');
    return response.json();
  }
  let refreshing = false;
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const data = await api(endpoint);
      data.tasks = data.tasks.filter(task => task.enabled !== false);
      if (message) message.textContent = `共 ${data.tasks.length} ${unit} · 已发布 ${data.counts.published} ${unit} · 正在${work} ${data.counts.generating} ${unit}` + (data.sync_state === 'retry' ? ' · 公开内容同步等待重试' : '');
      list?.replaceChildren();
      for (const task of data.tasks) {
        if (!ai) updateCard(task);
        if (!list) continue;
        const item = document.createElement('li');
        const text = document.createElement('span'); text.textContent = `${task.title} · ${(!ai && task.label) || labels[task.state] || task.state}${task.error ? ' · ' + task.error : ''}`;
        item.append(text);
        if (['failed','retry','missing_evidence','awaiting_fulltext'].includes(task.state)) {
          const button = document.createElement('button'); button.type='button'; button.className='text-button'; button.textContent='重试';
          button.addEventListener('click', async () => {button.disabled=true;try {await api(endpoint + '/' + encodeURIComponent(task.paper_id) + '/retry','POST');await refresh();} catch(e) {message.textContent=e.message;} finally {button.disabled=false;}});
          item.append(button);
        }
        list.append(item);
      }
    } catch(e) { if (message) message.textContent = e instanceof TypeError ? '无法连接本机助手，请启动后再查看。' : e.message; } finally { refreshing = false; }
  }
  panel?.addEventListener('toggle', () => {if (panel.open) refresh();});
  panel?.querySelector('[data-refresh-reading]').addEventListener('click', async () => {
    try {await api(endpoint + '/sync', 'POST'); await refresh();}
    catch(e) {message.textContent=e.message;}
  });
  setInterval(() => {if (!document.hidden && (panel?.open || (!ai && cards.length && hasSession()))) refresh();}, 30000);
  if (!ai) {
    document.addEventListener('paper-document-updated', event => {
      if (event.detail?.readingTask) updateCard(event.detail.readingTask);
      if (hasSession()) refresh();
    });
    for (const name of ['paper-chat-connected','paper-library-connected']) document.addEventListener(name, () => refresh());
    if (cards.length && hasSession()) refresh();
  }
  }
})();
