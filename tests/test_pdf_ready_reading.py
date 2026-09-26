import asyncio
from copy import deepcopy
import json

import pytest

from connectors.codex_bridge.reading_queue import ReadingQueue
from src.auto_reading import READER_VERSION, public_analysis
from tests.test_fulltext_reading import FullClient, material
from tests.test_reading_queue import sample, task, FakeClient, response


class Resolver:
    def __init__(self, value, signature=''):
        self.value, self.current = value, signature
    def signature(self, _):
        return self.current
    def __call__(self, *args, **kwargs):
        return {**self.value, 'local_signature': self.current}


def test_new_pdf_wakes_exhausted_task_and_same_file_is_idempotent(tmp_path):
    p = sample(); resolver = Resolver(material(p), 'old')
    q = ReadingQueue(tmp_path, tmp_path/'run', FakeClient({}), asyncio.Lock(), resolver=resolver)
    q.enqueue(p)
    with q.db() as db:
        db.execute("UPDATE tasks SET state='failed',attempts=3,next_attempt=9999999999,result='{}'")
    q.document_available(p['id'])
    assert task(q)['state'] == 'failed' and task(q)['attempts'] == 3
    resolver.current = 'new'
    status = q.document_available(p['id'])
    assert status['pdf_available'] and 'PDF 已就绪' in status['label']
    assert task(q)['attempts'] == task(q)['next_attempt'] == 0 and task(q)['revision'] == 1
    q2 = ReadingQueue(tmp_path, tmp_path/'run', FakeClient({}), asyncio.Lock(), resolver=resolver)
    assert task(q2)['state'] == 'pending' and task(q2)['result'] == '{}'
    q2.document_available(p['id'])
    assert task(q2)['revision'] == 1


def test_upload_during_generation_discards_old_result(tmp_path):
    p = sample(); resolver = Resolver(material(p, False), 'old')
    class Upload(FakeClient):
        async def turn(self, thread, prompt):
            resolver.current = 'new'
            q.document_available(p['id'])
            async for event in super().turn(thread, prompt):
                yield event
    q = ReadingQueue(tmp_path, tmp_path/'run', Upload(response(p)), asyncio.Lock(), resolver=resolver)
    q.enqueue(p); q.quiet_until = 0
    asyncio.run(q.process(task(q)))
    assert task(q)['state'] == 'pending' and task(q)['result'] is None and task(q)['revision'] == 1
    assert task(q)['attempts'] == 0


def test_new_pdf_blocks_older_ready_result_from_publication(tmp_path):
    p = sample(); resolver = Resolver(material(p, False), 'old')
    q = ReadingQueue(tmp_path, tmp_path/'run', FakeClient(response(p)), asyncio.Lock(), resolver=resolver,
                     fetch=lambda _: {}, publisher=lambda *a: pytest.fail('Old result must not publish'))
    q.enqueue(p); asyncio.run(q.generate(task(q)))
    assert task(q)['state'] == 'ready'
    resolver.current = 'new'; q.document_available(p['id']); q.publish_ready()
    assert task(q)['state'] == 'pending' and task(q)['result']


def test_reader_upgrade_recovers_only_unfinished_work(tmp_path):
    p = sample(); q = ReadingQueue(tmp_path, tmp_path/'run', FakeClient({}), asyncio.Lock())
    q.enqueue(p)
    with q.db() as db:
        db.execute("UPDATE tasks SET reader_version=1,state='failed',attempts=3,result='{}',checkpoint='old'")
    q = ReadingQueue(tmp_path, tmp_path/'run', FakeClient({}), asyncio.Lock())
    assert task(q)['reader_version'] == READER_VERSION and task(q)['attempts'] == 0
    assert task(q)['result'] == '{}' and task(q)['checkpoint'] == ''
    with q.db() as db:
        db.execute("UPDATE tasks SET reader_version=1,state='published',result=?", (json.dumps({'analysis':{'analysis_basis':'full_text'}}),))
    q = ReadingQueue(tmp_path, tmp_path/'run', FakeClient({}), asyncio.Lock())
    assert task(q)['state'] == 'published'


def test_original_defect_is_reported_after_all_pages_read(tmp_path, monkeypatch):
    p = sample(); m = material(p); m['directory'] = str(tmp_path)
    monkeypatch.setattr('connectors.codex_bridge.documents.render_scan', lambda *a, **kw: [tmp_path/'page.png'])
    class Defect(FullClient):
        async def turn(self, thread, prompt, images=()):
            async for event in super().turn(thread, prompt, images):
                if 'paper_matches' in prompt and event['type'] == 'delta' and '[P1]' in prompt:
                    value = json.loads(event['text'])
                    value['issues'] = [{'kind':'source_defect','page':'P1','detail':'公式（5）（6）在原页为 MERGEFORMAT 占位；无法验证这两式。'}]
                    event['text'] = json.dumps(value, ensure_ascii=False)
                yield event
    client = Defect(p)
    q = ReadingQueue(tmp_path, tmp_path/'run', client, asyncio.Lock(), resolver=Resolver(m))
    q.enqueue(p); q.quiet_until = 0; asyncio.run(q.process(task(q)))
    value = json.loads(task(q)['result'])
    assert task(q)['state'] == 'ready' and value['material']['covered_labels'] == ['P1','P2']
    assert value['material']['issues'][0]['kind'] == 'source_defect'
    assert 'MERGEFORMAT' in client.calls[-1][0]
    assert 'MERGEFORMAT' in value['analysis']['deep_read']['limitations']
    broken = deepcopy(value); broken['material']['issues'][0]['kind'] = 'unreadable'
    with pytest.raises(ValueError): public_analysis(broken)


def test_unreadable_page_does_not_prevent_later_page_reading(tmp_path, monkeypatch):
    p = sample(); m = material(p); m['directory'] = str(tmp_path)
    monkeypatch.setattr('connectors.codex_bridge.documents.render_scan', lambda *a, **kw: [tmp_path/'page.png'])
    class Partial(FullClient):
        async def turn(self, thread, prompt, images=()):
            async for event in super().turn(thread, prompt, images):
                if 'paper_matches' in prompt and event['type'] == 'delta' and '[P1]' in prompt:
                    value = json.loads(event['text']); value.update(readable=False, unreadable_pages=['P1'])
                    event['text'] = json.dumps(value, ensure_ascii=False)
                yield event
    q = ReadingQueue(tmp_path, tmp_path/'run', Partial(p), asyncio.Lock(), resolver=Resolver(m))
    q.enqueue(p); q.quiet_until = 0; asyncio.run(q.process(task(q)))
    assert task(q)['state'] == 'retry' and task(q)['result'] is None
    pages = json.loads(task(q)['checkpoint'])['pages']
    assert pages['P1']['status'] == 'unreadable' and pages['P2']['status'] == 'read'
    assert q.snapshot()['tasks'][0]['covered'] == 1
    client = FullClient(p); q.client = client
    asyncio.run(q.process(task(q)))
    assert task(q)['state'] == 'ready' and len(client.calls) == 2


def test_live_fulltext_status_takes_precedence_over_old_snippet():
    from tools.build_site import recommendation_context
    p = {**sample(), 'source':'Google Scholar', 'abstract':'', 'analysis_status':'ready', 'analysis_basis':'abstract',
         'reading_status': {'state':'generating','basis':'full_text','covered':4,'total':16,'pdf_available':True}}
    html = recommendation_context(p, './', set())
    assert '正在全文精读' in html and '4/16' in html and '完整资料待补充' not in html


def test_private_upload_does_not_create_public_task(tmp_path):
    q = ReadingQueue(tmp_path, tmp_path/'run', FakeClient({}), asyncio.Lock())
    assert q.document_available(sample()['id']) is None and not q.snapshot()['tasks']


def test_upload_during_acquisition_preserves_new_pending_revision(tmp_path):
    p = sample()
    class Upload(Resolver):
        def __call__(self, *args, **kwargs):
            self.current = 'new'; q.document_available(p['id'])
            return {**self.value, 'local_signature': 'old'}
    resolver = Upload(material(p), 'old')
    q = ReadingQueue(tmp_path, tmp_path/'run', FullClient(p), asyncio.Lock(), resolver=resolver)
    q.enqueue(p); q.quiet_until = 0; asyncio.run(q.process(task(q)))
    assert task(q)['state'] == 'pending' and task(q)['attempts'] == 0
    assert task(q)['wanted_signature'] == 'new' and task(q)['material'] == ''


def test_downloaded_pdf_is_shared_with_reader_library(tmp_path):
    from connectors.codex_bridge.server import create_app
    from connectors.codex_bridge.documents import parse_pdf
    from src.editions import write
    from tests.test_codex_bridge import FakeCodex
    from tests.test_pdf_annotations import source_pdf
    p = sample(); write(tmp_path/'data/daily.json', {'core':[p]})
    app = create_app(tmp_path, rpc=FakeCodex())
    q, store = app.state.reading_queue, app.state.store
    directory = tmp_path/'acquisition'; directory.mkdir()
    doc = parse_pdf(source_pdf(), directory)
    q.acquired(p['id'], {'document': doc, 'directory':str(directory)})
    assert store.document(p['id'])['hash'] == doc['hash']
    assert q.resolver.signature(p['id']) == doc['hash']
    q.acquired(p['id'], {'document': doc, 'directory':str(directory)})
    assert len(store.library.documents(p['id'])) == 1


def test_pdf_links_are_followed_before_using_html_fulltext(tmp_path, monkeypatch):
    from connectors.codex_bridge.reading_materials import MaterialResolver
    module = 'connectors.codex_bridge.reading_materials.'
    p = sample(); url = p['landing_url']; pdf_url = url+'/paper.pdf'
    monkeypatch.setattr(module+'discover', lambda *a: {'urls':[url], 'abstract':'', 'abstract_url':'', 'doi':p['doi']})
    monkeypatch.setattr(module+'inspect_html', lambda *a: ('', {'kind':'html','hash':'html','pages':[]}, [pdf_url]))
    resolver = MaterialResolver(tmp_path, fetch=lambda u: (b'%PDF-content' if u == pdf_url else b'html', u))
    monkeypatch.setattr(resolver, 'pdf', lambda *a: {'kind':'pdf','hash':'pdf','pages':[]})
    result = resolver(p)
    assert result['document']['kind'] == 'pdf' and result['source_url'] == pdf_url
