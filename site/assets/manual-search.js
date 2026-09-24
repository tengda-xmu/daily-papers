(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  if (!$('#manual-search')) return;
  const base = 'http://127.0.0.1:43127';
  const local = location.origin === base;
  const storage = local ? 'localStorage' : 'sessionStorage';
  const sessionKey = 'daily-papers-codex-session', browserKey = 'daily-papers-codex-browser';
  const jobKey = 'daily-papers-manual-search';
  const orderKey = 'daily-papers-search-order';
  const orderLabels = {relevance: '相关性优先', latest: '最新发表', citations: '热度（被引次数）'};
  const orderHelp = {
    relevance: '优先匹配研究主题。更改排序后点击“检索文献”，会按新条件查询来源。',
    latest: '优先查找近期发表的文献；以来源提供的日期为准，只有年份的记录不推定具体日期。',
    citations: '按被引次数衡量热度，不代表阅读量或近期关注度。缺少被引数据的记录置后，不当作 0 次。'
  };
  function read(kind, key) { try { return window[kind].getItem(key) || ''; } catch { return ''; } }
  function write(kind, key, value) { try { if (value) window[kind].setItem(key, value); else window[kind].removeItem(key); return true; } catch { return false; } }
  let token = read(storage, sessionKey), restorePromise, connected = false, job = '', snapshot = null, timer, signature = '';
  let searching = false, restoreForm = false;
  let journalIssn = new URLSearchParams(location.search).get('journal') || '', journalNames = new Map();
  const action = (text) => { $('#search-action-status').textContent = text; };
  function setConnection(ok, message) {
    connected = ok;
    $('#search-connection-state').textContent = message;
    $('#search-connection-state').dataset.connected = String(ok);
    $('#search-pairing').hidden = ok;
  }
  async function restore() {
    if (restorePromise) return restorePromise;
    restorePromise = (async () => {
      const credential = read('localStorage', browserKey);
      if (!credential) throw new Error('首次使用请连接本机论文助手。');
      const data = await api('/api/session/restore', {method: 'POST', body: JSON.stringify({device_token: credential})}, false);
      token = data.token; write(storage, sessionKey, token);
    })();
    try { await restorePromise; } finally { restorePromise = null; }
  }
  async function api(path, options = {}, retry = true, blob = false) {
    let response;
    try {
      response = await fetch(base + path, {...options, headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token, ...options.headers}, signal: AbortSignal.timeout(blob ? 180000 : 15000)});
    } catch {
      setConnection(false, '本机连接不可用，请启动论文助手；浏览器阻止时可在本机打开。');
      throw new Error('无法连接本机服务。请确认“启动论文助手.cmd”正在运行，并允许浏览器访问本地网络。');
    }
    if (response.status === 401 && retry && !path.startsWith('/api/session/') && path !== '/api/pair') {
      try { await restore(); return await api(path, options, false, blob); }
      catch (error) { setConnection(false, '需要重新配对本机论文助手。'); throw error; }
    }
    if (!response.ok) {
      const info = await response.json().catch(() => ({}));
      const message = typeof info.detail === 'string' ? info.detail : info.message;
      if (response.status === 404 && path === '/api/search/sources') throw new Error('本机助手尚未载入检索功能，请关闭后重新运行“启动论文助手.cmd”。');
      throw new Error(message || (response.status === 422 ? '请检查关键词、日期与来源选择。' : `请求未完成（${response.status}），请稍后重试。`));
    }
    return blob ? response.blob() : response.json();
  }
  async function connect() {
    if (!token) await restore();
    const libraries = await api('/api/search/sources');
    if (!libraries.sort_options) throw new Error('本机助手需要更新：请停止后重新运行“启动论文助手.cmd”，以启用检索排序。');
    journalNames = new Map((libraries.journals || []).map(j => [j.issn, j.name]));
    applyJournal();
    setConnection(true, '已连接本机 · 可检索 11 类来源');
  }
  $('#search-pair-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.submitter; button.disabled = true;
    try {
      const data = await api('/api/pair', {method: 'POST', body: JSON.stringify({code: $('#search-pair-code').value.trim(), remember: $('#search-remember').checked})}, false);
      token = data.token; write(storage, sessionKey, token);
      if (data.device_token && !write('localStorage', browserKey, data.device_token)) action('浏览器禁止保存数据，关闭页面后需要重新配对。');
      $('#search-pair-code').value = '';
      await connect();
    } catch (error) { action(error.message); }
    finally { button.disabled = false; }
  });
  const dateString = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const today = new Date(), start = new Date(today); start.setFullYear(start.getFullYear() - 5);
  $('#search-until').value = dateString(today); $('#search-since').value = dateString(start);
  for (const input of [$('#search-since'), $('#search-until')]) { input.min = '1900-01-01'; input.max = dateString(today); }
  const savedOrder = read('localStorage', orderKey);
  if (orderLabels[savedOrder]) $('#search-order').value = savedOrder;
  function orderDescription() { $('#search-order-help').textContent = orderHelp[$('#search-order').value]; }
  orderDescription();
  $('#search-order').onchange = () => {
    orderDescription(); write('localStorage', orderKey, $('#search-order').value);
    if (snapshot) action('检索排序已修改，点击“检索文献”获取新结果；下方仍是上次结果。');
  };
  const sourceBoxes = [...document.querySelectorAll('[name=library]')];
  let previousSources = null;
  function setJournal(value) {
    journalIssn = value;
    applyJournal();
  }
  function applyJournal() {
    const scoped = Boolean(journalIssn);
    if (scoped && previousSources === null) previousSources = sourceBoxes.filter(x => x.checked).map(x => x.value);
    sourceBoxes.forEach(x => { x.disabled = scoped; if (scoped) x.checked = x.value === 'Crossref'; else if (previousSources !== null) x.checked = previousSources.includes(x.value); });
    if (!scoped) previousSources = null;
    $('#select-libraries').disabled = scoped; $('#clear-libraries').disabled = scoped;
    $('#literature-query').required = !scoped;
    $('#search-journal-scope').hidden = !scoped;
    $('#search-journal-name').textContent = scoped ? (journalNames.get(journalIssn) || 'ISSN ' + journalIssn) : '';
    sourceCount();
  }
  $('#clear-journal').onclick = () => {
    setJournal('');
    const url = new URL(location.href); url.searchParams.delete('journal');
    history.replaceState(null, '', url);
    write('sessionStorage', jobKey, '');
    if (snapshot) action('已取消期刊限定，点击“检索文献”获取新结果；下方仍是上次结果。');
    $('#literature-query').focus();
  };
  applyJournal();
  function sourceCount() { const n = sourceBoxes.filter(x => x.checked).length; $('#selected-libraries').textContent = n === 11 ? '全部 11 类来源' : `已选 ${n} / 11 类来源`; }
  $('#search-libraries').addEventListener('change', sourceCount);
  $('#select-libraries').onclick = () => { sourceBoxes.forEach(x => { x.checked = true; }); sourceCount(); };
  $('#clear-libraries').onclick = () => { sourceBoxes.forEach(x => { x.checked = false; }); sourceCount(); };
  function busy(value) {
    searching = value; $('#literature-submit').disabled = value; $('#search-stop').hidden = !value;
    $('#search-order').disabled = value;
    $('#literature-submit').textContent = value ? '检索中…' : '检索文献';
  }
  $('#literature-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const sources = sourceBoxes.filter(x => x.checked).map(x => x.value);
    if (!sources.length) { action('请至少选择一个来源。'); return; }
    if ($('#search-since').value > $('#search-until').value) { action('起始日期不能晚于结束日期。'); return; }
    busy(true); action(''); clearTimeout(timer);
    $('#literature-status').textContent = '正在提交检索请求…';
    try {
      if (!connected) await connect();
      const result = await api('/api/search', {method: 'POST', body: JSON.stringify({query: $('#literature-query').value.trim(), sources, since: $('#search-since').value, until: $('#search-until').value, limit: Number($('#search-limit').value), journal_issn: journalIssn, sort_by: $('#search-order').value})});
      job = result.id; write('sessionStorage', jobKey, job); signature = ''; snapshot = null;
      $('#literature-results').replaceChildren(); $('#search-output').hidden = true;
      $('#search-result-source').value = '';
      $('#search-result-sort').value = 'retrieved';
      await poll();
    } catch (error) { busy(false); action(error.message); }
  });
  async function poll() {
    try {
      snapshot = await api('/api/search/' + job);
      if (restoreForm && snapshot.request) {
        const saved = snapshot.request;
        $('#literature-query').value = saved.query;
        $('#search-since').value = saved.since; $('#search-until').value = saved.until;
        $('#search-limit').value = String(saved.limit);
        $('#search-order').value = saved.sort_by || 'relevance'; orderDescription();
        sourceBoxes.forEach(box => { box.checked = saved.journal_issn ? true : saved.sources.includes(box.value); });
        previousSources = null; setJournal(saved.journal_issn || '');
        sourceCount(); restoreForm = false;
      }
      if (!$('#literature-query').value) $('#literature-query').value = snapshot.query;
      busy(snapshot.state === 'running'); draw();
      if (searching) timer = setTimeout(poll, 1600);
    } catch (error) {
      busy(false); action(error.message + ' 可再次提交同样的检索，已完成来源会复用缓存。');
    }
  }
  $('#search-stop').onclick = async () => {
    try { clearTimeout(timer); snapshot = await api('/api/search/' + job + '/stop', {method: 'POST'}); busy(false); draw(); }
    catch (error) { action(error.message); }
  };
  function node(tag, text, className) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (className) n.className = className; return n; }
  function link(text, url) { const a = node('a', text); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a; }
  function filtered() {
    const source = $('#search-result-source').value;
    const rows = (snapshot?.records || []).filter(p => !source || p.sources.includes(source));
    const mode = $('#search-result-sort').value;
    if (mode !== 'retrieved') rows.sort((a, b) => (a.ranks?.[mode] ?? Infinity) - (b.ranks?.[mode] ?? Infinity));
    return rows;
  }
  function draw() {
    const done = snapshot.sources.filter(s => !['queued', 'running'].includes(s.state)).length;
    const failed = snapshot.sources.filter(s => !['ok', 'no_data', 'queued', 'running'].includes(s.state)).length;
    $('#literature-status').textContent = `${snapshot.state === 'running' ? '正在检索' : snapshot.state === 'cancelled' ? '已停止检索' : '检索完成'} · ${done} / ${snapshot.sources.length} 类来源完成 · ${snapshot.records.length} 篇去重结果${failed ? ` · ${failed} 类来源未完整返回` : ''}`;
    $('#search-source-report').hidden = false;
    if (failed && !snapshot.records.length) $('#search-source-report').open = true;
    $('#search-source-states').replaceChildren(...snapshot.sources.map(s => {
      const row = node('div', undefined, 'search-source-state'); row.dataset.state = s.state;
      row.append(node('strong', s.id)); const detail = node('div', `${s.label} · ${s.count} 条${s.cached ? ' · 缓存' : ''}`);
      detail.append(node('small', s.detail || s.mode));
      if (s.sort_note) detail.append(node('small', s.sort_note));
      if (s.search_url) detail.append(link('在搜狗微信继续检索', s.search_url));
      row.append(detail); return row;
    }));
    const source = $('#search-result-source').value;
    $('#search-result-source').replaceChildren(new Option('全部来源', ''), ...snapshot.sources.map(s => new Option(s.id, s.id)));
    $('#search-result-source').value = source;
    $('#search-output').hidden = false;
    drawResults();
  }
  function drawResults() {
    const rows = filtered(); $('#search-result-count').textContent = rows.length + ' 篇';
    const mode = $('#search-result-sort').value;
    const applied = snapshot?.request?.sort_by || 'relevance';
    $('#search-result-sort').options[0].textContent = '检索顺序 · ' + orderLabels[applied];
    const citationMode = (mode === 'retrieved' ? applied : mode) === 'citations';
    const missing = rows.filter(p => p.citation_count == null).length;
    $('#search-result-order-help').textContent = (mode === 'retrieved'
      ? `本次检索：${orderLabels[applied]}。各来源实际排序方式见“来源检索状态”。`
      : `仅重排已返回的 ${rows.length} 条结果，不发起新检索、不增加 API 请求。`) + (citationMode
      ? ` 同篇文献取各来源报告的最高被引次数，不相加；不同数据库口径有差异。${missing ? ` ${missing} 条未提供被引数据，排在最后。` : ''}` : '');
    $('#export-references').disabled = !rows.length;
    const next = JSON.stringify([rows, searching]);
    if (next === signature) return;
    signature = next;
    const open = new Set([...document.querySelectorAll('.search-reference[open]')].map(x => x.dataset.id));
    const elements = rows.map(p => {
      const article = node('article', undefined, 'search-result');
      const meta = node('div', undefined, 'search-result-meta');
      meta.append(node('span', p.sources.join(' / ')), node('time', p.published_at || '日期未提供'));
      const counts = p.citation_counts || [];
      const citations = node('span', p.citation_count == null ? '被引数据未提供' : `被引 ${p.citation_count.toLocaleString('zh-CN')} 次`, 'search-result-citations');
      citations.title = counts.length ? counts.map(c => `${c.source}：${c.count} 次`).join('；') : '来源未提供可比的被引次数，不表示 0 次。';
      meta.append(citations);
      article.append(meta);
      const heading = node('h3'); heading.append(p.landing_url ? link(p.title, p.landing_url) : node('span', p.title)); article.append(heading);
      article.append(node('p', [p.authors.slice(0, 4).join(', ') + (p.authors.length > 4 ? ' 等' : ''), p.venue].filter(Boolean).join(' · '), 'search-result-authors'));
      if (p.abstract) article.append(node('p', p.abstract.slice(0, 520) + (p.abstract.length > 520 ? '…' : ''), 'search-result-abstract'));
      const actions = node('div', undefined, 'search-result-actions');
      if (p.landing_url) actions.append(link(p.link_kind === 'search_results' ? '公开检索线索' : '原文链接', p.landing_url));
      if (p.pdf_url && p.pdf_url !== p.landing_url) actions.append(link('全文入口', p.pdf_url));
      if (p.can_download) {
        const download = node('button', '下载 PDF'); download.type = 'button';
        download.onclick = async () => {
          download.disabled = true; download.textContent = '正在获取 PDF…'; action('正在尝试获取公开 PDF，请稍候。');
          try { const blob = await api(`/api/search/${job}/${p.id}/pdf`, {}, true, true); save(blob, `paper-${p.id}.pdf`); action('PDF 已下载。'); }
          catch (error) { action(error.message); }
          finally { download.disabled = false; download.textContent = '下载 PDF'; }
        };
        actions.append(download);
      } else { actions.append(node('span', '暂无直接 PDF')); }
      const details = node('details', undefined, 'search-reference'); details.dataset.id = p.id; details.open = open.has(p.id);
      details.append(node('summary', 'GB/T 7714 引用'));
      const citation = node('textarea'); citation.value = p.citation.text; citation.readOnly = true; citation.setAttribute('aria-label', 'GB/T 7714-2025 引用');
      const copy = node('button', '复制引用'); copy.type = 'button';
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(citation.value); action('GB/T 7714 引用已复制。'); }
        catch { citation.focus(); citation.select(); action('已选中引用，请按 Ctrl+C（Mac 使用 ⌘C）复制。'); }
      };
      details.append(citation, copy);
      if (p.citation.notes.length) details.append(node('p', '待核对：' + p.citation.notes.join('；') + '。'));
      article.append(actions, details); return article;
    });
    const incomplete = (snapshot?.sources || []).some(s => !['ok', 'no_data', 'queued', 'running'].includes(s.state));
    const empty = searching ? '结果正在返回，请稍候。' : incomplete && !snapshot.records.length
      ? '部分来源未完成检索，暂未取得结果。请查看上方具体原因；这不代表没有相关文章。'
      : '没有符合条件的结果。可扩大时间范围、减少关键词，或查看来源状态。';
    $('#literature-results').replaceChildren(...(elements.length ? elements : [node('p', empty, 'empty-state')]));
  }
  for (const id of ['#search-result-source', '#search-result-sort']) $(id).addEventListener('change', () => { signature = ''; drawResults(); });
  function save(blob, filename) { const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 10000); }
  $('#export-references').onclick = () => {
    const rows = filtered();
    const text = rows.map((p, i) => `[${i + 1}] ${p.citation.text}`).join('\n\n');
    save(new Blob(['\uFEFF' + text], {type: 'text/plain;charset=utf-8'}), 'references-GB-T-7714-2025.txt');
    action('已导出当前筛选结果的引用。请按条目提示核对缺项。');
  };
  if (local) {
    document.querySelectorAll('a[href]').forEach(a => {
      const url = new URL(a.href);
      if (url.origin === base && !['/search.html', '/journals.html'].includes(url.pathname) && !a.getAttribute('href').startsWith('#')) a.href = 'https://tengda-xmu.github.io/daily-papers/' + (url.pathname === '/' ? '' : url.pathname.slice(1));
    });
  }
  connect().then(async () => {
    job = read('sessionStorage', jobKey);
    if (job && !journalIssn) { restoreForm = true; await poll(); }
  }).catch(error => { setConnection(false, error.message); });
  window.addEventListener('focus', () => { if (!connected) connect().catch(() => {}); });
})();
