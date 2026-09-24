"""Authenticated manual search; isolated workers have bounded execution time."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import time
import uuid
from urllib.parse import urlsplit, unquote, urlencode
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from src.citations import reference, safe_link
from src.manual_search import SOURCES
from src.custom_journals import normalize_issn
from src.models import RawRecord
from src.search_ranking import SORT_OPTIONS, rank_results, sort_note
from src.pipeline import deduplicate
from .documents import fetch_pdf, pdf_candidates

SOURCE_IDS = {s['id'] for s in SOURCES}
STATE_MESSAGES = {
    'ok': '检索完成', 'no_data': '没有匹配结果', 'partial': '部分结果可用',
    'configuration_missing': '配置缺失，请在本机配置来源密钥或同步数据',
    'authorization_required': '需要 API 授权', 'access_denied': '来源拒绝访问，请检查授权',
    'quota_exhausted': '请求限频或额度已用完', 'error': '来源请求失败，可稍后重试',
    'timeout': '来源响应超时，已停止该来源', 'cancelled': '已停止', 'running': '正在检索', 'queued': '等待检索',
}


class SearchRequest(BaseModel):
    query: str = Field(default='', max_length=200)
    sources: list[str] = Field(min_length=1, max_length=11)
    since: date
    until: date
    limit: int = Field(default=10, ge=5, le=25)
    pagination: bool = False  # Older clients retain their bounded-request contract.
    _continuation: dict = PrivateAttr(default_factory=dict)
    journal_issn: str = Field(default='', max_length=16)
    sort_by: Literal['relevance', 'latest', 'citations'] = 'relevance'

    @field_validator('journal_issn')
    @classmethod
    def valid_issn(cls, value):
        return normalize_issn(value) if value else ''

    @field_validator('query')
    @classmethod
    def clean_query(cls, value):
        value = ' '.join(value.split())
        if value and (len(value) < 2 or any(ord(c) < 32 for c in value)):
            raise ValueError('请输入至少两个字符的关键词')
        # This is keyword search; reserved engine syntax has no common semantics.
        if value and not any(c.isalnum() for c in value):
            raise ValueError('请输入有效关键词')
        return value

    @field_validator('sources')
    @classmethod
    def valid_sources(cls, values):
        if not set(values) <= SOURCE_IDS:
            raise ValueError('不支持该来源')
        return list(dict.fromkeys(values))

    @model_validator(mode='after')
    def date_range(self):
        if not self.query and not self.journal_issn:
            raise ValueError('请输入关键词或选择期刊')
        if self.journal_issn:
            self.sources = ['Crossref']
        if self.since > self.until or self.since.year < 1900 or self.until > date.today():
            raise ValueError('日期范围应在 1900 年至今天之间，起始日期不晚于结束日期')
        return self


class MoreRequest(BaseModel):
    round: int = Field(ge=1)
    source: str = ''
    retry: bool = False


PAGE_OK = {'ok', 'no_data'}


class SearchService:
    def __init__(self, root, runtime, worker=None):
        self.root = Path(root)
        self.directory = Path(runtime) / 'manual-search'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.worker = worker or self._worker
        self.slots = asyncio.Semaphore(4)
        self.pdf_lock = asyncio.Semaphore(1)
        self.pdf_tasks = set()

    async def _worker(self, source, query):
        payload = query.model_dump(mode='json') | {'source': source, 'cache_dir': str(self.directory / 'provider-cache')}
        if query.pagination:
            payload['continuation'] = query._continuation
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
        proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'src.manual_search',
            cwd=self.root, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
        try:
            output, _ = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), timeout=50)
            if proc.returncode:
                raise RuntimeError('Search worker failed')
            return json.loads(output)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    def start(self, query):
        # Reuse active searches (e.g. a restored tab) without billing again.
        active = next((j for j in self.jobs.values() if j['state'] == 'running'), None)
        if active:
            if active['request'] == query:
                return active['id']
            raise HTTPException(409, '另一个检索仍在运行，请先停止或等待完成。')
        for key, job in list(self.jobs.items()):
            if time.time() - job['created'] > 86400 or len(self.jobs) > 20:
                self.jobs.pop(key)
        identifier = uuid.uuid4().hex
        job = {'id': identifier, 'created': time.time(), 'state': 'running', 'request': query, 'round': 1,
               'sources': {s: {'state': 'queued', 'records': [], 'cached': False, 'next': {}, 'pages': 0} for s in query.sources}}
        self.jobs[identifier] = job
        job['task'] = asyncio.create_task(self._run(job))
        return identifier

    async def _one(self, job, source):
        async with self.slots:
            result = job['sources'][source]
            result['state'] = 'running'
            query = job['request']
            if query.pagination:
                await self._page(job, source)
                return
            cache_key = query.model_dump(mode='json') | {'sources': [source]}
            if query.sort_by == 'relevance':
                cache_key.pop('sort_by')  # Preserve valid earlier relevance caches.
            if source in ('Semantic Scholar', 'PubMed'):
                cache_key['native_sort_version'] = 1
            if source == '微信公众号':
                # Discard old empty results produced by the daily account
                # allowlist without invalidating paid searches of other sources.
                cache_key['wechat_scope'] = 'all-accounts-v2'
            elif source == 'Elsevier':
                cache_key['scopus_search'] = 'relevance-paging-v2'
            key = hashlib.sha256(json.dumps(cache_key, sort_keys=True).encode()).hexdigest()
            path = self.directory / (key + '.json')
            try:
                cached = json.loads(path.read_text(encoding='utf-8'))
                ttl = 86400 if cached['state'] in ('ok', 'no_data') else 0
                if time.time() - path.stat().st_mtime < ttl:
                    result.update(cached, cached=True)
                    return
            except (ValueError, OSError, KeyError):
                pass
            try:
                data = await self.worker(source, query)
                result.update(data)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            except asyncio.CancelledError:
                result['state'] = 'cancelled'
                raise
            except (TimeoutError, asyncio.TimeoutError):
                result['state'] = 'timeout'
            except Exception:
                result['state'] = 'error'

    async def _page(self, job, source):
        result = job['sources'][source]
        query = job['request'].model_copy(deep=True)
        query._continuation = copy.deepcopy(result['next'])
        cache_key = query.model_dump(mode='json') | {'sources': [source], 'continuation': query._continuation, 'page_version': 1}
        key = hashlib.sha256(json.dumps(cache_key, sort_keys=True).encode()).hexdigest()
        path = self.directory / (key + '.json')
        data, cached = None, False
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            # Crossref continuation cursors expire; never reuse a stale first page.
            ttl = 240 if source in ('Crossref', 'CNS 子刊专项') else 86400
            if value['state'] in PAGE_OK and time.time() - path.stat().st_mtime < ttl:
                data, cached = value, True
        except (ValueError, OSError, KeyError):
            pass
        try:
            if data is None:
                data = await self.worker(source, query)
                if data['state'] in PAGE_OK:
                    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            previous = {r.get('doi') or r.get('source_id') or r['title']: r for r in result['records']}
            for row in data.get('records', []):
                previous[row.get('doi') or row.get('source_id') or row['title']] = row
            result.update({k: v for k, v in data.items() if k not in ('records', 'next')})
            result.update(records=list(previous.values()), cached=cached)
            # Failed pages retain their cursor and all previously received records.
            if data['state'] in PAGE_OK:
                result['next'] = data.get('next')
                result['pages'] += 1
        except asyncio.CancelledError:
            result['state'] = 'cancelled'
            raise
        except (TimeoutError, asyncio.TimeoutError):
            result['state'] = 'timeout'
        except Exception:
            result['state'] = 'error'

    def more(self, identifier, data):
        job = self.get(identifier)
        if not job['request'].pagination:
            raise HTTPException(409, '请重新检索以启用分页。')
        if data.source and data.source not in job['sources']:
            raise HTTPException(422, '该来源不在本次检索中。')
        if data.round < job['round']:
            return identifier  # Retrying the same click never consumes another page.
        if data.round != job['round']:
            raise HTTPException(409, '检索状态已变化，请刷新结果。')
        if any(j['state'] == 'running' for j in self.jobs.values()):
            raise HTTPException(409, '检索仍在运行，请等待完成或先停止。')
        sources = [s for s, value in job['sources'].items() if (not data.source or data.source == s)
                   and value['next'] is not None and (value['state'] in PAGE_OK or data.retry)]
        if sources:
            job['round'] += 1; job['state'] = 'running'
            for source in sources:
                job['sources'][source]['state'] = 'queued'
            job['task'] = asyncio.create_task(self._run(job, sources))
        return identifier

    async def _run(self, job, sources=None):
        tasks = [asyncio.create_task(self._one(job, s)) for s in (sources or job['sources'])]
        try:
            await asyncio.gather(*tasks)
            job['state'] = 'completed'
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for value in job['sources'].values():
                if value['state'] in ('queued', 'running'):
                    value['state'] = 'cancelled'
            job['state'] = 'cancelled'

    def get(self, identifier):
        if identifier not in self.jobs:
            raise HTTPException(404, '检索记录已过期或连接程序已重启，请重新检索（有效缓存会复用）。')
        return self.jobs[identifier]

    def snapshot(self, identifier):
        job = self.get(identifier)
        rows = []
        for source, value in job['sources'].items():
            for rank, record in enumerate(value['records'], 1):
                row = RawRecord(**copy.deepcopy(record))
                # Per-source keys survive metadata deduplication without summing counts.
                row.raw_metadata['search_observation:' + source] = {
                    'source': source, 'rank': rank, 'citation_count': row.citation_count}
                rows.append(row)
        for row in rows:
            # Scholar often omits the DOI but links to the very same publisher
            # article. Recover identifiers only from known publisher URL forms.
            if not row.doi:
                try:
                    url = urlsplit(row.landing_url)
                    if url.hostname in ('nature.com', 'www.nature.com') and re.fullmatch(r'/articles/[A-Za-z0-9.-]+', url.path):
                        row.doi = '10.1038/' + url.path.split('/')[-1].removesuffix('.pdf').lower()
                    elif url.hostname == 'doi.org' and re.fullmatch(r'/10\.\d{4,9}/.+', url.path):
                        row.doi = unquote(url.path[1:]).lower()
                except ValueError:
                    pass
        records = []
        result_map = {}
        # deduplicate() mutates the chosen record. Freeze structured donors
        # before that merge, so inherited metadata cannot become a new donor.
        bibliographic_rows = copy.deepcopy([r for r in rows if r.raw_metadata.get('bibliography', {}).get('authors')])
        bibliographic_rows.sort(key=lambda r: r.source not in ('Crossref', 'CNS 子刊专项'))
        for row in deduplicate(rows):
            paper = asdict(row)
            key = hashlib.sha256((row.doi or row.landing_url or row.title).encode()).hexdigest()[:20]
            paper['id'] = key
            paper['pdf_url'] = safe_link(row.raw_metadata.get('pdf_link') or row.oa_url)
            paper['landing_url'] = safe_link(row.landing_url) or (f'https://doi.org/{row.doi}' if row.doi else '')
            # The best record can have fewer bibliographic fields. Prefer
            # structured metadata from the matching Crossref record if present.
            for candidate in bibliographic_rows:
                if row.doi and candidate.doi == row.doi:
                    paper['raw_metadata']['bibliography'] = candidate.raw_metadata['bibliography']
                    # Keep year, venue and authors from the same bibliographic
                    # record. Indexes may date a journal DOI to its preprint.
                    for field in ('authors', 'venue', 'published_at', 'title'):
                        if getattr(candidate, field):
                            paper[field] = getattr(candidate, field)
                    paper['landing_url'] = safe_link(candidate.landing_url) or f'https://doi.org/{row.doi}'
                    if candidate.raw_metadata.get('pdf_link'):
                        paper['pdf_url'] = safe_link(candidate.raw_metadata['pdf_link'])
                    break
            result_map[key] = paper
            records.append({k: paper.get(k) for k in ('id', 'title', 'authors', 'venue', 'published_at', 'doi',
                'landing_url', 'pdf_url', 'abstract', 'citation_count')} | {
                'sources': row.raw_metadata.get('sources', [row.source]),
                'link_kind': row.raw_metadata.get('link_kind', 'article'),
                'can_download': bool(pdf_candidates(paper)), 'citation': reference(paper),
                '_search_observations': [v for k, v in row.raw_metadata.items() if k.startswith('search_observation:')],
            })
        records = rank_results(records, job['request'].query, job['request'].sort_by)
        if job['request'].pagination:
            # Keep already viewed pages stable as more sources/pages arrive.
            order = list(job.get('display_order', []))
            known = set(order)
            order.extend(row['id'] for row in records if row['id'] not in known)
            positions = {key: n for n, key in enumerate(order)}
            records.sort(key=lambda row: positions[row['id']])
            if job['state'] != 'running':
                job['display_order'] = order
        job['results'] = result_map
        return {'id': identifier, 'state': job['state'], 'query': job['request'].query,
            'round': job['round'], 'pagination': job['request'].pagination,
            'request': job['request'].model_dump(mode='json'),
            'sources': [{'id': source, 'state': value['state'], 'label': STATE_MESSAGES.get(value['state'], '来源异常'),
                         'count': len(value['records']), 'cached': value['cached'],
                         'detail': value.get('message', ''),
                         'has_more': job['request'].pagination and value['next'] is not None and value['state'] in PAGE_OK,
                         'can_retry': job['request'].pagination and value['next'] is not None and value['state'] not in PAGE_OK | {'queued', 'running'},
                         'pages': value['pages'],
                         'sort_note': value.get('sort_note') or sort_note(source, job['request'].sort_by),
                         'search_url': ('https://weixin.sogou.com/weixin?' + urlencode({'type': 2, 'query': job['request'].query})) if source == '微信公众号' else '',
                         'mode': next(s['mode'] for s in SOURCES if s['id'] == source)} for source, value in job['sources'].items()],
            'records': records, 'created_at': datetime.fromtimestamp(job['created'], timezone.utc).isoformat()}

    async def stop(self, identifier):
        job = self.get(identifier)
        job['task'].cancel()
        await asyncio.gather(job['task'], return_exceptions=True)
        return self.snapshot(identifier)

    async def pdf(self, identifier, result_id):
        job = self.get(identifier)
        paper = job.get('results', {}).get(result_id)
        if not paper:
            raise HTTPException(404, '请先检索并选择结果中的论文。')
        async with self.pdf_lock:
            folder = self.directory / 'pdfs' / result_id
            folder.mkdir(parents=True, exist_ok=True)
            meta = folder / 'document.json'
            if meta.exists():
                info = json.loads(meta.read_text(encoding='utf-8'))
            else:
                # Keep the lock until the download thread finishes, even if
                # the browser disconnects; a retry cannot start a duplicate.
                task = asyncio.create_task(asyncio.to_thread(fetch_pdf, paper, folder))
                self.pdf_tasks.add(task)
                try:
                    info = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
                except Exception:
                    raise HTTPException(409, '未取得公开 PDF。请打开原文或全文入口，按出版社提供的访问方式下载。')
                finally:
                    self.pdf_tasks.discard(task)
                meta.write_text(json.dumps({'file': info['file']}), encoding='utf-8')
            file = (folder / info['file']).resolve()
            if file.parent != folder.resolve() or file.suffix != '.pdf' or not file.is_file():
                raise HTTPException(404, 'PDF 缓存不可用，请重新检索。')
            return file

    async def close(self):
        for job in self.jobs.values():
            if job['state'] == 'running':
                await self.stop(job['id'])
        if self.pdf_tasks:
            await asyncio.gather(*self.pdf_tasks, return_exceptions=True)
