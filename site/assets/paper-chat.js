(() => {
  'use strict';
  const base = 'http://127.0.0.1:43127';
  const local = document.body.dataset.codexLocal === 'true';
  const key = 'daily-papers-codex-session';
  const session = local ? localStorage : sessionStorage;
  let token = session.getItem(key) || '';
  let paperId = '', mode = 'question', controller = null, busy = false, history = [], pairCode = '';
  let refreshTimer = null;
  let lastTrigger = null;
  const dialog = document.createElement('dialog');
  dialog.className = 'paper-chat';
  dialog.setAttribute('aria-labelledby', 'chat-heading');
  dialog.innerHTML = `<div class="chat-shell">
    <header class="chat-header"><div class="chat-heading-row"><h2 id="chat-heading">Codex 论文对话</h2><button class="chat-close" type="button" aria-label="关闭对话">×</button></div><p class="chat-paper-title"></p><p class="chat-status" role="status">未连接本机 Codex</p></header>
    <section class="chat-connect"><p>先运行项目中的“启动论文助手.cmd”，再粘贴本次启动的配对码。</p><div class="chat-pair-row"><input type="password" autocomplete="off" aria-label="本机配对码" placeholder="本机配对码"><button class="chat-button" type="button" data-chat-action="connect">连接</button></div><p class="chat-local-help"><a class="chat-local-link" target="_blank" rel="noopener noreferrer">在本机打开论文助手</a> · 电脑须保持运行</p></section>
    <details class="chat-documents"><summary>阅读依据：摘要与本站解读</summary><div class="chat-doc-actions"><button type="button" class="chat-button" data-chat-action="fulltext">获取开放全文</button><button type="button" class="chat-button" data-chat-action="upload">上传 PDF</button></div><input type="file" accept="application/pdf,.pdf" hidden><p class="chat-document-note">尚未读取全文。上传 PDF 仅保存在本机，最多 20 MB / 300 页。</p></details>
    <div class="chat-messages" aria-label="对话记录"></div>
    <div class="chat-history-actions"><button class="text-button" type="button" data-chat-action="export">导出对话</button><button class="text-button" type="button" data-chat-action="clear">清除本机记录</button></div>
    <form class="chat-composer"><div class="chat-shortcuts"><button class="chat-button" type="button" data-mode="summary">总结论文</button><button class="chat-button" type="button" data-mode="question" aria-pressed="true">深入提问</button><button class="chat-button" type="button" data-mode="translate">中英翻译</button><button class="chat-button" type="button" data-mode="figure">解释配图</button></div><textarea aria-label="向 Codex 提问" placeholder="例如：这篇论文的方法能如何用于结构可靠性分析？" maxlength="12000"></textarea><div class="chat-form-footer"><label>PDF 页码 <input class="chat-pages" aria-label="PDF 页码" placeholder="如 1-3,5"></label><div><button class="chat-button" type="button" data-chat-action="stop" hidden>停止</button> <button class="chat-send" type="submit">发送</button></div></div><p class="chat-notice" role="status">回答使用你的 Codex 账号额度。</p></form>
  </div>`;
  document.body.append(dialog);
  const $ = (selector) => dialog.querySelector(selector);
  const status = (text, state = '') => { $('.chat-status').textContent = text; $('.chat-status').dataset.state = state; };
  const notice = (text) => { $('.chat-notice').textContent = text; };
  const endpoint = (suffix = '') => `/api/papers/${paperId}${suffix}`;
  const pairControls = [];
  function setPairCode(code) {
    pairCode = code;
    pairControls.forEach(control => {
      control.querySelector('.pair-code-value').value = code;
      control.querySelectorAll('button').forEach(button => { button.disabled = !code; });
      control.querySelector('.pair-code-status').textContent = code ? '' : '请双击“启动论文助手.cmd”，在打开的本机页面获取配对码。';
    });
  }
  function showPairCode(control, text) {
    control.querySelector('.pair-code-manual').hidden = false;
    const input = control.querySelector('.pair-code-value');
    input.focus(); input.select();
    control.querySelector('.pair-code-status').textContent = text;
  }
  async function copyPairCode(control) {
    if (!pairCode) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(pairCode);
      control.querySelector('.pair-code-status').textContent = '已复制配对码，可粘贴到论文网站的连接框。';
    } catch {
      showPairCode(control, '浏览器未允许自动复制。已选中配对码，请按 Ctrl+C（Mac：⌘C），或长按复制。');
    }
  }
  if (local) {
    const sidebarPair = document.createElement('section');
    sidebarPair.className = 'local-pair-controls';
    $('.chat-header').append(sidebarPair);
    pairControls.push(sidebarPair, document.getElementById('local-pair-help'));
    pairControls.forEach(control => {
      control.hidden = false;
      control.setAttribute('aria-label', '连接在线论文网站');
      control.innerHTML = `<div class="pair-code-actions"><button type="button" class="chat-button pair-code-copy">复制配对码</button><button type="button" class="text-button pair-code-show">手动复制</button></div><label class="pair-code-manual" hidden>本次启动的配对码<input class="pair-code-value" type="text" readonly autocomplete="off" spellcheck="false"></label><p class="pair-code-status" role="status" aria-live="polite"></p>`;
      control.querySelector('.pair-code-copy').onclick = () => copyPairCode(control);
      control.querySelector('.pair-code-show').onclick = () => showPairCode(control, '已选中配对码，请按 Ctrl+C（Mac：⌘C），或长按复制。');
      control.querySelector('.pair-code-value').onclick = event => event.target.select();
    });
    setPairCode('');
  }
  const setBusy = (value) => {
    busy = value;
    $('.chat-send').disabled = value;
    $('[data-chat-action="stop"]').hidden = !value;
    dialog.querySelectorAll('[data-mode], [data-chat-action="fulltext"], [data-chat-action="upload"], [data-chat-action="clear"]').forEach(b => { b.disabled = value; });
  };

  async function api(path, options = {}) {
    const headers = { Authorization: `Bearer ${token}`, ...options.headers };
    if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
    let response;
    try { response = await fetch(base + path, { ...options, headers, signal: options.signal || AbortSignal.timeout(120000) }); }
    catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error('无法连接本机。请启动论文助手、允许浏览器访问本地网络，或使用“在本机打开”入口。');
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) { token = ''; session.removeItem(key); $('.chat-connect').hidden = false; }
      const err = new Error(data.message || (typeof data.detail === 'string' ? data.detail : '请求未完成，请重试。'));
      err.state = data.state;
      throw err;
    }
    return options.stream ? response : response.json();
  }

  function inline(parent, text) {
    text = text.replace(/\[([PS]\d+(?:\s*(?:[、,，]|[-–—])\s*[PS]?\d+)*)\]/g, (full, inner) => {
      let prefix = inner[0];
      const labels = [];
      for (const group of inner.split(/[、,，]/)) {
        const match = /^\s*([PS])?(\d+)(?:\s*[-–—]\s*[PS]?(\d+))?\s*$/.exec(group);
        if (!match) return full;
        prefix = match[1] || prefix;
        const first = Number(match[2]), last = Number(match[3] || match[2]);
        if (last < first || last - first > 8) return full;
        for (let n = first; n <= last; n++) labels.push(`[${prefix}${n}]`);
      }
      return labels.join(' ');
    });
    const pieces = text.split(/(\*\*[^*]+\*\*|\[(?:P|S)\d+\])/g);
    for (const piece of pieces) {
      if (/^\[(P|S)\d+\]$/.test(piece)) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'chat-cite'; button.textContent = piece;
        button.addEventListener('click', async () => {
          const old = parent.closest('.chat-message').querySelector('.chat-citation');
          if (old) old.remove();
          const box = document.createElement('div'); box.className = 'chat-citation';
          parent.closest('.chat-message').append(box);
          try {
            const version = parent.closest('.chat-message').dataset.documentHash;
            if (!version) throw new Error('此回答未关联全文资料版本，请对照原始资料核查引用。');
            const src = await api(endpoint(`/source/${piece.slice(1,-1)}?version=${encodeURIComponent(version)}`));
            box.textContent = `${piece}\n${src.scan ? '此页为扫描图片，需对照原 PDF 核查。' : src.text}`;
          }
          catch (e) { box.textContent = e.message; }
        });
        parent.append(button);
      } else if (piece.startsWith('**') && piece.endsWith('**')) {
        const strong = document.createElement('strong'); strong.textContent = piece.slice(2,-2); parent.append(strong);
      } else parent.append(document.createTextNode(piece));
    }
  }

  function format(body, text) {
    body.replaceChildren();
    for (const line of text.split('\n')) {
      const heading = /^#{1,4}\s+(.+)$/.exec(line);
      const el = document.createElement(heading ? 'h3' : 'div');
      inline(el, heading ? heading[1] : line || '\u00a0');
      body.append(el);
    }
  }

  function message(role, text, state = 'completed', documentHash = '') {
    const article = document.createElement('article'); article.className = 'chat-message'; article.dataset.role = role;
    article.dataset.documentHash = documentHash || '';
    const label = document.createElement('div'); label.className = 'chat-message-label';
    const name = document.createElement('span'); name.textContent = role === 'user' ? '你' : `Codex${state === 'interrupted' ? ' · 已停止' : state === 'failed' ? ' · 未完成' : ''}`;
    label.append(name);
    const body = document.createElement('div'); body.className = 'chat-message-body';
    if (role === 'assistant') {
      const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'text-button'; copy.textContent = '复制';
      copy.onclick = () => navigator.clipboard.writeText(body.innerText).then(() => notice('已复制回答。')).catch(() => notice('复制失败，可选中文字后复制。'));
      label.append(copy);
    }
    format(body, text || (state === 'failed' ? '本次没有生成完整回答。' : state === 'running' ? '正在阅读资料并生成回答…' : ''));
    article.append(label, body); $('.chat-messages').append(article);
    return body;
  }

  async function loadPaper() {
    if (!paperId) return;
    clearTimeout(refreshTimer);
    const currentPaper = paperId;
    const data = await api(endpoint());
    if (currentPaper !== paperId) return;
    history = data.history;
    $('.chat-paper-title').textContent = data.paper.title_zh || data.paper.title;
    $('.chat-messages').replaceChildren();
    if (!history.length) {
      const p = document.createElement('p'); p.className = 'chat-empty';
      p.textContent = '从一个具体问题开始。你可以先总结这篇论文，再追问方法、实验依据或研究启发。'; $('.chat-messages').append(p);
    }
    for (const row of history) message(row.role, row.content, row.status, row.document_hash);
    const doc = data.document;
    $('.chat-documents summary').textContent = `阅读依据：${doc ? doc.name : '摘要与本站解读'}`;
    $('.chat-document-note').textContent = doc ? `${doc.kind === 'pdf' ? `${doc.page_count} 页，其中 ${doc.scan_pages} 页需图片识别。` : '正文按段落编号，可点击回答中的引用核查。'} 资料已更新时会建立新上下文；上传内容请与论文标题核对。` : '尚未读取全文。上传 PDF 仅保存在本机，最多 20 MB / 300 页。';
    $('.chat-messages').scrollTop = $('.chat-messages').scrollHeight;
    if (!controller) {
      setBusy(data.busy);
      if (data.busy && dialog.open) refreshTimer = setTimeout(() => loadPaper().catch(e => notice(e.message)), 3000);
    }
  }

  async function connect() {
    status('正在连接本机 Codex…');
    try {
      if (!token) {
        const code = $('.chat-pair-row input').value.trim();
        if (!code) throw new Error('请先运行启动脚本，复制本机页面中的配对码。');
        const data = await api('/api/pair', { method: 'POST', body: JSON.stringify({ code }) });
        token = data.token; session.setItem(key, token); $('.chat-pair-row input').value = '';
      }
      if (local) {
        const pairing = await api('/api/pairing-code', { method: 'POST' });
        setPairCode(pairing.code);
      }
      const data = await api('/api/connect', { method: 'POST' });
      status(`已连接 · ${data.model}`, 'connected'); $('.chat-connect').hidden = true;
      if (local) {
        const list = await api('/api/papers');
        const select = document.getElementById('local-paper-list'); select.replaceChildren();
        list.papers.forEach(p => { const option = document.createElement('option'); option.value = p.id; option.textContent = p.title; select.append(option); });
        if (!paperId) paperId = select.value;
        select.value = paperId;
      }
      await loadPaper();
    } catch (error) { status(error.message, error.state || 'error'); $('.chat-connect').hidden = false; }
  }

  async function open(id, title, trigger) {
    if (busy) await stop();
    paperId = id; lastTrigger = trigger;
    $('.chat-paper-title').textContent = title || '选择一篇论文开始';
    $('.chat-local-link').href = `${base}/?paper=${encodeURIComponent(id || '')}`;
    if (!dialog.open) dialog.showModal();
    if (token) await connect();
    else { $('.chat-connect').hidden = false; $('.chat-messages').replaceChildren(); status('未连接本机 Codex'); }
  }

  async function stop() {
    clearTimeout(refreshTimer);
    if (busy || controller) {
      await api(endpoint('/stop'), { method: 'POST' }).catch(() => {});
      if (controller) controller.abort(); controller = null;
      setBusy(false);
    }
  }

  async function send(event) {
    event.preventDefault();
    const text = $('.chat-composer textarea').value.trim();
    if (!text || busy) return;
    if (!token) { notice('请先配对并连接本机 Codex。'); return; }
    if (!paperId) { notice('请先选择一篇论文。'); return; }
    const empty = $('.chat-empty'); if (empty) empty.remove();
    message('user', text); const body = message('assistant', '');
    let answer = '', done = false;
    setBusy(true); notice('Codex 正在阅读资料…');
    controller = new AbortController();
    try {
      const response = await api('/api/ask', { method: 'POST', stream: true, signal: controller.signal,
        body: JSON.stringify({ paper_id: paperId, message: text, mode, pages: $('.chat-pages').value, request_id: crypto.randomUUID() }) });
      $('.chat-composer textarea').value = '';
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
      while (true) {
        const part = await reader.read(); if (part.done) break;
        buffer += decoder.decode(part.value, { stream: true });
        let end;
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          if (!frame.startsWith('data: ')) continue;
          const value = JSON.parse(frame.slice(6));
          if (value.type === 'delta') { answer += value.text; body.textContent = answer; $('.chat-messages').scrollTop = $('.chat-messages').scrollHeight; }
          if (value.type === 'progress') notice(value.message);
          if (value.type === 'error') { status(value.message, value.state); notice(value.message); if (!answer) $('.chat-composer textarea').value = text; }
          if (value.type === 'done') { done = true; if (value.status === 'completed') notice('已完成。点击引用可核查原文；可继续追问。'); }
        }
      }
      if (!done) notice('连接中断，已收到的内容已保留。');
      format(body, answer);
    } catch (error) { if (error.name !== 'AbortError') notice(error.message); else notice('已停止生成。'); }
    finally { controller = null; setBusy(false); await loadPaper().catch(() => {}); }
  }

  async function action(name) {
    try {
      if (name === 'connect') return await connect();
      if (name === 'stop') return await stop();
      if (!token || !paperId) { notice('请先连接并选择论文。'); return; }
      if (name === 'upload') return $('.chat-documents input[type="file"]').click();
      if (name === 'fulltext') { notice('正在获取开放全文…'); const data = await api(endpoint('/fulltext'), { method: 'POST' }); await loadPaper(); notice(data.message); }
      if (name === 'clear' && window.confirm('清除这篇论文在本机助手中的资料和对话？Codex 自身会话历史仍由 Codex 管理。')) {
        const data = await api(endpoint(), { method: 'DELETE' }); await loadPaper(); notice(data.message);
      }
      if (name === 'export') {
        const data = await api(endpoint());
        const md = `# ${data.paper.title_zh || data.paper.title}\n\n` + data.history.map(m => `## ${m.role === 'user' ? '你' : 'Codex'}${m.status === 'completed' ? '' : '（未完成）'}\n\n${m.content}`).join('\n\n');
        const url = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
        const a = document.createElement('a'); a.href = url; a.download = `${paperId}-Codex.md`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) { notice(error.message); }
  }
  dialog.addEventListener('click', event => {
    const button = event.target.closest('[data-chat-action]'); if (button) action(button.dataset.chatAction);
    const shortcut = event.target.closest('[data-mode]');
    if (shortcut) {
      mode = shortcut.dataset.mode;
      dialog.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b === shortcut)));
      const input = $('.chat-composer textarea');
      const prompts = { summary: '请用中文总结这篇论文的研究问题、方法、主要发现与证据、局限，以及对我的研究方向的启发。', figure: '请解释这张论文配图的模块、信息流、方法原理及与研究结论的关系。' };
      if (prompts[mode]) input.value = prompts[mode];
      input.placeholder = mode === 'translate' ? '粘贴要翻译的段落，或填写章节/下方 PDF 页码。' : '输入你想进一步讨论的问题…';
      input.focus();
    }
  });
  $('.chat-documents input[type="file"]').addEventListener('change', async (event) => {
    const file = event.target.files[0]; if (!file) return;
    if (file.size > 20 * 1024 * 1024) { notice('PDF 超过 20 MB。'); return; }
    notice('正在解析 PDF…'); const form = new FormData(); form.append('file', file);
    try { const data = await api(endpoint('/pdf'), { method: 'POST', body: form }); await loadPaper(); notice(data.message); }
    catch (error) { notice(error.message); } finally { event.target.value = ''; }
  });
  $('.chat-composer').addEventListener('submit', send);
  $('.chat-composer textarea').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('.chat-composer').requestSubmit(); } });
  $('.chat-close').onclick = async () => { await stop(); dialog.close(); if (lastTrigger) lastTrigger.focus(); };
  dialog.addEventListener('cancel', () => { stop(); });
  document.addEventListener('click', event => { const button = event.target.closest('.codex-entry'); if (button) open(button.dataset.paperId, button.dataset.paperTitle, button); });
  if (local) {
    const hash = new URLSearchParams(location.hash.slice(1)); setPairCode(hash.get('pair') || '');
    if (pairCode) { historyReplace(); $('.chat-pair-row input').value = pairCode; }
    paperId = new URLSearchParams(location.search).get('paper') || '';
    document.getElementById('local-open-chat').onclick = () => open(document.getElementById('local-paper-list').value || paperId, '', null);
    open(paperId, '', null).then(() => { if (pairCode && !token) connect(); });
  }
  function historyReplace() { window.history.replaceState(null, '', location.pathname + location.search); }
})();
