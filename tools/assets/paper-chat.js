(() => {
  'use strict';
  const base = 'http://127.0.0.1:43127';
  const local = document.body.dataset.codexLocal === 'true';
  const key = 'daily-papers-codex-session';
  const session = local ? localStorage : sessionStorage;
  let token = session.getItem(key) || '';
  let connectedModel = '';
  const modelKey = 'daily-papers-codex-model';
  let availableModels = [];
  let paperId = '', mode = 'question', controller = null, busy = false, history = [], pairCode = '';
  let refreshTimer = null;
  let documentPending = false, remoteDocumentPending = false, uploadPaper = '';
  let progressTimer = null;
  let lastTrigger = null;
  const drafts = new Map();
  const modes = {
    question: { prompt: '', hint: '围绕这篇论文提出具体问题，回答会附关键证据、分析依据和适用边界。', placeholder: '例如：作者如何验证可靠性？哪些证据支持这一结论？', send: '发送问题' },
    summary: { prompt: '请用中文总结这篇论文的研究问题、方法、主要发现与证据、局限，以及对我的研究方向的启发。', hint: '按研究问题、方法、证据与局限总结；未加载全文时会明确资料范围。', placeholder: '填写你希望重点总结的内容…', send: '开始总结' },
    translate: { prompt: '', hint: '逐段对照翻译，保留公式、数字、单位和专业术语。', placeholder: '粘贴需要翻译的原文…', send: '开始翻译' },
    figure: { prompt: '请解释这张论文配图的模块、信息流、方法原理及与研究结论的关系。', hint: '读取实际配图，解释模块和信息流；也可上传 PDF 后指定图片所在页。', placeholder: '填写你想了解的图中模块或连接关系…', send: '解释配图' },
  };
  const dialog = document.createElement('dialog');
  dialog.className = 'paper-chat';
  dialog.setAttribute('aria-labelledby', 'chat-heading');
  dialog.innerHTML = `<div class="chat-shell">
    <header class="chat-header"><div class="chat-heading-row"><h2 id="chat-heading">Codex 论文对话</h2><button class="chat-close" type="button" aria-label="关闭对话">×</button></div><p class="chat-paper-title"></p><p class="chat-status" role="status">未连接本机 Codex</p><div class="chat-model-row"><label for="chat-model">模型</label><select id="chat-model" aria-describedby="chat-model-help" disabled><option value="">连接后加载可用模型</option></select></div><p class="chat-model-help" id="chat-model-help">选择将用于下一次发送。</p></header>
    <section class="chat-connect"><p>先运行项目中的“启动论文助手.cmd”，再粘贴本次启动的配对码。</p><div class="chat-pair-row"><input type="password" autocomplete="off" aria-label="本机配对码" placeholder="本机配对码"><button class="chat-button" type="button" data-chat-action="connect">连接</button></div><p class="chat-local-help"><a class="chat-local-link" target="_blank" rel="noopener noreferrer">在本机打开论文助手</a> · 电脑须保持运行</p></section>
    <section class="chat-documents" aria-label="论文资料">
      <div class="chat-doc-actions"><button type="button" class="chat-button" data-chat-action="upload">上传 PDF</button><button type="button" class="chat-button" data-chat-action="fetch-pdf">获取论文 PDF</button></div>
      <p class="chat-document-status" role="status">未载入 PDF，可上传或直接获取。</p>
      <details><summary>阅读依据：摘要与本站解读</summary><p class="chat-document-note">文件仅保存在本机，最多 20 MB / 300 页。</p><button type="button" class="text-button" data-chat-action="fulltext">获取网页全文</button></details>
      <input type="file" accept="application/pdf,.pdf" hidden>
    </section>
    <div class="chat-messages" aria-label="对话记录"></div>
    <div class="chat-history-actions"><button class="text-button" type="button" data-chat-action="export">导出对话</button><button class="text-button" type="button" data-chat-action="clear">清除本机记录</button></div>
    <form class="chat-composer">
      <div class="chat-shortcuts"><button class="chat-button" type="button" data-mode="summary">总结论文</button><button class="chat-button" type="button" data-mode="question" aria-pressed="true">深入提问</button><button class="chat-button" type="button" data-mode="translate">中英翻译</button><button class="chat-button" type="button" data-mode="figure">解释配图</button></div>
      <p class="chat-mode-help" id="chat-mode-help"></p>
      <div class="chat-translation" hidden><label>方向 <select class="chat-translation-target"><option value="zh">英译中</option><option value="en">中译英</option></select></label><label>内容 <select class="chat-translation-source"><option value="text">粘贴原文</option><option value="document">论文章节 / 页码</option></select></label></div>
      <textarea aria-label="向 Codex 提问" aria-describedby="chat-mode-help" maxlength="12000"></textarea>
      <div class="chat-form-footer"><label class="chat-pages-label">PDF 页码 <input class="chat-pages" aria-label="PDF 页码" placeholder="如 1-3,5"></label><div><button class="chat-button" type="button" data-chat-action="stop" hidden>停止</button> <button class="chat-send" type="submit">发送问题</button></div></div><p class="chat-notice" role="status">回答使用你的 Codex 账号额度。</p>
    </form>
  </div>`;
  document.body.append(dialog);
  const $ = (selector) => dialog.querySelector(selector);
  const status = (text, state = '') => { $('.chat-status').textContent = text; $('.chat-status').dataset.state = state; };
  const notice = (text) => { $('.chat-notice').textContent = text; };
  const documentNotice = (text, state = '') => { $('.chat-document-status').textContent = text; $('.chat-document-status').dataset.state = state; };
  const endpoint = (suffix = '') => `/api/papers/${paperId}${suffix}`;
  function chosenModel() { return availableModels.find(m => m.id === $('#chat-model').value); }
  function updateModelHint() {
    const selected = chosenModel();
    $('.chat-model-help').textContent = selected ? `${selected.images ? '支持文字与图片' : '仅支持文字'} · 用于下一次发送` : '此前所选模型当前不可用，请重新选择。';
    $('.chat-send').disabled = busy || documentPending || remoteDocumentPending || !selected;
  }
  function loadModels(data) {
    availableModels = data.models || [{id: data.model, label: data.model, images: data.images, is_default: true}];
    const wanted = localStorage.getItem(modelKey) || data.model;
    const select = $('#chat-model'); select.replaceChildren();
    for (const item of availableModels) {
      const option = document.createElement('option'); option.value = item.id;
      option.textContent = `${item.label}${item.is_default ? '（账号默认）' : ''}`; select.append(option);
    }
    if (!availableModels.some(m => m.id === wanted)) {
      const unavailable = document.createElement('option'); unavailable.value = wanted;
      unavailable.textContent = `${wanted}（当前不可用，请重新选择）`; unavailable.disabled = true; select.prepend(unavailable);
    }
    select.value = wanted; select.disabled = busy;
    updateModelHint();
  }
  const draftKey = () => `${paperId}:${mode}`;
  function updateMode() {
    const config = modes[mode], translating = mode === 'translate';
    const documentTranslation = translating && $('.chat-translation-source').value === 'document';
    $('.chat-translation').hidden = !translating;
    $('.chat-mode-help').textContent = config.hint;
    $('.chat-composer textarea').placeholder = documentTranslation ? '例如：翻译 Abstract；或在下方指定 PDF 页码。' : config.placeholder;
    $('.chat-send').textContent = config.send;
    $('.chat-pages-label').hidden = translating && !documentTranslation;
    dialog.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
  }
  function rememberDraft() { drafts.set(draftKey(), { text: $('.chat-composer textarea').value, pages: $('.chat-pages').value }); }
  function restoreDraft() {
    const draft = drafts.get(draftKey());
    $('.chat-composer textarea').value = draft ? draft.text : modes[mode].prompt;
    $('.chat-pages').value = draft?.pages || '';
    updateMode();
  }
  function tickProgress() {
    dialog.querySelectorAll('.chat-progress').forEach(box => {
      const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - Number(box.dataset.started)));
      const silence = Math.max(0, Math.floor((Date.now() - Number(box.dataset.received)) / 1000));
      box.querySelector('.chat-progress-time').textContent = `已用时 ${Math.floor(elapsed / 60)}分${elapsed % 60}秒 · 上次响应 ${silence} 秒前${silence > 35 ? '；尚无新响应，可点击停止' : elapsed > 90 ? '；可继续等待，或停止后缩小问题范围' : ''}`;
    });
  }
  function showProgress(body, progress = {}) {
    const article = body.closest('.chat-message');
    let box = article.querySelector('.chat-progress');
    if (!box) {
      box = document.createElement('div'); box.className = 'chat-progress';
      box.innerHTML = '<p class="chat-progress-stage" role="status"></p><p class="chat-progress-time"></p><details><summary>处理记录</summary><ol></ol></details>';
      box.dataset.started = String(progress.started_at || Date.now() / 1000);
      article.insertBefore(box, body);
    }
    box.dataset.received = String(Date.now());
    if (progress.started_at) box.dataset.started = String(progress.started_at);
    const stage = progress.message || '请求已发出，等待本机连接程序接收';
    if (box.querySelector('.chat-progress-stage').textContent !== stage) {
      box.querySelector('.chat-progress-stage').textContent = stage;
      const entry = document.createElement('li'); entry.textContent = stage;
      const list = box.querySelector('ol'); list.append(entry);
      if (list.children.length > 8) list.firstChild.remove();
    }
    tickProgress();
  }
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
    clearInterval(progressTimer);
    if (value) progressTimer = setInterval(tickProgress, 1000);
    const locked = value || documentPending || remoteDocumentPending;
    $('.chat-documents').setAttribute('aria-busy', String(documentPending || remoteDocumentPending));
    $('.chat-send').disabled = locked || !chosenModel();
    $('#chat-model').disabled = locked || !availableModels.length;
    $('[data-chat-action="stop"]').hidden = !value;
    dialog.querySelectorAll('[data-mode], .chat-translation select, [data-chat-action="fulltext"], [data-chat-action="fetch-pdf"], [data-chat-action="upload"], [data-chat-action="clear"]').forEach(b => { b.disabled = locked; });
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

  function message(role, text, state = 'completed', documentHash = '', error = '', model = '') {
    const article = document.createElement('article'); article.className = 'chat-message'; article.dataset.role = role;
    article.dataset.documentHash = documentHash || '';
    const label = document.createElement('div'); label.className = 'chat-message-label';
    const name = document.createElement('span'); name.className = 'chat-message-name'; name.textContent = role === 'user' ? '你' : `Codex${model ? ` · ${model}` : ''}${state === 'interrupted' ? ' · 已停止' : state === 'failed' ? ' · 未完成' : ''}`;
    label.append(name);
    const body = document.createElement('div'); body.className = 'chat-message-body';
    if (role === 'assistant') {
      const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'text-button'; copy.textContent = '复制';
      copy.disabled = !text;
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(body.innerText); notice('已复制回答。'); }
        catch { notice('复制失败，可选中文字后复制。'); }
      };
      label.append(copy);
    }
    format(body, text || (state === 'running' ? '正在等待 Codex 回复…' : state === 'interrupted' ? '本次生成已停止，尚未收到回答。可以重新发送问题。' : state === 'failed' ? '本次未生成回答，请根据下方提示重试。' : '本次没有返回可显示的内容，请重新提问。'));
    article.append(label, body); $('.chat-messages').append(article);
    if (error) { const note = document.createElement('p'); note.className = 'chat-message-error'; note.textContent = error; article.append(note); }
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
    for (const row of history) {
      const body = message(row.role, row.content, row.status, row.document_hash, row.error, row.model);
      if (row.status === 'running') showProgress(body, data.progress || { started_at: row.created, message: '本次回答仍在生成' });
    }
    const doc = data.document;
    remoteDocumentPending = Boolean(data.preparing);
    $('.chat-documents summary').textContent = `阅读依据：${doc ? doc.name : '摘要与本站解读'}`;
    $('.chat-document-note').textContent = doc ? `${doc.kind === 'pdf' ? `${doc.page_count} 页，其中 ${doc.scan_pages} 页需图片识别。` : '正文按段落编号，可点击回答中的引用核查。'} 资料已更新时会建立新上下文；上传内容请与论文标题核对。` : '尚未读取全文。上传 PDF 仅保存在本机，最多 20 MB / 300 页。';
    if (!documentPending) {
      if (remoteDocumentPending) documentNotice('本机正在获取或解析资料，请稍候…', 'loading');
      else documentNotice(doc ? (doc.kind === 'pdf' ? `已载入 PDF · ${doc.page_count} 页${doc.scan_pages ? ` · ${doc.scan_pages} 页需图片识别` : ''}` : '已载入网页全文；可继续获取 PDF 以按页阅读。') : '未载入 PDF，可上传或直接获取。', doc ? 'ready' : '');
    }
    $('.chat-messages').scrollTop = $('.chat-messages').scrollHeight;
    if (!controller) {
      setBusy(data.busy);
      if ((data.busy || data.preparing) && dialog.open) refreshTimer = setTimeout(() => loadPaper().catch(e => notice(e.message)), 3000);
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
      connectedModel = data.model;
      loadModels(data);
      status('已连接本机 Codex', 'connected'); $('.chat-connect').hidden = true;
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
    rememberDraft();
    paperId = id; lastTrigger = trigger;
    remoteDocumentPending = false;
    restoreDraft();
    $('.chat-paper-title').textContent = title || '选择一篇论文开始';
    $('.chat-local-link').href = `${base}/?paper=${encodeURIComponent(id || '')}`;
    if (!dialog.open) dialog.showModal();
    if (token) await connect();
    else { $('.chat-connect').hidden = false; $('.chat-messages').replaceChildren(); status('未连接本机 Codex'); }
  }

  async function stop() {
    clearTimeout(refreshTimer);
    if (busy || controller) {
      const active = controller;
      if (active) active.abort();
      await api(endpoint('/stop'), { method: 'POST' }).catch(() => {});
      if (!active) { setBusy(false); await loadPaper().catch(() => {}); }
    }
  }

  async function send(event) {
    event.preventDefault();
    const pages = $('.chat-pages').value.trim();
    const translatingText = mode === 'translate' && $('.chat-translation-source').value === 'text';
    const text = $('.chat-composer textarea').value.trim() || (mode === 'translate' && !translatingText && pages ? '翻译指定 PDF 页码。' : '');
    if (busy || documentPending || remoteDocumentPending) { if (documentPending || remoteDocumentPending) notice('资料正在准备，请完成后再发送。'); return; }
    if (!text) { notice(mode === 'translate' ? '请粘贴待译原文，或选择“论文章节 / 页码”后指定翻译范围。' : '请先输入具体问题。'); return; }
    if (!token) { notice('请先配对并连接本机 Codex。'); return; }
    if (!paperId) { notice('请先选择一篇论文。'); return; }
    const model = chosenModel();
    if (!model) { notice('请先选择当前可用的 Codex 模型。'); return; }
    if (mode === 'figure' && !model.images) { notice('所选模型仅支持文字，请切换支持图片的模型后解释配图。'); return; }
    const empty = $('.chat-empty'); if (empty) empty.remove();
    message('user', text); const body = message('assistant', '', 'running');
    showProgress(body);
    $('.chat-messages').scrollTop = $('.chat-messages').scrollHeight;
    let answer = '', done = false, finished = false, failure = '';
    const sentPaper = paperId;
    const sentDraft = draftKey();
    setBusy(true); notice('Codex 正在阅读资料…');
    status('请求已发送，正在等待 Codex', 'working');
    const activeController = new AbortController(); controller = activeController;
    try {
      const response = await api('/api/ask', { method: 'POST', stream: true, signal: activeController.signal,
        body: JSON.stringify({ paper_id: paperId, message: text, mode, model: model.id, pages: translatingText ? '' : pages,
          translation_target: $('.chat-translation-target').value, translation_source: $('.chat-translation-source').value, request_id: crypto.randomUUID() }) });
      $('.chat-composer textarea').value = '';
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
      while (true) {
        let timeout;
        let part;
        try { part = await Promise.race([reader.read(), new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('连续 45 秒未收到连接程序响应，已结束等待。请检查本机连接后重试。')), 45000); })]); }
        finally { clearTimeout(timeout); }
        if (part.done) break;
        buffer += decoder.decode(part.value, { stream: true });
        let end;
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          if (!frame.startsWith('data: ')) continue;
          const value = JSON.parse(frame.slice(6));
          if (value.type === 'model') {
            connectedModel = value.model;
            body.closest('.chat-message').querySelector('.chat-message-name').textContent = `Codex · ${value.model}`;
          }
          if (value.type === 'delta') {
            answer += value.text; body.textContent = answer;
            body.closest('.chat-message').querySelector('.text-button').disabled = !answer;
            showProgress(body, { message: '正在生成回答，内容将逐步显示' });
            $('.chat-messages').scrollTop = $('.chat-messages').scrollHeight;
          }
          if (value.type === 'progress') { showProgress(body, value); notice(value.message); }
          if (value.type === 'heartbeat') showProgress(body, value.progress || {});
          if (value.type === 'error') { failure = value.message; status(value.message, value.state); notice(value.message); }
          if (value.type === 'done') {
            done = true; finished = value.status === 'completed';
            notice(finished ? '已完成。可复制回答或继续提问。' : failure || '本次生成已停止，可重新发送问题。');
          }
        }
      }
      if (!done) { failure = activeController.signal.aborted ? '已停止生成，可以重新发送问题。' : '连接中断，已收到的内容已保留。可以重新发送问题。'; notice(failure); }
    } catch (error) {
      failure = error.name === 'AbortError' ? '已停止生成，可以重新发送问题。' : error.message;
      notice(failure);
    } finally {
      activeController.abort();
      if (controller === activeController) controller = null;
      setBusy(false);
      if (finished) status(`已连接 · 本次使用 ${connectedModel}`, 'connected');
      else if (!failure && done) status('已连接本机 Codex', 'connected');
      else if (failure) status(failure, 'error');
      body.closest('.chat-message').querySelector('.chat-progress')?.remove();
      format(body, answer || failure || '本次未收到回答，可以重新发送问题。');
      if (sentPaper === paperId) {
        if (!finished && !$('.chat-composer textarea').value) $('.chat-composer textarea').value = text;
        rememberDraft();
        await loadPaper().catch(() => {});
      } else drafts.set(sentDraft, { text: finished ? '' : text, pages });
    }
  }

  async function action(name) {
    try {
      if (name === 'connect') return await connect();
      if (name === 'stop') return await stop();
      if (!token || !paperId) { notice('请先连接并选择论文。'); return; }
      if (name === 'upload') { uploadPaper = paperId; return $('.chat-documents input[type="file"]').click(); }
      if (name === 'fetch-pdf') return prepareDocument('/fetch-pdf', null, '正在获取并解析论文 PDF…');
      if (name === 'fulltext') return prepareDocument('/fulltext', null, '正在获取网页全文…');
      if (name === 'clear' && window.confirm('清除这篇论文在本机助手中的资料和对话？Codex 自身会话历史仍由 Codex 管理。')) {
        const data = await api(endpoint(), { method: 'DELETE' }); await loadPaper(); notice(data.message);
      }
      if (name === 'export') {
        const data = await api(endpoint());
        const md = `# ${data.paper.title_zh || data.paper.title}\n\n` + data.history.map(m => `## ${m.role === 'user' ? '你' : `Codex${m.model ? ` · ${m.model}` : ''}`}${m.status === 'completed' ? '' : '（未完成）'}\n\n${m.content}`).join('\n\n');
        const url = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
        const a = document.createElement('a'); a.href = url; a.download = `${paperId}-Codex.md`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) { notice(error.message); }
  }
  dialog.addEventListener('click', event => {
    const button = event.target.closest('[data-chat-action]'); if (button) action(button.dataset.chatAction);
    const shortcut = event.target.closest('[data-mode]');
    if (shortcut) {
      rememberDraft();
      mode = shortcut.dataset.mode;
      restoreDraft();
      const input = $('.chat-composer textarea');
      input.focus();
    }
  });
  async function prepareDocument(path, body, pendingText) {
    if (busy || documentPending || remoteDocumentPending) return;
    const targetPaper = paperId;
    documentPending = true; setBusy(busy); documentNotice(pendingText, 'loading'); notice(pendingText);
    let outcome = '', failed = false;
    try {
      const data = await api(`/api/papers/${targetPaper}${path}`, { method: 'POST', ...(body ? { body } : {}) });
      outcome = data.message;
    } catch (error) { outcome = error.message; failed = true; }
    finally {
      documentPending = false;
      if (paperId === targetPaper) {
        await loadPaper().catch(() => {});
        if (!remoteDocumentPending) documentNotice(outcome, failed ? 'error' : 'ready');
        notice(outcome);
      }
      setBusy(busy);
    }
  }
  $('.chat-documents input[type="file"]').addEventListener('change', async (event) => {
    const file = event.target.files[0]; if (!file) return;
    event.target.value = '';
    if (uploadPaper !== paperId) { notice('论文已切换，请在当前论文重新选择要上传的 PDF。'); return; }
    if (file.size > 20 * 1024 * 1024) { documentNotice('PDF 超过 20 MB，请选择较小的文件。', 'error'); return; }
    const form = new FormData(); form.append('file', file);
    await prepareDocument('/pdf', form, `正在上传并解析 ${file.name}…`);
  });
  $('.chat-composer').addEventListener('submit', send);
  $('.chat-translation-source').addEventListener('change', updateMode);
  $('#chat-model').addEventListener('change', () => {
    if (chosenModel()) localStorage.setItem(modelKey, $('#chat-model').value);
    updateModelHint();
    notice(`后续请求将使用 ${$('#chat-model').value}，当前论文的对话记录会保留。`);
  });
  updateMode();
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
