(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const button = $('#archive-manage');
  if (!button) return;
  const base = 'http://127.0.0.1:43127';
  const storage = location.origin === base ? 'localStorage' : 'sessionStorage';
  const key = 'daily-papers-archive-delete';
  const active = new Set(['confirming', 'queued', 'running', 'publishing']);
  let snapshot, selected = new Set(), managing = false, busy = false, timer, request, operation;
  function read(kind, name) { try { return window[kind].getItem(name) || ''; } catch { return ''; } }
  function save(value) { request = value; try { if (value) localStorage.setItem(key, JSON.stringify(value)); else localStorage.removeItem(key); } catch {} }
  function message(text) { $('#archive-operation').hidden = false; $('#archive-operation').textContent = text; }
  function write(kind, name, value) { try { window[kind].setItem(name, value); } catch {} }
  async function api(path, options = {}, retry = true) {
    let response;
    try {
      response = await fetch(base + path, {...options, headers: {'Content-Type': 'application/json',
        Authorization: 'Bearer ' + read(storage, 'daily-papers-codex-session')}, signal: AbortSignal.timeout(65000)});
    } catch {
      $('#archive-connect').hidden = false;
      throw new Error('无法连接本机助手，请启动后重试。已提交的任务会保留。');
    }
    if (response.status === 401 && retry && read('localStorage', 'daily-papers-codex-browser')) {
      const data = await api('/api/session/restore', {method: 'POST', body: JSON.stringify({device_token: read('localStorage', 'daily-papers-codex-browser')})}, false);
      write(storage, 'daily-papers-codex-session', data.token);
      return api(path, options, false);
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) { $('#archive-connect').hidden = false; $('#archive-pair').hidden = false; }
      const error = new Error(typeof data.detail === 'string' ? data.detail : data.message || '请求未完成，请重试。');
      error.status = response.status;
      throw error;
    }
    return response.json();
  }
  function element(tag, text, attrs = {}) {
    const node = document.createElement(tag); if (text) node.textContent = text;
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v)); return node;
  }
  function render() {
    const open = new Set([...document.querySelectorAll('.archive-day[open]')].map(n => n.dataset.day));
    const list = $('.archive-list'); list.replaceChildren();
    const days = [...new Set(snapshot.editions.map(e => e.date))].sort().reverse();
    days.forEach(day => {
      const rows = snapshot.editions.filter(e => e.date === day).sort((a, b) => b.number - a.number);
      const group = element('details', '', {class: 'archive-day', 'data-day': day}); group.open = open.has(day);
      group.append(element('summary', `${day} · ${rows.length} 批`));
      const dayLabel = element('label', '', {class: 'archive-day-choice'});
      dayLabel.append(element('input', '', {type: 'checkbox', 'data-day': day}), '选择当天可删除批次'); group.append(dayLabel);
      const ul = element('ul');
      rows.forEach(e => {
        const li = element('li', '', {class: 'archive-entry', 'data-edition-id': e.id});
        const check = element('input', '', {type: 'checkbox', class: 'archive-choice', 'data-edition-id': e.id, 'aria-label': `选择 ${e.date} 第 ${e.number} 批`});
        check.disabled = e.id === snapshot.current_id; li.append(check);
        const info = element('div');
        const name = `${e.date}--${e.id}`;
        const time = new Date(e.generated_at).toLocaleString('sv-SE', {timeZone: 'Asia/Shanghai'}).slice(0, 16);
        info.append(element('a', `第 ${e.number} 批 · ${time}`, {class: 'archive-date', href: name + '.html'}));
        if (e.id === snapshot.current_id) info.append(element('span', '当前推荐', {class: 'archive-current'}));
        const trigger = {manual: '手动更新', scheduled: '自动更新', recovered: '历史恢复'}[e.trigger] || '更新';
        info.append(element('p', `${trigger} · 核心推荐 ${e.core_count} 篇 · 扩展阅读 ${e.extended_count} 篇`));
        li.append(info, element('a', '下载数据', {href: name + '.json', download: ''})); ul.append(li);
      });
      group.append(ul); const item = element('li'); item.append(group); list.append(item);
    });
    $('#archive-count').textContent = `共 ${snapshot.editions.length} 批归档`;
    sync();
  }
  function sync() {
    document.body.classList.toggle('archive-managing', managing);
    $('#archive-controls').hidden = !managing;
    button.setAttribute('aria-expanded', String(managing)); button.disabled = busy;
    $('#archive-selected').textContent = `已选择 ${selected.size} 批`;
    $('#archive-delete').disabled = busy || !selected.size;
    $('#archive-cancel').disabled = busy;
    const available = (snapshot?.editions || []).filter(e => e.id !== snapshot.current_id);
    const all = $('#archive-select-all'); all.disabled = busy || !available.length;
    all.checked = !!available.length && selected.size === available.length; all.indeterminate = selected.size > 0 && !all.checked;
    document.querySelectorAll('.archive-choice').forEach(c => { c.checked = selected.has(c.dataset.editionId); c.disabled = busy || c.dataset.editionId === snapshot?.current_id; });
    document.querySelectorAll('.archive-day-choice input').forEach(c => {
      const rows = available.filter(e => e.date === c.dataset.day), count = rows.filter(e => selected.has(e.id)).length;
      c.checked = !!rows.length && count === rows.length; c.indeterminate = count > 0 && !c.checked; c.disabled = busy || !rows.length;
    });
  }
  async function load() {
    snapshot = await api('/api/recommendations/editions');
    selected = new Set([...selected].filter(i => snapshot.editions.some(e => e.id === i && i !== snapshot.current_id)));
    $('#archive-connect').hidden = true; $('#archive-pair').hidden = true;
    render();
    if (active.has(snapshot.operation.state)) {
      operation = snapshot.operation;
      save({request_id: operation.request_id, revision: operation.revision, edition_ids: operation.edition_ids});
      await display(operation);
    }
  }
  async function display(data) {
    operation = data; message(data.message); busy = active.has(data.state);
    $('#archive-retry').hidden = data.state !== 'failed';
    clearTimeout(timer); sync();
    if (busy) timer = setTimeout(poll, 8000);
    else if (data.state === 'succeeded') {
      save(null); selected.clear(); managing = false;
      await load(); button.focus();
    }
  }
  async function poll() {
    if (!request) return;
    try { await display(await api('/api/recommendations/editions/operations/' + request.request_id)); }
    catch (error) {
      message(error.message); busy = false; sync(); $('#archive-retry').hidden = false;
      if (error.status !== 404 && error.status !== 401) timer = setTimeout(poll, 20000);
    }
  }
  button.hidden = false;
  button.addEventListener('click', async () => {
    busy = true; sync();
    try { managing = true; await load(); }
    catch (error) { managing = false; message(error.message); }
    finally { busy = active.has(operation?.state); sync(); }
  });
  $('#archive-cancel').addEventListener('click', () => { selected.clear(); managing = false; sync(); button.focus(); });
  $('.archive-list').addEventListener('change', event => {
    const c = event.target;
    const ids = c.matches('.archive-choice') ? [c.dataset.editionId] : c.matches('.archive-day-choice input') ? snapshot.editions.filter(e => e.date === c.dataset.day && e.id !== snapshot.current_id).map(e => e.id) : [];
    ids.forEach(id => c.checked ? selected.add(id) : selected.delete(id)); sync();
  });
  $('#archive-select-all').addEventListener('change', event => { selected = new Set(event.target.checked ? snapshot.editions.filter(e => e.id !== snapshot.current_id).map(e => e.id) : []); sync(); });
  $('#archive-delete').addEventListener('click', async () => {
    const rows = snapshot.editions.filter(e => selected.has(e.id));
    if (!rows.length || busy) return;
    if (!window.confirm(`删除以下 ${rows.length} 批归档？\n\n${rows.map(e => `${e.date} 第 ${e.number} 批`).join('\n')}\n\n删除后，相关论文可重新参与推荐。PDF、批注、星级、精读和对话保留。`)) return;
    busy = true; sync();
    save({request_id: crypto.randomUUID(), revision: snapshot.revision, edition_ids: rows.map(e => e.id)});
    message('正在提交删除…');
    try { await display(await api('/api/recommendations/editions/delete', {method: 'POST', body: JSON.stringify(request)})); }
    catch (error) {
      message(error.message); busy = false; sync();
      if (error.status === 409) { save(null); await load().catch(() => {}); }
      else $('#archive-retry').hidden = false;
    }
  });
  $('#archive-retry').addEventListener('click', async () => {
    if (!request) return;
    busy = true; sync(); $('#archive-retry').hidden = true;
    try {
      let state;
      try { state = await api('/api/recommendations/editions/operations/' + request.request_id); }
      catch (error) { if (error.status !== 404) throw error; }
      await display(state ? await api('/api/recommendations/editions/operations/' + request.request_id + '/retry', {method: 'POST'})
        : await api('/api/recommendations/editions/delete', {method: 'POST', body: JSON.stringify(request)}));
    } catch (error) { message(error.message); busy = false; sync(); $('#archive-retry').hidden = false; }
  });
  $('#archive-pair').addEventListener('submit', async event => {
    event.preventDefault(); event.submitter.disabled = true;
    try {
      const data = await api('/api/pair', {method: 'POST', body: JSON.stringify({code: $('#archive-pair-code').value.trim(), remember: $('#archive-remember').checked})}, false);
      write(storage, 'daily-papers-codex-session', data.token);
      if (data.device_token) write('localStorage', 'daily-papers-codex-browser', data.device_token);
      $('#archive-pair-code').value = ''; managing = true; await load();
    } catch (error) { message(error.message); }
    finally { event.submitter.disabled = false; }
  });
  try { request = JSON.parse(read('localStorage', key) || 'null'); } catch { request = null; }
  if (request?.request_id && (read(storage, 'daily-papers-codex-session') || read('localStorage', 'daily-papers-codex-browser'))) poll();
})();
