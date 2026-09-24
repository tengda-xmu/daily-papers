(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  if (!$('#journal-manager')) return;
  const base = 'http://127.0.0.1:43127', local = location.origin === base;
  const storage = local ? 'localStorage' : 'sessionStorage';
  const sessionKey = 'daily-papers-codex-session', browserKey = 'daily-papers-codex-browser';
  function read(kind, key) { try { return window[kind].getItem(key) || ''; } catch { return ''; } }
  function write(kind, key, value) { try { window[kind].setItem(key, value); return true; } catch { return false; } }
  let token = read(storage, sessionKey), state, connected = false, pending = false, editing = '';
  const status = (message) => { $('#journal-status').textContent = message; };
  function connection(ok, message) {
    connected = ok; $('#journal-connection').textContent = message; $('#journal-pairing').hidden = ok;
    lock(pending);
  }
  function lock(value) {
    pending = value;
    $('#journal-fields').disabled = value || !connected;
    for (const el of document.querySelectorAll('#journal-list button, #journal-sync, #journal-refresh')) el.disabled = value || !connected;
  }
  async function restore() {
    const device = read('localStorage', browserKey);
    if (!device) throw new Error('首次使用请连接本机论文助手。');
    const data = await api('/api/session/restore', {method: 'POST', body: JSON.stringify({device_token: device})}, false);
    token = data.token; write(storage, sessionKey, token);
  }
  async function api(path, options = {}, retry = true) {
    let response;
    try {
      response = await fetch(base + path, {...options, headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token}, signal: AbortSignal.timeout(path.endsWith('/sync') ? 110000 : 45000)});
    } catch {
      connection(false, '本机连接不可用，请启动论文助手。');
      throw new Error('连接未完成，请检查本机助手和网络；如刚才在保存或同步，请连接后刷新列表确认结果。');
    }
    if (response.status === 401 && retry && !path.startsWith('/api/session/') && path !== '/api/pair') {
      try { await restore(); return await api(path, options, false); }
      catch (error) { connection(false, '请重新连接本机论文助手。'); throw error; }
    }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 404 && path === '/api/journals') throw new Error('请重新启动论文助手，以载入期刊管理功能。');
      throw new Error(typeof result.detail === 'string' ? result.detail : result.message || '操作未完成，请检查填写内容并重试。');
    }
    return result;
  }
  function node(tag, text) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; }
  function groupInput(focus = false) {
    const creating = $('#journal-group').value === '';
    $('#journal-new-group-field').hidden = !creating;
    $('#journal-new-group').disabled = !creating;
    $('#journal-new-group').required = creating;
    $('#journal-group').required = !creating;
    if (creating && focus) $('#journal-new-group').focus();
  }
  function drawGroups(data) {
    const select = $('#journal-group'), selected = select.value;
    const groups = new Set([...data.groups, ...data.journals.map(journal => journal.group), '自定义期刊']);
    if (selected) groups.add(selected);
    select.replaceChildren(...[...groups].map(group => new Option(group, group, group === '自定义期刊')), new Option('＋ 新建分组…', ''));
    select.value = selected;
    groupInput();
  }
  function draw(data) {
    state = data;
    $('#journal-count').textContent = `${data.journals.length} / ${data.max_journals} 本`;
    drawGroups(data);
    $('#journal-sync-state').textContent = data.pending ? '已保存到本机 · 有修改尚未同步' : data.commit_url ? '已同步 GitHub' : '本机配置与已载入的仓库版本一致';
    const commit = $('#journal-commit'); commit.hidden = !/^https:\/\/github\.com\/tengda-xmu\/daily-papers\/commit\/[a-f0-9]+$/.test(data.commit_url || '');
    if (!commit.hidden) commit.href = data.commit_url;
    const rows = data.journals.map(journal => {
      const row = node('article'); row.className = 'journal-row';
      const info = node('div'); info.append(node('h3', journal.name), node('p', `${journal.issn} · ${journal.group} · ${journal.enabled ? '每日检索已开启' : '已暂停每日检索'}`));
      const actions = node('div'); actions.className = 'journal-row-actions';
      const search = node('a', '检索此刊'); search.href = 'search.html?journal=' + encodeURIComponent(journal.issn); actions.append(search);
      for (const [label, callback] of [
        ['编辑', () => edit(journal)],
        [journal.enabled ? '暂停' : '启用', () => change({issn: journal.issn, name: journal.name, group: journal.group, enabled: !journal.enabled})],
        ['移除', () => mutate('/api/journals/' + journal.issn, 'DELETE', {revision: state.revision}, '已从本机自定义列表移除。同步 GitHub 后线上生效。')],
      ]) { const b = node('button', label); b.type = 'button'; b.onclick = callback; actions.append(b); }
      row.append(info, actions); return row;
    });
    $('#journal-list').replaceChildren(...(rows.length ? rows : [node('p', '尚未添加自定义期刊。上方输入 ISSN 即可开始。')]));
    lock(pending);
  }
  async function refresh() {
    if (!token) await restore();
    draw(await api('/api/journals')); connection(true, '已连接本机 · 可管理期刊');
  }
  async function mutate(path, method, payload, message) {
    if (pending) return;
    lock(true); status(path.endsWith('/sync') ? '正在同步 GitHub，请稍候…' : '正在保存…');
    try { draw(await api(path, {method, body: JSON.stringify(payload)})); reset(); status(message); }
    catch (error) { status(error.message); }
    finally { lock(false); }
  }
  function reset() {
    editing = ''; $('#journal-form').reset(); $('#journal-issn').readOnly = false; $('#journal-cancel').hidden = true;
    $('#journal-editor-title').textContent = '添加期刊'; $('#journal-verification').textContent = ''; $('#journal-verify').hidden = false;
    $('#journal-new-group').setCustomValidity(''); groupInput();
  }
  function edit(journal) {
    editing = journal.issn; $('#journal-issn').value = journal.issn; $('#journal-issn').readOnly = true;
    $('#journal-name').value = journal.name; $('#journal-group').value = journal.group; $('#journal-enabled').checked = journal.enabled;
    $('#journal-new-group').setCustomValidity(''); groupInput();
    $('#journal-cancel').hidden = false; $('#journal-verify').hidden = true; $('#journal-editor-title').textContent = '编辑期刊';
    $('#journal-verification').textContent = 'ISSN 保持不变；可调整刊名、分组和每日检索状态。'; $('#journal-name').focus();
  }
  const change = (data) => mutate('/api/journals', 'POST', {...data, revision: state.revision}, '已保存到本机。点击“同步 GitHub”，将修改用于线上目录和每日更新。');
  $('#journal-form').onsubmit = (event) => {
    event.preventDefault();
    if (!state || pending) return;
    const group = ($('#journal-group').value || $('#journal-new-group').value).trim();
    if (!group) {
      $('#journal-new-group').setCustomValidity('请填写新分组名称。'); $('#journal-new-group').reportValidity(); return;
    }
    change({issn: editing || $('#journal-issn').value.trim(), name: $('#journal-name').value.trim(), group, enabled: $('#journal-enabled').checked});
  };
  $('#journal-group').onchange = () => { $('#journal-new-group').setCustomValidity(''); groupInput(true); };
  $('#journal-new-group').oninput = () => $('#journal-new-group').setCustomValidity('');
  $('#journal-verify').onclick = async () => {
    lock(true); $('#journal-verification').textContent = '正在验证 ISSN…';
    try {
      const data = await api('/api/journals/lookup/' + encodeURIComponent($('#journal-issn').value.trim()));
      $('#journal-issn').value = data.issn; $('#journal-name').value = data.name;
      $('#journal-verification').textContent = `已验证：${data.name}${data.publisher ? ' · ' + data.publisher : ''}`;
    } catch (error) { $('#journal-verification').textContent = error.message; }
    finally { lock(false); }
  };
  $('#journal-cancel').onclick = reset;
  $('#journal-refresh').onclick = async () => { lock(true); try { await refresh(); status('本机列表已刷新。'); } catch (error) { status(error.message); } finally { lock(false); } };
  $('#journal-sync').onclick = () => mutate('/api/journals/sync', 'POST', {revision: state.revision}, '已同步 GitHub。线上目录正在自动部署；新期刊从下次每日任务开始参与检索。');
  $('#journal-pair-form').onsubmit = async (event) => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const data = await api('/api/pair', {method: 'POST', body: JSON.stringify({code: $('#journal-code').value.trim(), remember: $('#journal-remember').checked})}, false);
      token = data.token; write(storage, sessionKey, token);
      if (data.device_token && !write('localStorage', browserKey, data.device_token)) status('浏览器禁止保存配对，关闭页面后需要重新连接。');
      $('#journal-code').value = ''; await refresh();
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  };
  if (local) {
    document.querySelectorAll('a[href]').forEach(a => {
      const url = new URL(a.href);
      if (url.origin === base && !['/search.html', '/journals.html'].includes(url.pathname)) a.href = 'https://tengda-xmu.github.io/daily-papers/' + (url.pathname === '/' ? '' : url.pathname.slice(1)) + url.hash;
    });
  }
  refresh().catch(error => { connection(false, '尚未连接本机论文助手'); status(error.message); });
  window.addEventListener('focus', () => { if (!connected && !pending) refresh().catch(() => {}); });
})();
