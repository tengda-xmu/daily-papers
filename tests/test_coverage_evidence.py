import asyncio
import hashlib
import json

import pytest

from src.coverage_evidence import (PRIVATE, PUBLIC, enqueue_review, extraction_prompt,
                                  load_reviews, match_evidence, public_review, public_mirrors,
                                  review_key, save_manual_review, save_syndicated_review)
from src.public_sources import read, write

URL = 'https://mp.weixin.qq.com/s/DXvEHDH1FQRqGQkQhBYKyw'
PAPER = {'id': 'c53c3b080d69', 'doi': '10.1038/s42256-026-01262-4',
         'title': 'Guiding generative models to uncover diverse and novel crystals via reinforcement learning',
         'authors': ['Hyunsoo Park', 'Aron Walsh']}
ARTICLE = {'url': URL, 'title': 'npj Comput. Mater. | 晶体生成模型的新颖稳定性能协同优化',
           'account': 'Ai赋能材料', 'published_at': '2026-07-22T18:00:00+08:00',
           'body_status': 'challenge'}
EVIDENCE = {'dois': [PAPER['doi']], 'title': PAPER['title'], 'authors': PAPER['authors'],
            'legible': True, 'context': 'paper_front_page', 'main_topic': True,
            'reference_only': False, 'conflict': False, 'provenance': 'User supplied screenshot and original URL'}

MIRROR = {'url': 'https://hub.baai.ac.cn/view/53949', 'body_status': 'readable',
          'kind': 'article', 'checked_at': '2026-10-15T10:00:00+08:00'}


def test_syndicated_body_receipt_keeps_original_challenge_and_never_publishes_body(tmp_path):
    parsed = {**ARTICLE, 'source_version': 'a' * 64, 'evidence_source': 'syndicated_article',
              'mirror_url': MIRROR['url'], 'mirror_kind': 'article', 'mirror_body_status': 'readable',
              'original_body_status': 'challenge', 'match_text': PAPER['title'] + ' by Aron Walsh',
              'blocks': [{'type': 'paragraph', 'text': 'Private fetched body'}]}
    receipt = save_syndicated_review(tmp_path, PAPER, parsed)
    actual = load_reviews(tmp_path, PAPER)[0]
    assert actual['match_basis'] == 'title_author' and actual['verification_method'] == 'syndication'
    assert actual['body_status'] == 'challenge' and actual['mirrors'][0]['body_status'] == 'readable'
    assert actual['blocks'] == [] and actual['summary'] == '' and actual['display_mode'] == 'source'
    assert 'match_text' not in actual and 'mirror_url' not in actual
    private = read(tmp_path / PRIVATE / 'reviews' / (receipt['id'] + '.json'))
    assert private['evidence']['blocks'] == parsed['blocks']


@pytest.mark.parametrize('change', [
    {'mirror_url': 'https://evil.example/53949'}, {'mirror_body_status': 'challenge'},
    {'evidence_source': 'search_snippet'}, {'match_text': ''},
    {'match_text': PAPER['title'] + ' by Different Author'},
])
def test_syndicated_review_requires_attributed_main_body_identity(tmp_path, change):
    article = {**ARTICLE, 'source_version': 'a' * 64, 'evidence_source': 'syndicated_article',
               'mirror_url': MIRROR['url'], 'mirror_kind': 'article', 'mirror_body_status': 'readable',
               'match_text': PAPER['title'] + ' by Aron Walsh'}
    with pytest.raises(ValueError):
        save_syndicated_review(tmp_path, PAPER, {**article, **change})
    assert not list((tmp_path / PUBLIC).glob('*.json'))


def test_mirrors_are_sanitized_and_newer_receipt_does_not_discard_existing_copy(tmp_path):
    from src.coverage_evidence import merge_review
    rows = public_mirrors([{**MIRROR, 'url': MIRROR['url'] + '?tracking=secret#private',
                           'raw_html': '<script>secret</script>', 'local_path': 'secret.png'},
                          {**MIRROR, 'url': 'https://hub.baai.ac.cn.evil.test/view/53949'},
                          {**MIRROR, 'url': 'https://user:secret@hub.baai.ac.cn/view/53949'}])
    assert rows == [MIRROR]
    receipt = save_manual_review(tmp_path, PAPER, {**ARTICLE, 'mirrors': rows}, EVIDENCE)
    assert receipt['article']['mirrors'] == rows
    newer = {**receipt['article'], 'verified_at': '2099-01-01T00:00:00+00:00',
             'verification_method': 'body', 'match_basis': 'doi', 'body_status': 'readable'}
    newer.pop('mirrors')
    merged = merge_review(newer, receipt['article'])
    assert merged['verification_method'] == 'body' and merged['mirrors'] == rows


def test_visual_worker_allows_only_the_attributed_mirror_image_host(tmp_path, monkeypatch):
    import io
    from PIL import Image
    from connectors.codex_bridge import coverage_queue
    from src.public_sources import allowed
    payload = io.BytesIO()
    Image.new('RGB', (2, 2), 'white').save(payload, format='PNG')
    fetched = []
    def fetch(url, source):
        assert allowed(url, source), 'Unapproved source host'
        fetched.append(url)
        return payload.getvalue()
    monkeypatch.setattr(coverage_queue, 'fetch', fetch)
    queue = coverage_queue.CoverageQueue(tmp_path, tmp_path, None, asyncio.Lock())
    image = {'remote_url': 'https://simg.baai.ac.cn/hub-detail/front-page.webp'}
    attributed = {**ARTICLE, 'evidence_source': 'syndicated_article', 'mirror_url': MIRROR['url']}
    file, digest = queue.image_file({'article': attributed}, image)
    assert file.exists() and len(digest) == 64 and fetched == [image['remote_url']]
    for invalid in (ARTICLE, {**attributed, 'mirror_url': 'https://unknown.test/view/53949'},
                    {**attributed, 'mirror_url': 'https://cloud.tencent.com/developer/article/2656311'}):
        with pytest.raises(AssertionError, match='Unapproved'):
            queue.image_file({'article': invalid}, image)
    with pytest.raises(AssertionError, match='Unapproved'):
        queue.image_file({'article': attributed}, {'remote_url': 'https://evil.example/front-page.webp'})


def test_real_source_manual_screenshot_confirms_identity_despite_wrong_journal_and_captcha(tmp_path):
    receipt = save_manual_review(tmp_path, PAPER, ARTICLE, EVIDENCE)
    actual = load_reviews(tmp_path, PAPER)[0]
    assert actual['match_basis'] == 'manual_screenshot'
    assert actual['body_status'] == 'challenge'
    assert actual['url'] == URL
    assert actual['account'] == 'Ai赋能材料'
    assert actual['summary'] == '' and actual['blocks'] == []
    assert receipt['id'] == review_key(PAPER['id'], URL, receipt['source_version'])
    private = read(tmp_path / PRIVATE / 'reviews' / (receipt['id'] + '.json'))
    assert private['evidence']['dois'] == [PAPER['doi']]


@pytest.mark.parametrize('change', [
    {'context': 'references'}, {'context': 'recommendation'}, {'context': 'unclear'},
    {'main_topic': False}, {'reference_only': True}, {'conflict': True}, {'legible': False},
    {'dois': ['10.1038/s42256-026-01262-5']},
    {'dois': [PAPER['doi'], '10.1234/unrelated']},
    {'title_complete': True, 'title': 'A completely different study of crystal diffusion'},
])
def test_unclear_reference_or_conflicting_images_cannot_confirm(change):
    assert match_evidence(PAPER, {**EVIDENCE, **change}) == ''


def test_title_and_author_fallback_rejects_same_title_different_authors():
    assert match_evidence(PAPER, {**EVIDENCE, 'dois': []}) == 'image_title_author'
    assert not match_evidence(PAPER, {**EVIDENCE, 'dois': [], 'authors': ['Different Park']})


def test_visual_author_accepts_citation_order_but_requires_all_initials():
    paper = {**PAPER, 'authors': ['King S.H.']}
    evidence = {**EVIDENCE, 'dois': [], 'authors': ['Samuel H. King']}
    assert match_evidence(paper, evidence) == 'image_title_author'
    assert not match_evidence(paper, {**evidence, 'authors': ['Samuel R. King']})
    assert not match_evidence(paper, {**evidence, 'authors': ['Samuel King']})
    assert not match_evidence({**paper, 'authors': ['Samuel Harry King']}, evidence)
    assert match_evidence({**paper, 'authors': ['Bülbül Emre F']},
                          {**evidence, 'authors': ['Emre F. Bülbül']}) == 'image_title_author'


def test_image_prompt_is_not_primed_with_target_paper_identity():
    prompt = extraction_prompt({'context_before': '研究介绍', 'context_after': ''})
    assert PAPER['doi'] not in prompt and PAPER['title'] not in prompt
    assert '不要猜测' in prompt and '不使用工具' in prompt


def test_review_projection_excludes_all_unapproved_body_and_private_evidence(tmp_path):
    receipt = save_manual_review(tmp_path, PAPER, ARTICLE, EVIDENCE)
    receipt.update(evidence=EVIDENCE, local_path='C:/secret.png')
    receipt['article'].update(summary='copied original article', blocks=[{'type': 'image', 'src': 'secret.png'}],
                              display_mode='reprint', transcript='DOI from screenshot')
    projected = public_review(receipt)
    assert 'evidence' not in projected and 'local_path' not in projected
    assert projected['article']['blocks'] == [] and projected['article']['summary'] == ''
    assert projected['article']['display_mode'] == 'source'
    assert 'transcript' not in projected['article']
    with pytest.raises(ValueError):
        public_review({**receipt, 'id': '../arbitrary'})


def test_new_source_version_supersedes_pending_task_and_completed_versions_are_not_reset(tmp_path):
    article = {**ARTICLE, 'source_version': 'a' * 64, 'blocks': []}
    first = enqueue_review(tmp_path, PAPER, article)
    second = enqueue_review(tmp_path, PAPER, {**article, 'source_version': 'b' * 64})
    assert first != second
    assert read(tmp_path / PRIVATE / 'tasks' / (first + '.json'))['state'] == 'superseded'
    second_path = tmp_path / PRIVATE / 'tasks' / (second + '.json')
    task = read(second_path)
    write(second_path, {**task, 'state': 'ready'})
    enqueue_review(tmp_path, PAPER, {**article, 'source_version': 'b' * 64})
    assert read(second_path)['state'] == 'ready'


class FakeClient:
    def __init__(self, evidence):
        self.evidence, self.prompts = evidence, []

    async def start(self):
        pass

    async def thread(self, **kwargs):
        return 'visual-thread'

    async def turn(self, thread, text, images):
        assert images
        self.prompts.append(text)
        yield {'type': 'delta', 'text': json.dumps(self.evidence)}
        yield {'type': 'completed', 'status': 'completed'}


def test_worker_checks_images_without_reference_pages_and_checkpoint_is_resumable(tmp_path):
    from connectors.codex_bridge.coverage_queue import CoverageQueue
    image = {'type': 'image', 'remote_url': 'https://mmbiz.qpic.cn/a.png',
             'position': 1, 'context_before': '研究介绍'}
    article = {**ARTICLE, 'body_status': 'readable', 'source_version': 'c' * 64,
               'blocks': [{**image, 'reference_section': True, 'position': 0}, image]}
    key = enqueue_review(tmp_path, PAPER, article)
    client = FakeClient(EVIDENCE)
    queue = CoverageQueue(tmp_path, tmp_path, client, asyncio.Lock())
    queue.image_file = lambda *_: (tmp_path / 'test.png', 'd' * 64)
    task = queue.next_task()
    asyncio.run(queue.process(task))
    assert len(client.prompts) == 1
    assert queue.next_task() is None
    approved = load_reviews(tmp_path, PAPER)
    assert approved[0]['match_basis'] == 'image_doi'
    stored = read(tmp_path / PRIVATE / 'tasks' / (key + '.json'))
    assert stored['state'] == 'ready'
    stored['state'] = 'pending'
    write(tmp_path / PRIVATE / 'tasks' / (key + '.json'), stored)
    asyncio.run(queue.process(stored))
    assert len(client.prompts) == 1  # Model output restored from the exact image/context checkpoint.


def test_worker_retries_failures_without_attempt_count_cap(tmp_path):
    from connectors.codex_bridge.coverage_queue import CoverageQueue
    article = {**ARTICLE, 'source_version': 'e' * 64, 'blocks': [{'type': 'image', 'remote_url': 'https://mmbiz.qpic.cn/a'}]}
    key = enqueue_review(tmp_path, PAPER, article)
    queue = CoverageQueue(tmp_path, tmp_path, FakeClient(EVIDENCE), asyncio.Lock())
    def fail(*_):
        raise ValueError('Network unavailable')
    queue.image_file = fail
    for _ in range(8):
        task = read(tmp_path / PRIVATE / 'tasks' / (key + '.json'))
        task['next_attempt'] = 0
        write(tmp_path / PRIVATE / 'tasks' / (key + '.json'), task)
        assert queue.next_task() is not None
        asyncio.run(queue.process(task))
    assert read(tmp_path / PRIVATE / 'tasks' / (key + '.json'))['state'] == 'retry'


def test_worker_publish_and_ci_apply_receipt_preserve_link_during_access_failure(tmp_path):
    from connectors.codex_bridge.coverage_queue import CoverageQueue
    from tools.import_coverage_reviews import apply_reviews
    write(tmp_path / 'data/daily.json', {'core': [PAPER]})
    receipt = save_manual_review(tmp_path, PAPER, ARTICLE, EVIDENCE)
    write(tmp_path / 'data/paper-coverage.json', {'papers': {PAPER['id']: {
        'state': 'unavailable', 'candidates': [{'url': URL, 'reason': 'article_challenge'}], 'articles': []}}})
    sent = []
    queue = CoverageQueue(tmp_path, tmp_path, FakeClient(EVIDENCE), asyncio.Lock(), publisher=lambda *args: sent.append(args))
    queue.publish_ready()
    queue.publish_ready()
    assert len(sent) == 1 and sent[0][2:] == ('coverage-reviews', 'id')
    assert sent[0][1] == [receipt]
    assert apply_reviews(tmp_path) == 1
    record = read(tmp_path / 'data/paper-coverage.json')['papers'][PAPER['id']]
    assert record['state'] == 'matched' and record['candidates'] == []
    assert record['articles'][0]['body_status'] == 'challenge'
    assert record['articles'][0]['url'] == URL


def test_worker_recovers_remote_candidate_even_with_existing_local_negative(tmp_path, monkeypatch):
    from connectors.codex_bridge.coverage_queue import CoverageQueue
    from src import paper_coverage
    write(tmp_path / 'data/daily.json', {'core': [PAPER]})
    write(tmp_path / 'data/paper-coverage.json', {'papers': {PAPER['id']: {'state': 'no_match', 'candidates': []}}})
    remote = {'papers': {PAPER['id']: {'state': 'pending_match', 'candidates': [{**ARTICLE, 'reason': 'visual_pending'}]}}}
    monkeypatch.setattr(paper_coverage, 'fetch_coverage', lambda *_: b'body')
    monkeypatch.setattr(paper_coverage, 'parse_article', lambda *_: {**ARTICLE, 'source_version': 'f' * 64,
                                                                 'blocks': [{'type': 'image'}]})
    queue = CoverageQueue(tmp_path, tmp_path, FakeClient(EVIDENCE), asyncio.Lock(),
                          fetcher=lambda path: remote if path == 'paper-coverage.json' else {'core': [PAPER]})
    queue.sync()
    assert queue.next_task()['paper']['id'] == PAPER['id']


def test_worker_recovers_mirror_images_when_wechat_is_challenged_and_new_copy_bypasses_retry(tmp_path, monkeypatch):
    from connectors.codex_bridge.coverage_queue import CoverageQueue
    from src import paper_coverage
    write(tmp_path / 'data/daily.json', {'core': [PAPER]})
    candidate = {**ARTICLE, 'reason': 'visual_pending', 'mirrors': [dict(MIRROR)]}
    remote = {'papers': {PAPER['id']: {'state': 'pending_match', 'candidates': [candidate]}}}
    fetched = []
    def cached(root, url, now, fetcher):
        fetched.append(url)
        return [{**ARTICLE, 'source_version': ('a' if len(fetched) == 1 else 'b') * 64,
                 'evidence_source': 'syndicated_article', 'mirror_url': url, 'body_status': 'pending',
                 'blocks': [{'type': 'image', 'remote_url': 'https://simg.baai.ac.cn/a.png'}]}], 'ok', now.isoformat()
    def reject_original(*_):
        raise AssertionError('A readable attributed mirror does not require the challenged original')
    monkeypatch.setattr(paper_coverage, '_cached_syndication', cached)
    monkeypatch.setattr(paper_coverage, 'fetch_coverage', reject_original)
    queue = CoverageQueue(tmp_path, tmp_path, FakeClient(EVIDENCE), asyncio.Lock(),
                          fetcher=lambda path: remote if path == 'paper-coverage.json' else {'core': [PAPER]})
    queue.sync()
    task = queue.next_task()
    assert task['article']['mirrors'] == [MIRROR]
    assert task['article']['body_status'] == 'challenge'
    queue.sync()
    assert len(fetched) == 1
    candidate['mirrors'][0]['checked_at'] = '2026-10-16T10:00:00+08:00'
    queue.sync()
    assert len(fetched) == 2
    assert queue.next_task()['article']['source_version'] == 'b' * 64


def test_importing_older_manual_receipt_preserves_newer_body_verification(tmp_path):
    from datetime import datetime, timedelta
    from tools.import_coverage_reviews import apply_reviews
    write(tmp_path / 'data/daily.json', {'core': [PAPER]})
    receipt = save_manual_review(tmp_path, PAPER, ARTICLE, EVIDENCE)
    newer = (datetime.fromisoformat(receipt['article']['verified_at']) + timedelta(hours=1)).isoformat()
    summary = '根据可读取的公众号正文整理了该论文的研究介绍，详细实验结果和研究结论请参阅论文以及公众号原文。'
    body = {**receipt['article'], 'match_basis': 'doi', 'verification_method': 'body',
            'body_status': 'readable', 'verified_at': newer, 'source_version': 'b' * 64,
            'summary': summary, 'summary_status': 'ready', 'summary_content_version': 'c' * 64}
    write(tmp_path / 'data/paper-coverage.json', {'papers': {PAPER['id']: {
        'state': 'matched', 'verified_at': newer, 'articles': [body], 'candidates': []}}})
    apply_reviews(tmp_path)
    apply_reviews(tmp_path)
    record = read(tmp_path / 'data/paper-coverage.json')['papers'][PAPER['id']]
    actual = record['articles'][0]
    assert actual['body_status'] == 'readable'
    assert actual['match_basis'] == 'doi' and actual['verification_method'] == 'body'
    assert actual['verified_at'] == record['verified_at'] == newer
    assert actual['source_version'] == 'b' * 64
    assert actual['summary_status'] == 'ready' and actual['summary'] == summary


def test_changed_image_bytes_at_same_url_create_new_visual_review_without_repeat_approval(tmp_path):
    from connectors.codex_bridge.coverage_queue import CoverageQueue
    image = {'type': 'image', 'remote_url': 'https://mmbiz.qpic.cn/mutable.png', 'position': 0}
    article = {**ARTICLE, 'body_status': 'readable', 'source_version': 'a' * 64, 'blocks': [image]}
    discovery_id = enqueue_review(tmp_path, PAPER, article)
    client = FakeClient({**EVIDENCE, 'legible': False})
    queue = CoverageQueue(tmp_path, tmp_path, client, asyncio.Lock())
    image_hash = ['b' * 64]
    queue.image_file = lambda *_: (tmp_path / 'test.png', image_hash[0])
    asyncio.run(queue.process(queue.next_task()))
    probe_path = tmp_path / PRIVATE / 'tasks' / (discovery_id + '.json')
    first = read(probe_path)
    assert first['state'] == 'unconfirmed' and len(client.prompts) == 1
    assert queue.next_task() is None
    # Next-day probing of unchanged bytes reuses the indeterminate result.
    write(probe_path, {**first, 'next_attempt': 0})
    asyncio.run(queue.process(queue.next_task()))
    assert len(client.prompts) == 1
    # Same HTML and image URL, but now a clear paper screenshot is served.
    image_hash[0] = 'c' * 64
    client.evidence = EVIDENCE
    write(probe_path, {**read(probe_path), 'next_attempt': 0})
    asyncio.run(queue.process(queue.next_task()))
    updated = read(probe_path)
    assert updated['state'] == 'ready' and len(client.prompts) == 2
    assert updated['review_key'] != first['review_key']
    receipt = load_reviews(tmp_path, PAPER)[0]
    assert receipt['source_version'] == updated['material_version'] != article['source_version']
    assert updated['review_key'] == review_key(PAPER['id'], URL, receipt['source_version'])
    before = list((tmp_path / PUBLIC).glob('*.json'))[0].read_bytes()
    # Confirmed versions are rechecked after cache expiry without generating a
    # new approval timestamp or repeatedly republishing the same receipt.
    write(probe_path, {**updated, 'next_attempt': 0})
    asyncio.run(queue.process(queue.next_task()))
    assert len(client.prompts) == 2
    assert list((tmp_path / PUBLIC).glob('*.json'))[0].read_bytes() == before
