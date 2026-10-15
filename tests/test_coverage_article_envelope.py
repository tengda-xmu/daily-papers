import pytest

from src import paper_coverage as coverage
from tests.test_paper_coverage import PAPER, URL


def test_normal_wechat_script_overhead_does_not_hide_readable_article():
    body = (b'<html><script>' + b' ' * 4_100_000 +
            b'</script><h1 id="activity-name">Paper report</h1>'
            b'<a id="js_name">Research account</a><div id="js_content">' +
            (PAPER['title'] + ' ' + PAPER['doi'] + ' This article introduces the research.').encode() +
            b'</div></html>')
    article = coverage.parse_article(body, URL)
    assert coverage.match_article(PAPER, article) == 'doi'
    assert article['account'] == 'Research account'
    assert len(article['match_text']) < 500


def test_article_response_still_has_a_bounded_size():
    with pytest.raises(ValueError, match='Invalid article response'):
        coverage.parse_article(b' ' * (coverage.MAX_ARTICLE_BYTES + 1), URL)
