(() => {
  'use strict';
  if (document.documentElement.classList.contains('mobile-public')) return;
  const $ = selector => document.querySelector(selector), host = $('#direction-manager');
  if (!host) return;
  const base = 'http://127.0.0.1:43127', local = location.origin === base;
  const storage = local ? 'localStorage' : 'sessionStorage';
  const sessionKey = 'daily-papers-codex-session', browserKey = 'daily-papers-codex-browser';
  const read = (kind, key) => { try { return window[kind].getItem(key) || ''; } catch { return ''; } };
  const write = (kind, key, value) => { try { window[kind].setItem(key, value); } catch {} };
  let draft = JSON.parse(host.dataset.profile), revision = '', connected = false, busy = false, dirty = false, editing = null;
  const message = text => { $('#directions-status').textContent = text; };
  const node = (tag, text) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; return el; };
  const countFields = [['core', 'core_count', '核心推荐', 1], ['extended', 'extended_count', '扩展阅读', 0]];
  function clearCountErrors() {
    for (const [id] of countFields) {
      const input = $('#directions-' + id + '-count');
      input.setCustomValidity(''); input.removeAttribute('aria-invalid');
    }
  }
  function validateCounts() {
    for (const [id, key, label, min] of countFields) {
      const input = $('#directions-' + id + '-count'), value = input.valueAsNumber;
      let error = '';
      if (!input.value.trim()) error = `${label}不能为空，请填写 ${min}–20 的整数。`;
      else if (!Number.isFinite(value) || !Number.isInteger(value)) error = `${label}请输入 ${min}–20 的整数，不能填写小数。`;
      else if (value < min || value > 20) error = `${label}范围为 ${min}–20 篇。` + (min === 0 ? '0 表示关闭本期扩展阅读。' : '');
      input.setCustomValidity(error);
      if (error) {
        input.setAttribute('aria-invalid', 'true'); message(error + ' 当前修改尚未保存。');
        input.focus(); input.reportValidity(); return false;
      }
      input.removeAttribute('aria-invalid'); draft[key] = value;
    }
    return true;
  }
  function showApplication(application) {
    const validCount = value => Number.isInteger(value) && value >= 0;
    if (!application || !validCount(application.core_count) || !validCount(application.extended_count)) {
      $('#directions-application').textContent = '尚未取得本期实际展示数量，保存并应用后可再次确认。';
      $('#directions-update').hidden = true; return false;
    }
    let text = `本期实际展示：核心 ${application.core_count} 篇、扩展 ${application.extended_count} 篇。`;
    const requested = application.requested || {}, available = application.available || {};
    if (validCount(available.core_count) && validCount(available.extended_count)) {
      text += `本期可用：核心 ${available.core_count} 篇、扩展 ${available.extended_count} 篇。`;
    }
    const missing = [['core_count', '核心'], ['extended_count', '扩展']].flatMap(([key, label]) =>
      validCount(requested[key]) && requested[key] > application[key] ? [`${label}缺 ${requested[key] - application[key]} 篇`] : []);
    if (missing.length) text += `本期现有文献不足，${missing.join('、')}；后续更新继续补充。`;
    $('#directions-application').textContent = text;
    const applied = application.local_applied === true && application.scope === 'current_edition' && application.revision === revision;
    $('#directions-update').hidden = !applied;
    return applied;
  }
  function lock(value) {
    busy = value;
    host.querySelectorAll('#directions-list button, #directions-list input, #directions-list select, #direction-form input, #direction-form textarea, #direction-form button, #direction-new, #directions-reload, .direction-counts input').forEach(el => { el.disabled = busy || !connected; });
    $('#directions-save').disabled = busy || !connected || !dirty || !$('#direction-editor').hidden;
  }
  function changed() { dirty = true; $('#directions-state').textContent = '有修改尚未应用'; $('#directions-update').hidden = true; lock(false); }
  async function api(path, options = {}, retry = true) {
    let response;
    try {
      response = await fetch(base + path, {...options, headers: {'Content-Type': 'application/json',
        Authorization: 'Bearer ' + read(storage, sessionKey)}, signal: AbortSignal.timeout(65000)});
    } catch { throw new Error('无法连接本机助手，请运行“启动论文助手.cmd”；也可使用“在本机管理”。'); }
    if (response.status === 401 && retry) {
      const device = read('localStorage', browserKey);
      if (device) {
        const restored = await api('/api/session/restore', {method: 'POST', body: JSON.stringify({device_token: device})}, false);
        write(storage, sessionKey, restored.token); return api(path, options, false);
      }
    }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401) { connected = false; $('#directions-pairing').hidden = false; }
      const detail = typeof result.detail === 'string' ? result.detail : result.detail?.[0]?.msg;
      throw new Error(response.status === 404 ? '请停止并重新启动论文助手，以载入研究方向管理功能。' : detail || result.message || '操作未完成，请检查内容后重试。');
    }
    return result;
  }
  function closeEditor() { $('#direction-editor').hidden = true; editing = null; lock(false); }
  function edit(direction) {
    editing = direction?.id || '';
    $('#direction-editor').hidden = false;
    $('#direction-editor-title').textContent = direction ? '编辑方向' : '添加方向';
    $('#direction-name').value = direction?.name || '';
    for (const [id, key] of [['keywords','keywords'], ['require','require_any'], ['exclude','exclude']]) $('#direction-' + id).value = (direction?.[key] || []).join('\n');
    lock(false); $('#direction-name').focus();
  }
  function draw() {
    $('#directions-core-count').value = draft.core_count;
    $('#directions-extended-count').value = draft.extended_count;
    $('#directions-count').textContent = `${draft.directions.filter(d => d.enabled).length} 个启用 / ${draft.directions.length} 个方向`;
    $('#directions-list').replaceChildren(...draft.directions.map(d => {
      const row = node('article'); row.className = 'direction-row'; row.dataset.id = d.id; row.dataset.enabled = d.enabled;
      const info = node('div'); info.append(node('h3', d.name), node('p', d.keywords.slice(0, 4).join(' · ') + (d.keywords.length > 4 ? '…' : '')));
      const controls = node('div'); controls.className = 'direction-row-controls';
      for (const [key, title] of [['enabled','启用'], ['core','核心'], ['extended','扩展']]) {
        const label = node('label'), input = node('input'); input.type = 'checkbox'; input.checked = d[key]; input.dataset.field = key;
        input.setAttribute('aria-label', d.name + '：' + title);
        input.onchange = () => { d[key] = input.checked; changed(); draw(); };
        label.append(input, document.createTextNode(' ' + title)); controls.append(label);
      }
      const priority = node('select'); priority.setAttribute('aria-label', d.name + '优先级');
      priority.append(new Option('普通优先', '1'), new Option('重点关注', '2'), new Option('最高优先', '3')); priority.value = d.weight;
      priority.onchange = () => { d.weight = Number(priority.value); changed(); }; controls.append(priority);
      for (const [label, callback] of [['编辑', () => edit(d)], ['移除', () => { draft.directions = draft.directions.filter(item => item.id !== d.id); if (editing === d.id) closeEditor(); changed(); draw(); }]]) {
        const button = node('button', label); button.type = 'button'; button.onclick = callback; controls.append(button);
      }
      row.append(info, controls); return row;
    })); lock(busy);
  }
  async function refresh() {
    lock(true);
    try {
      const data = await api('/api/research-directions');
      draft = data.profile; revision = data.revision; connected = true; dirty = false; closeEditor();
      clearCountErrors();
      $('#directions-connection').textContent = '已连接本机 · 可管理研究方向'; $('#directions-pairing').hidden = true;
      const applied = showApplication(data.application);
      $('#directions-state').textContent = applied ? '本期推荐数量已应用' : '设置已保存，数量尚待应用';
      message(data.message || '方向与关键词将在下一轮检索中使用。'); draw();
    } catch (error) {
      $('#directions-connection').textContent = '尚未连接本机论文助手'; $('#directions-pairing').hidden = false; message(error.message);
    } finally { lock(false); }
  }
  $('#direction-new').onclick = () => { if (draft.directions.length >= 12) return message('最多 12 个方向，可编辑或移除已有方向。'); edit(null); };
  $('#direction-cancel').onclick = closeEditor;
  $('#direction-form').onsubmit = event => {
    event.preventDefault();
    const original = draft.directions.find(d => d.id === editing);
    const direction = {...(original || {id: 'direction_' + crypto.randomUUID().slice(0, 8), enabled: true, core: true, extended: true, weight: 1}), name: $('#direction-name').value.trim()};
    for (const [id, key] of [['keywords','keywords'], ['require','require_any'], ['exclude','exclude']]) direction[key] = $('#direction-' + id).value.split(/[\n;；]+/).map(s => s.trim()).filter(Boolean);
    if (!direction.keywords.length) return message('请至少填写一个主题关键词。');
    if (draft.directions.some(d => d.id !== editing && d.name.toLowerCase() === direction.name.toLowerCase())) return message('方向名称重复，请编辑已有方向。');
    if (original) draft.directions = draft.directions.map(d => d.id === editing ? direction : d); else draft.directions.push(direction);
    closeEditor(); changed(); draw(); message('已加入待应用配置，请点击“保存并应用”。');
  };
  for (const [id, key] of countFields) $('#directions-' + id + '-count').oninput = event => {
    const input = event.target;
    draft[key] = input.value === '' ? '' : input.valueAsNumber;
    input.setCustomValidity(''); input.removeAttribute('aria-invalid'); changed();
  };
  $('#directions-reload').onclick = refresh;
  $('#directions-save').onclick = async () => {
    if (!validateCounts()) return;
    lock(true); message('正在保存并应用研究方向…');
    try {
      const data = await api('/api/research-directions', {method: 'POST', body: JSON.stringify({revision, profile: draft})});
      draft = data.profile; revision = data.revision; dirty = false; clearCountErrors(); draw();
      const applied = showApplication(data.application);
      $('#directions-state').textContent = applied ? '数量已应用' : '设置已保存，数量应用待确认';
      message((applied ? '本机首页已按数量设置展示本期推荐。' : '尚未确认本期展示数量，请刷新配置确认。') +
        (data.commit_url ? '公共站正在自动发布，完成前可能仍显示旧数量。' : '公共站由自动发布流程更新。') +
        '方向与关键词将在下一轮检索中使用。');
    } catch (error) { message(error.message + ' 当前修改仍保留在此页。'); }
    finally { lock(false); }
  };
  $('#directions-pair-form').onsubmit = async event => {
    event.preventDefault(); event.submitter.disabled = true;
    try {
      const data = await api('/api/pair', {method: 'POST', body: JSON.stringify({code: $('#directions-code').value.trim(), remember: $('#directions-remember').checked})}, false);
      write(storage, sessionKey, data.token); if (data.device_token) write('localStorage', browserKey, data.device_token);
      $('#directions-code').value = ''; await refresh();
    } catch (error) { message(error.message); }
    finally { event.submitter.disabled = false; }
  };
  if (local) {
    host.querySelectorAll('a[href="./"], #directions-update').forEach(a => { a.href = '/recommendations.html'; });
  }
  window.addEventListener('beforeunload', event => { if (dirty || !$('#direction-editor').hidden) { event.preventDefault(); event.returnValue = ''; } });
  draw(); refresh();
})();
