() => {
  const rows = [];
  const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content || '';
  const publicationURL = (value) => {
    try {
      const u = new URL(value, location.href);
      if (u.hostname.endsWith('researchgate.net') && /^\/publication\/\d+/.test(u.pathname)) {
        return u.origin + u.pathname;
      }
    } catch (_) {}
    return '';
  };
  const canonical = publicationURL(document.querySelector('link[rel="canonical"]')?.href || location.href);
  if (canonical && meta('citation_title')) {
    rows.push({title: meta('citation_title'), landing_url: canonical,
      authors: Array.from(document.querySelectorAll('meta[name="citation_author"]')).map(n => n.content),
      published_at: meta('citation_publication_date') || meta('citation_date'),
      venue: meta('citation_journal_title'), doi: meta('citation_doi')});
  }
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    let parsed;
    try { parsed = JSON.parse(script.textContent); } catch (_) { continue; }
    const visit = (node) => {
      if (Array.isArray(node)) { node.forEach(visit); return; }
      if (!node || typeof node !== 'object') return;
      if (node['@graph']) visit(node['@graph']);
      if (!['ScholarlyArticle', 'Article', 'Chapter'].includes(node['@type'])) return;
      const url = publicationURL(node.url || node.mainEntityOfPage?.['@id'] || canonical);
      if (!url) return;
      const authors = Array.isArray(node.author) ? node.author : [node.author];
      rows.push({title: node.headline || node.name, landing_url: url,
        authors: authors.map(a => typeof a === 'string' ? a : a?.name).filter(Boolean),
        published_at: node.datePublished, venue: node.isPartOf?.name || '',
        doi: typeof node.identifier === 'string' && node.identifier.startsWith('10.') ? node.identifier : ''});
    };
    visit(parsed);
  }
  for (const link of document.querySelectorAll('a[href*="/publication/"]')) {
    const url = publicationURL(link.href);
    const title = (link.innerText || '').trim();
    if (!url || title.length < 15 || /^(view|download|request|read|show)\b/i.test(title)) continue;
    let container = link.parentElement;
    for (let level = 0; level < 6 && container?.parentElement; level++) {
      const parent = container.parentElement;
      if (['MAIN', 'BODY', 'HTML'].includes(parent.tagName)) break;
      const ids = new Set(Array.from(parent.querySelectorAll('a[href*="/publication/"]'))
        .map(a => a.pathname.match(/^\/publication\/(\d+)/)?.[1]).filter(Boolean));
      if (ids.size !== 1) break;
      container = parent;
    }
    const text = container.innerText || '';
    const date = container.querySelector('time')?.getAttribute('datetime') ||
      text.match(/\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:19|20)\d{2}\b/i)?.[0] ||
      container.querySelector('[class*="date"], [class*="year"]')?.textContent || '';
    const authors = Array.from(container.querySelectorAll('a[href*="/profile/"]'))
      .map(a => a.innerText.trim()).filter(Boolean);
    const venue = container.querySelector('a[href*="/journal/"], [class*="journal"], [class*="venue"]')?.textContent?.trim() || '';
    rows.push({title, landing_url: url, authors: [...new Set(authors)], published_at: date, venue, text});
  }
  return rows;
}
