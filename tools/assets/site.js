(function () {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
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
  const reset = () => {
    [search, source, topic, venue, journal].forEach((el) => { if (el) el.value = ''; });
    if (sort) sort.value = 'recommended';
    $$('.cns-line button').forEach((button) => button.setAttribute('aria-pressed', 'false'));
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
    if (count) count.textContent = `显示 ${visible} / ${cards.length} 篇`;
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
    if (source) source.value = button.dataset.sourceFilter || '';
    $('#reading')?.scrollIntoView({ behavior: 'smooth' }); filter();
  }));
  $$('[data-journal-filter]').forEach((button) => button.addEventListener('click', () => {
    if (journal) journal.value = button.dataset.journalFilter || '';
    $('#reading')?.scrollIntoView({ behavior: 'smooth' }); filter();
  }));
  $$('[data-group-filter]').forEach((button) => button.addEventListener('click', () => {
    const active = button.getAttribute('aria-pressed') === 'true';
    $$('[data-group-filter]').forEach((b) => b.setAttribute('aria-pressed', 'false'));
    if (active) { if (venue) venue.value = ''; } else { button.setAttribute('aria-pressed', 'true'); if (venue) venue.value = button.dataset.groupFilter || ''; }
    $('#reading')?.scrollIntoView({ behavior: 'smooth' }); filter();
  }));
  sort?.addEventListener('change', () => {
    ['core', 'extended'].forEach((id) => {
      const container = $(`#${id}`); if (!container) return;
      const items = $$('.paper', container);
      items.sort((a, b) => sort.value === 'latest' ? (b.dataset.date || '').localeCompare(a.dataset.date || '') : Number(a.dataset.rank) - Number(b.dataset.rank)).forEach((item) => container.appendChild(item));
    });
  });
  $$('.copy-citation').forEach((button) => button.addEventListener('click', async () => {
    const toast = $('#toast');
    try { await navigator.clipboard.writeText(button.dataset.citation || ''); if (toast) { toast.textContent = '引用已复制'; toast.hidden = false; setTimeout(() => { toast.hidden = true; }, 1800); } }
    catch (_) { if (toast) { toast.textContent = '浏览器不允许复制，请手动选择引用'; toast.hidden = false; setTimeout(() => { toast.hidden = true; }, 2400); } }
  }));
  const top = $('.back-to-top');
  window.addEventListener('scroll', () => { if (top) top.hidden = window.scrollY < 480; }, { passive: true });
  const params = new URLSearchParams(window.location.search);
  if (params.get('topic') && topic) topic.value = params.get('topic');
  filter();
}());
