(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const button = $('#manual-update');
  if (!button) return;
  const base = 'http://127.0.0.1:43127';
  const local = location.origin === base;
  const storage = local ? 'localStorage' : 'sessionStorage';
  const sessionKey = 'daily-papers-codex-session', browserKey = 'daily-papers-codex-browser';
  const pendingKey = 'daily-papers-daily-update';
  const issue = $('#recommendation-issue');
  const active = new Set(['confirming', 'queued', 'running', 'publishing']);
  let timer, refreshing = false;
  function read(kind, key) { try { return window[kind].getItem(key) || ''; } catch { return ''; } }
  function write(kind, key, value) { try { if (value) window[kind].setItem(key, value); else window[kind].removeItem(key); } catch {} }
  function message(text) { $('#daily-update-panel').hidden = false; $('#daily-update-status').textContent = text; }
  function busy(value) { button.disabled = value; button.textContent = value ? '更新中…' : '手动更新'; button.setAttribute('aria-busy', String(value)); }
  async function api(path, options = {}, retry = true) {
    let response;
    try {
      response = await fetch(base + path, {...options, headers: {'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + read(storage, sessionKey)}, signal: AbortSignal.timeout(65000)});
    } catch {
      $('#daily-update-local').hidden = local;
      throw new Error('无法连接本机助手。请运行“启动论文助手.cmd”；浏览器阻止本地连接时，可在本机更新推荐。');
    }
    if (response.status === 401 && retry) {
      const credential = read('localStorage', browserKey);
      if (credential) {
        const data = await api('/api/session/restore', {method: 'POST', body: JSON.stringify({device_token: credential})}, false);
        write(storage, sessionKey, data.token);
        return api(path, options, false);
      }
    }
    if (!response.ok) {
      const info = await response.json().catch(() => ({}));
      if (response.status === 401) { $('#daily-update-pair').hidden = false; $('#daily-update-local').hidden = local; }
      const text = typeof info.detail === 'string' ? info.detail : info.message;
      throw new Error(response.status === 404 ? '本机助手需要更新，请停止后重新运行“启动论文助手.cmd”。'
        : response.status === 401 ? '首次更新请连接本机论文助手。' : text || '更新请求未完成，请稍后重试。');
    }
    return response.json();
  }
  function again(callback, delay = 12000) { clearTimeout(timer); timer = setTimeout(callback, delay); }
  function runLink(data) {
    const link = $('#daily-update-run');
    if (/^\d+$/.test(data.run_id || '')) {
      link.href = 'https://github.com/tengda-xmu/daily-papers/actions/runs/' + data.run_id; link.hidden = false;
    }
  }
  async function showPublished(data) {
    if (issue.dataset.runId === data.run_id) {
      write('localStorage', pendingKey, ''); busy(false);
      message(data.message + (data.changed === false ? '本轮没有更合适的新文献，保留已有推荐。' : ''));
      return;
    }
    // Do not interrupt an open paper conversation, including an unsent draft.
    if ($('.paper-chat[open]')) {
      message('最新推荐已发布；关闭论文对话后会自动刷新。'); again(() => showPublished(data), 3000); return;
    }
    const target = local ? new URL('/recommendations.html', base) : new URL('./', location.href);
    target.searchParams.set('updated', data.run_id);
    try {
      const response = await fetch(target, {cache: 'no-store', signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error('Publication unavailable');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (page.querySelector('#recommendation-issue')?.dataset.runId !== data.run_id) throw new Error('Publication pending');
      message('最新推荐已就绪，正在刷新核心推荐和扩展阅读…');
      location.replace(target.href);
    } catch {
      message('新文献已生成，正在等待页面发布完成；将自动重试。');
      again(() => showPublished(data));
    }
  }
  async function display(data, initial = false) {
    if (data.state === 'idle') { busy(false); return; }
    if (initial && !active.has(data.state) && !read('localStorage', pendingKey)) return;
    $('#daily-update-pair').hidden = true; runLink(data);
    message(data.message); busy(active.has(data.state) || data.state === 'succeeded');
    if (active.has(data.state)) {
      write('localStorage', pendingKey, data.request_id); again(poll);
    } else if (data.state === 'succeeded') {
      await showPublished(data);
    } else {
      write('localStorage', pendingKey, ''); busy(false);
    }
  }
  async function poll(initial = false) {
    try { await display(await api('/api/recommendations/update'), initial); }
    catch (error) {
      if (initial && !read('localStorage', pendingKey)) return;
      message(error.message + ' 已提交的更新可通过运行记录查看。');
      busy(false); again(poll, 24000);
    }
  }
  async function start() {
    if (refreshing) return;
    refreshing = true; busy(true); clearTimeout(timer);
    message('正在连接并启动更新…');
    try {
      // Resume an outstanding job before creating a new request.
      const existing = await api('/api/recommendations/update');
      if (active.has(existing.state)) { await display(existing); return; }
      let requestId = read('localStorage', pendingKey);
      if (!/^[0-9a-f-]{36}$/.test(requestId) || existing.request_id === requestId) requestId = crypto.randomUUID();
      write('localStorage', pendingKey, requestId);
      await display(await api('/api/recommendations/update', {method: 'POST', body: JSON.stringify({request_id: requestId})}));
    } catch (error) { message(error.message); busy(false); }
    finally { refreshing = false; }
  }
  button.addEventListener('click', start);
  $('#daily-update-pair').addEventListener('submit', async event => {
    event.preventDefault(); event.submitter.disabled = true;
    try {
      const data = await api('/api/pair', {method: 'POST', body: JSON.stringify({code: $('#daily-pair-code').value.trim(), remember: $('#daily-remember').checked})}, false);
      write(storage, sessionKey, data.token);
      if (data.device_token) write('localStorage', browserKey, data.device_token);
      $('#daily-pair-code').value = ''; $('#daily-update-pair').hidden = true;
      await start();
    } catch (error) { message(error.message); }
    finally { event.submitter.disabled = false; }
  });
  if (read(storage, sessionKey) || read('localStorage', browserKey)) poll(true);
})();
