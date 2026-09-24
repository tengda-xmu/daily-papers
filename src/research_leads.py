"""Public research leads: curated events, official feeds and recent WeChat metadata.

Kept separate from peer-reviewed paper selection. No private connector state is read.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from src.models import parse_date
from src.research_directions import active_directions, load_profile
from src.wechat_metadata import excerpt, public_records

ROOT = Path(__file__).resolve().parents[1]
CHINA = timezone(timedelta(hours=8))
KINDS = {"conference": "学术会议", "call": "征稿与专题", "news": "研究动态", "resource": "论文库与数据"}


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def web_url(value, hosts=None):
    try:
        url = urlsplit(str(value))
        return url.scheme in ("https", "http") and bool(url.hostname) and not url.username and not url.password and (hosts is None or url.hostname in hosts)
    except ValueError:
        return False


def classify(title):
    text = title.casefold()
    if any(word in text for word in ("征稿", "征集", "专刊", "专题", "call for", "submission", "deadline", "workshop")):
        return "call"
    if any(word in text for word in ("会议", "大会", "研讨会", "论坛", "conference", "symposium")):
        return "conference"
    if any(word in text for word in ("数据集", "开源", "工具", "dataset", "toolkit", "tool ")):
        return "resource"
    return "news"


def snippet(value):
    text = excerpt(value)
    if len(text) <= 220:
        return text
    # Avoid cutting an English word in half in announcement previews.
    return text[:220].rsplit(' ', 1)[0].rstrip(' ,;:') + '…'


def state(row, now):
    """Never infer a call is open merely because a conference is upcoming."""
    today = now.astimezone(CHINA).date().isoformat()
    if row.get("end"):
        if row["end"] < today:
            return "ended", "已结束 · 可回看"
        return ("upcoming", "即将召开") if row.get("start", "") > today else ("ongoing", "正在举行")
    if row.get("deadline"):
        deadline = parse_date(row.get("deadline_at"))
        if (deadline and now > deadline) or (not deadline and row["deadline"] < today):
            return "ended", "已截止"
        if row.get("opens", "") > today:
            return "planned", "尚未开放投稿"
        return "deadline", "截止时间已公布"
    if row.get("kind") == "conference":
        return "unknown", "会期见原文"
    if row.get("kind") == "call":
        return "unknown", row.get("resource_type", "投稿状态见原文")
    return "reference", row.get("resource_type", "研究动态")


def wechat_leads(records):
    rows = []
    for row in public_records(records):
        rows.append({"id": "wechat-" + hashlib.sha256((row['account'] + row['title']).encode()).hexdigest()[:20],
                     "kind": classify(row['title']), "title": row['title'], "summary": row['summary'],
                     "source": row['account'], "provider": "wechat", "url": row['landing_url'],
                     "published_at": row['published_at'], "indexed": row.get('access_mode') == 'public_index'})
    return rows


def parse_feed(body, feed, now, days=90):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("Unsupported XML declarations")
    root = ET.fromstring(body)
    if root.tag.rsplit('}', 1)[-1] not in ('rss', 'feed'):
        raise ValueError('Expected RSS or Atom')
    rows = []
    for node in root.findall("./channel/item") + root.findall("{*}entry"):
        title = excerpt(node.findtext("title") or node.findtext("{*}title"))
        date_text = node.findtext("pubDate") or node.findtext("{*}published") or node.findtext("{*}updated")
        published = parse_date(date_text)
        if not published and date_text:
            try:
                published = parsedate_to_datetime(date_text)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
        link = node.findtext("link")
        if not link:
            link = next((n.get('href') for n in node.findall('{*}link') if n.get('rel', 'alternate') == 'alternate'), '')
        if not title or not published or not now - timedelta(days=days) <= published <= now or not web_url(link, feed['hosts']):
            continue
        rows.append({"id": "official-" + hashlib.sha256(link.encode()).hexdigest()[:20],
                     "kind": classify(title), "title": title, "source": feed['name'], "provider": "official",
                     "url": link, "published_at": published.isoformat(), "topics": feed.get('topics', []),
                     # Publish only a short excerpt, never a feed's full article.
                     "summary": snippet(node.findtext('description') or node.findtext('{*}summary'))})
    return rows


def fetch_feed(feed):
    request = Request(feed['url'], headers={'User-Agent': 'DailyPapers/1.0 (+https://tengda-xmu.github.io/daily-papers/)'})
    with urlopen(request, timeout=15) as response:
        if not web_url(response.url, feed['hosts']):
            raise ValueError('Unexpected feed redirect')
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('Feed too large')
        return body


def refresh(root=ROOT, *, now=None, fetch=fetch_feed):
    """A failed or empty feed never deletes still-recent earlier announcements."""
    now = now or datetime.now(timezone.utc)
    config = read_json(root / 'config/research-leads.json', {})
    path = root / 'data/research-leads.json'
    previous = read_json(path, {})
    days = config.get('lookback_days', 90)
    previous_status = {s['id']: s for s in previous.get('sources', [])}
    allowed_hosts = {h for feed in config.get('feeds', []) for h in feed['hosts']}
    rows = {r['id']: r for r in previous.get('entries', [])
            if parse_date(r.get('published_at')) and now - timedelta(days=days) <= parse_date(r['published_at']) <= now
            and web_url(r.get('url'), allowed_hosts)}
    statuses = []
    for feed in config.get('feeds', []):
        status = {'id': feed['id'], 'name': feed['name'], 'url': feed['url'], 'checked_at': now.isoformat(),
                  'last_success': previous_status.get(feed['id'], {}).get('last_success', '')}
        try:
            fresh = parse_feed(fetch(feed), feed, now, days)
            rows.update({r['id']: r for r in fresh})
            status.update(status='ok' if fresh else 'no_data', count=len(fresh), last_success=now.isoformat())
        except Exception as exc:
            # Do not serialize exception URLs, request headers or credentials.
            status.update(status='error', count=0, error=type(exc).__name__)
        statuses.append(status)
    payload = {'version': 1, 'checked_at': now.isoformat(), 'entries': sorted(rows.values(), key=lambda r: r['published_at'], reverse=True), 'sources': statuses}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)
    return payload


def collect(payload, root=ROOT, *, now=None):
    now = now or datetime.now(timezone.utc)
    config = read_json(root / 'config/research-leads.json', {})
    days = config.get('lookback_days', 90)
    history = []
    # Read published archives only; private subscriptions and caches stay private.
    for path in sorted((root / 'data/archive').glob('*.json')):
        day = parse_date(path.stem)
        if day and day >= now - timedelta(days=days + 1):
            history.extend(read_json(path, {}).get('wechat_articles', []))
    history.extend(payload.get('wechat_articles', []))
    feed_data = read_json(root / 'data/research-leads.json', {})
    entries = [{**r, 'provider': 'official'} for r in config.get('entries', [])]
    entries += feed_data.get('entries', []) + wechat_leads(history)
    directions = active_directions(load_profile(root))
    result = {}
    for row in entries:
        if row.get('kind') not in KINDS or not web_url(row.get('url')):
            continue
        published = parse_date(row.get('published_at'))
        if row.get('published_at') and (not published or not now - timedelta(days=days) <= published <= now):
            continue
        text = (row.get('title', '') + ' ' + row.get('summary', '')).casefold()
        topics = [d['id'] for d in directions if not any(t.casefold() in text for t in d.get('exclude', []))
                  and (d['id'] in row.get('topics', []) or any(t.casefold() in text for t in d['keywords']))]
        status, status_label = state(row, now)
        result[row['id']] = {**row, 'topics': topics, 'state': status, 'state_label': status_label}
    # Upcoming events first, then recent announcements, resources and past events.
    def order(row):
        if row['state'] == 'ended':
            return 3, row.get('end', row.get('deadline', ''))
        if row.get('start'):
            return 0, row['start']
        if row.get('published_at'):
            return 1, -parse_date(row['published_at']).timestamp()
        return 2, row.get('title', '')
    return {'entries': sorted(result.values(), key=order), 'directions': directions, 'sources': feed_data.get('sources', []),
            'days': days, 'checked_at': feed_data.get('checked_at', ''), 'built_at': now.isoformat()}


if __name__ == '__main__':
    data = refresh()
    for source in data['sources']:
        print(f"{source['name']}: {source['status']} ({source['count']})")
    print(f"Official research leads retained: {len(data['entries'])}")
