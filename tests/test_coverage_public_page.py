import socket

import pytest

from src.coverage_public_page import public_page_url, require_public_host
from src.coverage_syndication import parse_syndication, canonical_syndication_url
from src.paper_coverage import match_article, ArticleChallenge

URL = 'https://research.example.org/news/report?id=19'
MP = 'https://mp.weixin.qq.com/s/synthetic-report'
PAPER = {'doi': '10.1234/synthetic', 'title': 'Synthetic fatigue prediction with independent measurements',
         'authors': ['Alice Example']}


def page(body='', source=None):
    source = source if source is not None else '<p>来源：<a href="' + MP + '">材料研究</a></p>'
    return ('<h1>人工智能辅助疲劳寿命研究</h1>' + source + '<article>'
            + body + '</article>').encode()


def test_other_domain_explicit_account_and_main_paper_identity():
    rows = parse_syndication(page('<p>本文介绍了一项新的实验研究。' * 8
                                  + ' DOI: 10.1234/synthetic</p>'), URL)
    assert len(rows) == 1
    row = rows[0]
    assert row['url'] == MP and row['account'] == '材料研究'
    assert row['mirror_url'] == URL and row['published_at'] == ''
    assert match_article(PAPER, row) == 'doi'


def test_body_without_wechat_provenance_is_not_a_confirmed_copy():
    assert parse_syndication(page('<p>10.1234/synthetic</p>', source=''), URL) == []


def test_discuz_plaintext_original_is_attributed_to_main_post_only():
    body = ('<h1>Scientific report</h1><table><tr><td class="t_f" id="postmessage_3873">'
            '<p>' + 'The study introduces measured fatigue performance. ' * 3
            + '10.1234/synthetic</p><p>本文转载于 ScienceAI：' + MP + '</p></td></tr></table>')
    row, = parse_syndication(body.encode(), 'https://research.example.org/forum.php?mod=viewthread&tid=3533')
    assert row['account'] == 'ScienceAI' and row['url'] == MP
    assert match_article(PAPER, row) == 'doi'


def test_reference_identity_and_related_source_do_not_confirm():
    body = '<p>' + '独立研究的具体介绍。' * 12 + '</p><h2>参考文献</h2><p>10.1234/synthetic</p>'
    row, = parse_syndication(page(body), URL)
    assert not match_article(PAPER, row)
    related = '<aside><p>来源：<a href="' + MP + '">材料研究</a></p></aside>'
    assert parse_syndication(page(body, related), URL) == []
    reference = '<h2>参考文献</h2><p>来源：<a href="' + MP + '">材料研究</a></p>'
    assert parse_syndication(page(body + reference, ''), URL) == []


def test_multiple_originals_do_not_guess_a_source():
    source = '<p>来源：<a href="' + MP + '">甲</a>、<a href="https://mp.weixin.qq.com/s/another">乙</a></p>'
    assert parse_syndication(page('<p>研究介绍</p>', source), URL) == []


def test_generic_image_order_and_reference_context_survive():
    body = '<p>论文首页</p><img src="https://images.example.org/paper.png">' \
           '<h2>参考文献</h2><img src="https://images.example.org/related.png">'
    row, = parse_syndication(page(body), URL)
    images = [block for block in row['blocks'] if block['type'] == 'image']
    assert len(images) == 1
    assert images[0]['reference_section'] is False


def test_challenge_is_not_an_empty_search_result():
    with pytest.raises(ArticleChallenge):
        parse_syndication(b'<html>captcha required</html>', URL)


@pytest.mark.parametrize('topic, account, title', [
    ('198001', '人工智能化学与材料', '机器学习驱动无铅弛豫体的逆向设计与多模态文献挖掘验证'),
    ('199812', '高分子科学前沿', '机器学习实现3D双相复合材料抗冲击性能优化'),
])
def test_python88_readable_body_with_login_to_reply_is_not_a_challenge(topic, account, title):
    # Reduced from both real pages: intact article div, account footer image,
    # and a separate "log in to reply" control; no located WeChat source link.
    body = ('<h1>' + title + '</h1><div id="js_content" class="rich_media_content">'
            '<p>' + '研究介绍了实验条件、模型方法和独立数据上的验证结果。' * 8 + '</p>'
            '<p>' + '图表说明了材料结构与性能之间的关系，并讨论了验证范围。' * 8 + '</p>'
            '<img src="https://images.example.org/paper.png">'
            '<img src="https://images.example.org/brand.png" alt="公众号：' + account + '"></div>'
            '<footer><a href="/login?next=/topic/' + topic + '">登录后回复</a>'
            '<form>请输入验证码</form></footer>')
    assert parse_syndication(body.encode(), 'https://www.python88.com/topic/' + topic) == []


@pytest.mark.parametrize('content', [
    '<div id="js_content">登录后阅读全文</div>',
    '<article><p>请输入验证码，验证身份后继续</p></article>',
    '<article><p>' + 'A public abstract preview. ' * 30 + '</p><p>Sign in to continue</p></article>',
])
def test_empty_article_or_preview_behind_login_wall_remains_challenge(content):
    with pytest.raises(ArticleChallenge):
        parse_syndication(('<h1>Paper report</h1>' + content).encode(), URL)


@pytest.mark.parametrize('url', ['https://127.0.0.1/a', 'https://localhost/a',
    'https://localhost.local/a', 'https://host.internal/a', 'https://user:secret@example.org/a',
    'https://example.org:8080/a', 'https://example.org/%2e%2e/private',
    'https://example.org/a?access_token=secret', 'https://example.org\\@localhost/a'])
def test_nonpublic_targets_are_rejected(url):
    assert not public_page_url(url)


def test_meaningful_query_retained_tracking_removed():
    assert canonical_syndication_url(URL + '&utm_source=search#heading') == URL


def test_dns_destination_must_be_public(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError, match='Non-public'):
        require_public_host(URL)
