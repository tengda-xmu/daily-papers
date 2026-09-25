"""Public note discovery and bounded share-link reading; no account session required."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from bs4 import BeautifulSoup
from src.models import parse_date
from src.public_sources import ROOT, clean, date, read, write

HOSTS = {'xiaohongshu.com', 'www.xiaohongshu.com', 'xhslink.com', 'www.xhslink.com'}
CHINA = timezone(timedelta(hours=8))
QUERIES = {
    'leads': ['航空 开放基金 课题申报', '青年编委 招募', '力学 科研项目 申请', '学术会议 征稿'],
    'ai': ['智能体 科研 实践', 'Agent Skills MCP 用法', '大模型 发布 开源', 'AI 编程 工具 工作流'],
}


class NoteReadUnavailable(ValueError):
    def __init__(self, url):
        super().__init__('暂时无法读取笔记正文，可手动补充。')
        self.resolved_url = public_url(url)


def share_url(value):
    match = re.search(r'https?://[^\s<>"\u201c\u201d]+', str(value))
    url = match[0].rstrip('，。；！!?）)]') if match else ''
    try:
        parts = urlsplit(url)
        if parts.hostname not in HOSTS or parts.username or parts.password or parts.port not in (None, 80, 443):
            raise ValueError()
        if parts.hostname.endswith('xhslink.com'):
            if not re.fullmatch(r'/(?:a/|o/)?[A-Za-z0-9_-]+/?', parts.path):
                raise ValueError()
        elif not note_id(url):
            raise ValueError()
    except ValueError:
        raise ValueError('请填写有效的小红书笔记分享链接。') from None
    return url


def note_id(url):
    match = re.fullmatch(r'/(?:explore|discovery/item)/([a-fA-F0-9]{24})/?', urlsplit(url).path)
    return match[1].lower() if match else ''


def public_url(url):
    identity = note_id(url)
    return 'https://www.xiaohongshu.com/explore/' + identity if identity else url.split('?')[0]


def fetch_note(url):
    share_url(url)
    resolved = [url]
    class Redirects(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, target):
            share_url(target)
            if note_id(target):
                resolved[0] = target
            return super().redirect_request(request, fp, code, msg, headers, target)
    try:
        with build_opener(Redirects).open(Request(url, headers={'User-Agent': 'DailyPapers/1.0'}), timeout=15) as response:
            share_url(response.url)
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ValueError('笔记页面过大，请手动补充内容。')
            return body, response.url
    except Exception:
        raise NoteReadUnavailable(resolved[0]) from None


def parse_note(body, url):
    soup = BeautifulSoup(body.decode('utf-8', 'replace'), 'html.parser')
    identity = note_id(url)
    result = {'url': public_url(url), 'article_id': identity, 'title': '', 'author': '', 'published_at': '', 'evidence_text': ''}
    def notes(value):
        if isinstance(value, dict):
            if (value.get('noteId') or value.get('id')) == identity and isinstance(value.get('desc'), str):
                yield value
            for v in value.values():
                yield from notes(v)
        elif isinstance(value, list):
            for v in value:
                yield from notes(v)
    for script in soup.find_all('script'):
        text = script.string or ''
        match = re.search(r'(?:window\.)?__INITIAL_STATE__\s*=\s*(\{.*\})\s*;?\s*$', text, re.S)
        if not match:
            continue
        try:
            payload = json.loads(re.sub(r':\s*undefined(?=\s*[,}])', ':null', match[1]))
        except ValueError:
            continue
        note = next(notes(payload), None)
        if note:
            result.update(title=clean(note.get('title')), author=clean((note.get('user') or {}).get('nickname')),
                          evidence_text=clean(note.get('desc'))[:18000])
            stamp = note.get('time')
            if isinstance(stamp, (int, float)) and 946684800000 <= stamp < 4102444800000:
                result['published_at'] = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
            break
    if not result['title']:
        title, description = soup.select_one('meta[property="og:title"]'), soup.select_one('meta[name="description"]')
        if title and description:
            result.update(title=clean(title.get('content')), evidence_text=clean(description.get('content'))[:900], evidence_kind='excerpt')
    if not identity or not result['title'] or re.search(r'登录|安全验证|页面不存在|无法访问|小红书.*你的生活指南', result['title']):
        raise ValueError('暂时无法读取笔记，可补充标题和正文。')
    result.setdefault('evidence_kind', 'article')
    return result


def read_note(value, fetcher=fetch_note):
    url = share_url(value)
    body, final = fetcher(url)
    try:
        return parse_note(body, final)
    except ValueError:
        raise NoteReadUnavailable(final) from None


def search(query, key):
    url = 'https://serpapi.com/search.json?' + urlencode({'engine': 'google', 'q': query, 'api_key': key, 'hl': 'zh-cn', 'num': 10})
    with urlopen(Request(url, headers={'User-Agent': 'DailyPapers/1.0'}), timeout=25) as response:
        payload = json.loads(response.read(2_000_000))
    from src.redaction import public_metadata
    payload = public_metadata(payload, (key,))
    if payload.get('search_metadata', {}).get('status') == 'Success' and "hasn't returned any results" in payload.get('error',''):
        payload.pop('error')
    if payload.get('error'):
        raise ValueError('搜索服务未返回有效结果。')
    return payload


def discover(root=ROOT, *, now=None, searcher=search, key=None):
    now = now or datetime.now(timezone.utc)
    key = os.getenv('SERPAPI_API_KEY', '') if key is None else key
    cache = root / 'data/cache/xiaohongshu'
    budget = read(cache / 'budget.json')
    day = now.astimezone(CHINA).date()
    if budget.get('day') != day.isoformat():
        budget = {'day': day.isoformat(), 'count': 0}
    entries, results = [], []
    for column, pool in QUERIES.items():
        query = 'site:xiaohongshu.com/explore/ ' + pool[day.toordinal() % len(pool)]
        path = cache / (hashlib.sha256(query.encode()).hexdigest() + '.json')
        saved = read(path)
        checked = parse_date(saved.get('checked_at'))
        status = {'id': 'xiaohongshu-' + column, 'name': '小红书 · ' + ('科研线索' if column == 'leads' else 'AI 前沿'),
                  'url': 'https://www.xiaohongshu.com/', 'checked_at': now.isoformat(), 'last_success': saved.get('checked_at', '')}
        payload = None
        if checked and timedelta(0) <= now - checked < timedelta(hours=24):
            payload = saved['payload']
            status['cached'] = True
        elif not key:
            status['status'] = 'configuration_missing'
        elif budget['count'] >= 2:
            status['status'] = 'quota_exhausted'
            status['message'] = '今日两个发现查询已执行，下一日轮换主题；可继续手动添加笔记。'
        else:
            budget['count'] += 1
            write(cache / 'budget.json', budget)
            try:
                payload = searcher(query, key)
                write(path, {'checked_at': now.isoformat(), 'payload': payload})
                status['last_success'] = now.isoformat()
            except Exception as exc:
                status['status'] = 'quota_exhausted' if getattr(exc, 'code', None) == 429 else 'error'
                status['message'] = ('搜索服务返回 HTTP ' + str(exc.code)) if getattr(exc, 'code', None) else '搜索服务未返回有效数据，请稍后重试。'
        count = 0
        for item in (payload or {}).get('organic_results', []):
            try:
                url = share_url(item.get('link', ''))
            except ValueError:
                continue
            if not note_id(url) or not item.get('title'):
                continue
            entries.append({'id': 'xhs-' + note_id(url), 'article_id': note_id(url), 'platform': 'xiaohongshu',
                'provider': 'xiaohongshu', 'source': '小红书', 'title': clean(item['title']), 'url': public_url(url),
                'published_at': date(item.get('date')), 'summary': clean(item.get('snippet'))[:220],
                'evidence_kind': 'search_snippet', 'verification': 'pending', 'checked_at': now.isoformat()})
            count += 1
        if payload is not None:
            status['status'] = 'ok' if count else 'no_data'
        status['count'] = count
        results.append(status)
    return entries, results
