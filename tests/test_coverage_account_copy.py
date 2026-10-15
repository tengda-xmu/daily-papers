from copy import deepcopy
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import pytest

from src import paper_coverage as coverage
from src.coverage_evidence import (PRIVATE, PUBLIC, load_reviews, match_evidence, public_review,
                                   review_key, save_public_account_copy_review)
from src.public_sources import read, write
from tests.test_paper_coverage import NOW, PAPER, setup
from tests.test_companion_reconnect_browser import browser
from tests.test_reading_queue import sample
from tools.build_site import render
from tools.coverage_pages import coverage_panel, reprint_available
from tools.import_coverage_reviews import apply_reviews


COPY = {'url': 'https://www.python88.com/topic/198001', 'title': '机器学习驱动材料设计',
        'account': '人工智能化学与材料', 'source_version': 'a' * 64, 'body_status': 'readable'}
EVIDENCE = {'dois': [PAPER['doi']], 'title': PAPER['title'], 'authors': PAPER['authors'],
            'legible': True, 'context': 'paper_front_page', 'main_topic': True,
            'reference_only': False, 'conflict': False, 'title_complete': True}
BRAND = {'account': COPY['account'], 'legible': True, 'main_brand': True, 'non_generic': True,
         'context': 'article_footer_brand', 'source_url': COPY['url'], 'image_sha256': 'b' * 64,
         'transcript': '公众号：人工智能化学与材料', 'provenance': 'Read article footer image directly'}


def approve(root):
    return save_public_account_copy_review(root, PAPER, COPY, EVIDENCE, BRAND)


def test_copy_review_keeps_private_proof_and_publishes_only_typed_reading_link(tmp_path):
    receipt = approve(tmp_path)
    row, = load_reviews(tmp_path, PAPER)
    assert row['url'] == COPY['url'] and row['source_type'] == 'public_account_copy'
    assert row['original_url_status'] == 'unlocated' and row['match_basis'] == 'image_doi'
    assert row['display_mode'] == 'source' and not row['blocks'] and not row['summary']
    assert receipt['id'] == review_key(PAPER['id'], COPY['url'], receipt['source_version'], source_type='public_account_copy')
    private = read(tmp_path / PRIVATE / 'reviews' / (receipt['id'] + '.json'))
    assert private['account_evidence'] == BRAND and private['evidence'] == EVIDENCE
    assert 'transcript' not in str(receipt) and 'image_sha256' not in str(receipt)
    assert not reprint_available({**row, 'display_mode': 'reprint', 'blocks': [{'type': 'paragraph', 'text': 'Copy'}]})


@pytest.mark.parametrize('change', [
    {'legible': False}, {'main_brand': False}, {'non_generic': False}, {'account': 'Other account'},
    {'context': 'recommended_article'}, {'source_url': 'https://www.python88.com/topic/999999'},
    {'image_sha256': ''}, {'transcript': 'A username'}, {'provenance': ''},
])
def test_uploader_name_or_unreviewed_brand_cannot_confirm(tmp_path, change):
    with pytest.raises(ValueError):
        save_public_account_copy_review(tmp_path, PAPER, COPY, EVIDENCE, {**BRAND, **change})
    assert not list((tmp_path / PUBLIC).glob('*.json'))


@pytest.mark.parametrize('change', [{'dois': ['10.1234/wrong']}, {'reference_only': True}, {'context': 'references'}])
def test_copy_brand_does_not_replace_paper_identity(tmp_path, change):
    with pytest.raises(ValueError):
        save_public_account_copy_review(tmp_path, PAPER, COPY, {**EVIDENCE, **change}, BRAND)


def test_unknown_copy_flag_without_receipt_never_becomes_public(tmp_path):
    setup(tmp_path)
    row = {**COPY, 'source_type': 'public_account_copy', 'original_url_status': 'unlocated',
           'match_status': 'verified', 'match_basis': 'image_doi', 'verification_method': 'account_copy_review',
           'account_verification': 'visual_brand', 'review_id': 'c' * 64}
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'matched', 'articles': [row], 'candidates': []}}})
    assert not coverage.load_public(tmp_path)[PAPER['id']]['articles']
    candidate = coverage._public_candidate(row)
    assert candidate['match_status'] == 'pending' and 'source_type' not in candidate


def test_copy_import_and_collect_survive_read_failure_without_new_search(tmp_path):
    setup(tmp_path)
    approve(tmp_path)
    apply_reviews(tmp_path)
    assert coverage.load_public(tmp_path)[PAPER['id']]['articles'][0]['url'] == COPY['url']
    row = coverage.collect(tmp_path, NOW, perform_search=False, fetcher=lambda *_: b'<html>captcha required</html>',
                           summarizer=lambda *_: None)['papers'][PAPER['id']]
    assert row['state'] == 'matched' and row['articles'][0]['body_status'] == 'challenge'
    assert row['articles'][0]['source_type'] == 'public_account_copy'
    assert not row['candidates']


def test_copy_entry_directly_reads_sync_without_calling_it_wechat_original(tmp_path):
    article = approve(tmp_path)['article']
    entry, panel = coverage_panel(PAPER['id'], {'articles': [article]})
    link = BeautifulSoup(entry, 'html.parser').select_one('.coverage-link')
    assert link['href'] == COPY['url'] and link['target'] == '_blank'
    assert link.get_text() == '公众号介绍（公开同步）'
    assert '微信原文链接待定位' in link['title']
    assert not panel and 'iframe' not in entry


def test_copy_public_receipt_drops_body_even_if_tampered(tmp_path):
    receipt = approve(tmp_path)
    forged = deepcopy(receipt)
    forged['article'].update(display_mode='reprint', blocks=[{'type': 'paragraph', 'text': 'PRIVATE'}],
                              transcript='PRIVATE', summary='PRIVATE')
    public = public_review(forged)
    assert public['article']['display_mode'] == 'source'
    assert 'PRIVATE' not in str(public)


def test_old_review_key_stays_byte_compatible():
    url = 'https://mp.weixin.qq.com/s/example'
    expected = hashlib.sha256(f"{PAPER['id']}\n{url}\n{'a' * 64}\nv1".encode()).hexdigest()
    assert review_key(PAPER['id'], url, 'a' * 64) == expected


def test_placeholder_author_cannot_confirm_same_title():
    paper = {**PAPER, 'doi': '', 'authors': ['Unaffiliated']}
    assert not match_evidence(paper, {**EVIDENCE, 'dois': [], 'authors': ['Unaffiliated']})


def test_visual_entry_uses_title_bound_doi_alias(tmp_path, monkeypatch):
    from src import paper_identity
    path = tmp_path / 'identities.json'
    write(path, {'entries': [{'paper_ids': [PAPER['id']], 'title': PAPER['title'], 'doi': PAPER['doi']}]})
    monkeypatch.setattr(paper_identity, 'CATALOG', path)
    assert match_evidence({**PAPER, 'doi': ''}, {**EVIDENCE, 'title': '', 'title_complete': False, 'authors': []}) == 'image_doi'


@pytest.mark.parametrize('width', [390, 1440])
def test_copy_home_archive_popup_and_layout(tmp_path, browser, width):
    root = Path(__file__).resolve().parents[1]
    paper = sample()
    source = approve(tmp_path)['article']
    source['title'] *= 6
    sources = {paper['id']: {'articles': [source]}}
    pages = {'/index.html': render({'core': [paper]}, coverage=sources),
             '/archive/example.html': render({'core': [paper]}, archive_date='2026-10-15', coverage=sources)}
    context = browser.new_context(viewport={'width': width, 'height': 920})
    def route(request):
        parsed = urlsplit(request.request.url)
        if parsed.hostname == 'www.python88.com':
            request.fulfill(body='<h1>Readable public account copy</h1>', content_type='text/html')
        elif parsed.path in pages:
            request.fulfill(body=pages[parsed.path], content_type='text/html')
        elif '/assets/' in parsed.path:
            path = root / 'tools/assets' / parsed.path.split('/assets/', 1)[1]
            if path.is_file():
                request.fulfill(body=path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            else:
                request.abort()
        else:
            request.abort()
    context.route('**/*', route)
    page = context.new_page()
    try:
        for path in pages:
            page.goto('https://site.example.org' + path, wait_until='networkidle')
            link = page.locator('.coverage-link')
            assert link.inner_text() == '公众号介绍（公开同步）'
            assert '微信原文链接待定位' in link.get_attribute('title')
            actions = page.locator('.paper-tools > *').all_text_contents()
            assert next(i for i, text in enumerate(actions) if '公众号介绍' in text) < next(i for i, text in enumerate(actions) if 'Codex 对话' in text)
            with page.expect_popup() as opened:
                link.click()
            opened.value.wait_for_load_state()
            assert opened.value.url == COPY['url']
            opened.value.close()
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
    finally:
        context.close()
