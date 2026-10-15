"""Synthetic fixtures preserve the observed publisher structures, not article bodies."""
import json

import pytest

from src.coverage_syndication import (
    canonical_syndication_url, fetch_syndication, is_syndication_url, parse_syndication,
)
from src.paper_coverage import ArticleChallenge, match_article

TENCENT = 'https://cloud.tencent.com/developer/article/2656311'
BAAI = 'https://hub.baai.ac.cn/view/53949'
SYNAPSE = 'https://synapse.zhihuiya.com/organization/b53ea946752160f3c870a15e059df21f'
MP = 'https://mp.weixin.qq.com/s/synthetic-report'
OTHER_MP = 'https://mp.weixin.qq.com/s/other-report'
PAPER = {'doi': '10.1234/synthetic',
         'title': 'Structural optimization of synthetic molecules with language models',
         'authors': ['Alice Example']}
TEXT = 'This report introduces the primary research and its experimental limitations. '
IMAGE = 'https://mmbiz.qpic.cn/mmbiz_png/synthetic/640'


def tencent(blocks=None, **source):
    content = {'blocks': blocks or [{'type': 'unstyled', 'text': TEXT + ' DOI: 10.1234/synthetic'}],
               'entityMap': {'0': {'type': 'IMAGE', 'data': {
                   'imageUrl': 'https://developer.qcloudimg.com/synthetic.png'}}}}
    data = {'props': {'pageProps': {'fallback': {'article-detail': {
        'articleInfo': {'title': 'Synthetic introduction', 'content': content},
        'sourceDetail': {'sourceLink': MP, 'wechatNickName': 'Research account',
                         'sourceAuthor': 'Article author', 'originalTime': '2026-04-14', **source}}}}}}
    return ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>').encode()


def baai(content=None, original=MP):
    content = content or '<p>' + TEXT + '</p><img src="' + IMAGE + '">'
    post = '{title:a,url:' + json.dumps(original) + ',content:' + json.dumps(content) + ',created_at:b,user_name:c}'
    args = ','.join(json.dumps(value) for value in ['Synthetic introduction', '2026-04-15 10:50', 'Unverified uploader'])
    script = 'window.__NUXT__=(function(a,b,c){return {data:[{post_data:' + post + '}]}}(' + args + '));'
    script = script.replace('</', '<\\/')
    return ('<script>' + script + '</script><div id="post-content"><div id="js_content">'
            + content + '</div></div>').encode()


def card(title, text, url=MP, account='Research account'):
    return ('<div data-testid="news-document-card"><span>2026-05-11</span><a href="' + url + '">'
            + account + '</a><a data-testid="news-document-card__title" href="' + url + '">'
            + title + '</a><div data-testid="news-document-card__content">' + text + '</div></div>')


@pytest.mark.parametrize('url', [TENCENT, BAAI, SYNAPSE])
def test_exact_public_page_allowlist(url):
    assert is_syndication_url(url)
    assert canonical_syndication_url(url + '?utm_source=search#part') == url
    assert not is_syndication_url(url + '?redirect=https://evil.example')


@pytest.mark.parametrize('url', [
    'http://hub.baai.ac.cn/view/53949', 'https://hub.baai.ac.cn.evil.example/view/53949',
    'https://user@hub.baai.ac.cn/view/53949', 'https://hub.baai.ac.cn:444/view/53949',
    'https://127.0.0.1/view/53949', 'https://cloud.tencent.com/developer/api/proxy',
    'https://hub.baai.ac.cn/view/../admin', 'https://hub.baai.ac.cn/view/%35',
    'https://hub.baai.ac.cn/view/53949\n', 'https://hub.baai.ac.cn/view/53949?x=1\n',
])
def test_unsafe_url_rejected_without_fetch(url, monkeypatch):
    def forbidden(*args):
        raise AssertionError('No network for unsafe URL')
    monkeypatch.setattr('src.coverage_syndication.build_opener', forbidden)
    assert not canonical_syndication_url(url)
    with pytest.raises(ValueError):
        fetch_syndication(url)


def test_tencent_explicit_source_metadata_and_draft_body():
    row, = parse_syndication(tencent(), TENCENT)
    assert row['url'] == MP
    assert row['account'] == 'Research account' and row['author'] == 'Article author'
    assert row['published_at'].startswith('2026-04-14')
    assert row['mirror_kind'] == 'article' and row['mirror_body_status'] == 'readable'
    assert row['evidence_source'] == 'syndicated_article'
    assert len(row['source_version']) == 64
    assert match_article(PAPER, row) == 'doi'
    assert 'original_body_status' not in row


@pytest.mark.parametrize('source', ['https://evil.example/article', '', 'javascript:alert(1)'])
def test_tencent_needs_explicit_wechat_source_not_only_same_topic(source):
    assert parse_syndication(tencent(sourceLink=source), TENCENT) == []


def test_tencent_reference_doi_cannot_confirm_and_images_remain_ordered():
    rows = [{'type': 'unstyled', 'text': TEXT},
            {'type': 'atomic', 'entityRanges': [{'key': 0}]},
            {'type': 'header-two', 'text': '参考资料'},
            {'type': 'unstyled', 'text': '10.1234/synthetic'},
            {'type': 'atomic', 'entityRanges': [{'key': 0}]}]
    row, = parse_syndication(tencent(rows), TENCENT)
    assert match_article(PAPER, row) == ''
    images = [block for block in row['blocks'] if block['type'] == 'image']
    assert len(images) == 2
    assert not images[0]['reference_section'] and images[1]['reference_section']
    assert 'primary research' in images[0]['context_before']
    assert images[0]['position'] < images[1]['position']


def test_baai_data_only_nuxt_bindings_and_intro_image():
    row, = parse_syndication(baai(), BAAI)
    assert row['url'] == MP and row['title'] == 'Synthetic introduction'
    assert row['image_count'] == 1
    assert row['blocks'][1]['remote_url'] == IMAGE
    assert match_article(PAPER, row) == ''  # image identity still requires review
    assert row['published_at'] == '' and row['account'] == ''  # no invented metadata


def test_baai_content_string_fallback_is_parsed_without_browser():
    page = baai('<p>' + TEXT + ' 10.1234/synthetic</p>')
    page = page[:page.index(b'<div id="post-content">')]
    row, = parse_syndication(page, BAAI)
    assert match_article(PAPER, row) == 'doi'


def test_baai_computed_javascript_not_evaluated():
    page = baai().replace(b'url:"https://mp.weixin.qq.com/s/synthetic-report"', b'url:evilFunction()')
    with pytest.raises(ValueError):
        parse_syndication(page, BAAI)


def test_baai_body_scripts_and_unapproved_images_never_become_evidence():
    content = ('<p>' + TEXT + '</p><script>10.1234/synthetic</script>'
               '<form>10.1234/synthetic</form><img src="https://evil.example/a.png">')
    row, = parse_syndication(baai(content), BAAI)
    assert match_article(PAPER, row) == ''
    assert not any(block['type'] == 'image' for block in row['blocks'])


def test_synapse_source_is_bound_to_each_card_not_neighboring_article():
    page = card('Different report', TEXT, OTHER_MP)
    page += card('Chinese rewritten title', TEXT + '\n' + PAPER['title'] + ' Alice Example')
    page += card('Unapproved link', TEXT + PAPER['doi'], 'https://evil.example')
    rows = parse_syndication(page.encode(), SYNAPSE)
    assert len(rows) == 2
    assert match_article(PAPER, rows[0]) == ''
    assert match_article(PAPER, rows[1]) == 'title_author'
    assert rows[1]['url'] == MP and rows[1]['account'] == 'Research account'
    assert rows[1]['mirror_kind'] == 'collection'
    assert rows[1]['published_at'].startswith('2026-05-11')


@pytest.mark.parametrize('heading', ['参考文献', '参考资料', '参考链接：', 'References'])
def test_synapse_newline_references_do_not_verify_identity(heading):
    text = TEXT + '\n' + heading + '\n' + PAPER['title'] + ' Alice Example 10.1234/synthetic'
    row, = parse_syndication(card('Other research', text).encode(), SYNAPSE)
    assert match_article(PAPER, row) == ''


def test_synapse_same_title_different_author_does_not_confirm():
    row, = parse_syndication(card('Research', TEXT + PAPER['title'] + ' Different Author').encode(), SYNAPSE)
    assert match_article(PAPER, row) == ''


def test_synapse_image_order_survives_linebreak_restoration():
    content = TEXT + '\n<img src="' + IMAGE + '">\nMethods and results\n参考资料\n<img src="' + IMAGE + '">'
    row, = parse_syndication(card('Research', content).encode(), SYNAPSE)
    images = [block for block in row['blocks'] if block['type'] == 'image']
    assert len(images) == 2
    assert not images[0]['reference_section'] and images[1]['reference_section']


def test_oversize_invalid_and_unsupported_pages_fail_closed():
    with pytest.raises(ValueError):
        parse_syndication(b'x' * 4_000_001, BAAI)
    with pytest.raises(ValueError):
        parse_syndication(b'<article>hello</article>', 'https://evil.example')
    with pytest.raises(ValueError):
        parse_syndication(b'<html>Unknown new page layout</html>', TENCENT)


@pytest.mark.parametrize('url', [TENCENT, BAAI, SYNAPSE])
@pytest.mark.parametrize('message', ['Login required', '验证码', 'captcha'])
def test_challenge_is_not_an_empty_success(url, message):
    with pytest.raises(ArticleChallenge):
        parse_syndication(('<html>' + message + '</html>').encode(), url)


def test_unrelated_next_data_is_not_an_empty_success():
    with pytest.raises(ValueError):
        parse_syndication(b'<script id="__NEXT_DATA__">{"props":{}}</script>', TENCENT)


def test_tencent_cached_other_article_is_not_this_readable_mirror():
    page = tencent().replace(b'"title": "Synthetic introduction"', b'"articleId": 999, "title": "Synthetic introduction"')
    with pytest.raises(ValueError):
        parse_syndication(page, TENCENT)


def test_baai_id_mismatch_is_not_this_readable_mirror():
    page = baai().replace(b'post_data:{title:', b'post_data:{id:999,title:')
    with pytest.raises(ValueError):
        parse_syndication(page, BAAI)
