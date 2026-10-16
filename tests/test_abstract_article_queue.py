import asyncio
from copy import deepcopy
import json

import pytest

from connectors.codex_bridge.reading_materials import bundle, verified_abstract
from connectors.codex_bridge.reading_queue import ReadingQueue
from src.auto_reading import public_analysis
from src.reading_article import ARTICLE_VERSION, article_claim_targets
from tests.test_reading_queue import sample, response, task, FakeClient


def abstract_article():
    return {'version': ARTICLE_VERSION, 'basis': 'abstract',
            'lead': '这项研究关注工程预测中的数据与物理关系，下面依据作者摘要说明研究思路、已有证据及仍需阅读全文确认的问题。',
            'sections': [{'id': key, 'title': title,
                          'paragraphs': ['摘要介绍了研究问题与方法思路，但具体实验条件仍需结合全文核对，不能据此推断尚未报告的性能或推广范围。']}
                         for key, title in [('context', '研究为何关注这一问题'), ('method', '摘要提出的方法思路'),
                                            ('findings', '作者报告的主要发现'), ('limitations', '证据范围与未报告条件'),
                                            ('inspiration', '继续阅读时关注什么')]],
            'diagrams': [], 'figure_groups': []}


class Client(FakeClient):
    def __init__(self, paper):
        super().__init__(response(paper))
        self.article = abstract_article()
        self.approve = True
        self.prompts = []
        self.images = []

    async def thread(self, existing=None, *, purpose='reading'):
        return 'abstract-test'

    async def turn(self, thread, prompt, images=()):
        self.prompts.append(prompt); self.images.append(images)
        if prompt.startswith('核验以下摘要'):
            targets = json.loads(prompt.split('\n待核验目标：', 1)[1].split('\n', 1)[0])
            value = {'approved': self.approve, 'issues': [] if self.approve else ['图文不一致'],
                     'checked_claims': targets}
        elif 'claim_evidence' in prompt:
            value = deepcopy(self.article)
            value['claim_evidence'] = {k: ['abstract:0'] for k in article_claim_targets(value)}
        else:
            value = self.value
        yield {'type': 'delta', 'text': json.dumps(value, ensure_ascii=False)}
        yield {'type': 'completed', 'status': 'completed'}


def queue(tmp_path):
    paper = sample()
    client = Client(paper)
    material = bundle(paper, None, None, abstract=paper['abstract'], url=paper['landing_url'])
    q = ReadingQueue(tmp_path, tmp_path / 'run', client, asyncio.Lock(), resolver=lambda *a, **kw: material)
    q.quiet_until = 0
    q.enqueue(paper)
    asyncio.run(q.process(task(q)))
    with q.db() as db:
        db.execute("UPDATE tasks SET published_result=result,state='awaiting_fulltext'")
    return q, client


def test_published_abstract_enters_article_queue_and_reads_original_again(tmp_path):
    q, client = queue(tmp_path)
    row = q.articles.next_task()
    assert row is not None
    source = json.loads(row['material'])['text']
    asyncio.run(q.articles.process(row))
    saved = task(q)
    assert saved['article_state'] == 'complete', saved['article_error']
    value = public_analysis(json.loads(saved['result']))
    assert value['analysis']['article']['basis'] == 'abstract'
    assert source in next(p for p in client.prompts if p.startswith('核验以下摘要'))
    assert 'claim_evidence' not in json.dumps(value)
    assert q.articles.next_task() is None


@pytest.mark.parametrize('kind', ['snippet', 'wrong_paper', 'legacy'])
def test_snippets_unverified_identity_and_legacy_material_require_refresh(tmp_path, kind):
    q, _ = queue(tmp_path)
    row = task(q); m = json.loads(row['material'])
    if kind == 'snippet':
        m['text'] += '...'
    elif kind == 'wrong_paper':
        m['abstract_identity'] = '0' * 64
    else:
        m.pop('abstract_complete')
    with q.db() as db:
        db.execute('UPDATE tasks SET material=?', (json.dumps(m),))
    assert not verified_abstract(m, json.loads(row['paper']))
    assert q.articles.next_task() is None
    assert task(q)['next_material'] == 0


@pytest.mark.parametrize('failure', ['number', 'unit', 'figure', 'review'])
def test_invalid_abstract_article_cannot_replace_published_reading(tmp_path, failure, monkeypatch):
    q, client = queue(tmp_path)
    before = task(q)
    if failure == 'number':
        client.article['lead'] += '误差下降了999999%。'
    elif failure == 'unit':
        client.article['lead'] += '加载应力为999999 GPa。'
    elif failure == 'figure':
        client.article['figure_groups'] = [{'section': 'method', 'title': '原论文方法图',
            'panels': [{'figure_id': 'fig-made-up', 'caption': '来源未经确认的虚构图像。'}]}]
    else:
        client.approve = False
    asyncio.run(q.articles.process(q.articles.next_task()))
    saved = task(q)
    assert saved['article_state'] == 'retry'
    assert saved['published_result'] == before['published_result'] and saved['result'] == before['result']


def test_actual_original_figure_is_reviewed_separately(tmp_path, monkeypatch):
    q, client = queue(tmp_path)
    image = tmp_path / 'figure.png'
    figures = [{'figure_id': 'fig-real', 'caption': 'Model workflow illustrating the research method.',
                'caption_basis': 'publisher'}]
    monkeypatch.setattr('src.article_figures.eligible_article_figures', lambda *a: figures)
    monkeypatch.setattr('connectors.codex_bridge.abstract_article_queue.eligible_article_figures', lambda *a: figures)
    monkeypatch.setattr('connectors.codex_bridge.abstract_article_queue.article_figure_file', lambda *a: image)
    client.article['figure_groups'] = [{'section': 'method', 'title': '原文方法示意',
        'panels': [{'figure_id': 'fig-real', 'caption': '原文模型方法流程的图示，具体步骤以原图为准。'}]}]
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'complete', task(q)['article_error']
    assert [image] in client.images


def test_new_fulltext_source_supersedes_abstract_draft(tmp_path):
    q, client = queue(tmp_path)
    before = task(q); normal = client.turn
    async def replace(thread, prompt, images=()):
        if prompt.startswith('核验以下摘要'):
            material = json.loads(before['material']); material.update(basis='full_text', version='b' * 64)
            with q.db() as db:
                db.execute('UPDATE tasks SET material=?', (json.dumps(material),))
        async for event in normal(thread, prompt, images):
            yield event
    client.turn = replace
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['result'] == before['result']


def test_completed_fulltext_never_downgrades_on_source_outage(tmp_path):
    from tests.test_reading_articles import article_queue
    q, _ = article_queue(tmp_path)
    before = task(q)
    paper = json.loads(before['paper'])
    q.resolver = lambda *a, **kw: bundle(paper, None, None, abstract=paper['abstract'], url=paper['landing_url'])
    asyncio.run(q.process(before))
    assert task(q)['result'] == before['result']
    assert json.loads(task(q)['material'])['basis'] == 'full_text'


def test_local_published_abstract_uses_public_material_proof_when_index_has_only_snippet(tmp_path):
    from fastapi.testclient import TestClient
    from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN
    from tests.test_codex_bridge import FakeCodex
    q, _ = queue(tmp_path)
    asyncio.run(q.articles.process(q.articles.next_task()))
    row = task(q)
    paper = json.loads(row['paper']); paper['abstract'] = 'Search snippet...'
    with q.db() as db:
        db.execute("UPDATE tasks SET paper=?,published_result=result,state='awaiting_fulltext'", (json.dumps(paper),))
    app = create_app(tmp_path, runtime=tmp_path/'run', rpc=FakeCodex())
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        response = client.get('/readings/' + row['paper_id'] + '.html')
        assert response.status_code == 200
        assert '这项研究关注工程预测中的数据与物理关系' in response.text
        assert '研究为何关注这一问题' in response.text


def test_nature_abstract_heading_does_not_hide_complete_sibling_content():
    from connectors.codex_bridge.reading_materials import inspect_html
    paper = sample()
    source = 'The framework predicts material properties and validates the inverse design with independently measured compositions. ' * 3
    body = ('<meta name="citation_doi" content="' + paper['doi'] + '">'
            '<div id="Abs1-section"><h2 id="Abs1">Abstract</h2><div id="Abs1-content"><p>'
            + source + '</p></div></div><aside>Unrelated recommended paper</aside>').encode()
    abstract, _, _ = inspect_html(body, paper['landing_url'], paper)
    assert abstract == source.strip()
    assert 'Unrelated' not in abstract
