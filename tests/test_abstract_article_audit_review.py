"""Independent checks of resumed audits and every original-image review batch."""
import asyncio
import json

from PIL import Image

from src.editions import read, write
from src.reading_article import article_claim_targets
from tests.test_abstract_article_queue import queue
from tests.test_reading_queue import task


def review_targets(prompt):
    return json.loads(prompt.split('\n待核验目标：', 1)[1].split('\n', 1)[0])


def test_invalid_saved_review_is_reaudited_instead_of_repeatedly_failing(tmp_path):
    q, client = queue(tmp_path)
    original = task(q)['result']
    asyncio.run(q.articles.process(q.articles.next_task()))
    finished = task(q)
    assert finished['article_state'] == 'complete'
    directory = q.runtime / 'article-drafts' / finished['paper_id'] / finished['article_key']
    path = next(directory.glob('review-*-abstract.json'))
    cached = read(path)
    cached['review']['checked_claims'].pop()
    write(path, cached)
    before = len([prompt for prompt in client.prompts if prompt.startswith('核验以下摘要')])
    with q.db() as db:
        db.execute("UPDATE tasks SET result=?,article_state='pending',state='awaiting_fulltext'", (original,))
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'complete', task(q)['article_error']
    assert len([prompt for prompt in client.prompts if prompt.startswith('核验以下摘要')]) == before + 1
    repaired = read(path)['review']
    assert set(repaired['checked_claims']) == set(article_claim_targets(json.loads(task(q)['result'])['analysis']['article']))


def test_all_selected_panels_and_figure_bound_prose_receive_actual_image_review(tmp_path, monkeypatch):
    q, client = queue(tmp_path)
    figures, files = [], {}
    for index in range(6):
        identifier = f'fig-{chr(97 + index)}'
        path = tmp_path / (identifier + '.png')
        Image.new('RGB', (240, 200), (index * 20, 50, 100)).save(path)
        files[identifier] = path
        figures.append({'figure_id': identifier, 'caption': 'Original diagram of the studied model workflow.',
                        'caption_basis': 'publisher'})
    monkeypatch.setattr('src.article_figures.eligible_article_figures', lambda *args: figures)
    monkeypatch.setattr('connectors.codex_bridge.abstract_article_queue.eligible_article_figures', lambda *args: figures)
    monkeypatch.setattr('connectors.codex_bridge.abstract_article_queue.article_figure_file',
                        lambda root, figure: files[figure['figure_id']])
    client.article['figure_groups'] = [
        {'section': section, 'title': '原文中的研究方法与结果图示',
         'panels': [{'figure_id': figure['figure_id'], 'caption': '原图展示研究模型的流程，具体信息以原始图注为准。'}
                    for figure in group]}
        for section, group in [('method', figures[:3]), ('findings', figures[3:])]]
    normal = client.turn

    async def bind_figure_prose(thread, prompt, images=()):
        async for event in normal(thread, prompt, images):
            if event['type'] == 'delta' and 'claim_evidence' in prompt and not prompt.startswith('核验以下摘要'):
                value = json.loads(event['text'])
                value['claim_evidence']['sections.0.paragraphs.0'] = ['figure:fig-f']
                event = {**event, 'text': json.dumps(value, ensure_ascii=False)}
            yield event

    client.turn = bind_figure_prose
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'complete', task(q)['article_error']
    article = json.loads(task(q)['result'])['analysis']['article']
    checks = [(prompt, images) for prompt, images in zip(client.prompts, client.images)
              if prompt.startswith('核验以下摘要')]
    assert set(review_targets(checks[0][0])) == set(article_claim_targets(article))
    assert checks[0][1] == []
    assert [len(images) for _, images in checks[1:]] == [4, 2]
    for group_index, group in enumerate(article['figure_groups']):
        for panel_index, panel in enumerate(group['panels']):
            path = files[panel['figure_id']]
            prompt, _ = next((prompt, images) for prompt, images in checks[1:] if path in images)
            targets = review_targets(prompt)
            assert f'figure_groups.{group_index}.title' in targets
            assert f'figure_groups.{group_index}.panels.{panel_index}.caption' in targets
            assert path.is_file()
    bound_prompt = next(prompt for prompt, images in checks[1:] if files['fig-f'] in images)
    assert 'sections.0.paragraphs.0' in review_targets(bound_prompt)


def test_missing_review_target_cannot_publish_even_when_model_says_approved(tmp_path):
    q, client = queue(tmp_path)
    before = task(q)
    normal = client.turn

    async def omit_claim(thread, prompt, images=()):
        async for event in normal(thread, prompt, images):
            if prompt.startswith('核验以下摘要') and event['type'] == 'delta':
                review = json.loads(event['text'])
                review['checked_claims'].pop()
                event = {**event, 'text': json.dumps(review, ensure_ascii=False)}
            yield event

    client.turn = omit_claim
    asyncio.run(q.articles.process(q.articles.next_task()))
    assert task(q)['article_state'] == 'retry'
    assert task(q)['result'] == before['result']
    assert task(q)['published_result'] == before['published_result']


def test_legacy_fulltext_ready_publication_can_be_acknowledged_without_regeneration(tmp_path):
    from tests.test_reading_articles import article_queue
    q, client = article_queue(tmp_path)
    asyncio.run(q.articles.process(q.articles.next_task()))
    legacy = json.loads(task(q)['result'])
    legacy['analysis']['article']['version'] = 2
    legacy['analysis']['article'].pop('basis')
    encoded = json.dumps(legacy, ensure_ascii=False)
    with q.db() as db:
        db.execute("UPDATE tasks SET result=?,published_result='',state='ready'", (encoded,))
    calls = []
    q.fetch = lambda path: legacy
    q.publisher = lambda *args: calls.append(args)
    assert q.articles.next_task() is None  # A schema bump is not new source evidence.
    q.publish_ready()
    assert task(q)['state'] == 'published'
    assert task(q)['published_result'] == encoded
    assert calls == []


def test_new_reading_without_article_is_not_hidden_by_old_complete_article_state(tmp_path):
    q, client = queue(tmp_path)
    asyncio.run(q.articles.process(q.articles.next_task()))
    complete = task(q)
    assert complete['article_state'] == 'complete'
    reviews_before = len([prompt for prompt in client.prompts if prompt.startswith('核验以下摘要')])
    paper = json.loads(complete['paper'])
    paper['authors'] = ['Corrected Author']
    q.enqueue(paper)
    asyncio.run(q.process(task(q)))
    refreshed = task(q)
    assert refreshed['revision'] > complete['revision']
    assert json.loads(refreshed['material'])['version'] == json.loads(complete['material'])['version']
    assert 'article' not in json.loads(refreshed['result'])['analysis']
    pending = q.articles.next_task()
    assert pending is not None, 'The completed state belongs to the previous result, which was replaced'
    asyncio.run(q.articles.process(pending))
    assert task(q)['article_state'] == 'complete', task(q)['article_error']
    assert json.loads(task(q)['result'])['analysis']['article']['basis'] == 'abstract'
    assert len([prompt for prompt in client.prompts if prompt.startswith('核验以下摘要')]) > reviews_before
