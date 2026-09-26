"""Shared social article archive, independent of paper recommendation batches."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from src.models import parse_date
from src.public_sources import ROOT, clean, read, write, fetch, article, canonical
from src.wechat_metadata import public_records, article_url

PATH = 'data/social-articles.json'
MANUAL = 'config/xiaohongshu-notes.json'
FIELDS = ('id', 'article_id', 'platform', 'provider', 'source', 'account', 'author', 'title', 'url',
          'published_at', 'reported_at', 'checked_at', 'verified_at', 'verification', 'summary',
          'evidence_text', 'evidence_kind', 'content_type', 'column', 'kind', 'categories', 'scenarios',
          'official_urls', 'related_urls', 'source_links', 'account_verified', 'read_status')
EXCLUDED = r'代写|代发论文|论文代发|保录|付费课程|带货|供应商注册|物资采购|工程施工|普通会员注册|获批名单|拟资助|拟立项|获奖名单'
AI_ANCHOR = r'\b(?:AI|LLM|GPT|claude|gemini|qwen|deepseek|codex|copilot|agent|agents|agentic|MCP|RAG|langchain|langgraph)\b|人工智能|大模型|智能体|机器学习|Agent Skills|AI工具|代码生成|vibe coding'


def route(row):
    from src.opportunities import classify as opportunity
    from src.research_leads import classify
    from src.ai_updates import classify as ai_classify, scenarios
    row = dict(row)
    title = clean(row.get('title'))
    text = title + ' ' + clean(row.get('summary')) + ' ' + clean(row.get('evidence_text'))
    if not title or re.search(EXCLUDED, title, re.I):
        return None
    kind = opportunity(title)
    if not kind and re.search(r'招聘|招工|招生|求职|会员注册', title):
        return None
    kind = kind or classify(title)
    if kind not in ('funding', 'academic_role', 'conference', 'call') and re.search(AI_ANCHOR, text, re.I):
        row.update(column='ai', kind='ai', categories=ai_classify(text) or ['tools'], scenarios=scenarios(text))
    else:
        row.update(column='leads', kind=kind)
    row['content_type'] = ('practice' if re.search(r'教程|实战|实践|经验|工作流|手把手|how to|tutorial|workflow', title, re.I)
                           else 'report')
    if row.get('evidence_kind') not in ('article', 'manual_text'):
        row['verification'] = 'pending'
    row.setdefault('evidence_kind', 'search_snippet')
    row.setdefault('verification', 'pending')
    row['summary'] = clean(row.get('summary') or row.get('evidence_text'))[:220]
    row['evidence_text'] = clean(row.get('evidence_text'))[:900]
    row.setdefault('reported_at', row.get('published_at', ''))
    return {k: v for k, v in row.items() if k in FIELDS}


def provenance(row):
    return {'platform': row.get('platform', row.get('provider', 'official')), 'name': row.get('account') or row.get('author') or row.get('source', ''),
            'url': row['url'], 'published_at': row.get('reported_at') or row.get('published_at', ''),
            'evidence_kind': row.get('evidence_kind', 'official')}


def links(rows):
    return list({r['url']: r for r in rows if r.get('url')}.values())


def combine(official, social):
    """A matching primary notice wins; original practice stays a separate article."""
    from src.ai_updates import event_identity
    result = [dict(r) for r in official]
    for row in social:
        row = dict(row)
        target = None
        for existing in result:
            same = row['url'] == existing['url'] or row['id'] == existing['id']
            evidence_match = existing['url'] in row.get('official_urls', []) and sum(r['url'] in row.get('official_urls', []) for r in result) == 1
            release = row.get('kind') == 'ai' and event_identity(row).startswith('release:') and event_identity(row) == event_identity(existing)
            if same or (row.get('content_type') != 'practice' and (evidence_match or release)):
                target = existing
                break
        if target is None:
            row['source_links'] = links(row.get('source_links', []) + [provenance(row)])
            result.append(row)
        else:
            target['source_links'] = links(target.get('source_links', []) + [provenance(target)] + row.get('source_links', []) + [provenance(row)])
    return result


def enrich_public(row, root=ROOT, *, fetcher=fetch):
    """Only structured article text counts as reading evidence, not a login shell."""
    row = dict(row)
    if row.get('platform') == 'xiaohongshu':
        from src.xiaohongshu import read_note
        content = read_note(row['url'])
        row.update({k: v for k, v in content.items() if v})
        text = row.get('evidence_text', '')
        body = None
    else:
        url = article_url(row['url'])
        if not url:
            return row
        body = fetcher(url, {'hosts': ['mp.weixin.qq.com']})
        soup = BeautifulSoup(body.decode('utf-8', 'replace'), 'html.parser')
        main = soup.select_one('#js_content')
        if main is None or len(clean(main.get_text())) < 50:
            raise ValueError('No readable WeChat article')
        text = clean(main.get_text(' ', strip=True))
        row.update(evidence_text=text, evidence_kind='article')
        heading = soup.select_one('#activity-name')
        account = soup.select_one('#js_name')
        if heading:
            row['title'] = clean(heading.get_text())
        if account:
            row['account'] = row['source'] = clean(account.get_text())
        stamp = re.search(r'(?:var\s+)?ct\s*=\s*["\'](\d{10})["\']', body.decode('utf-8', 'replace'))
        if stamp:
            row['published_at'] = datetime.fromtimestamp(int(stamp[1]), timezone.utc).isoformat()
    sources = [s for path in ('config/ai-sources.json', 'config/opportunity-sources.json') for s in read(root/path).get('sources', [])]
    hosts = {host for s in sources for host in s.get('hosts', [])}
    candidates = re.findall(r'https?://[^\s<>"\u201c\u201d]+', text)
    if body:
        candidates += [urljoin(row['url'], a['href']) for a in BeautifulSoup(body, 'html.parser').select('a[href]')]
    row['official_urls'] = list(dict.fromkeys(canonical(u) for u in candidates if urlsplit(u).hostname in hosts))[:12]
    # A successful content read verifies this text, not the claims or account affiliation.
    row['verification'] = 'verified' if row.get('evidence_kind') == 'article' else 'pending'
    return row


def wechat_snapshot(root, now):
    from src.sources.wechat_rss import WeChatRSSAdapter
    from src.sources.wechat_public_index import WeChatPublicIndexAdapter, daily_queries
    from src.wechat_subscriptions import effective_accounts, name_key
    accounts = [r for r in effective_accounts(root) if r['enabled']]
    if not accounts:
        return [], [{'id':'wechat-rss', 'name':'微信公众号', 'url':'https://mp.weixin.qq.com/', 'status':'no_data', 'count':0, 'checked_at':now.isoformat()}]
    since = now - timedelta(days=90)
    rss = WeChatRSSAdapter(import_path=root/'data/inbox/wechat.json', merge_import=True)
    records = rss.fetch(since, now)
    policy = read(root/'config/wechat_accounts.json')
    queries = daily_queries(policy, accounts, now)
    index = WeChatPublicIndexAdapter(directory=root/'data/inbox/wechat-subscriptions.json', cache_dir=root/'data/cache/wechat-public', queries=queries)
    records.extend(index.fetch(since, now))
    names = {name_key(r['name']) for r in accounts}
    rows = [r for r in public_records(records) if name_key(r['account']) in names]
    statuses = []
    for adapter, label in ((rss, '公众号订阅与同步'), (index, '公众号公开索引')):
        status = adapter.status
        statuses.append({'id': 'wechat-' + ('rss' if adapter is rss else 'index'), 'name': label, 'url': 'https://mp.weixin.qq.com/',
            'status': status.status, 'count': status.count, 'message': status.message, 'checked_at': now.isoformat(),
            'last_success': now.isoformat() if status.status == 'ok' else ''})
    return rows, statuses


def refresh(root=ROOT, *, now=None, wechat_fetch=wechat_snapshot, xhs_fetch=None, enrich=enrich_public):
    from src.xiaohongshu import discover
    now = now or datetime.now(timezone.utc)
    previous = read(root/PATH)
    rows = {r['id']: r for r in previous.get('entries', [])}
    statuses, fresh = [], []
    try:
        records, sources = wechat_fetch(root, now)
        statuses.extend(sources)
    except Exception:
        records = []
        statuses.append({'id':'wechat-rss', 'name':'微信公众号', 'url':'https://mp.weixin.qq.com/', 'status':'error', 'checked_at':now.isoformat()})
    # Idempotent migration from earlier published paper snapshots.
    for path in [root/'data/daily.json', *sorted((root/'data/archive').glob('*.json'))]:
        records.extend(public_records(read(path).get('wechat_articles', [])))
    identities = {(r.get('account'),r['title'],r.get('published_at','')[:10]):r['id'] for r in rows.values() if r.get('platform') == 'wechat'}
    article_ids = {article_url(r['url']):r['id'] for r in rows.values() if r.get('platform') == 'wechat' and article_url(r['url'])}
    for r in records:
        key = (r['account'],r['title'],r['published_at'][:10])
        identity = article_ids.get(article_url(r['landing_url'])) or identities.get(key)
        identity = identity or 'wechat-' + hashlib.sha256('\n'.join(key).encode()).hexdigest()[:20]
        identities[key] = identity
        if article_url(r['landing_url']):
            article_ids[article_url(r['landing_url'])] = identity
        fresh.append({'id':identity, 'platform':'wechat', 'provider':'wechat', 'account':r['account'], 'source':r['account'],
            'article_id':hashlib.sha256((article_url(r['landing_url']) or '\n'.join(key)).encode()).hexdigest()[:20],
            'title':r['title'], 'url':r['landing_url'], 'published_at':r['published_at'], 'summary':r['summary'],
            'evidence_kind':'search_snippet' if r.get('access_mode') == 'public_index' else 'excerpt', 'verification':'pending'})
    try:
        found, sources = (xhs_fetch or discover)(root, now=now)
        fresh.extend(found); statuses.extend(sources)
    except Exception:
        statuses.append({'id':'xiaohongshu', 'name':'小红书', 'url':'https://www.xiaohongshu.com/', 'status':'error', 'checked_at':now.isoformat()})
    manual = read(root/MANUAL).get('entries', [])
    # Tombstones remove only manually imported copies; independent discoveries survive.
    for old in previous.get('manual_ids', []):
        if old not in {r['id'] for r in manual}:
            rows.pop(old, None)
    attempts = 0
    for raw in fresh + manual:
        old = rows.get(raw['id'], {})
        row = {**old, **raw}
        if old.get('evidence_text') and not raw.get('evidence_text'):
            row.update({k: old[k] for k in ('evidence_text','evidence_kind','verification','official_urls','read_status') if k in old})
        checked = parse_date(old.get('checked_at'))
        if row.get('evidence_kind') != 'manual_text' and (not checked or now - checked >= timedelta(days=1)) and attempts < 20 and 'weixin.sogou.com' not in row['url']:
            attempts += 1
            try:
                row = enrich(row, root)
                row['read_status'] = 'readable'
                row['verified_at'] = now.isoformat()
            except Exception:
                row['read_status'] = 'unavailable'
            row['checked_at'] = now.isoformat()
        normalized = route(row)
        if normalized:
            rows[row['id']] = normalized
    old_status = {s['id']: s for s in previous.get('sources', [])}
    for s in statuses:
        if not s.get('last_success'):
            s['last_success'] = old_status.get(s['id'], {}).get('last_success', '')
    outcome = 'ok' if all(s['status'] in ('ok','no_data') for s in statuses) else 'partial'
    result = {'version':1, 'checked_at':now.isoformat(), 'entries':list(rows.values()), 'sources':statuses,
              'outcome':outcome, 'manual_ids':[r['id'] for r in manual]}
    write(root/PATH, result)
    return result


def column(root, name):
    result = []
    bindings = read(root/'config/social-sources.json').get('official_accounts', [])
    snapshot = read(root/PATH)
    manual = read(root/MANUAL).get('entries', [])
    by_id = {r['id']:r for r in snapshot.get('entries',[]) if r['id'] not in snapshot.get('manual_ids',[])}
    by_id.update({r['id']:r for r in manual})
    for raw in by_id.values():
        row = route(raw)
        if not row or row['column'] != name:
            continue
        if row['kind'] in ('academic_role','funding'):
            from src.opportunities import enrich
            # Only explicitly verified publisher accounts can establish application status.
            binding = next((b for b in bindings if b.get('platform') == row['platform'] and b.get('article_prefix') and row['url'].startswith(b['article_prefix'])), None)
            row['verification'] = 'verified' if binding and row.get('evidence_kind') == 'article' else 'pending'
            if binding:
                row.update(source=binding['organization'], organization_group=binding.get('organization_group',''))
            identity = row['id']
            evidence = row.get('evidence_text', '')[:900]
            row = enrich(row)
            if row:
                row['id'] = identity
                row['evidence_text'] = evidence
        if row:
            result.append(row)
    return result
