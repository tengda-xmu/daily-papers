(() => {
  'use strict';
  if (document.documentElement.classList.contains('mobile-public')) return;
  const chatScript = new URL(document.currentScript.src), readerAsset = name => new URL(name + chatScript.search, chatScript).href;
  if (document.getElementById('manual-search') || document.getElementById('journal-manager') || document.getElementById('direction-manager')) return;
  const base = 'http://127.0.0.1:43127';
  const local = document.body.dataset.codexLocal === 'true';
  const key = 'daily-papers-codex-session';
  const session = location.origin === base ? 'localStorage' : 'sessionStorage';
  const browserKey = 'daily-papers-codex-browser', rememberKey = 'daily-papers-codex-remember';
  function readStorage(storage, name) { try { return window[storage].getItem(name) || ''; } catch { return ''; } }
  function writeStorage(storage, name, value) {
    try { if (value) window[storage].setItem(name, value); else window[storage].removeItem(name); return true; } catch { return false; }
  }
  let token = readStorage(session, key);
  let deviceToken = readStorage('localStorage', browserKey);
  if (!/^[A-Za-z0-9_-]{43}$/.test(deviceToken)) deviceToken = '';
  let devicePersisted = Boolean(deviceToken);
  let restorePromise = null, connectPromise = null, reconnectTimer = null, connecting = false;
  let connectedModel = '';
  const modelKey = 'daily-papers-codex-model';
  let availableModels = [];
  let paperId = '', mode = 'question', controller = null, busy = false, history = [], pairCode = '';
  let refreshTimer = null;
  let documentPending = false, remoteDocumentPending = false, documentAction = '', uploadPaper = '';
  let currentDocument = null, pdfVersions = [], translationSource = 'text';
  let progressTimer = null;
  let lastTrigger = null;
  let screenshotPending = false, screenshotPaper = '', screenshots = [];
  const imageUrls = new Map();
  let previewEpoch = 0;
  let activeLeaf = 0, followLatest = true, editing = null;
  let reader = null, readerPromise = null;
  const drafts = new Map();
  const modes = {
    question: { prompt: '', hint: '围绕这篇论文提出具体问题，回答会附关键证据、分析依据和适用边界。', placeholder: '例如：作者如何验证可靠性？哪些证据支持这一结论？', send: '发送问题' },
    summary: { prompt: '请用中文总结这篇论文的研究问题、方法、主要发现与证据、局限，以及对我的研究方向的启发。', hint: '按研究问题、方法、证据与局限总结；未加载全文时会明确资料范围。', placeholder: '填写你希望重点总结的内容…', send: '开始总结' },
    translate: { prompt: '', hint: '逐段对照翻译，保留公式、数字、单位和专业术语。', placeholder: '粘贴需要翻译的原文…', send: '开始翻译' },
    figure: { prompt: '请解释这张论文配图的模块、信息流、方法原理及与研究结论的关系。', hint: '读取论文配图，或上传、粘贴你希望解释的截图。', placeholder: '填写你想了解的图中模块或连接关系…', send: '解释配图' },
  };
  const dialog = document.createElement('dialog');
  dialog.className = 'paper-chat';
  dialog.setAttribute('aria-labelledby', 'chat-heading');
  dialog.innerHTML = `<div class="chat-width-resizer" role="separator" tabindex="0" aria-orientation="vertical" aria-label="调整对话侧栏宽度" aria-controls="chat-shell" title="左右拖动调整宽度；双击恢复默认"></div><div class="chat-shell" id="chat-shell">
    <header class="chat-header"><div class="chat-heading-row"><h2 id="chat-heading">Codex 论文对话</h2><button class="chat-close" type="button" aria-label="关闭对话">×</button></div><p class="chat-paper-title"></p><div class="chat-status-row"><p class="chat-status" role="status">未连接本机 Codex</p><button class="text-button" type="button" data-chat-action="reader">原文阅读</button><button class="text-button chat-settings-toggle" type="button" data-chat-action="settings" aria-expanded="false" aria-controls="chat-settings">模型与资料</button></div>
    <div class="chat-settings" id="chat-settings" hidden><div class="chat-model-row"><label for="chat-model">模型</label><select id="chat-model" aria-describedby="chat-model-help" disabled><option value="">连接后加载可用模型</option></select></div><p class="chat-model-help" id="chat-model-help">选择将用于下一次发送。</p>
    <section class="chat-documents" aria-label="论文资料">
      <div class="chat-doc-actions"><button type="button" class="chat-button" data-chat-action="upload">上传 PDF</button><button type="button" class="chat-button" data-chat-action="fetch-pdf">获取论文 PDF</button></div>
      <p class="chat-document-status" role="status">未载入 PDF，可上传或直接获取。</p>
      <details><summary>阅读依据：摘要与本站解读</summary><p class="chat-document-note">文件仅保存在本机，最多 20 MB / 300 页。</p><button type="button" class="text-button" data-chat-action="fulltext">获取网页全文</button></details>
      <input type="file" accept="application/pdf,.pdf" hidden>
    </section></div></header>
    <section class="chat-connect"><p>先运行“启动论文助手.cmd”。首次连接请粘贴配对码，记住浏览器后可自动连接。</p><div class="chat-pair-row"><input type="password" autocomplete="off" aria-label="本机配对码" placeholder="首次连接的配对码"><button class="chat-button" type="button" data-chat-action="connect">连接</button></div><label class="chat-remember"><input type="checkbox" checked>记住此浏览器，下次自动连接</label><p class="chat-local-help"><a class="chat-local-link" target="_blank" rel="noopener noreferrer">在本机打开论文助手</a> · 电脑须保持运行</p></section>
    <div class="chat-workspace">
    <div class="chat-messages" id="chat-reading-pane" aria-label="对话记录"></div>
    <button class="chat-scroll-latest text-button" type="button" data-chat-action="latest" hidden>↓ 回到最新回复</button>
    <div class="chat-history-actions"><button class="text-button" type="button" data-chat-action="export-pdf">导出对话 PDF</button><button class="text-button" type="button" data-chat-action="export">对话文本</button><button class="text-button" type="button" data-chat-action="clear">清除对话</button></div>
    <div class="chat-height-resizer" role="separator" tabindex="0" aria-orientation="horizontal" aria-label="调整对话阅读区域高度" aria-controls="chat-reading-pane" title="上下拖动调整阅读区域；双击恢复默认"><span aria-hidden="true"></span><small aria-hidden="true">拖动调整阅读区域</small></div>
    <div class="chat-bottom">
    <form class="chat-composer">
      <div class="chat-shortcuts"><button class="chat-button" type="button" data-mode="summary">总结论文</button><button class="chat-button" type="button" data-mode="question" aria-pressed="true">深入提问</button><button class="chat-button" type="button" data-mode="translate">中英翻译</button><button class="chat-button" type="button" data-mode="figure">解释配图</button></div>
      <p class="chat-mode-help" id="chat-mode-help"></p>
      <div class="chat-translation" hidden><label>方向 <select class="chat-translation-target"><option value="zh">英译中</option><option value="en">中译英</option></select></label><label>内容 <select class="chat-translation-source"><option value="text">粘贴原文</option><option value="image">截图翻译</option><option value="layout">全文译文 PDF（原版式）</option><option value="layout-bilingual">中英对照 PDF（原版式）</option><option value="full">全文文本对照（重新排版）</option><option value="document">论文章节</option></select></label></div>
      <textarea rows="2" aria-label="向 Codex 提问" aria-describedby="chat-mode-help" maxlength="12000"></textarea>
      <div class="chat-attachments" aria-label="待发送截图" hidden></div>
      <input class="chat-image-input" type="file" accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp" multiple hidden>
      <div class="chat-form-footer"><button class="chat-button" type="button" data-chat-action="screenshot" title="支持 PNG、JPEG、WebP，每张最多 8 MB，每次最多 4 张">上传截图</button><span class="chat-paste-hint">也可 Ctrl+V 粘贴</span><div><button class="chat-button" type="button" data-chat-action="stop" hidden>停止</button> <button class="chat-send" type="submit">发送问题</button></div></div><p class="chat-notice" role="status"></p>
    </form>
    </div></div>
  </div>`;
  document.body.append(dialog);
  const $ = (selector) => dialog.querySelector(selector);
  const status = (text, state = '') => { $('.chat-status').textContent = text; $('.chat-status').dataset.state = state; };
  const notice = (text) => { $('.chat-notice').textContent = text; };
  const documentNotice = (text, state = '', inReader = false) => {
    $('.chat-document-status').textContent = text; $('.chat-document-status').dataset.state = state;
    if (inReader) reader?.documentNotice(text, state);
  };
  const endpoint = (suffix = '') => `/api/papers/${paperId}${suffix}`;
  function scrollLatest(force = false) {
    if (force || followLatest) {
      followLatest = true;
      $('.chat-messages').scrollTop = $('.chat-messages').scrollHeight;
    }
    $('.chat-scroll-latest').hidden = followLatest;
  }
  $('.chat-messages').addEventListener('scroll', () => {
    const pane = $('.chat-messages');
    followLatest = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 64;
    $('.chat-scroll-latest').hidden = followLatest;
  });
  $('.chat-remember input').checked = readStorage('localStorage', rememberKey) !== 'off';
  const remembered = document.createElement('p'); remembered.className = 'chat-remembered'; remembered.hidden = true;
  remembered.innerHTML = '<span>已记住此浏览器 · 自动连接</span><button type="button" class="text-button" data-chat-action="forget-browser">取消记住</button>';
  $('.chat-settings').append(remembered);
  function toggleSettings(open = $('.chat-settings').hidden) {
    $('.chat-settings').hidden = !open;
    $('.chat-settings-toggle').setAttribute('aria-expanded', String(open));
    refreshLayout();
  }
  const fullTranslation = row => row.role === 'assistant' && /^## (?:PDF )?全文翻译 · (中文|英文)\s/.test(row.content || '');
  const layoutTranslation = row => /^## PDF 全文翻译 ·/.test(row?.content || '');
  const bilingualTranslation = row => row?.artifact?.preferred_view === 'bilingual';
  const pdfLabel = row => bilingualTranslation(row) ? '下载中英对照 PDF' : '下载原版式译文 PDF';
  const latestTranslation = () => history.filter(fullTranslation).at(-1);
  const fullTranslationPrompt = '请完整翻译已载入论文的全部正文，保留原文顺序、术语、公式与引用。';
  function existingPdf() {
    const source = $('.chat-translation-source').value;
    if (mode !== 'translate' || !['layout', 'layout-bilingual'].includes(source) || currentDocument?.kind !== 'pdf' || screenshots.length) return null;
    const view = source === 'layout-bilingual' ? 'bilingual' : 'translated';
    const prompt = $('.chat-composer textarea').value.trim() || fullTranslationPrompt;
    const row = history.findLast(row => row.role === 'assistant' && row.status === 'completed'
      && row.artifact?.kind === 'layout-pdf' && row.artifact.source_hash === currentDocument.hash
      && row.artifact.target === $('.chat-translation-target').value && row.model === chosenModel()?.id
      && row.request?.message === prompt
      && pdfVersions.some(v => v.view === view && (v.message_id === row.id || v.message_ids?.includes(row.id))));
    const stored=pdfVersions.filter(v=>v.view===view && v.available!==false && v.source_hash===currentDocument.hash
      && v.target===$('.chat-translation-target').value && v.model===chosenModel()?.id && v.instructions===prompt).sort((a,b)=>b.created-a.created)[0];
    return stored ? {row:{libraryVersion:stored.hash},view} : row ? {row, view} : null;
  }
  async function previewPdf(row, view = row.artifact?.preferred_view || 'translated') {
    const targetPaper = paperId;
    if(row.libraryVersion){const reader=await ensureReader();if(targetPaper===paperId)await reader.showVersion(row.libraryVersion);return;}
    if (!pdfVersions.some(v => v.view === view && (v.message_id === row.id || v.message_ids?.includes(row.id)))) throw new Error('此 PDF 版本已不可预览，请检查当前论文资料。');
    const reader = await ensureReader();
    if (targetPaper !== paperId) return;
    await reader.update(targetPaper, currentDocument, pdfVersions);
    if (targetPaper === paperId) await reader.showTranslation(row.id, view);
  }
  function updateRemembered() {
    remembered.hidden = !deviceToken;
    remembered.querySelector('span').textContent = devicePersisted ? '已记住此浏览器 · 自动连接' : '自动连接仅在当前页面有效';
  }
  function saveSession(data) { token = data.token; writeStorage(session, key, token); }
  function clearSession() { token = ''; writeStorage(session, key, ''); }
  function saveBrowser(data) {
    if (!data.device_token) return;
    deviceToken = data.device_token;
    devicePersisted = writeStorage('localStorage', browserKey, deviceToken);
    if (!devicePersisted) notice('浏览器禁止保存数据；本次可以使用，关闭页面后需重新配对。');
    updateRemembered();
  }
  const refreshLayout = setupLayout();
  async function ensureReader() {
    if (!readerPromise) readerPromise = (async () => {
      const link = document.createElement('link'); link.rel='stylesheet'; link.href=readerAsset('paper-reader.css'); document.head.append(link);
      const module = await import(readerAsset('paper-reader.js'));
      reader = module.createReader({dialog, assets:new URL('.',chatScript), api, onDocument:action, onLayout:refreshLayout,
        onSource:async version=>{
          if(busy || editing || documentPending || remoteDocumentPending)throw new Error('请先完成或停止当前操作，再切换原文版本。');
          const data=await api(endpoint('/select-source'),{method:'POST',body:JSON.stringify({version})});
          currentDocument=data.document;pdfVersions=data.pdf_versions;updateMode();
          $('.chat-documents summary').textContent='阅读依据：'+currentDocument.name;
          documentNotice(`已载入原文版本 ${version.slice(0,8)} · ${currentDocument.page_count} 页`,'ready');
          return currentDocument;
        },
        onSelection:async selected => {
          if (busy || editing || documentPending || screenshotPending) { notice('请先完成当前操作，再翻译或提问。'); return false; }
          if (selected.documentHash !== currentDocument?.hash) { notice('原文已更换，请重新选择文字。'); return false; }
          if (screenshots.length) { notice('请先发送或移除待发送截图，再处理原文选段。'); return false; }
          rememberDraft();
          if (selected.action === 'question') {
            mode='question'; restoreDraft();
            const input=$('.chat-composer textarea'), quote=`关于${selected.translated?'译文（请对照原文核查）':'原文'} [${selected.label}]：\n“${selected.text}”\n\n`;
            if (input.value.length + quote.length > input.maxLength) { notice('当前草稿和选段过长，请先整理草稿。'); return false; }
            input.value=quote+input.value;rememberDraft();input.focus();return true;
          }
          mode='translate'; translationSource='text';$('.chat-translation-source').value='text';$('.chat-translation-target').value=selected.action==='translate-en'?'en':'zh';restoreDraft();
          const previous=$('.chat-composer textarea').value;$('.chat-composer textarea').value=selected.text;rememberDraft();
          const key=draftKey();reader.showChat();await send(null);
          if (previous) {drafts.set(key,{text:previous});if(draftKey()===key){$('.chat-composer textarea').value=previous;rememberDraft();}}
          return true;
        }});
      reader.documentControls(busy || documentPending || remoteDocumentPending || screenshotPending || Boolean(editing), documentPending || remoteDocumentPending, documentAction);
      return reader;
    })().catch(error=>{readerPromise=null;notice('原文阅读器加载失败：'+error.message);throw error;});
    return readerPromise;
  }
  function setupLayout() {
    const storageKey = 'daily-papers-chat-layout';
    const widthHandle = $('.chat-width-resizer'), heightHandle = $('.chat-height-resizer');
    const workspace = $('.chat-workspace'), reading = $('.chat-messages');
    const availableHeight = () => workspace.clientHeight - heightHandle.offsetHeight - $('.chat-history-actions').offsetHeight;
    const layout = { width: null, ratio: null };
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
      if (Number.isFinite(saved?.width) && saved.width > 0) layout.width = saved.width;
      if (Number.isFinite(saved?.ratio) && saved.ratio > 0 && saved.ratio < 1) layout.ratio = saved.ratio;
    } catch { /* A blocked or invalid preference must not prevent opening chat. */ }
    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    const save = () => { try { localStorage.setItem(storageKey, JSON.stringify(layout)); } catch {} };
    const bounds = (axis) => axis === 'width'
      ? { min: Math.min(360, window.innerWidth), max: Math.min(1200, window.innerWidth) }
      : { min: 100, max: Math.max(100, availableHeight() - 140) };
    function refresh() {
      if (!dialog.open) return;
      const wide = window.innerWidth > 640;
      const width = bounds('width');
      if (layout.width !== null && wide) dialog.style.setProperty('--chat-width', `${clamp(layout.width, width.min, width.max)}px`);
      else dialog.style.removeProperty('--chat-width');
      widthHandle.tabIndex = wide ? 0 : -1;
      const height = bounds('height');
      if (layout.ratio !== null) {
        const available = availableHeight();
        reading.style.flex = `0 0 ${clamp(available * layout.ratio, height.min, height.max)}px`;
        $('.chat-bottom').style.flex = '1 1 0';
        $('.chat-bottom').style.maxHeight = 'none';
      } else {
        reading.style.removeProperty('flex');
        $('.chat-bottom').style.removeProperty('flex');
        $('.chat-bottom').style.removeProperty('max-height');
      }
      for (const [handle, range, value] of [[widthHandle, width, dialog.getBoundingClientRect().width], [heightHandle, height, reading.getBoundingClientRect().height]]) {
        handle.setAttribute('aria-valuemin', String(Math.round(range.min)));
        handle.setAttribute('aria-valuemax', String(Math.round(range.max)));
        handle.setAttribute('aria-valuenow', String(Math.round(value)));
        handle.setAttribute('aria-valuetext', `${Math.round(value)} 像素`);
      }
    }
    function change(axis, value) {
      const range = bounds(axis);
      value = clamp(value, range.min, range.max);
      if (axis === 'width') layout.width = value;
      else layout.ratio = value / Math.max(1, availableHeight());
      refresh();
    }
    let drag = null;
    function finish(event) {
      if (!drag || (event?.pointerId !== undefined && event.pointerId !== drag.pointerId)) return;
      const previous = drag; drag = null;
      dialog.classList.remove('chat-resizing-width', 'chat-resizing-height');
      if (previous.handle.hasPointerCapture(previous.pointerId)) previous.handle.releasePointerCapture(previous.pointerId);
      save();
    }
    for (const [handle, axis] of [[widthHandle, 'width'], [heightHandle, 'height']]) {
      handle.addEventListener('pointerdown', event => {
        if (event.button !== 0 || (axis === 'width' && window.innerWidth <= 640)) return;
        event.preventDefault(); finish(); handle.focus({ preventScroll: true });
        drag = { axis, handle, pointerId: event.pointerId, x: event.clientX, y: event.clientY,
          start: axis === 'width' ? dialog.getBoundingClientRect().width : reading.getBoundingClientRect().height };
        handle.setPointerCapture(event.pointerId);
        dialog.classList.add(`chat-resizing-${axis}`);
      });
      handle.addEventListener('pointermove', event => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        change(axis, drag.start + (axis === 'width' ? drag.x - event.clientX : event.clientY - drag.y));
      });
      for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) handle.addEventListener(type, finish);
      const reset = () => { if (axis === 'width') layout.width = null; else layout.ratio = null; refresh(); save(); };
      handle.addEventListener('dblclick', reset);
      handle.addEventListener('keydown', event => {
        const direction = axis === 'width' ? { ArrowLeft: 1, ArrowRight: -1 } : { ArrowDown: 1, ArrowUp: -1 };
        if (!(event.key in direction) && !['Home', 'End', 'Enter'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Enter') { reset(); return; }
        const range = bounds(axis), value = axis === 'width' ? dialog.getBoundingClientRect().width : reading.getBoundingClientRect().height;
        change(axis, event.key === 'Home' ? range.min : event.key === 'End' ? range.max : value + direction[event.key] * (event.shiftKey ? 50 : 20));
        save();
      });
    }
    window.addEventListener('resize', () => { finish(); refresh(); });
    window.addEventListener('blur', () => finish());
    dialog.addEventListener('close', () => finish());
    // Header wrapping, mode changes and browser zoom all change the free space.
    const observer = new ResizeObserver(refresh);
    observer.observe(workspace);
    observer.observe(reading);
    return refresh;
  }
  function chosenModel() { return availableModels.find(m => m.id === $('#chat-model').value); }
  function updateModelHint() {
    const selected = chosenModel();
    $('.chat-model-help').textContent = selected ? `${selected.images ? '支持文字与图片' : '仅支持文字'} · 用于下一次发送` : '此前所选模型当前不可用，请重新选择。';
    $('.chat-composer .chat-send').disabled = busy || documentPending || remoteDocumentPending || screenshotPending || !selected || Boolean(editing);
  }
  function loadModels(data) {
    availableModels = data.models || [{id: data.model, label: data.model, images: data.images, is_default: true}];
    const wanted = readStorage('localStorage', modelKey) || data.model;
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
    updateMode();
  }
  const draftKey = () => `${paperId}:${mode}${mode === 'translate' ? ':' + translationSource : ''}`;
  function updateMode() {
    const config = modes[mode], translating = mode === 'translate';
    const documentTranslation = translating && $('.chat-translation-source').value === 'document';
    const fullTranslation = translating && ['full', 'layout', 'layout-bilingual'].includes($('.chat-translation-source').value);
    const layoutTranslation = translating && ['layout', 'layout-bilingual'].includes($('.chat-translation-source').value);
    const bilingual = translating && $('.chat-translation-source').value === 'layout-bilingual';
    const imageTranslation = translating && $('.chat-translation-source').value === 'image';
    const existing = existingPdf();
    $('.chat-translation').hidden = !translating;
    $('.chat-mode-help').textContent = layoutTranslation ? (bilingual ? '原文与译文逐页配对，保留各页尺寸、分栏及图表；左侧并排阅读，可选择文字。' : '译文在原 PDF 文字框内排版，保留页数、尺寸、分栏和图表；完成后左侧可切换阅读。') + ' 译文字体与换行可能调整；放不下会提示，绝不截断。' : fullTranslation ? '按原文顺序分批翻译，导出重新排版的文本对照 PDF；保留原版式请选择上方 PDF 选项。' : imageTranslation ? '翻译截图中的文字，保留公式和术语，标注识别不清处。' : config.hint;
    $('.chat-composer textarea').placeholder = fullTranslation ? '可选：填写术语或表达偏好；留空即可开始全文翻译。' : imageTranslation ? '可选：填写术语偏好或需要翻译的区域…' : documentTranslation ? '例如：翻译 Abstract 或 Methods 章节。' : config.placeholder;
    $('.chat-composer .chat-send').textContent = bilingual ? '生成中英对照 PDF' : layoutTranslation ? '翻译并生成 PDF' : fullTranslation ? '开始全文翻译' : config.send;
    if (layoutTranslation && busy) $('.chat-composer .chat-send').textContent = '正在生成 PDF…';
    else if (existing) {
      $('.chat-composer .chat-send').textContent = '查看已生成 PDF';
      $('.chat-mode-help').textContent = `已有相同模型、方向和要求的${bilingual ? '中英对照' : '译文'} PDF，可直接查看与下载。填写新要求后可生成新译文；需要重译时，点击结果下的“重新翻译”。`;
    }
    $('.chat-composer .chat-send').title = existing && !busy ? '打开已生成的文件，不提交翻译任务' : '';
    dialog.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
  }
  function rememberDraft() { drafts.set(draftKey(), { text: $('.chat-composer textarea').value }); }
  function restoreDraft() {
    const draft = drafts.get(draftKey());
    $('.chat-composer textarea').value = draft ? draft.text : modes[mode].prompt;
    updateMode();
  }
  function tickProgress() {
    dialog.querySelectorAll('.chat-progress').forEach(box => {
      const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - Number(box.dataset.started)));
      const silence = Math.max(0, Math.floor((Date.now() - Number(box.dataset.received)) / 1000));
      const modelSilence = box.dataset.modelActivity ? Math.max(0, Math.floor(Date.now() / 1000 - Number(box.dataset.modelActivity))) : null;
      const modelStatus = modelSilence === null ? '尚未收到模型输出' : `最近模型活动 ${modelSilence} 秒前`;
      const connection = silence > 35 ? '本机连接无新响应' : '本机连接正常';
      const hint = elapsed > 90 ? '；可继续等待或停止，重发会重新开始生成' : '';
      box.querySelector('.chat-progress-time').textContent = `已用时 ${Math.floor(elapsed / 60)}分${elapsed % 60}秒 · ${connection} · ${modelStatus}${hint}`;
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
    if (progress.model_activity_at) box.dataset.modelActivity = String(progress.model_activity_at);
    const stage = progress.message || box.querySelector('.chat-progress-stage').textContent || '请求已发出，等待本机连接程序接收';
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
    $('.chat-settings').append(sidebarPair);
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
    const locked = value || documentPending || remoteDocumentPending || screenshotPending;
    reader?.documentControls(locked || Boolean(editing), documentPending || remoteDocumentPending, documentAction);
    $('.chat-documents').setAttribute('aria-busy', String(documentPending || remoteDocumentPending));
    $('#chat-model').disabled = locked || !availableModels.length;
    $('[data-chat-action="stop"]').hidden = !value;
    dialog.querySelectorAll('[data-mode], .chat-translation select, [data-chat-action="fulltext"], [data-chat-action="fetch-pdf"], [data-chat-action="upload"], [data-chat-action="screenshot"], .chat-attachment-remove, [data-chat-action="clear"], [data-chat-action="forget-browser"]').forEach(b => { b.disabled = locked || Boolean(editing); });
    dialog.querySelectorAll('.chat-revise, .chat-version-button').forEach(b => { b.disabled = locked || b.dataset.unavailable === 'true'; });
    $('.chat-composer .chat-send').disabled = locked || !chosenModel() || Boolean(editing);
    $('[data-chat-action="forget-browser"]').disabled = locked || connecting || Boolean(editing);
    updateMode();
  };

  async function restoreSession() {
    if (!deviceToken) return;
    if (restorePromise) return restorePromise;
    restorePromise = (async () => {
      try {
        const data = await api('/api/session/restore', { method: 'POST', body: JSON.stringify({ device_token: deviceToken }) }, false);
        saveSession(data);
      } catch (error) {
        if (error.state === 'unpaired') {
          deviceToken = ''; writeStorage('localStorage', browserKey, ''); clearSession(); updateRemembered();
        }
        throw error;
      }
    })().finally(() => { restorePromise = null; });
    return restorePromise;
  }

  async function api(path, options = {}, retryAuth = true) {
    const sentToken = token;
    const headers = { Authorization: `Bearer ${token}`, ...options.headers };
    if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
    let response;
    try { response = await fetch(base + path, { ...options, headers, signal: options.signal || AbortSignal.timeout(120000) }); }
    catch (error) {
      if (error.name === 'AbortError') throw error;
      if (error.name === 'TimeoutError') {
        const timeout = new Error('等待本机响应超时，操作可能仍在进行，请稍候查看状态。');
        timeout.state = 'timeout'; throw timeout;
      }
      const offline = new Error('无法连接本机。请启动论文助手、允许浏览器访问本地网络，或使用“在本机打开”入口。');
      offline.state = 'offline'; throw offline;
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const protectedRoute = path !== '/api/pair' && !path.startsWith('/api/session/');
      // Only replay a request explicitly rejected before authentication. Never
      // replay a timed-out/disconnected generation or any non-401 response.
      if (response.status === 401 && protectedRoute && retryAuth && deviceToken) {
        if (token === sentToken) { clearSession(); await restoreSession(); }
        else if (!token) await restoreSession();
        return api(path, options, false);
      }
      if (response.status === 401 && protectedRoute) { clearSession(); $('.chat-connect').hidden = false; }
      const err = new Error(data.message || (typeof data.detail === 'string' ? data.detail : '请求未完成，请重试。'));
      err.state = data.state;
      err.status = response.status;
      throw err;
    }
    return options.stream ? response : response.json();
  }

  function releasePreviews() {
    previewEpoch++;
    for (const entry of imageUrls.values()) if (entry.url) URL.revokeObjectURL(entry.url);
    imageUrls.clear();
  }
  async function screenshotUrl(targetPaper, item) {
    const id = `${targetPaper}:${item.id}`;
    if (!imageUrls.has(id)) {
      const entry = {}, epoch = previewEpoch;
      entry.promise = (async () => {
        const response = await api(`/api/papers/${targetPaper}/screenshots/${encodeURIComponent(item.id)}`, {stream: true});
        const blob = await response.blob();
        if (epoch !== previewEpoch) throw new Error('论文已切换');
        if (!blob.type.includes('image/png')) throw new Error('截图预览不可用');
        return entry.url = URL.createObjectURL(blob);
      })().catch(error => { imageUrls.delete(id); throw error; });
      imageUrls.set(id, entry);
    }
    return imageUrls.get(id).promise;
  }
  function renderScreenshots(container, items, targetPaper, removable) {
    container.replaceChildren();
    container.hidden = !items.length;
    for (const [index, item] of items.entries()) {
      const card = document.createElement('div'); card.className = 'chat-attachment';
      const link = document.createElement('a'); link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.setAttribute('aria-label', `查看截图 ${index + 1}：${item.name}`);
      const image = document.createElement('img'); image.alt = `截图 ${index + 1}`; image.width = 112; image.height = 76;
      const label = document.createElement('span'); label.textContent = item.name;
      link.append(image, label); card.append(link);
      if (removable) {
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'chat-attachment-remove';
        remove.textContent = '移除'; remove.setAttribute('aria-label', `移除截图 ${index + 1}`);
        remove.onclick = async () => {
          if (busy || screenshotPending || documentPending || remoteDocumentPending) return;
          screenshotPending = true; setBusy(busy);
          try {
            if (!item.sent) await api(`/api/papers/${targetPaper}/screenshots/${item.id}`, {method: 'DELETE'});
            if (targetPaper === paperId) { screenshots = screenshots.filter(x => x.id !== item.id); drawScreenshots(); notice('已从待发送内容中移除截图。'); }
          } catch (error) { notice(error.message); }
          finally { screenshotPending = false; setBusy(busy); }
        };
        card.append(remove);
      }
      container.append(card);
      screenshotUrl(targetPaper, item).then(url => { if (card.isConnected) { image.src = url; link.href = url; } })
        .catch(() => { if (card.isConnected) { image.remove(); label.textContent = item.name + '（预览暂不可用）'; } });
    }
  }
  function drawScreenshots() {
    renderScreenshots($('.chat-attachments'), screenshots, paperId, true);
    setBusy(busy);
  }
  async function uploadScreenshots(files, targetPaper = paperId) {
    if (!files.length) return;
    if (busy || screenshotPending || documentPending || remoteDocumentPending) { notice('请等待当前操作完成后再添加截图。'); return; }
    if (!token || !targetPaper) { notice('请先连接并选择论文。'); return; }
    if (targetPaper !== paperId) { notice('论文已切换，请在当前论文重新选择截图。'); return; }
    if (files.length + screenshots.length > 4) { notice('每次最多添加 4 张截图，请先移除不需要的图片。'); return; }
    if (files.some(f => !['image/png', 'image/jpeg', 'image/webp'].includes(f.type) && !/\.(png|jpe?g|webp)$/i.test(f.name))) { notice('请使用 PNG、JPEG 或 WebP 图片。'); return; }
    if (files.some(f => !f.size || f.size > 8 * 1024 * 1024)) { notice('截图为空或超过 8 MB，请裁剪后再上传。'); return; }
    screenshotPending = true; setBusy(busy); notice('正在上传截图…');
    try {
      for (const file of files) {
        const form = new FormData(); form.append('file', file);
        const item = await api(`/api/papers/${targetPaper}/screenshots`, {method: 'POST', body: form});
        if (targetPaper !== paperId) return;
        screenshots.push(item); drawScreenshots();
      }
      if (mode === 'translate' && translationSource !== 'image') {
        rememberDraft(); translationSource = 'image'; $('.chat-translation-source').value = 'image'; restoreDraft();
      }
      notice(`已添加 ${screenshots.length} 张截图，发送时会与问题一起交给模型。`);
    } catch (error) { if (targetPaper === paperId) notice(error.message); }
    finally { screenshotPending = false; setBusy(busy); }
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
            if (/^\[P\d+\]$/.test(piece) && currentDocument?.kind === 'pdf' && version === currentDocument.hash) reader?.showPage(Number(piece.slice(2,-1)));
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

  function message(role, text, state = 'completed', documentHash = '', error = '', model = '', messageId = null, attachments = [], artifact = {}, row = null) {
    const article = document.createElement('article'); article.className = 'chat-message'; article.dataset.role = role;
    if (messageId) article.dataset.messageId = messageId;
    article.dataset.documentHash = documentHash || '';
    const label = document.createElement('div'); label.className = 'chat-message-label';
    const name = document.createElement('span'); name.className = 'chat-message-name'; name.textContent = role === 'user' ? '你' : `Codex${model ? ` · ${model}` : ''}${state === 'interrupted' ? ' · 已停止' : state === 'failed' ? ' · 未完成' : ''}`;
    label.append(name);
    const body = document.createElement('div'); body.className = 'chat-message-body';
    const actions = document.createElement('div'); actions.className = 'chat-message-actions';
    if (row?.versions?.length > 1) {
      const position = row.versions.indexOf(row.id);
      const versions = document.createElement('span'); versions.className = 'chat-versions';
      for (const [offset, title, glyph] of [[-1, '上一个版本', '‹'], [1, '下一个版本', '›']]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'text-button chat-version-button';
        button.textContent = glyph; button.setAttribute('aria-label', title);
        button.dataset.unavailable = String(position + offset < 0 || position + offset >= row.versions.length);
        button.disabled = busy || button.dataset.unavailable === 'true';
        button.onclick = () => switchVersion(row.versions[position + offset]);
        if (offset === 1) { const count = document.createElement('span'); count.textContent = `${position + 1} / ${row.versions.length}`; count.setAttribute('aria-label', `版本 ${position + 1}，共 ${row.versions.length} 个`); versions.append(count); }
        versions.append(button);
      }
      actions.append(versions);
    }
    if (role === 'user' && row) {
      const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'text-button chat-revise chat-edit'; edit.textContent = '编辑'; edit.disabled = busy;
      edit.onclick = () => editQuestion(row); actions.append(edit);
    }
    if (role === 'assistant') {
      const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'text-button'; copy.textContent = '复制';
      copy.disabled = !text;
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(body.innerText); notice('已复制回答。'); }
        catch { notice('复制失败，可选中文字后复制。'); }
      };
      actions.append(copy);
      if (row && row.parent_id) {
        const regenerate = document.createElement('button'); regenerate.type = 'button'; regenerate.className = 'text-button chat-revise chat-regenerate';
        regenerate.textContent = state === 'completed' ? layoutTranslation(row) ? '重新翻译' : '重新生成' : '重试'; regenerate.disabled = busy || state === 'running';
        if (layoutTranslation(row)) regenerate.title = '重新调用模型翻译，生成新版本；旧版 PDF 保留';
        regenerate.onclick = () => send(null, {kind:'regenerate', row}); actions.append(regenerate);
      }
      if (messageId && text) {
        const pdf = document.createElement('button'); pdf.type = 'button'; pdf.className = 'text-button chat-answer-pdf'; pdf.textContent = '导出 PDF';
        const exportPaper = paperId;
        const translation = fullTranslation({role, content: text});
        const layout = layoutTranslation({content: text});
        if (translation) pdf.textContent = state === 'completed' ? '导出译文 PDF' : '导出部分译文 PDF';
        if (layout) pdf.textContent = artifact.filename ? pdfLabel({artifact}) : '译文 PDF 尚未完成';
        pdf.disabled = state === 'running' || (layout && !artifact.filename);
        pdf.onclick = () => downloadPdf(exportPaper, messageId, translation, pdf).catch(e => notice(e.message));
        if (!translation) actions.append(pdf);
        if (translation && state !== 'running') {
          const footer = document.createElement('div'); footer.className = 'chat-translation-download';
          const note = document.createElement('span'); note.textContent = state === 'completed' ? '全文译文已保存，可导出原文与译文。' : '本次翻译尚未完成，可导出已保存内容。';
          if (layout) note.textContent = artifact.filename ? (bilingualTranslation({artifact}) ? `中英对照 PDF · ${artifact.pages} 对页（${artifact.pages * 2} 页）` : `原版式译文 PDF · ${artifact.pages} 页`) : '已完成的翻译已保存，再次开始可继续生成 PDF。';
          const fileActions = document.createElement('div'); fileActions.className = 'chat-translation-file-actions';
          footer.append(note, fileActions); article.append(footer);
          if (layout && artifact.filename && state === 'completed' && documentHash === currentDocument?.hash) {
            const preview=document.createElement('button');preview.type='button';preview.className='chat-button';preview.textContent='在左侧阅读';
            preview.onclick=()=>previewPdf({id:messageId,artifact}).catch(e=>notice(e.message));
            fileActions.append(preview);
          }
          if (!layout || artifact.filename) { pdf.className = 'chat-button chat-answer-pdf'; fileActions.append(pdf); }
        }
      }
    }
    format(body, text || (state === 'running' ? '正在等待 Codex 回复…' : state === 'interrupted' ? '本次生成已停止，尚未收到回答。可以重新发送问题。' : state === 'failed' ? '本次未生成回答，请根据下方提示重试。' : '本次没有返回可显示的内容，请重新提问。'));
    article.prepend(label, body, actions); $('.chat-messages').append(article);
    if (attachments.length) {
      const previews = document.createElement('div'); previews.className = 'chat-message-attachments';
      article.append(previews); renderScreenshots(previews, attachments, paperId, false);
    }
    if (error) { const note = document.createElement('p'); note.className = 'chat-message-error'; note.textContent = error; article.append(note); }
    return body;
  }

  function drawHistory(rows) {
    $('.chat-messages').replaceChildren();
    for (const row of rows) message(row.role, row.content, row.status, row.document_hash, row.error, row.model, row.id, row.attachments || [], row.artifact || {}, row);
  }
  function closeEditor() {
    if (!editing) return;
    const article = $(`[data-message-id="${editing.id}"]`);
    article?.querySelector('.chat-inline-editor')?.remove();
    if (article) { article.querySelector('.chat-message-body').hidden = false; article.querySelector('.chat-message-actions').hidden = false; }
    editing = null; setBusy(busy);
  }
  function editQuestion(row, value = row.request?.message || row.content) {
    if (busy || documentPending || screenshotPending) return;
    closeEditor(); editing = row;
    const article = $(`[data-message-id="${row.id}"]`); if (!article) { editing = null; return; }
    article.querySelector('.chat-message-body').hidden = true; article.querySelector('.chat-message-actions').hidden = true;
    const form = document.createElement('form'); form.className = 'chat-inline-editor';
    const input = document.createElement('textarea'); input.value = value; input.maxLength = 12000; input.rows = 4; input.required = true; input.setAttribute('aria-label', '编辑已发送的问题');
    const hint = document.createElement('p'); hint.textContent = '保存后从此处重新回答；原版本及后续对话会保留。截图和翻译范围沿用原问题。';
    const controls = document.createElement('div');
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'chat-button'; cancel.textContent = '取消'; cancel.onclick = closeEditor;
    const save = document.createElement('button'); save.type = 'submit'; save.className = 'chat-send'; save.textContent = '保存并重新发送';
    controls.append(cancel, save); form.append(input, hint, controls); article.append(form);
    form.onsubmit = event => { event.preventDefault(); if (input.value.trim()) send(null, {kind:'edit', row, text:input.value.trim()}); };
    input.onkeydown = event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeEditor(); } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); form.requestSubmit(); } };
    setBusy(false); save.disabled = false; input.focus();
  }
  async function switchVersion(messageId) {
    if (busy || !messageId) return;
    closeEditor(); setBusy(true);
    try {
      await api(endpoint('/branch'), {method:'POST', body:JSON.stringify({message_id:messageId, expected_leaf:activeLeaf})});
      await loadPaper({anchor:messageId}); notice('已切换对话版本，可从此处继续提问。');
    } catch (error) { notice(error.message); }
    finally { setBusy(false); }
  }

  async function loadPaper({anchor = 0} = {}) {
    if (!paperId) return;
    clearTimeout(refreshTimer);
    const currentPaper = paperId;
    const data = await api(endpoint());
    if (currentPaper !== paperId) return;
    history = data.history;
    currentDocument = data.document; pdfVersions = data.pdf_versions || [];
    activeLeaf = data.active_leaf || history.at(-1)?.id || 0;
    const oldScroll = $('.chat-messages').scrollTop, stick = followLatest;
    closeEditor();
    $('.chat-paper-title').textContent = data.paper.title_zh || data.paper.title;
    $('.chat-paper-title').title = $('.chat-paper-title').textContent;
    updateMode();
    $('.chat-messages').replaceChildren();
    if (!history.length) {
      const p = document.createElement('p'); p.className = 'chat-empty';
      p.textContent = '从一个具体问题开始。你可以先总结这篇论文，再追问方法、实验依据或研究启发。'; $('.chat-messages').append(p);
    }
    for (const row of history) {
      const body = message(row.role, row.content, row.status, row.document_hash, row.error, row.model, row.id, row.attachments || [], row.artifact || {}, row);
      if (row.status === 'running') showProgress(body, data.progress || { started_at: row.created, message: '本次回答仍在生成' });
    }
    let doc = data.document;
    currentDocument = doc;
    if (dialog.open) await ensureReader().then(view => view.update(currentPaper, doc, data.pdf_versions || [], '', data.reading)).catch(error => notice(error.message));
    doc=currentDocument;
    $('.chat-settings-toggle').textContent = '模型与资料' + (doc ? doc.kind === 'pdf' ? ` · PDF ${doc.page_count} 页` : ' · 已载入全文' : '');
    if (!screenshotPending) { screenshots = data.screenshots || []; drawScreenshots(); }
    const wasPreparing = remoteDocumentPending;
    remoteDocumentPending = Boolean(data.preparing);
    $('.chat-documents summary').textContent = `阅读依据：${doc ? doc.name : '摘要与本站解读'}`;
    $('.chat-document-note').textContent = doc ? `${doc.kind === 'pdf' ? `${doc.page_count} 页，其中 ${doc.scan_pages} 页需图片识别。` : '正文按段落编号，可点击回答中的引用核查。'} 资料已更新时会建立新上下文；上传内容请与论文标题核对。` : '尚未读取全文。上传 PDF 仅保存在本机，最多 20 MB / 300 页。';
    if (!documentPending) {
      if (remoteDocumentPending) documentNotice('本机正在获取或解析资料，请稍候…', 'loading', true);
      else documentNotice(doc ? (doc.kind === 'pdf' ? `已载入 PDF · ${doc.page_count} 页${doc.scan_pages ? ` · ${doc.scan_pages} 页需图片识别` : ''}` : '已载入网页全文；可继续获取 PDF 以按页阅读。') : '未载入 PDF，可上传或直接获取。', doc ? 'ready' : '', wasPreparing);
    }
    followLatest = stick;
    if (anchor) {
      const article = $(`[data-message-id="${anchor}"]`), pane = $('.chat-messages');
      if (article) pane.scrollTop += article.getBoundingClientRect().top - pane.getBoundingClientRect().top - 8;
      followLatest = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 64;
    } else if (stick) scrollLatest(true);
    else $('.chat-messages').scrollTop = oldScroll;
    $('.chat-scroll-latest').hidden = followLatest;
    if (!controller) {
      setBusy(data.busy);
      if ((data.busy || data.preparing) && dialog.open) refreshTimer = setTimeout(() => loadPaper().catch(e => notice(e.message)), 3000);
    }
  }

  function connect() {
    if (connectPromise) return connectPromise;
    clearTimeout(reconnectTimer);
    connectPromise = performConnect().finally(() => { connectPromise = null; });
    return connectPromise;
  }
  async function pairWithCode(code) {
    const data = await api('/api/pair', { method: 'POST', body: JSON.stringify({ code, remember: $('.chat-remember input').checked }) }, false);
    saveSession(data); saveBrowser(data); $('.chat-pair-row input').value = '';
  }
  async function performConnect() {
    connecting = true;
    status('正在连接本机 Codex…');
    $('[data-chat-action="connect"]').disabled = true;
    $('[data-chat-action="forget-browser"]').disabled = true;
    $('.chat-remember input').disabled = true;
    const code = $('.chat-pair-row input').value.trim();
    try {
      if (!token && deviceToken) {
        try { await restoreSession(); } catch (error) { if (error.state !== 'unpaired' || !code) throw error; }
      }
      if (!token) {
        if (!code) throw new Error('请先运行启动脚本，复制本机页面中的配对码。');
        await pairWithCode(code);
      }
      let data;
      try { data = await api('/api/connect', { method: 'POST' }); }
      catch (error) {
        // Startup links contain a fresh one-time code. Recover old short
        // sessions from versions that did not support remembered browsers.
        if (error.state !== 'unpaired' || !code) throw error;
        await pairWithCode(code); data = await api('/api/connect', { method: 'POST' });
      }
      if ($('.chat-remember input').checked && !deviceToken) {
        try { saveBrowser(await api('/api/session/remember', { method: 'POST' })); }
        catch { notice('本次连接可用；记住浏览器未完成，请更新并重启本机论文助手。'); }
      }
      if (local) {
        const pairing = await api('/api/pairing-code', { method: 'POST' });
        setPairCode(pairing.code);
      }
      connectedModel = data.model;
      loadModels(data);
      status('已连接本机 Codex', 'connected'); $('.chat-connect').hidden = true; $('.chat-pair-row input').value = ''; updateRemembered();
      if (local) {
        const list = await api('/api/papers');
        const select = document.getElementById('local-paper-list'); select.replaceChildren();
        list.papers.forEach(p => { const option = document.createElement('option'); option.value = p.id; option.textContent = p.title; select.append(option); });
        if (!paperId) paperId = select.value;
        select.value = paperId;
      }
      await loadPaper();
      document.dispatchEvent(new CustomEvent('paper-chat-connected'));
    } catch (error) {
      status(error.message, error.state || 'error'); $('.chat-connect').hidden = false;
      // Saved PDFs remain readable when the local model connection is unavailable.
      if(token && paperId)await loadPaper().catch(()=>{});
      if (error.state === 'offline' && (token || deviceToken || code) && dialog.open) {
        status('等待本机助手启动，将自动重连。若浏览器提示，请允许访问本地网络。', 'offline');
        reconnectTimer = setTimeout(() => { if (dialog.open && !busy && !document.hidden) connect(); }, 8000);
      }
    } finally {
      connecting = false; $('[data-chat-action="connect"]').disabled = false; $('.chat-remember input').disabled = false;
      $('[data-chat-action="forget-browser"]').disabled = busy || documentPending || remoteDocumentPending;
    }
  }

  async function open(id, title, trigger) {
    if(reader && paperId!==id && !await reader.prepareLeave())return;
    if (busy) await stop();
    rememberDraft();
    if (paperId !== id) {
      releasePreviews(); screenshots = []; drawScreenshots(); history = []; currentDocument = null; pdfVersions = [];
      updateMode(); $('.chat-settings-toggle').textContent = '模型与资料';
    }
    closeEditor(); followLatest = true;
    paperId = id; lastTrigger = trigger;
    remoteDocumentPending = false;
    restoreDraft();
    $('.chat-paper-title').textContent = title || '选择一篇论文开始';
    $('.chat-paper-title').title = $('.chat-paper-title').textContent;
    $('.chat-local-link').href = `${base}/?paper=${encodeURIComponent(id || '')}`;
    if (!dialog.open) dialog.showModal();
    ensureReader().then(view=>view.update(id,currentDocument)).catch(error=>notice(error.message));
    refreshLayout();
    if(token || deviceToken){
      await loadPaper().catch(error=>notice(error.message));
      if(trigger?.dataset.openReader==='true')reader?.open();
    }
    if (token || deviceToken || $('.chat-pair-row input').value.trim()) await connect();
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

  async function send(event, revision = null) {
    event?.preventDefault();
    if (editing && !revision) { notice('请先保存或取消正在编辑的问题。'); return; }
    const user = revision ? (revision.row.role === 'user' ? revision.row : history.find(row => row.id === revision.row.parent_id)) : null;
    const task = revision ? (Object.keys(revision.row.request || {}).length ? revision.row.request : user?.request || {}) : {mode, translation_source:$('.chat-translation-source').value, translation_target:$('.chat-translation-target').value};
    const taskMode = task.mode || 'question', taskSource = task.translation_source || 'document';
    const attached = revision ? user?.attachments || [] : screenshots;
    const fullTranslation = taskMode === 'translate' && ['full', 'layout', 'layout-bilingual'].includes(taskSource);
    const layoutPdf = taskMode === 'translate' && ['layout', 'layout-bilingual'].includes(taskSource);
    const imageTranslation = taskMode === 'translate' && taskSource === 'image';
    const text = revision ? (revision.text || task.message || user?.content || '') : $('.chat-composer textarea').value.trim() || (fullTranslation ? fullTranslationPrompt : attached.length ? imageTranslation ? '请逐段翻译上传截图中的全部可见文字。' : '请分析上传截图，说明其中的关键信息与含义。' : '');
    if (busy || documentPending || remoteDocumentPending || screenshotPending) { if (documentPending || remoteDocumentPending || screenshotPending) notice('资料正在准备，请完成后再发送。'); return; }
    if (!text) { notice(imageTranslation ? '请先上传或粘贴待译截图。' : mode === 'translate' ? '请粘贴待译原文，或选择“论文章节”后指定翻译范围。' : '请输入问题，或上传截图。'); return; }
    if (!token) { notice('请先配对并连接本机 Codex。'); return; }
    if (!paperId) { notice('请先选择一篇论文。'); return; }
    if (!revision && existingPdf()) {
      const targetPaper = paperId;
      try {
        // Revalidate local files before opening; never turn a stale download into a new model request.
        await loadPaper();
        if (targetPaper !== paperId) return;
        const existing = existingPdf();
        if (!existing) { notice('资料或翻译要求已变化，请确认后再生成 PDF。'); return; }
        await previewPdf(existing.row, existing.view);
        notice('已打开现有 PDF，未提交翻译任务；可在左侧下载当前版本。');
      } catch (error) { notice(error.message); }
      return;
    }
    if (imageTranslation && !attached.length) { notice('请先上传或粘贴待译截图。'); return; }
    if (taskMode === 'translate' && attached.length && !imageTranslation) { notice('已附加截图，请选择“截图翻译”；翻译全文前请先移除截图。'); return; }
    if (fullTranslation && !currentDocument) { notice('请先上传 PDF、获取论文 PDF 或获取网页全文，再开始全文翻译。'); return; }
    if (layoutPdf && currentDocument?.kind !== 'pdf') { notice('保留版式翻译需要原 PDF，请先上传 PDF 或点击“获取论文 PDF”。'); return; }
    const model = chosenModel();
    if (!model) { notice('请先选择当前可用的 Codex 模型。'); return; }
    if ((taskMode === 'figure' || attached.length) && !model.images) { notice('所选模型仅支持文字，请切换支持图片的模型后发送截图或解释配图。'); return; }
    const sentScreenshots = [...attached];
    const empty = $('.chat-empty'); if (empty) empty.remove();
    if (revision) { closeEditor(); drawHistory(history.slice(0, history.findIndex(row => row.id === revision.row.id))); }
    if (revision?.kind !== 'regenerate') message('user', text, 'completed', '', '', '', null, sentScreenshots);
    const body = message('assistant', '', 'running');
    showProgress(body);
    scrollLatest(true);
    let answer = '', done = false, finished = false, failure = '', submitted = false;
    const sentPaper = paperId;
    const sentDraft = draftKey();
    setBusy(true); notice('Codex 正在阅读资料…');
    status('请求已发送，正在等待 Codex', 'working');
    const activeController = new AbortController(); controller = activeController;
    try {
      const response = await api('/api/ask', { method: 'POST', stream: true, signal: activeController.signal,
        body: JSON.stringify({ paper_id: paperId, message: text, mode:taskMode, model: model.id, attachment_ids: sentScreenshots.map(x => x.id),
          pages:task.pages || '', translation_target:task.translation_target || 'zh', translation_source:taskSource, expected_leaf:activeLeaf, expected_document_hash:currentDocument?.hash || '',
          edit_message_id:revision?.kind === 'edit' ? revision.row.id : 0, regenerate_message_id:revision?.kind === 'regenerate' ? revision.row.id : 0,
          request_id: crypto.randomUUID() }) });
      if (!revision && !layoutPdf) $('.chat-composer textarea').value = '';
      submitted = true;
      if (!revision) { screenshots = []; drawScreenshots(); }
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
            showProgress(body, { ...(value.translation ? {} : { message: '正在生成回答，内容将逐步显示' }), ...(value.model_activity === false ? {} : {model_activity_at: Date.now() / 1000}) });
            scrollLatest();
          }
          if (value.type === 'progress') { showProgress(body, value); notice(value.message); }
          if (value.type === 'activity') showProgress(body, value);
          if (value.type === 'heartbeat') showProgress(body, value.progress || {});
          if (value.type === 'error') { failure = value.message; status(value.message, value.state); notice(value.message); }
          if (value.type === 'done') {
            done = true; finished = value.status === 'completed';
            notice(finished ? '已完成，可复制或导出 PDF。' : failure || (fullTranslation ? '全文翻译已停止；已完成部分已保存，保持相同模型、方向和要求再次开始可继续。' : '本次生成已停止，可重新发送问题。'));
          }
        }
      }
      if (!done) { failure = activeController.signal.aborted ? '已停止生成，可以重新发送问题。' : '连接中断，已收到的内容已保留。可以重新发送问题。'; notice(failure); }
    } catch (error) {
      failure = error.name === 'AbortError' ? (fullTranslation ? '全文翻译已停止，已完成部分已保存；再次开始可继续。' : '已停止生成，可以重新发送问题。') : error.message;
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
        if (!revision && !finished && !$('.chat-composer textarea').value) $('.chat-composer textarea').value = text;
        if (!revision) rememberDraft();
        await loadPaper().catch(() => {});
        if (revision?.kind === 'edit' && !submitted) editQuestion(revision.row, text);
        if (finished && layoutPdf) {
          const row = latestTranslation();
          if (row?.artifact?.kind === 'layout-pdf') {
            await previewPdf(row).catch(e => notice(e.message));
            await downloadPdf(sentPaper, row.id, true).catch(e => notice(e.message + ' 可在左侧点击“下载当前 PDF”重试。'));
          }
        }
        if (!revision && !finished && sentScreenshots.length) {
          const pendingIds = new Set(screenshots.map(x => x.id));
          screenshots = sentScreenshots.map(x => ({...x, sent: x.sent || (submitted && !pendingIds.has(x.id))})); drawScreenshots();
        }
      } else if (!revision) drafts.set(sentDraft, { text: finished ? '' : text });
    }
  }

  async function downloadPdf(targetPaper, messageId = null, translation = false, button = null) {
    if (button?.disabled) return;
    if (button) button.disabled = true;
    const title = ($('.chat-paper-title').textContent || targetPaper).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 70).trim();
    const paired=translation && bilingualTranslation(history.find(row=>row.id===messageId));
    try {
      notice(translation && layoutTranslation(history.find(row => row.id === messageId)) ? '正在下载已生成的 PDF…' : '正在导出 PDF…');
      const response = await api(`/api/papers/${targetPaper}/export-pdf${messageId ? '?message_id=' + messageId : ''}`, { stream: true });
      const blob = await response.blob();
      if (!blob.type.includes('application/pdf')) throw new Error('未取得 PDF 文件，请重试。');
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url; a.download = `${title}-${paired ? '中英对照' : translation ? '全文翻译' : messageId ? '回答-' + messageId : '对话记录'}.pdf`;
      a.click(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      notice(paired ? '中英对照 PDF 已下载。' : translation ? '译文 PDF 已下载。' : 'PDF 已生成并下载。');
    } finally { if (button) button.disabled = false; }
  }

  async function action(name) {
    try {
      if (name === 'reader') { const view=await ensureReader();view.open();await view.update(paperId,currentDocument,pdfVersions);return; }
      if (name === 'latest') return scrollLatest(true);
      if (name === 'settings') return toggleSettings();
      if (name === 'connect') return await connect();
      if (name === 'stop') return await stop();
      if (name === 'forget-browser') return await forgetBrowser();
      if (!token || !paperId) { notice('请先连接并选择论文。'); if (['upload','fetch-pdf'].includes(name)) documentNotice('请先连接本机助手并选择论文。', 'error', true); return; }
      if (name === 'export-pdf') return await downloadPdf(paperId, null, false, $('[data-chat-action="export-pdf"]'));
      if (name === 'upload') { uploadPaper = paperId; return $('.chat-documents input[type="file"]').click(); }
      if (name === 'screenshot') { screenshotPaper = paperId; return $('.chat-image-input').click(); }
      if (name === 'fetch-pdf') return await prepareDocument('/fetch-pdf', null, '正在获取并解析论文 PDF，请稍候…');
      if (name === 'fulltext') return await prepareDocument('/fulltext', null, '正在获取网页全文…');
      if (name === 'clear' && window.confirm('清除这篇论文的本机对话？星级、原文、译文、翻译进度和批注会保留。')) {
        const data = await api(endpoint(), { method: 'DELETE' }); releasePreviews(); screenshots = []; drawScreenshots(); await loadPaper(); notice(data.message);
      }
      if (name === 'export') {
        const data = await api(endpoint());
        const md = `# ${data.paper.title_zh || data.paper.title}\n\n` + data.history.map(m => `## ${m.role === 'user' ? '你' : `Codex${m.model ? ` · ${m.model}` : ''}`}${m.status === 'completed' ? '' : '（未完成）'}\n\n${m.content}`).join('\n\n');
        const url = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
        const a = document.createElement('a'); a.href = url; a.download = `${paperId}-Codex.md`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) { notice(error.message); if (['upload','fetch-pdf','fulltext'].includes(name)) documentNotice(error.message, 'error', true); }
  }
  async function forgetBrowser() {
    if (busy || documentPending || remoteDocumentPending || screenshotPending) return;
    await reader?.savePosition().catch(()=>{});
    if (deviceToken) await api('/api/session/forget', { method: 'POST', body: JSON.stringify({ device_token: deviceToken }) }, false);
    clearTimeout(reconnectTimer);
    deviceToken = ''; writeStorage('localStorage', browserKey, ''); clearSession();
    writeStorage('localStorage', rememberKey, 'off'); $('.chat-remember input').checked = false;
    $('.chat-pair-row input').value = ''; if (local) setPairCode('');
    availableModels = []; $('#chat-model').replaceChildren(new Option('连接后加载可用模型', '')); setBusy(false);
    updateRemembered(); $('.chat-connect').hidden = false;
    status('已取消自动连接；下次使用时请重新配对。', 'unpaired');
  }
  $('.chat-remember input').addEventListener('change', async event => {
    const checked = event.target.checked;
    try {
      if (!checked && deviceToken) await forgetBrowser();
      else writeStorage('localStorage', rememberKey, checked ? 'on' : 'off');
    } catch (error) { event.target.checked = true; notice(error.message); }
  });
  dialog.addEventListener('click', event => {
    const button = event.target.closest('[data-chat-action]'); if (button) action(button.dataset.chatAction);
    const shortcut = event.target.closest('[data-mode]');
    if (shortcut) {
      rememberDraft();
      mode = shortcut.dataset.mode;
      if (mode === 'translate' && screenshots.length) { translationSource = 'image'; $('.chat-translation-source').value = 'image'; }
      restoreDraft();
      const input = $('.chat-composer textarea');
      input.focus();
    }
  });
  async function prepareDocument(path, body, pendingText) {
    const blocked = () => busy || documentPending || remoteDocumentPending || screenshotPending || editing;
    if (blocked()) { documentNotice('请先完成当前操作，再获取或上传 PDF。', 'error', true); return; }
    const targetPaper = paperId;
    if(reader && !await reader.prepareLeave())return;
    if (targetPaper !== paperId || blocked()) return;
    documentPending = true; documentAction = path; setBusy(busy); documentNotice(pendingText, 'loading', true); notice(pendingText);
    let outcome = '', failed = false;
    try {
      const data = await api(`/api/papers/${targetPaper}${path}`, { method: 'POST', ...(body ? { body } : {}) });
      outcome = data.message;
    } catch (error) { outcome = error.message; failed = true; }
    finally {
      documentPending = false; documentAction = '';
      if (paperId === targetPaper) {
        await loadPaper().catch(() => {});
        if (!remoteDocumentPending) documentNotice(outcome, failed ? 'error' : 'ready', true);
        notice(outcome);
      }
      setBusy(busy);
    }
  }
  $('.chat-documents input[type="file"]').addEventListener('change', async (event) => {
    const file = event.target.files[0]; if (!file) return;
    event.target.value = '';
    if (uploadPaper !== paperId) { notice('论文已切换，请在当前论文重新选择要上传的 PDF。'); return; }
    if (file.size > 20 * 1024 * 1024) { documentNotice('PDF 超过 20 MB，请选择较小的文件。', 'error', true); return; }
    const form = new FormData(); form.append('file', file);
    await prepareDocument('/pdf', form, `正在上传并解析 ${file.name}…`);
  });
  $('.chat-composer').addEventListener('submit', send);
  $('.chat-image-input').addEventListener('change', event => {
    const files = [...event.target.files]; event.target.value = ''; uploadScreenshots(files, screenshotPaper);
  });
  $('.chat-composer').addEventListener('paste', event => {
    const files = [...(event.clipboardData?.items || [])].filter(x => x.kind === 'file' && x.type.startsWith('image/')).map(x => x.getAsFile()).filter(Boolean);
    if (files.length) { event.preventDefault(); uploadScreenshots(files); }
  });
  $('.chat-composer').addEventListener('dragover', event => {
    if ([...(event.dataTransfer?.types || [])].includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }
  });
  $('.chat-composer').addEventListener('drop', event => {
    if (event.dataTransfer?.files.length) { event.preventDefault(); uploadScreenshots([...event.dataTransfer.files]); }
  });
  window.addEventListener('pagehide', releasePreviews);
  $('.chat-translation-source').addEventListener('change', () => {
    rememberDraft(); translationSource = $('.chat-translation-source').value; restoreDraft();
  });
  $('.chat-translation-target').addEventListener('change', updateMode);
  $('.chat-composer textarea').addEventListener('input', updateMode);
  $('#chat-model').addEventListener('change', () => {
    if (chosenModel()) writeStorage('localStorage', modelKey, $('#chat-model').value);
    updateModelHint();
    updateMode();
    notice(`后续请求将使用 ${$('#chat-model').value}，当前论文的对话记录会保留。`);
  });
  updateMode();
  $('.chat-paste-hint').textContent = 'Enter 发送 · Shift+Enter 换行';
  $('.chat-composer textarea').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing && event.keyCode !== 229 && (!event.shiftKey || event.ctrlKey || event.metaKey)) {
      event.preventDefault(); $('.chat-composer').requestSubmit();
    }
  });
  let closing=false;
  $('.chat-close').onclick = async () => {
    if(closing)return;closing=true;
    try {if(reader && !await reader.prepareLeave())return;clearTimeout(reconnectTimer);await stop();dialog.close();if(lastTrigger)lastTrigger.focus();}
    finally {closing=false;}
  };
  dialog.addEventListener('cancel', event => { event.preventDefault(); $('.chat-close').click(); });
  function reconnectWhenVisible() {
    if (dialog.open && !document.hidden && !busy && (token || deviceToken) && $('.chat-status').dataset.state === 'offline') connect();
  }
  window.addEventListener('online', reconnectWhenVisible);
  window.addEventListener('focus', reconnectWhenVisible);
  document.addEventListener('visibilitychange', reconnectWhenVisible);
  window.addEventListener('storage', event => {
    if (event.key !== browserKey) return;
    deviceToken = readStorage('localStorage', browserKey);
    if (!/^[A-Za-z0-9_-]{43}$/.test(deviceToken)) deviceToken = '';
    devicePersisted = Boolean(deviceToken); clearSession(); updateRemembered();
    if (!deviceToken) { status('浏览器授权已在其他标签页取消，请重新配对。', 'unpaired'); $('.chat-connect').hidden = false; }
    else if (dialog.open && !busy) connect();
  });
  document.addEventListener('paper-library-connected',()=>{token=readStorage(session,key);deviceToken=readStorage('localStorage',browserKey);if(dialog.open)connect();});
  document.addEventListener('click', event => { const button = event.target.closest('.codex-entry'); if (button) open(button.dataset.paperId, button.dataset.paperTitle, button).then(()=>{if(button.dataset.openReader==='true')reader?.open();}); });
  if (local) {
    const hash = new URLSearchParams(location.hash.slice(1)); setPairCode(hash.get('pair') || '');
    if (pairCode) { historyReplace(); $('.chat-pair-row input').value = pairCode; }
    paperId = new URLSearchParams(location.search).get('paper') || '';
    document.getElementById('local-open-chat').onclick = () => open(document.getElementById('local-paper-list').value || paperId, '', null);
    open(paperId, '', null);
  }
  function historyReplace() { window.history.replaceState(null, '', location.pathname + location.search); }
})();
