"""Publication gates for grounded claims, original images and versioned caches."""
import asyncio
from copy import deepcopy
import json

import pytest

from connectors.codex_bridge.article_queue import ArticleWorker, FACTS_VERSION, support_catalog
from src.editions import read, write
from src.reading_article import ARTICLE_VERSION, EXPERIMENT_FIELDS, article_claim_targets, public_article
from tests.test_reading_articles import article_fixture, article_queue
from tests.test_reading_queue import task


SOURCE = 'Ten independent specimens produced 300 augmented samples. Stress was 100 MPa and error was 12.5%.'


def grounded_facts():
    quote = {'page': 'P1', 'source': 'text', 'quote': SOURCE}
    return {'facts': [{'kind': 'result', 'detail': '应力为100 MPa，预测误差为12.5%，增强后得到300个样本。',
                      'refs': ['P1'], 'evidence': [quote]}],
            'experiments': [{**{field: '本批未报告' for field in EXPERIMENT_FIELDS},
                             'refs': ['P1'], 'evidence': {field: [] for field in EXPERIMENT_FIELDS}}],
            'queries': ['fatigue specimens']}


def bindings_for(article, identifier='b0.f0'):
    return {key: [] if key.startswith('experiments.') and value == '原文未报告' else [identifier]
            for key, value in article_claim_targets(article).items()}


@pytest.mark.parametrize('change,error', [
    (lambda v: v['facts'][0]['evidence'][0].update(quote=SOURCE.replace('12.5%', '2.5%')), '摘录不在'),
    (lambda v: v['facts'][0].update(detail='预测误差为2.5%，增强后得到300个样本。'), '关键数字'),
    (lambda v: v['facts'][0].update(detail='应力为100 GPa，预测误差为12.5%。'), '单位'),
    (lambda v: v['facts'][0]['evidence'][0].update(quote=SOURCE.replace('MPa', 'mPa')), '单位'),
    (lambda v: v['facts'][0]['evidence'][0].update(page='P2'), '页码'),
    (lambda v: v['experiments'][0].update(samples='300个独立试件'), '逐项原文摘录'),
])
def test_valid_page_refs_do_not_allow_fabricated_quotes_numbers_or_units(change, error):
    value = grounded_facts()
    ArticleWorker.validate_facts(value, [{'label': 'P1', 'text': SOURCE}])
    change(value)
    with pytest.raises(ValueError, match=error):
        ArticleWorker.validate_facts(value, [{'label': 'P1', 'text': SOURCE}])


def test_visual_evidence_needs_actual_scan_and_a_precise_locator():
    value = grounded_facts()
    evidence = value['facts'][0]['evidence'][0]
    evidence.update(source='scan', location='Table 2, stress and error columns')
    pages = [{'label': 'P1', 'text': 'OCR omitted the scientific table.'}]
    with pytest.raises(ValueError, match='原页图像'):
        ArticleWorker.validate_facts(value, pages)
    ArticleWorker.validate_facts(value, pages, visual=True)
    evidence.pop('location')
    with pytest.raises(ValueError, match='图表定位'):
        ArticleWorker.validate_facts(value, pages, visual=True)


def test_each_article_claim_is_bound_and_numeric_changes_are_rejected():
    article = article_fixture()
    supports = support_catalog([grounded_facts()])
    bindings = bindings_for(article)
    ArticleWorker.validate_claims(article, bindings, supports)
    missing = deepcopy(bindings); missing.pop('lead')
    with pytest.raises(ValueError, match='完整'):
        ArticleWorker.validate_claims(article, missing, supports)
    wrong = deepcopy(bindings); wrong['lead'] = ['b99.f99']
    with pytest.raises(ValueError, match='未知'):
        ArticleWorker.validate_claims(article, wrong, supports)
    article['experiments'][0]['results'] = '误差为2.5%'
    bindings['experiments.0.results'] = ['b0.f0']
    with pytest.raises(ValueError, match='关键数字'):
        ArticleWorker.validate_claims(article, bindings, supports)


@pytest.mark.parametrize('review', [
    {'approved': True, 'issues': []},
    {'approved': True, 'issues': [], 'checked_claims': []},
    {'approved': True, 'issues': ['样本类别混淆'], 'checked_claims': ['lead']},
    {'approved': True, 'issues': [], 'checked_claims': ['lead', 'lead']},
    {'approved': False, 'issues': [], 'checked_claims': ['lead']},
])
def test_review_requires_explicit_complete_claim_coverage(review):
    with pytest.raises(ValueError, match='未逐项核验'):
        ArticleWorker.validate_review(review, ['lead'])


def test_final_review_sees_real_pages_scans_and_rejects_sample_type_confusion(tmp_path, monkeypatch):
    q, client = article_queue(tmp_path)
    original = task(q)['result']
    source = 'Ten independent specimens produced 300 augmented samples.'
    client.fact_quote = source
    client.fact_detail = '原研究使用独立试件，经过增强得到300个样本。'
    client.article_value['experiments'][0]['samples'] = '300个独立试件'
    row = task(q); material = json.loads(row['material'])
    for page in material['document']['pages']:
        page['text'] = source
    material['directory'] = str(tmp_path / 'original')
    with q.db() as db:
        db.execute('UPDATE tasks SET material=?', (json.dumps(material),))
    calls = []
    def render(document, directory, numbers):
        calls.append(numbers)
        return [tmp_path / f'original-page-{number}.png' for number in numbers]
    monkeypatch.setattr('connectors.codex_bridge.documents.render_scan', render)
    normal = client.turn
    observed = []
    async def review(thread, prompt, images=()):
        if prompt.startswith('核验以下'):
            assert source in prompt.split('\n真实原文：', 1)[1]
            assert '300个独立试件' in prompt and '300 augmented samples' in prompt
            assert images == [tmp_path / 'original-page-1.png', tmp_path / 'original-page-2.png']
            observed.append(prompt)
            targets = json.loads(prompt.split('\n待核验目标：', 1)[1].split('\n', 1)[0])
            yield {'type': 'delta', 'text': json.dumps({'approved': False, 'issues': ['增强样本不能称为独立试件'], 'checked_claims': targets})}
            yield {'type': 'completed', 'status': 'completed'}
        else:
            async for event in normal(thread, prompt, images):
                yield event
    client.turn = review
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert observed and calls == [[1, 2], [1, 2]]
    assert task(q)['article_state'] == 'retry' and task(q)['result'] == original


def test_v1_published_article_and_old_facts_survive_failed_v2_upgrade(tmp_path):
    q, client = article_queue(tmp_path, False)
    old = json.loads(task(q)['result'])
    article = article_fixture(); article['version'] = 1
    old['analysis'].update(article=article, article_updated_at='2026-10-03T00:00:00+00:00', article_profile='b' * 64)
    old_result = json.dumps(old)
    with q.db() as db:
        db.execute("UPDATE tasks SET result=?,published_result=?,state='published'", (old_result, old_result))
    old_cache = q.runtime / 'article-facts' / task(q)['paper_id'] / ('a' * 64 + '-v1') / 'facts-0.json'
    write(old_cache, {'facts': [{'detail': 'Old unsupported fact without quotes'}]})
    saved = old_cache.read_bytes()
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['result'] == old_result and task(q)['published_result'] == old_result
    assert old_cache.read_bytes() == saved and FACTS_VERSION > 1
    assert len([p for p in client.article_calls if p.startswith('从以下已核实')]) == 2
    assert public_article(article)['version'] == 1
    assert ARTICLE_VERSION > 1


def test_public_payload_strips_private_claim_evidence_and_validates_groups():
    article = article_fixture()
    article['claim_evidence'] = {'lead': ['private-page-quote']}
    article['figure_groups'] = [{'section': 'validation', 'title': '验证结果原图',
        'panels': [{'figure_id': 'fig-safe', 'caption': '原论文报告的实验对比结果。', 'path': 'private-file'}]}]
    public = public_article(article)
    assert 'claim_evidence' not in public and 'private' not in json.dumps(public)
    article['figure_groups'][0]['panels'].append(deepcopy(article['figure_groups'][0]['panels'][0]))
    with pytest.raises(ValueError, match='重复'):
        public_article(article)
    article['figure_groups'][0]['panels'] = [{'figure_id': '../secret', 'caption': '不能作为原文图像。'}]
    with pytest.raises(ValueError, match='标识'):
        public_article(article)


def test_figure_arrival_requeues_article_without_reextracting_facts(tmp_path, monkeypatch):
    q, client = article_queue(tmp_path)
    figures = []
    monkeypatch.setattr('src.article_figures.eligible_article_figures', lambda *args: figures)
    asyncio.run(q.articles.process(q.articles.next_task()))
    first = task(q)['article_key']
    figures.append({'figure_id': 'fig-late', 'caption': 'Original validation results'})
    pending = q.articles.next_task()
    assert pending is not None and pending['article_key'] != first
    asyncio.run(q.articles.process(pending))
    assert task(q)['article_state'] == 'complete'
    assert len([p for p in client.article_calls if p.startswith('从以下已核实')]) == 2


def test_review_cache_is_bound_to_actual_draft_content(tmp_path):
    q, client = article_queue(tmp_path)
    original = task(q)['result']
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'complete'
    directory = q.runtime / 'article-drafts' / task(q)['paper_id'] / task(q)['article_key']
    draft_path = next(directory.glob('article-*.json'))
    draft = read(draft_path)
    draft['sections'][0]['paragraphs'][0] += '这项结果足以支持所有场景的工程应用。'
    write(draft_path, draft)
    with q.db() as db:
        db.execute("UPDATE tasks SET result=?,article_state='retry',article_next_attempt=0", (original,))
    client.approve = False
    count = len(client.article_calls)
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert any(p.startswith('核验以下') for p in client.article_calls[count:])
    assert task(q)['result'] == original and task(q)['article_state'] == 'retry'


def test_unknown_original_figure_cannot_publish(tmp_path):
    q, client = article_queue(tmp_path)
    original = task(q)['result']
    client.article_value['figure_groups'] = [{'section': 'method', 'title': '来源不明的图片',
        'panels': [{'figure_id': 'fig-invented', 'caption': '模型随意编造的原文图片说明。'}]}]
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'retry' and task(q)['result'] == original
    assert '未经核实' in task(q)['article_error']


def test_translated_caption_alone_cannot_anchor_a_measured_result():
    article = article_fixture()
    article['experiments'][0]['results'] = '该条件下预测误差为12.5%。'
    figures = [{'figure_id': 'fig-translation', 'caption': '预测误差为12.5%。', 'caption_basis': 'curated_translation'}]
    supports = support_catalog([grounded_facts()], figures)
    bindings = bindings_for(article)
    bindings['experiments.0.results'] = ['figure:fig-translation']
    with pytest.raises(ValueError, match='关键数字'):
        ArticleWorker.validate_claims(article, bindings, supports)
    bindings['experiments.0.results'].append('b0.f0')
    ArticleWorker.validate_claims(article, bindings, supports)


def test_group_captions_are_explicitly_reviewed_with_original_image(tmp_path, monkeypatch):
    q, client = article_queue(tmp_path)
    image = tmp_path / 'original-figure.png'
    figures = [{'figure_id': 'fig-original', 'caption': 'Original independent experimental validation.', 'caption_basis': 'publisher'}]
    monkeypatch.setattr('src.article_figures.eligible_article_figures', lambda *args: figures)
    monkeypatch.setattr('src.article_figures.article_figure_file', lambda *args: image)
    client.article_value['figure_groups'] = [{'section': 'validation', 'title': '独立试验验证',
        'panels': [{'figure_id': 'fig-original', 'caption': '原研究独立试验的验证结果。'}]}]
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'complete'
    # Even bindings to page facts must check the selected image's correspondence.
    reviews = [prompt for prompt, images in zip(client.article_calls, client.article_images)
               if prompt.startswith('核验以下') and images == [image]]
    assert len(reviews) == 1
    targets = json.loads(reviews[0].split('\n待核验目标：', 1)[1].split('\n', 1)[0])
    assert targets == ['figure_groups.0.title', 'figure_groups.0.panels.0.caption']
    result = json.loads(task(q)['result'])
    assert result['analysis']['article']['figure_groups'] == client.article_value['figure_groups']
    assert 'claim_evidence' not in json.dumps(result)


def test_proposal_fields_and_diagram_are_checked_without_treating_planned_numbers_as_results(tmp_path):
    q, client = article_queue(tmp_path)
    client.article_value['routes'][0]['design'] += '建议另外采集36个独立试件，作为尚未开展的验证设计。'
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'complete'
    reviews = [p for p in client.article_calls if p.startswith('核验以下')]
    for prompt in reviews:
        targets = json.loads(prompt.split('\n待核验目标：', 1)[1].split('\n', 1)[0])
        payload = json.loads(prompt.split('\n目标证据：', 1)[1].split('\n', 1)[0])
        assert {'routes.0.comparison', 'routes.0.design', 'routes.0.criterion', 'diagrams.1'} <= set(targets)
        assert payload['routes.0.design']['basis'] == 'proposal'
        if 'routes.0.gap' in payload:
            assert payload['routes.0.gap']['basis'] == 'source'


def test_non_object_review_cache_is_rebuilt(tmp_path):
    q, client = article_queue(tmp_path)
    original = task(q)['result']
    asyncio.run(q.articles.process(q.articles.next_task()))
    directory = q.runtime / 'article-drafts' / task(q)['paper_id'] / task(q)['article_key']
    for path in directory.glob('review-*-pages-*.json'):
        write(path, ['invalid cache'])
    with q.db() as db:
        db.execute("UPDATE tasks SET result=?,article_state='retry',article_next_attempt=0", (original,))
    count = len(client.article_calls)
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'complete'
    assert any(p.startswith('核验以下') for p in client.article_calls[count:])
