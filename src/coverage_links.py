"""Private, durable recovery of public-index links; never identity evidence.

Only normal public redirects are followed. Captchas pause the redirect channel
without marking the independent search index unavailable. Expired links request
a fresh title search instead of leaving an index-only candidate stranded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit

from src.models import parse_date
from src.public_sources import clean, read, write
from src.sources.wechat_public_index import PublicSearchChallenge
from src.wechat_metadata import article_url, public_index_url


VERSION = 1
PRIVATE = 'data/cache/paper-coverage/links'
HEALTH = 'data/cache/paper-coverage/providers/sogou_redirect.json'
SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')


def _now(value):
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _redirect(value):
    """Do not admit credentials, arbitrary paths, hosts or control characters."""
    try:
        value = str(value or '')
        if not value or len(value) > 16000 or any(ord(char) < 32 for char in value):
            return ''
        value = urljoin('https://weixin.sogou.com/', value)
        parsed = urlsplit(value)
        if (parsed.scheme not in ('https', 'http')
                or parsed.hostname not in ('weixin.sogou.com', 'www.sogou.com')
                or parsed.path != '/link' or not parsed.query or parsed.fragment
                or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
            return ''
        return value
    except ValueError:
        return ''


def _identifier(paper):
    value = str(paper.get('id') or '') if isinstance(paper, dict) else str(paper or '')
    if not SAFE_ID.fullmatch(value):
        raise ValueError('A stable paper ID is required for link recovery')
    return value


def remember(root, paper, candidate, now=None):
    """Persist an unresolved Sogou href privately; return its task or None.

    Repeated discovery of the same href preserves completed work and genuine
    retry intervals. A changed href or source version creates fresh work, still
    subject to the redirect service's current retry hint during ``resume``.
    """
    identifier = _identifier(paper)
    redirect = _redirect(candidate.get('_redirect_url'))
    title, account = clean(candidate.get('title'))[:300], clean(candidate.get('account'))[:100]
    if not redirect or not title:
        return None
    now = _now(now)
    index_url = public_index_url(account, title)
    key = hashlib.sha256(f'{identifier}\n{index_url}'.encode()).hexdigest()
    path = Path(root) / PRIVATE / identifier / (key + '.json')
    old = read(path)
    source_version = str(candidate.get('source_version') or candidate.get('_source_version') or '')[:128]
    unchanged = (old.get('version') == VERSION and old.get('redirect_url') == redirect
                 and old.get('source_version', '') == source_version)
    task = dict(old) if unchanged else {}
    published = parse_date(candidate.get('published_at'))
    task.update(version=VERSION, id=key, paper_id=identifier, index_url=index_url,
                redirect_url=redirect, title=title, account=account,
                published_at=published.isoformat() if published else old.get('published_at', ''),
                source_version=source_version)
    if not unchanged:
        task.update(state='pending', discovered_at=now.isoformat(), last_attempt_at='')
    write(path, task)
    return task


def _resolve(url):
    from src.coverage_discovery import _resolve as resolve, request_page
    return resolve(url, 'https://weixin.sogou.com/', request_page)


def _retry_after(error, now):
    headers = getattr(error, 'headers', None) or {}
    value = next((str(value) for key, value in headers.items() if key.lower() == 'retry-after'), '')
    try:
        result = now + timedelta(seconds=int(value)) if value.isdigit() else parsedate_to_datetime(value)
        result = _now(result)
        if result > now:
            return result
    except (TypeError, ValueError, OverflowError):
        pass
    return now + timedelta(days=1)


def _query(task):
    terms = [clean(task.get(key)).replace('"', '') for key in ('account', 'title')]
    return 'site:mp.weixin.qq.com ' + ' '.join('"' + term + '"' for term in terms if term)


def _candidate(task, url):
    # Rebuild the boundary: never return raw redirect parameters or task state.
    return {'url': url, 'title': task['title'], 'account': task.get('account', ''),
            'published_at': task.get('published_at', ''), 'index_url': task['index_url'],
            'reason': 'identity_unconfirmed'}


def mark_refreshed(root, paper, queries, now=None):
    """Record title searches the caller actually handled, never merely queued.

    An unchanged expired href should not force another title lookup on every
    collection. A newly discovered href still resets this task in ``remember``.
    Returns the number of private tasks updated.
    """
    identifier, now = _identifier(paper), _now(now)
    wanted = {queries} if isinstance(queries, str) else set(queries or [])
    changed = 0
    for path in (Path(root) / PRIVATE / identifier).glob('*.json'):
        task = read(path)
        if (task.get('version') != VERSION or task.get('paper_id') != identifier
                or task.get('state') != 'needs_refresh' or _query(task) not in wanted):
            continue
        task.update(refresh_checked_at=now.isoformat(),
                    next_refresh_at=(now + timedelta(days=1)).isoformat())
        write(path, task)
        changed += 1
    return changed


def resume(root, paper, now=None, *, resolver=None, before_request=None):
    """Return resolved candidates, title searches to refresh and actual I/O time.

    ``resolver`` accepts one validated Sogou URL and returns a public article URL
    or an empty string. Neither a recovered link nor its title confirms a paper.
    There is no daily/per-paper request counter. Only service and failure retry
    times defer work; resolved tasks are reused without further requests. An
    optional ``before_request()`` callback can suspend remaining I/O when the
    caller's execution deadline is reached, without changing cached tasks.
    """
    identifier, now = _identifier(paper), _now(now)
    resolver = resolver or _resolve
    health_path = Path(root) / HEALTH
    health = read(health_path)
    service_retry = parse_date(health.get('retry_at')) if health.get('scope') == 'redirect' else None
    paused = bool(service_retry and now < service_retry and health.get('state') in ('blocked', 'error'))
    result = {'candidates': [], 'refresh_queries': [], 'attempted_at': ''}
    seen_candidates, seen_queries = set(), set()

    def refresh(task):
        due = parse_date(task.get('next_refresh_at'))
        if due and now < due:
            return
        query = _query(task)
        if query not in seen_queries:
            seen_queries.add(query)
            result['refresh_queries'].append(query)

    for path in sorted((Path(root) / PRIVATE / identifier).glob('*.json')):
        task = read(path)
        if (task.get('version') != VERSION or task.get('paper_id') != identifier
                or not clean(task.get('title'))
                or task.get('index_url') != public_index_url(task.get('account', ''), task.get('title', ''))):
            continue
        resolved = article_url(task.get('resolved_url')) if task.get('state') == 'resolved' else ''
        if resolved:
            if resolved not in seen_candidates:
                seen_candidates.add(resolved)
                result['candidates'].append(_candidate(task, resolved))
            continue
        if task.get('state') == 'needs_refresh' or not _redirect(task.get('redirect_url')):
            refresh(task)
            continue
        if paused:
            continue
        retry_at = parse_date(task.get('retry_at'))
        if retry_at and now < retry_at:
            continue
        if before_request is not None and not before_request():
            paused = True
            continue
        task['last_attempt_at'] = result['attempted_at'] = now.isoformat()
        try:
            resolved = article_url(resolver(task['redirect_url']))
            if resolved:
                task.update(state='resolved', resolved_url=resolved)
                task.pop('retry_at', None)
                if resolved not in seen_candidates:
                    seen_candidates.add(resolved)
                    result['candidates'].append(_candidate(task, resolved))
                # A real successful response is evidence this channel recovered.
                write(health_path, {'scope': 'redirect', 'state': 'ok', 'checked_at': now.isoformat()})
            else:
                task.update(state='needs_refresh')
                task.pop('retry_at', None)
                refresh(task)
        except (PublicSearchChallenge, HTTPError) as exc:
            if isinstance(exc, PublicSearchChallenge) or getattr(exc, 'code', None) in (403, 429):
                retry_at = _retry_after(exc, now).isoformat()
                task.update(state='blocked', retry_at=retry_at)
                write(health_path, {'scope': 'redirect', 'state': 'blocked', 'reason': 'verification_required',
                                    'checked_at': now.isoformat(), 'retry_at': retry_at})
                paused = True  # Retain resolved tasks below, but do no more I/O.
            elif getattr(exc, 'code', None) in (404, 410):
                task.update(state='needs_refresh')
                task.pop('retry_at', None)
                refresh(task)
            else:
                task.update(state='error', retry_at=_retry_after(exc, now).isoformat())
        except Exception:
            task.update(state='error', retry_at=(now + timedelta(days=1)).isoformat())
        write(path, task)
    return result
