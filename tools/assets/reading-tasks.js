(() => {
  'use strict';
  if (document.documentElement.classList.contains('mobile-public')) return;
  const panel = document.querySelector('#automatic-reading');
  if (!panel) return;
  const base = 'http://127.0.0.1:43127';
  const storage = location.origin === base ? 'localStorage' : 'sessionStorage';
  const message = panel.querySelector('[role=status]'), list = panel.querySelector('ul');
  const labels = {pending:'等待本机精读', generating:'正在精读', ready:'精读完成，等待发布', published:'已发布', retry:'等待重试', failed:'未完成，可重试', missing_evidence:'摘要资料不足'};
  function read(kind, key) { try {return window[kind].getItem(key) || '';} catch {return '';} }
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
  async function refresh() {
    try {
      const data = await api('/api/recommendations/reading-tasks');
      message.textContent = `共 ${data.tasks.length} 篇 · 已发布 ${data.counts.published} 篇 · 正在精读 ${data.counts.generating} 篇` + (data.sync_state === 'retry' ? ' · 公开推荐同步等待重试' : '');
      list.replaceChildren();
      for (const task of data.tasks) {
        const item = document.createElement('li');
        const text = document.createElement('span'); text.textContent = `${task.title} · ${labels[task.state] || task.state}${task.error ? ' · ' + task.error : ''}`;
        item.append(text);
        if (['failed','retry','missing_evidence'].includes(task.state)) {
          const button = document.createElement('button'); button.type='button'; button.className='text-button'; button.textContent='重试';
          button.addEventListener('click', async () => {button.disabled=true;try {await api('/api/recommendations/reading-tasks/' + encodeURIComponent(task.paper_id) + '/retry','POST');await refresh();} catch(e) {message.textContent=e.message;} finally {button.disabled=false;}});
          item.append(button);
        }
        list.append(item);
      }
    } catch(e) { message.textContent = e instanceof TypeError ? '无法连接本机助手，请启动后再查看。' : e.message; }
  }
  panel.addEventListener('toggle', () => {if (panel.open) refresh();});
  panel.querySelector('[data-refresh-reading]').addEventListener('click', async () => {
    try {await api('/api/recommendations/reading-tasks/sync', 'POST'); await refresh();}
    catch(e) {message.textContent=e.message;}
  });
  setInterval(() => {if (panel.open && !document.hidden) refresh();}, 30000);
})();
