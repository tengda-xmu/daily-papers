from copy import deepcopy
import hashlib
import json

import pytest

from src.coverage_evidence import (PRIVATE, PUBLIC, load_linked_report_reviews, load_reviews,
                                   public_review, save_linked_report_review)
from src import paper_coverage as coverage
from src.public_sources import read, write


URL = 'https://mp.weixin.qq.com/s/F1d79roHgkwomxsdC57tBg'
PAPER = {'id': '229ee813a122', 'doi': '10.1126/sciadv.aef6263',
         'title': 'Deep Learning-Enabled Versatile Shape Perception for Soft Robots via Single-Ended Multimode Fiber',
         'authors': ['Zhaofan He', 'Qirong Xiao']}
HEADLINE = '先进激光技术团队报道深度学习赋能单端多模光纤，实现软体机器人多样形态感知'
TITLE = '科研风采第五十七期 | Sci. Adv.发文：' + HEADLINE
ARTICLE = {'url': URL, 'title': TITLE, 'account': '', 'body_status': 'challenge'}
INDEX = {'url': 'https://www.dpi.tsinghua.edu.cn/',
         'html': '<html><li><a href="' + URL + '">' + TITLE + '</a></li></html>',
         'fetched_at': '2026-10-15T10:00:00+00:00', 'provenance': 'Read official department index DOM'}
BODY = '<p>The team presents ' + PAPER['title'] + ' by Zhaofan He and Qirong Xiao.</p><p>DOI: ' + PAPER['doi'] + '</p>'
REPORT = {'url': 'https://faculty.dpi.tsinghua.edu.cn/xiaoqirong/zh_CN/article/12623/content/2912.htm',
          'html': '<html><h2>Science Advances发文：' + HEADLINE + '</h2><article>' + BODY + '</article></html>',
          'title_selector': 'h2', 'body_selector': 'article',
          'fetched_at': '2026-10-15T10:01:00+00:00', 'provenance': 'Read corresponding author institutional report'}


def approve(root, article=None, index=None, report=None, paper=None):
    return save_linked_report_review(root, paper or PAPER, article or ARTICLE,
                                    index or INDEX, report or REPORT)


def test_official_link_confirms_identity_while_account_and_wechat_body_remain_unknown(tmp_path):
    receipt = approve(tmp_path, article={**ARTICLE, 'published_at': '2026-06-14',
        'blocks': [{'type': 'paragraph', 'text': 'UNAUTHORIZED BODY'}], 'summary': 'PRIVATE SUMMARY'})
    row, = load_reviews(tmp_path, PAPER)
    assert row['url'] == URL and row['title'] == TITLE
    assert row['match_basis'] == 'doi' and row['verification_method'] == 'official_crosslink'
    assert row['account'] == '' and row['account_status'] == 'not_observed'
    assert row['published_at'] == '' and row['body_status'] == 'challenge'
    assert row['display_mode'] == 'source' and row['blocks'] == [] and row['summary'] == ''
    assert row['mirrors'] == [{'url': REPORT['url'], 'body_status': 'readable', 'kind': 'article',
                               'checked_at': REPORT['fetched_at']}]
    assert '高校官网索引' in row['source_note'] and '公众号名称尚未直接读取' in row['source_note']
    assert load_linked_report_reviews(tmp_path)[(PAPER['id'], URL)] == row
    proof = read(tmp_path / PRIVATE / 'reviews' / (receipt['id'] + '.json'))
    assert proof['index_evidence']['html'] == INDEX['html']
    assert proof['report_evidence']['html'] == REPORT['html']
    assert proof['material']['hashes']['index'] == hashlib.sha256(INDEX['html'].encode()).hexdigest()
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert not any(value in serialized for value in ('UNAUTHORIZED BODY', 'PRIVATE SUMMARY', 'match_text', 'provenance', 'body_selector'))
    assert INDEX['url'] not in serialized


@pytest.mark.parametrize('change', [
    {'html': ''}, {'html': True}, {'html': '<script>' + INDEX['html'] + '</script>'},
    {'html': INDEX['html'].replace('<a ', '<a hidden ')},
    {'html': INDEX['html'].replace('<li>', '<li style="display:none">')},
    {'html': INDEX['html'].replace(URL, 'https://mp.weixin.qq.com/s/OTHER')},
    {'html': INDEX['html'].replace(TITLE, '相关公众号文章')},
    {'fetched_at': ''}, {'provenance': ''},
    {'url': 'https://tsinghua.edu.cn.attacker.com/index.html'},
])
def test_flags_scripts_hidden_links_and_unread_pages_do_not_prove_crosslink(tmp_path, change):
    with pytest.raises(ValueError):
        approve(tmp_path, index={**INDEX, **change})
    assert not list((tmp_path / PUBLIC).glob('*.json'))


@pytest.mark.parametrize('change', [
    {'url': 'https://faculty.other.edu.cn/article/2912.htm'},
    {'html': REPORT['html'].replace(HEADLINE, HEADLINE + '后续课题')},
    {'html': '<h2>Science Advances发文：' + HEADLINE + '</h2><article>' + ('No identity here. ' * 20) + '</article>'},
    {'html': REPORT['html'].replace('<article>', '<article><h3>参考文献</h3>')},
    {'html': REPORT['html'].replace('<article>', '<article><p>相关阅读</p>')},
    {'html': REPORT['html'].replace('<article>', '<article><aside>').replace('</article>', '</aside></article>')},
    {'html': REPORT['html'].replace('<article>', '<article><section class="references">').replace('</article>', '</section></article>')},
    {'body_selector': 'body'}, {'body_selector': 'does-not-exist'}, {'body_selector': '['},
    {'title_selector': 'article'}, {'title_selector': '['}, {'fetched_at': ''},
])
def test_same_institution_report_headline_and_main_body_identity_are_required(tmp_path, change):
    with pytest.raises(ValueError):
        approve(tmp_path, report={**REPORT, **change})
    assert not list((tmp_path / PUBLIC).glob('*.json'))


def test_title_author_match_requires_actual_author_in_main_body(tmp_path):
    no_doi = {**REPORT, 'html': REPORT['html'].replace(PAPER['doi'], '')}
    receipt = approve(tmp_path, report=no_doi)
    assert receipt['article']['match_basis'] == 'title_author'
    wrong_authors = {**PAPER, 'authors': ['Different Person']}
    with pytest.raises(ValueError):
        approve(tmp_path, report=no_doi, paper=wrong_authors)


def test_institution_name_cannot_become_unobserved_account(tmp_path):
    with pytest.raises(ValueError):
        approve(tmp_path, article={**ARTICLE, 'account': '清华大学精密仪器系'})


@pytest.mark.parametrize('change', [
    {'review_id': ''}, {'account': 'Guessed institution account'}, {'account_status': 'observed'},
    {'mirrors': []}, {'mirrors': [{'url': 'https://example.com/article/2912', 'body_status': 'readable',
                                  'kind': 'article', 'checked_at': REPORT['fetched_at']}]},
])
def test_crosslink_receipt_projection_rejects_missing_review_or_attribution_tampering(tmp_path, change):
    receipt = approve(tmp_path)
    changed = deepcopy(receipt)
    changed['article'].update(change)
    with pytest.raises(ValueError):
        public_review(changed)


def test_evidence_version_changes_when_source_dom_changes(tmp_path):
    first = approve(tmp_path)
    second = approve(tmp_path, index={**INDEX, 'html': INDEX['html'] + '<!-- next fetched version -->'})
    assert first['id'] != second['id'] and first['source_version'] != second['source_version']


def test_public_projection_rejects_crosslink_flag_without_independent_receipt(tmp_path):
    write(tmp_path / 'data/daily.json', {'core': [PAPER], 'extended': []})
    forged = {**ARTICLE, 'verification_method': 'official_crosslink', 'match_status': 'verified',
              'match_basis': 'doi', 'source_version': 'a' * 64, 'review_id': 'b' * 64,
              'account_status': 'not_observed'}
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'matched', 'articles': [forged]}}})
    assert coverage.load_public(tmp_path)[PAPER['id']]['articles'] == []


def test_public_projection_requires_receipt_metadata_and_never_accepts_injected_body(tmp_path):
    write(tmp_path / 'data/daily.json', {'core': [PAPER], 'extended': []})
    row = approve(tmp_path)['article']
    forged = {**row, 'title': 'INJECTED TITLE', 'account': 'INJECTED ACCOUNT',
              'body_status': 'unavailable', 'display_mode': 'reprint',
              'blocks': [{'type': 'paragraph', 'text': 'UNAUTHORIZED BODY'}]}
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'matched', 'articles': [forged]}}})
    projected, = coverage.load_public(tmp_path)[PAPER['id']]['articles']
    assert projected['title'] == TITLE and projected['account'] == ''
    assert projected['body_status'] == 'unavailable'
    assert projected['display_mode'] == 'source' and projected['blocks'] == []
    forged['source_version'] = 'c' * 64
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'matched', 'articles': [forged]}}})
    projected, = coverage.load_public(tmp_path)[PAPER['id']]['articles']
    assert projected['body_status'] == 'challenge'
