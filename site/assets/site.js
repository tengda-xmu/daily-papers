(function () {
  document.documentElement.classList.add('js');
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  if (location.origin === 'http://127.0.0.1:43127') {
    $$('a[href]').forEach((link) => {
      const url = new URL(link.href);
      if (url.origin === location.origin && url.pathname === '/setup.html') {
        link.href = 'https://tengda-xmu.github.io/daily-papers/setup.html' + url.search + url.hash;
      }
    });
  }
  // Keep old bookmarks working after moving the directory to settings.
  if (location.hash === '#journals' && !$('#journals') && $('#reading')) {
    const settings = $('#main-navigation a[href$="setup.html"]');
    if (settings) { location.replace(settings.href + '#journals'); return; }
  }
  const menu = $('.menu-toggle');
  const nav = $('#main-navigation');
  if (menu && nav) {
    menu.addEventListener('click', () => {
      const open = menu.getAttribute('aria-expanded') === 'true';
      menu.setAttribute('aria-expanded', String(!open));
      nav.classList.toggle('open', !open);
    });
    $$('.main-navigation a').forEach((link) => link.addEventListener('click', () => {
      menu.setAttribute('aria-expanded', 'false'); nav.classList.remove('open');
    }));
  }
  const search = $('#search');
  const source = $('#source');
  const topic = $('#topic');
  const venue = $('#venue');
  const journal = $('#journal');
  const sort = $('#sort');
  const cards = $$('.paper');
  const count = $('#result-count');
  const noData = $('#no-data');
  const noMatch = $('#no-match');
  const filterPanel = $('#advanced-filters');
  const filterToggle = $('#filter-toggle');
  function showFilters(open) {
    if (filterPanel) filterPanel.hidden = !open;
    filterToggle?.setAttribute('aria-expanded', String(open));
  }
  filterToggle?.addEventListener('click', () => showFilters(filterToggle.getAttribute('aria-expanded') !== 'true'));
  $('#filters')?.addEventListener('submit', (event) => event.preventDefault());
  function sortCards() {
    ['core', 'extended'].forEach((id) => {
      const container = $(`#${id}`); if (!container) return;
      $$('.paper', container).sort((a, b) => sort?.value === 'latest'
        ? (b.dataset.date || '').localeCompare(a.dataset.date || '')
        : Number(a.dataset.rank) - Number(b.dataset.rank)).forEach((item) => container.appendChild(item));
    });
  }
  const reset = () => {
    [search, source, topic, venue, journal].forEach((el) => { if (el) el.value = ''; });
    if (sort) sort.value = 'recommended';
    sortCards();
    filter();
  };
  function filter() {
    const query = (search?.value || '').trim().toLowerCase();
    const selectedSource = source?.value || '';
    const selectedTopic = topic?.value || '';
    const selectedVenue = venue?.value || '';
    const selectedJournal = journal?.value || '';
    let visible = 0;
    cards.forEach((card) => {
      const sources = JSON.parse(card.dataset.sources || '[]');
      const topics = JSON.parse(card.dataset.topics || '[]');
      const text = (card.dataset.search || '').toLowerCase();
      const show = (!query || text.includes(query)) && (!selectedSource || sources.includes(selectedSource)) &&
        (!selectedTopic || topics.includes(selectedTopic)) && (!selectedVenue || card.dataset.venue === selectedVenue) &&
        (!selectedJournal || card.dataset.journal === selectedJournal);
      card.hidden = !show;
      if (show) visible += 1;
    });
    const activeFilters = [source, topic, venue, journal].filter((el) => el?.value).length;
    const filtering = activeFilters > 0 || Boolean(query);
    if (count) count.textContent = filtering ? `${visible} / ${cards.length} 篇` : `${cards.length} 篇推荐`;
    const filterCount = $('#filter-count');
    if (filterCount) { filterCount.textContent = String(activeFilters); filterCount.hidden = activeFilters === 0; }
    $$('.clear-filters').forEach((button) => { button.hidden = !filtering; });
    $$('[data-group-filter]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.groupFilter === selectedVenue)));
    $$('[data-topic-filter]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.topicFilter === selectedTopic)));
    $$('[data-journal-filter]').forEach((button) => button.classList.toggle('active', button.dataset.journalFilter === selectedJournal));
    if (noData) noData.hidden = cards.length > 0;
    if (noMatch) noMatch.hidden = cards.length === 0 || visible > 0;
    ['core', 'extended'].forEach((id) => {
      const section = $(`#${id}`)?.closest('.paper-section');
      if (!section) return;
      const shown = $$('.paper:not([hidden])', section).length;
      section.hidden = cards.length > 0 && shown === 0;
      const badge = $(`#${id}-count`);
      if (badge) badge.textContent = `${shown} 篇`;
    });
  }
  [search, source, topic, venue, journal].forEach((el) => el?.addEventListener('input', filter));
  [source, topic, venue, journal].forEach((el) => el?.addEventListener('change', filter));
  $('#reset')?.addEventListener('click', reset);
  $$('[data-reset]').forEach((button) => button.addEventListener('click', reset));
  $$('[data-source-filter]').forEach((button) => button.addEventListener('click', () => {
    reset();
    if (source) source.value = button.dataset.sourceFilter || '';
    showFilters(true); filter(); scrollToReading();
  }));
  $$('[data-journal-filter]').forEach((button) => button.addEventListener('click', () => {
    reset();
    if (journal) journal.value = button.dataset.journalFilter || '';
    showFilters(true); filter(); scrollToReading();
  }));
  $$('[data-topic-filter]').forEach((button) => button.addEventListener('click', () => {
    if (topic) topic.value = topic.value === button.dataset.topicFilter ? '' : button.dataset.topicFilter;
    filter();
    if (button.classList.contains('paper-direction')) scrollToReading();
  }));
  $$('[data-group-filter]').forEach((button) => button.addEventListener('click', () => {
    const active = button.getAttribute('aria-pressed') === 'true';
    $$('[data-group-filter]').forEach((b) => b.setAttribute('aria-pressed', 'false'));
    if (active) { if (venue) venue.value = ''; } else { button.setAttribute('aria-pressed', 'true'); if (venue) venue.value = button.dataset.groupFilter || ''; }
    if (journal) journal.value = '';
    filter();
  }));
  sort?.addEventListener('change', sortCards);
  function scrollToReading() {
    $('#reading')?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  }
  $$('.paper-toggle').forEach((button) => button.addEventListener('click', () => {
    const panel = document.getElementById(button.getAttribute('aria-controls'));
    if (!panel) return;
    const open = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', String(open));
    panel.hidden = !open;
    button.firstChild.nodeValue = open ? '收起详情' : (button.dataset.label || '摘要与笔记');
    $('span', button).textContent = open ? '−' : '＋';
  }));
  const previews = $$('.figure-preview');
  let figureDialog;
  let figureOpener;
  if (previews.length && typeof HTMLDialogElement !== 'undefined') {
    figureDialog = document.createElement('dialog');
    figureDialog.className = 'figure-dialog';
    figureDialog.setAttribute('aria-labelledby', 'figure-dialog-title');
    figureDialog.innerHTML = '<div class="figure-dialog-header"><h2 id="figure-dialog-title"></h2><button type="button" class="figure-dialog-close" autofocus>关闭</button></div><img class="figure-dialog-image" alt=""><div class="figure-dialog-footer"><div class="figure-dialog-credit"></div><a class="figure-original-size" target="_blank" rel="noopener noreferrer">查看原尺寸</a></div>';
    document.body.append(figureDialog);
    $('.figure-dialog-close', figureDialog).addEventListener('click', () => figureDialog.close());
    figureDialog.addEventListener('click', (event) => {
      const box = figureDialog.getBoundingClientRect();
      if (event.target === figureDialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) figureDialog.close();
    });
    figureDialog.addEventListener('close', () => {
      document.body.classList.remove('figure-opened');
      figureOpener?.focus({ preventScroll: true });
    });
  }
  previews.forEach((preview) => {
    const image = $('img', preview);
    const figure = preview.closest('figure');
    function unavailable() {
      image.hidden = true;
      figure.classList.add('image-unavailable');
      $('.figure-open', preview).textContent = '图片暂不可用，查看原文图表';
      preview.href = $('.figure-credit a', figure).href;
      preview.setAttribute('aria-label', '图片暂不可用，查看原文图表');
    }
    image.addEventListener('error', unavailable);
    if (image.complete && image.naturalWidth === 0) unavailable();
    preview.addEventListener('click', (event) => {
      if (!figureDialog || image.hidden || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      figureOpener = preview;
      $('#figure-dialog-title').textContent = $('.figure-title', figure).textContent;
      const large = $('.figure-dialog-image', figureDialog);
      large.src = image.currentSrc || image.src;
      large.alt = image.alt;
      $('.figure-dialog-credit', figureDialog).replaceChildren($('.figure-credit', figure).cloneNode(true));
      $('.figure-original-size', figureDialog).href = preview.href;
      figureDialog.showModal();
      document.body.classList.add('figure-opened');
    });
  });
  function openAnchor(hash) {
    let target;
    try { target = document.getElementById(decodeURIComponent(hash.slice(1))); } catch (_) { return; }
    for (let node = target; node; node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true;
    }
  }
  $$('a[href*="#"]').forEach((link) => link.addEventListener('click', () => {
    const url = new URL(link.href);
    if (url.origin === location.origin && url.pathname === location.pathname) openAnchor(url.hash);
  }));
  window.addEventListener('hashchange', () => openAnchor(location.hash));
  $$('.copy-citation').forEach((button) => button.addEventListener('click', async () => {
    const toast = $('#toast');
    try { await navigator.clipboard.writeText(button.dataset.citation || ''); if (toast) { toast.textContent = '引用已复制'; toast.hidden = false; setTimeout(() => { toast.hidden = true; }, 1800); } }
    catch (_) { if (toast) { toast.textContent = '浏览器不允许复制，请手动选择引用'; toast.hidden = false; setTimeout(() => { toast.hidden = true; }, 2400); } }
  }));
  const top = $('.back-to-top');
  window.addEventListener('scroll', () => { if (top) top.hidden = window.scrollY < 480; }, { passive: true });
  const params = new URLSearchParams(window.location.search);
  if (params.get('topic') && topic) { topic.value = params.get('topic'); showFilters(true); }
  if (params.get('journal') && journal) {
    const value = params.get('journal');
    if (Array.from(journal.options).some(option => option.value === value)) {
      journal.value = value; showFilters(true);
    }
  }
  openAnchor(location.hash);
  filter();
}());
