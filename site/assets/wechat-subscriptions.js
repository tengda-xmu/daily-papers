(() => {
  'use strict';
  if (document.documentElement.classList.contains('mobile-public')) return;
  const $ = s => document.querySelector(s);
  if (!$('#wechat-manager')) return;
  const base = 'http://127.0.0.1:43127', local = location.origin === base;
  const storage = local ? 'localStorage' : 'sessionStorage';
  const sessionKey = 'daily-papers-codex-session', browserKey = 'daily-papers-codex-browser';
  function read(kind, key) { try { return window[kind].getItem(key) || ''; } catch { return ''; } }
  function write(kind, key, value) { try { window[kind].setItem(key, value); return true; } catch { return false; } }
  let token = read(storage, sessionKey), state, connected = false, busy = false, editing = '';
  const status = message => { $('#wechat-status').textContent = message; };
  function lock(value) {
    busy = value;
    if ($('#social-note-fields')) $('#social-note-fields').disabled = busy || !connected;
    if ($('#social-note-sync')) $('#social-note-sync').disabled = busy || !connected || !noteState;
    document.querySelectorAll('#social-note-list button').forEach(el => { el.disabled = busy; });
    $('#wechat-fields').disabled = busy || !connected;
    $('#wechat-sync').disabled = busy || !connected;
    $('#wechat-refresh').disabled = busy;
    document.querySelectorAll('#wechat-list button').forEach(el => { el.disabled = busy || !connected; });
  }
  function connection(ok, message) {
    connected = ok; $('#wechat-connection').textContent = message; $('#wechat-pairing').hidden = ok; lock(busy);
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
      response = await fetch(base + path, {...options, headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token}, signal: AbortSignal.timeout(path.endsWith('/sync') ? 110000 : 20000)});
    } catch {
      connection(false, '本机连接不可用，请启动论文助手。');
      throw new Error('连接未完成。请连接后刷新列表确认刚才的保存或同步结果。');
    }
    if (response.status === 401 && retry && !path.startsWith('/api/session/') && path !== '/api/pair') {
      try { await restore(); return await api(path, options, false); }
      catch (error) { connection(false, '请重新连接本机论文助手。'); throw error; }
    }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 404 && path === '/api/wechat-subscriptions') throw new Error('请重新启动论文助手，以载入公众号订阅管理。');
      throw new Error(typeof result.detail === 'string' ? result.detail : result.message || '操作未完成，请检查填写内容后重试。');
    }
    return result;
  }
  function node(tag, text) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; }
  function groupInput(focus = false) {
    const custom = $('#wechat-group').value === '';
    $('#wechat-custom-group-field').hidden = !custom;
    $('#wechat-custom-group').disabled = !custom;
    $('#wechat-custom-group').required = custom;
    $('#wechat-group').required = !custom;
    const group = state?.group_overview?.groups.find(row => row.name === $('#wechat-group').value);
    $('#wechat-group-help').textContent = custom ? '填写一个新分组；保存订阅后可在列表中重复选择。' : group?.description || '选择适合这个公众号的研究方向。';
    if (custom && focus) $('#wechat-custom-group').focus();
  }
  function setGroup(value) {
    const select = $('#wechat-group');
    if (value && ![...select.options].some(option => option.value === value)) {
      select.add(new Option(value, value), select.options.length - 1);
    }
    select.value = value; groupInput();
  }
  function drawGroups(overview) {
    const selected = $('#wechat-group').value;
    $('#wechat-group').replaceChildren(...overview.groups.map(row =>
      new Option(`${row.name}（${row.total} 个）`, row.name, row.name === '科研综合')),
      new Option('＋ 自定义分组…', ''));
    setGroup(selected);
    $('#wechat-group-total').textContent = `${overview.groups.length} 类 · ${overview.total} 个公众号`;
    $('#wechat-group-statistics-note').textContent = `本机当前目录：共 ${overview.total} 个公众号，启用 ${overview.enabled} 个，${overview.multi_group} 个归入多个分组。`;
    $('#wechat-group-rows').replaceChildren(...overview.groups.map(group => {
      const row = node('tr'), name = node('th', group.name); name.scope = 'row';
      row.append(name, node('td', group.description), node('td', group.total), node('td', group.enabled));
      return row;
    }));
  }
  function reset() {
    editing = ''; $('#wechat-form').reset(); $('#wechat-editor-title').textContent = '添加公众号';
    $('#wechat-save').textContent = '保存订阅'; $('#wechat-cancel').hidden = true;
    setGroup('科研综合');
  }
  function edit(row) {
    editing = row.id;
    for (const field of ['name', 'alias']) $('#wechat-' + field).value = row[field];
    setGroup(row.group);
    $('#wechat-enabled').checked = row.enabled;
    $('#wechat-editor-title').textContent = '编辑公众号'; $('#wechat-save').textContent = '保存修改';
    $('#wechat-cancel').hidden = false; $('#wechat-name').focus();
  }
  function draw(data) {
    state = data;
    $('#wechat-count').textContent = `${data.accounts.length} 个`;
    drawGroups(data.group_overview);
    $('#wechat-sync-state').textContent = data.pending ? '本机已保存 · 有修改尚未同步到每日更新' : '本机订阅与已载入的云端版本一致';
    const rows = data.accounts.map(row => {
      const article = node('article'); article.className = 'wechat-row';
      const info = node('div'); info.append(node('h4', row.name), node('p', [row.alias, row.group, row.enabled ? '已启用' : '已暂停'].filter(Boolean).join(' · ')));
      const actions = node('div'); actions.className = 'wechat-row-actions';
      for (const [label, run] of [
        ['编辑', () => edit(row)],
        [row.enabled ? '暂停' : '启用', () => mutate('/api/wechat-subscriptions', 'POST', {...row, enabled: !row.enabled, revision: state.revision}, '订阅状态已保存。同步到每日更新后云端生效。')],
        ['删除', () => mutate('/api/wechat-subscriptions/' + row.id, 'DELETE', {revision: state.revision}, '已从手动订阅中删除。同步后云端生效；已有文章保留。', editing === row.id)],
      ]) { const button = node('button', label); button.type = 'button'; button.onclick = run; actions.append(button); }
      article.append(info, actions); return article;
    });
    $('#wechat-list').replaceChildren(...(rows.length ? rows : [node('p', '还没有手动添加的公众号，可在上方填写名称。已有订阅可在下方目录查看。')]));
    lock(busy);
  }
  async function refresh() {
    if (!token) await restore();
    const data = await api('/api/wechat-subscriptions');
    if (!data.group_overview) throw new Error('请重新启动论文助手，以载入分组统计功能。');
    draw(data); connection(true, '已连接本机 · 可管理公众号订阅');
    if ($('#social-note-manager')) {
      try { drawNotes(await api('/api/social-notes')); }
      catch { $('#social-note-status').textContent = '笔记管理尚未连接，请更新并重启本机助手后重试。'; }
    }
  }
  async function mutate(path, method, payload, message, clear = false) {
    if (busy) return;
    lock(true); status(path.endsWith('/sync') ? '正在同步到每日更新…' : '正在保存…');
    try { draw(await api(path, {method, body: JSON.stringify(payload)})); if (clear) reset(); status(message); }
    catch (error) { status(error.message); }
    finally { lock(false); }
  }
  $('#wechat-form').onsubmit = event => {
    event.preventDefault();
    mutate('/api/wechat-subscriptions', 'POST', {revision: state.revision, id: editing,
      name: $('#wechat-name').value.trim(), alias: $('#wechat-alias').value.trim(),
      group: $('#wechat-group').value || $('#wechat-custom-group').value.trim(),
      enabled: $('#wechat-enabled').checked}, '订阅已保存到本机。点击“同步到每日更新”后，云端将按新目录采集。', true);
  };
  $('#wechat-cancel').onclick = reset;
  $('#wechat-group').onchange = () => groupInput(true);
  $('#wechat-refresh').onclick = async () => { lock(true); try { await refresh(); status('订阅列表已刷新。'); } catch (error) { status(error.message); } finally { lock(false); } };
  $('#wechat-sync').onclick = () => mutate('/api/wechat-subscriptions/sync', 'POST', {revision: state.revision}, '已同步到每日更新。下次采集将使用新目录，线上目录正在更新。');
  $('#wechat-pair-form').onsubmit = async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const data = await api('/api/pair', {method: 'POST', body: JSON.stringify({code: $('#wechat-code').value.trim(), remember: $('#wechat-remember').checked})}, false);
      token = data.token; write(storage, sessionKey, token);
      if (data.device_token && !write('localStorage', browserKey, data.device_token)) status('浏览器无法记住配对，关闭后需重新连接。');
      $('#wechat-code').value = ''; await refresh();
    } catch (error) { status(error.message); } finally { button.disabled = false; }
  };
  if (local) document.querySelectorAll('a[href]').forEach(a => {
    const url = new URL(a.href);
    if (url.origin === base && !['/setup.html', '/search.html', '/journals.html', '/directions.html'].includes(url.pathname) && !a.getAttribute('href').startsWith('#')) {
      a.href = 'https://tengda-xmu.github.io/daily-papers/' + (url.pathname === '/' ? '' : url.pathname.slice(1)) + url.hash;
    }
  });
  let noteState, noteEditing = '';
  const noteLabels = {ai:'AI 前沿', leads:'科研线索'};
  function noteReset() { noteEditing=''; $('#social-note-form').reset(); $('#social-note-cancel').hidden=true; }
  function noteData() { return {revision:noteState?.revision || '', id:noteEditing,
    share:$('#social-note-share').value.trim(), title:$('#social-note-title').value.trim(),
    author:$('#social-note-author').value.trim(), body:$('#social-note-body').value,
    published_at:$('#social-note-date').value}; }
  function drawNotes(data) {
    noteState=data;
    $('#social-note-sync-state').textContent = data.pending ? '本机有尚未同步的笔记修改。' : '笔记与已载入的网站版本一致。';
    $('#social-note-list').replaceChildren(...data.entries.map(row => {
      const article=node('article'); article.className='wechat-row';
      const info=node('div'); info.append(node('h4',row.title),node('p',`${noteLabels[row.column] || '待分类'} · ${row.author || '作者待补充'} · ${row.evidence_kind==='manual_text'?'手动补充':row.read_status==='readable'?'已读取内容':'待补充'}`));
      const actions=node('div'); actions.className='wechat-row-actions';
      const edit=node('button','编辑'), remove=node('button','删除'); edit.type=remove.type='button';
      edit.onclick=()=> { noteEditing=row.id; $('#social-note-share').value=row.share || row.url;
        $('#social-note-title').value=row.title; $('#social-note-author').value=row.author || '';
        $('#social-note-body').value=row.body || ''; $('#social-note-date').value=(row.published_at || '').slice(0,10);
        $('#social-note-cancel').hidden=false; $('#social-note-share').focus(); };
      remove.onclick=()=>noteAction('/api/social-notes/'+row.id,'DELETE',{revision:noteState.revision},'已从本机列表移除；同步后网站生效。');
      actions.append(edit,remove); article.append(info,actions); return article;
    })); lock(busy);
  }
  async function noteAction(path,method,data,message,clear=false) {
    if (busy) return; lock(true); $('#social-note-status').textContent='正在处理…';
    try { const result=await api(path,{method,body:JSON.stringify(data)});
      if (path.endsWith('/preview')) $('#social-note-status').textContent=`${noteLabels[result.column]} · ${result.title}。${result.evidence_text ? result.summary : '未取得正文，可手动补充。'}`;
      else { drawNotes(result); if(clear)noteReset(); $('#social-note-status').textContent=message; }
    } catch(error) { $('#social-note-status').textContent=error.message; } finally {lock(false);}
  }
  if ($('#social-note-manager')) {
    $('#social-note-form').onsubmit=e=>{e.preventDefault();noteAction('/api/social-notes','POST',noteData(),'已保存到本机；同步后进入公开栏目。',true);};
    $('#social-note-preview').onclick=()=>{if($('#social-note-form').reportValidity())noteAction('/api/social-notes/preview','POST',noteData());};
    $('#social-note-sync').onclick=()=>noteAction('/api/social-notes/sync','POST',{revision:noteState.revision},'已同步，网站正在更新。');
    $('#social-note-cancel').onclick=noteReset;
  }
  refresh().catch(error => { connection(false, '尚未连接本机论文助手'); status(error.message); });
  window.addEventListener('focus', () => { if (!connected && !busy) refresh().catch(() => {}); });
})();
