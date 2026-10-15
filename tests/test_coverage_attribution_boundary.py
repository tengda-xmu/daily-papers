"""Regression cases: a page's other reports cannot supply its main provenance."""
import pytest

from src.coverage_syndication import parse_syndication
from src.paper_coverage import match_article


URL = 'https://research.example.org/news/123'
MP = 'https://mp.weixin.qq.com/s/another-report'
TEXT = '<p>' + 'This study describes independently measured fatigue. ' * 8 + 'DOI 10.1234/synthetic</p>'
SOURCE = '<p>Source: <a href="' + MP + '">OtherAccount</a></p>'


@pytest.mark.parametrize('html', [
    '<h1>Target report</h1><article>' + TEXT + '</article>'
    '<article><h2>Another report</h2>' + SOURCE + '</article>',
    '<h1>Target report</h1><table><tr><td class="t_f" id="postmessage_1">' + TEXT
    + '</td></tr><tr><td class="t_f" id="postmessage_2">' + SOURCE + '</td></tr></table>',
    '<h1>Target report</h1><article>' + TEXT
    + '<p><strong>References</strong></p>' + SOURCE + '</article>',
    '<h1>Target report</h1><main><article>' + TEXT + '</article>'
    '<article><h2>Another report</h2>' + SOURCE + '</article></main>',
    '<h1>Target report</h1><article>' + TEXT
    + '<p><strong>相关阅读</strong></p>' + SOURCE + '</article>',
], ids=['another-article', 'forum-reply', 'paragraph-references', 'main-with-other-article',
        'paragraph-related-reading'])
def test_other_report_attribution_is_not_main_provenance(html):
    assert parse_syndication(html.encode('utf-8'), URL) == []


def test_immediate_article_byline_remains_valid():
    html = '<h1>Target report</h1>' + SOURCE + '<article>' + TEXT + '</article>'
    row, = parse_syndication(html.encode('utf-8'), URL)
    assert row['url'] == MP and row['account'] == 'OtherAccount'


@pytest.mark.parametrize('heading', ['相关阅读', '推荐阅读', '参考文献', 'Related reading',
                                    'Recommended articles', 'Further reading'])
@pytest.mark.parametrize('tag', ['h2', 'p', 'strong'])
def test_valid_byline_cannot_attach_related_paper_doi_or_image(heading, tag):
    html = ('<h1>A different report</h1>' + SOURCE + '<article><p>'
            + 'This is a different independent study. ' * 8 + '</p>'
            '<img src="https://images.example.org/main.png">'
            '<' + tag + '>' + heading + '</' + tag + '><p>DOI 10.1234/target</p>'
            '<img src="https://images.example.org/target.png"></article>')
    row, = parse_syndication(html.encode('utf-8'), URL)
    assert not match_article({'doi': '10.1234/target'}, row)
    assert '10.1234/target' not in row['match_text']
    images = [block for block in row['blocks'] if block['type'] == 'image']
    assert [image['remote_url'] for image in images] == ['https://images.example.org/main.png']
