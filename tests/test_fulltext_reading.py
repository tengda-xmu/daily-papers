import asyncio
from copy import deepcopy
import hashlib
import json
import sqlite3
import time

import pytest

from connectors.codex_bridge.reading_queue import ReadingQueue
from connectors.codex_bridge.reading_materials import MaterialResolver
from src.auto_reading import fingerprint, public_analysis
from src.editions import read
from src.paper_sources import clean_abstract, inspect_html
from tests.test_reading_queue import sample, response, task, FakeClient


def material(p, full=True):
    text = p['abstract']
    if full:
        page_text = (text + ' ') * (40000 // (len(text) + 1) + 1)
        pages = [{'label': 'P1', 'text': page_text, 'scan': False},
                 {'label': 'P2', 'text': page_text, 'scan': False}]
        return {'basis': 'full_text', 'version': 'a' * 64, 'source_url': p['landing_url'],
                'text': '\n'.join(x['text'] for x in pages),
                'document': {'kind': 'pdf', 'pages': pages, 'page_count': 2, 'scan_pages': 0, 'identity_checked': True}}
    return {'basis': 'abstract', 'version': 'b' * 64, 'source_url': p['landing_url'], 'text': text}


class FullClient(FakeClient):
    def __init__(self, p, readable=True):
        super().__init__(response(p))
        self.calls = []
        self.readable = readable
        self.value['references'] = {'findings': ['P1'], 'method': ['P2']}

    async def turn(self, thread, prompt, images=()):
        self.calls.append((prompt, images))
        value = {'readable': self.readable, 'paper_matches': True,
                 'notes': ('研究方法和主要证据均来自给定原文，结果有明确的实验条件。' * 5) + '[P1] [P2]'} if 'paper_matches' in prompt else self.value
        yield {'type': 'delta', 'text': json.dumps(value, ensure_ascii=False)}
        yield {'type': 'completed', 'status': 'completed'}


def test_fulltext_covers_every_batch_and_keeps_snapshot_identity(tmp_path):
    p = sample(); client = FullClient(p); m = material(p)
    q = ReadingQueue(tmp_path, tmp_path / 'run', client, asyncio.Lock(), resolver=lambda p, **kw: m)
    q.quiet_until = 0; q.enqueue(p)
    asyncio.run(q.process(task(q)))
    row = task(q)
    assert row['state'] == 'ready'
    value = json.loads(row['result'])
    assert value['fingerprint'] == fingerprint(p)
    assert value['analysis']['analysis_basis'] == 'full_text'
    assert value['material']['covered'] == value['material']['total'] == 2
    assert len(json.loads(row['checkpoint'])['notes']) == 2
    assert len(client.calls) == 3
    assert 'document' not in value['material'] and 'text' not in value['material']
    assert json.loads(row['paper'])['abstract'] == p['abstract']
    bad = deepcopy(value); bad['material']['covered'] = 1
    with pytest.raises(ValueError):
        public_analysis(bad)


def test_unreadable_pages_never_complete_and_retries_reuse_completed_batches(tmp_path):
    p = sample(); client = FullClient(p, readable=False); m = material(p)
    q = ReadingQueue(tmp_path, tmp_path / 'run', client, asyncio.Lock(), resolver=lambda p, **kw: m)
    q.quiet_until = 0; q.enqueue(p)
    asyncio.run(q.process(task(q)))
    assert task(q)['state'] == 'retry' and task(q)['result'] is None
    assert task(q)['attempts'] == 1
    client.readable = True
    checkpoint = {'version': m['version'], 'notes': ['已完成第一批 [P1] 的阅读要点。' * 10]}
    with q.db() as db:
        db.execute('UPDATE tasks SET checkpoint=?', (json.dumps(checkpoint),))
    client.calls.clear()
    asyncio.run(q.process(task(q)))
    assert task(q)['state'] == 'ready'
    assert len(client.calls) == 2  # Only the remaining batch, then final synthesis.


def test_abstract_publication_does_not_stop_fulltext_upgrade(tmp_path):
    p = sample(); selected = [material(p, False)]
    client = FullClient(p); client.value.pop('references')
    q = ReadingQueue(tmp_path, tmp_path / 'run', client, asyncio.Lock(), resolver=lambda p, **kw: selected[0])
    q.quiet_until = 0; q.enqueue(p)
    asyncio.run(q.process(task(q)))
    original = task(q)['result']
    assert json.loads(original)['analysis']['analysis_basis'] == 'abstract'
    q.fetch = lambda path: json.loads(original); q.publisher = lambda root, values: None
    q.publish_ready()
    assert task(q)['state'] == 'awaiting_fulltext'
    selected[0] = material(p)
    client.value['references'] = {'findings': ['P1']}
    asyncio.run(q.process(task(q)))
    assert json.loads(task(q)['result'])['analysis']['analysis_basis'] == 'full_text'
    assert task(q)['published_result'] == original


def test_source_failure_retries_without_spending_model_attempts(tmp_path):
    p = sample(); p['abstract'] = ''
    q = ReadingQueue(tmp_path, tmp_path / 'run', FakeClient({}), asyncio.Lock(), resolver=lambda p, **kw: {'basis': 'missing'})
    q.enqueue(p); asyncio.run(q.process(task(q)))
    assert task(q)['state'] == 'missing_evidence' and task(q)['attempts'] == 0
    assert task(q)['next_material'] > time.time()
    q.retry(p['id'])
    assert task(q)['next_material'] == 0


def test_old_published_notes_are_preserved_and_queue_is_backed_up(tmp_path):
    directory = tmp_path / 'run'; directory.mkdir()
    p = sample()
    with sqlite3.connect(directory / 'reading-queue.sqlite3') as db:
        db.execute('''CREATE TABLE tasks(paper_id TEXT PRIMARY KEY,paper TEXT,fingerprint TEXT,state TEXT,
          attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,result TEXT,error TEXT DEFAULT '',updated_at REAL,
          thread_id TEXT,draft TEXT DEFAULT '',priority INTEGER DEFAULT 0)''')
        db.execute('INSERT INTO tasks(paper_id,paper,fingerprint,state,result,updated_at) VALUES(?,?,?,?,?,?)',
                   (p['id'], json.dumps(p), fingerprint(p), 'published', '{"saved":"notes"}', time.time()))
    q = ReadingQueue(tmp_path, directory, FakeClient({}), asyncio.Lock())
    assert task(q)['state'] == 'pending'
    assert task(q)['result'] == task(q)['published_result'] == '{"saved":"notes"}'
    assert (directory / 'before-fulltext-queue.sqlite3').exists()
    with q.db() as db:
        db.execute("UPDATE tasks SET state='generating',attempts=1,checkpoint='kept'")
    q = ReadingQueue(tmp_path, directory, FakeClient({}), asyncio.Lock())
    assert task(q)['state'] == 'pending' and task(q)['checkpoint'] == 'kept' and task(q)['attempts'] == 0


def test_missing_abstract_is_resolved_and_cached_without_accepting_search_teaser(tmp_path, monkeypatch):
    p = sample(); p['abstract'] = ''; p['source'] = 'Google Scholar'
    full_abstract = 'The authors evaluate physical models for fatigue life and quantify uncertainty using held-out observations. ' * 3
    calls = []
    def discovery(p, fetch):
        calls.append(p['id'])
        return {'doi': p['doi'], 'abstract': full_abstract, 'abstract_url': p['landing_url'], 'urls': []}
    monkeypatch.setattr('connectors.codex_bridge.reading_materials.discover', discovery)
    resolver = MaterialResolver(tmp_path)
    first = resolver(p)
    assert first['basis'] == 'abstract' and first['text'] == full_abstract
    assert resolver(p) == first and len(calls) == 1
    assert not clean_abstract('The authors propose ... ' * 20)


def test_long_abstract_page_is_not_fulltext_and_wrong_paper_is_rejected():
    p = sample()
    html = f'<meta name="citation_doi" content="{p["doi"]}"><article><h2>Abstract</h2><p>' + p['abstract'] * 20 + '</p></article>'
    abstract, document, links = inspect_html(html, p['landing_url'], p)
    assert document is None
    with pytest.raises(ValueError):
        inspect_html(html, p['landing_url'], {**p, 'doi': '10.test/other', 'title': 'A wholly unrelated research paper'})


def test_scanned_pages_are_supplied_as_images_and_not_silently_skipped(tmp_path, monkeypatch):
    p = sample(); m = material(p); client = FullClient(p)
    m['directory'] = str(tmp_path)
    m['document']['scan_pages'] = 1
    m['document']['pages'][0].update(text='', scan=True)
    m['text'] = m['document']['pages'][1]['text']
    seen = []
    def scans(doc, directory, numbers):
        seen.extend(numbers)
        return [tmp_path / f'page-{n}.png' for n in numbers]
    monkeypatch.setattr('connectors.codex_bridge.documents.render_scan', scans)
    q = ReadingQueue(tmp_path, tmp_path / 'run', client, asyncio.Lock(), resolver=lambda p, **kw: m)
    q.quiet_until = 0; q.enqueue(p); asyncio.run(q.process(task(q)))
    assert seen == [1] and any(images for _, images in client.calls)
    assert task(q)['state'] == 'ready'


def test_publication_failure_retains_result_without_regeneration(tmp_path):
    p = sample(); q = ReadingQueue(tmp_path, tmp_path / 'run', FakeClient(response(p)), asyncio.Lock())
    q.enqueue(p); asyncio.run(q.generate(task(q)))
    saved = task(q)['result']; attempts = task(q)['attempts']
    q.fetch = lambda path: {}
    def fail(*args):
        raise OSError('offline')
    q.publisher = fail
    with pytest.raises(OSError):
        q.publish_ready()
    assert task(q)['result'] == saved and task(q)['attempts'] == attempts
