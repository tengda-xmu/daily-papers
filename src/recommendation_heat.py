"""Source-specific observations and conservative repeat recommendation evidence."""
from __future__ import annotations

import hashlib
from difflib import SequenceMatcher
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from src.editions import BEIJING, read, write
from src.models import parse_date, normalize_doi

DEFAULTS = {'cooldown_days': 30, 'window_days': 30, 'min_samples': 3, 'min_span_days': 14,
            'min_increases': 2, 'min_citations': 5, 'min_growth': .2, 'max_age_days': 3,
            'attention_sources': 2, 'historical_slots': 1}


def settings(root):
    configured = read(root / 'config/recommendation-policy.json')
    return {**DEFAULTS, **{k: v for k, v in configured.items() if k in DEFAULTS and type(v) in (int, float) and v >= 0}}


def citation_evidence(observations, last_core, now, policy=None):
    policy = {**DEFAULTS, **(policy or {})}
    since = max(last_core, now - timedelta(days=policy['window_days']))
    sources = sorted({r.get('source', '') for r in observations})
    for source in sources:
        days = {}
        for row in observations:
            when = parse_date(row.get('observed_at'))
            if (source and row.get('source') == source and when and since < when <= now
                    and type(row.get('count')) is int and row['count'] >= 0):
                day = when.astimezone(BEIJING).date()
                if day not in days or when > days[day][0]:
                    days[day] = (when, row['count'])
        rows = sorted(days.values())
        # A correction invalidates older comparisons; accumulate a new baseline.
        start = 0
        for i in range(1, len(rows)):
            if rows[i][1] < rows[i-1][1]:
                start = i
        rows = rows[start:]
        if len(rows) < policy['min_samples'] or now - rows[-1][0] > timedelta(days=policy['max_age_days']):
            continue
        if (rows[-1][0] - rows[0][0]).days < policy['min_span_days']:
            continue
        increases = sum(b[1] > a[1] for a, b in zip(rows, rows[1:]))
        delta = rows[-1][1] - rows[0][1]
        if increases >= policy['min_increases'] and delta >= policy['min_citations'] and (not rows[0][1] or delta / rows[0][1] >= policy['min_growth']):
            return {'kind': 'citations', 'source': source, 'before': rows[0][1], 'after': rows[-1][1],
                    'from': rows[0][0].isoformat(), 'to': rows[-1][0].isoformat(),
                    'reason': f"{source} 被引数由 {rows[0][1]} 增至 {rows[-1][1]}（新增 {delta} 次）"}
    return None


def attention_evidence(events, last_core, now, policy=None):
    policy = {**DEFAULTS, **(policy or {})}
    since = max(last_core, now - timedelta(days=policy['window_days']))
    seen, accepted = set(), []
    for event in sorted(events, key=lambda e: e.get('published_at', '')):
        when = parse_date(event.get('published_at'))
        identity = event.get('story_id')
        if not (when and since < when <= now and event.get('verified') is True and identity and
                event.get('organization') and urlsplit(event.get('url', '')).scheme in ('http', 'https')):
            continue
        text = event.get('story_text', '')
        if identity in seen or any(text and prior.get('story_text') and
                                  SequenceMatcher(None, text, prior['story_text']).ratio() >= .82 for prior in accepted):
            continue
        seen.add(identity)
        accepted.append(event)
    if (len({e['organization'] for e in accepted}) >= policy['attention_sources']
            and len({parse_date(e['published_at']).astimezone(BEIJING).date() for e in accepted}) >= 2):
        return {'kind': 'attention', 'events': accepted,
                'reason': f"近期新增 {len({e['organization'] for e in accepted})} 家独立学术来源关注"}
    return None


def repeat_evidence(state, heat, now, policy=None):
    policy = {**DEFAULTS, **(policy or {})}
    last = parse_date(state['last_core']['generated_at'])
    if (now.astimezone(BEIJING).date() - last.astimezone(BEIJING).date()).days < policy['cooldown_days']:
        return None
    unused = [e for e in heat.get('attention', []) if e.get('story_id') not in state.get('used_attention', set())]
    return (citation_evidence(heat.get('citations', []), last, now, policy)
            or attention_evidence(unused, last, now, policy))


def fetch_citations(paper):
    """A fixed DOI endpoint, independent of the discovery publication window."""
    doi = normalize_doi(paper.get('doi'))
    headers = {'User-Agent': 'DailyPapers/1.0 (+https://tengda-xmu.github.io/daily-papers/)'}
    if not doi:
        # Preprints can be monitored by a canonical arXiv identifier as well.
        values = ' '.join(str(paper.get(k) or '') for k in ('source_id', 'landing_url', 'oa_url'))
        match = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}|[a-z.-]+/\d{7})', values, re.I)
        if not match:
            return []
        if os.getenv('SEMANTIC_SCHOLAR_API_KEY'):
            headers['x-api-key'] = os.environ['SEMANTIC_SCHOLAR_API_KEY']
        request = Request('https://api.semanticscholar.org/graph/v1/paper/ARXIV:' + quote(match[1], safe='')
                          + '?fields=citationCount,externalIds', headers=headers)
        with urlopen(request, timeout=12) as response:
            item = json.loads(response.read(2 * 1024 * 1024))
        if (item.get('externalIds') or {}).get('ArXiv', '').lower() != match[1].lower():
            raise ValueError('Citation identity mismatch')
        count = item.get('citationCount')
        return [{'source': 'Semantic Scholar', 'count': count}] if type(count) is int and count >= 0 else []
    request = Request('https://api.crossref.org/works/' + quote(doi, safe=''), headers=headers)
    with urlopen(request, timeout=12) as response:
        item = json.loads(response.read(2 * 1024 * 1024))['message']
    if normalize_doi(item.get('DOI')) != doi:
        raise ValueError('Citation identity mismatch')
    value = item.get('is-referenced-by-count')
    return [{'source': 'Crossref', 'count': value}] if type(value) is int and value >= 0 else []


def verified_attention(paper, leads):
    """Only explicitly linked paper mentions, never conferences or search snippets."""
    doi = normalize_doi(paper.get('doi'))
    if not doi:
        return []
    events = []
    for lead in leads:
        if lead.get('kind') != 'news' or lead.get('provider') not in ('official', 'wechat') or lead.get('indexed'):
            continue
        text = ' '.join(str(lead.get(k) or '') for k in ('title', 'summary', 'description', 'url'))
        dois = {normalize_doi(d) for d in re.findall(r'10\.\d{4,9}/[^\s<>"\u4e00-\u9fff]+', text, re.I)}
        if doi not in dois or len(str(lead.get('summary') or '')) < 80:
            continue
        url = lead.get('url', '')
        host = urlsplit(url).hostname
        if not host or not parse_date(lead.get('published_at')):
            continue
        # Official publisher sites identify an organization; WeChat needs the
        # publishing account, not the shared mp.weixin.qq.com host.
        organization = lead.get('organization') or (lead.get('source') if host == 'mp.weixin.qq.com' else host.removeprefix('www.'))
        title = re.sub(r'\W+', '', lead.get('title', '')).casefold()
        story = lead.get('original_url') or lead.get('story_id') or hashlib.sha256(title.encode()).hexdigest()
        events.append({'verified': True, 'published_at': lead['published_at'], 'url': url,
                       'title': lead.get('title', ''), 'organization': organization, 'story_id': story,
                       'story_text': re.sub(r'\W+', '', lead.get('summary', '')).casefold()[:500]})
    return events


def refresh(data, history, records, leads, now, fetch=fetch_citations):
    path = data / 'recommendation-heat.json'
    result = read(path, {'papers': {}})
    day = now.astimezone(BEIJING).date().isoformat()
    counts = {'checked_at': now.isoformat(), 'fresh': 0, 'failed': 0, 'unavailable': 0}
    observed = {}
    # Preserve provenance BEFORE source deduplication fills metadata fields.
    for record in records:
        if type(record.citation_count) is int and record.citation_count >= 0:
            matched = history.find(record.to_dict())
            if matched:
                observed.setdefault(matched['id'], []).append({'source': record.source, 'count': record.citation_count})
    for identifier, state in history.papers.items():
        row = result['papers'].setdefault(identifier, {'citations': [], 'attention': []})
        paper = state['paper']
        samples = observed.get(identifier, [])[:]
        already = {r['source'] for r in row['citations'] if str(r.get('observed_at', ''))[:10] == day}
        failed = False
        if row.get('checked_day') != day:
            try:
                samples.extend(fetch(paper))
                row['checked_day'] = day
            except Exception:
                failed = True
        for sample in samples:
            if sample['source'] not in already:
                row['citations'].append({**sample, 'observed_at': now.astimezone(BEIJING).isoformat()})
                already.add(sample['source'])
        row['attention'] = list({e['story_id']: e for e in row['attention'] + verified_attention(paper, leads)}.values())
        counts['failed' if failed else 'fresh' if already else 'unavailable'] += 1
        row['status'] = 'partial' if failed and already else 'failed' if failed else 'ok' if already else 'unavailable'
    result['status'] = counts
    write(path, result)
    return result
