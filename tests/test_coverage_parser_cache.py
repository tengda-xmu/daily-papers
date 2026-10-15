import pytest

from src import paper_coverage as coverage
from src.public_sources import read, write
from tests.test_paper_coverage import NOW


URL = 'https://research.example.org/news/123'
MP = 'https://mp.weixin.qq.com/s/wrong-source'


def test_old_success_cache_reparses_provenance_without_network_or_false_timestamp(tmp_path):
    raw = '<h1>Target</h1><article><p>' + 'Research methods and results. ' * 8 + '10.1234/fixture</p></article>' \
          '<article><p>Source: <a href="' + MP + '">Another account</a></p></article>'
    path = tmp_path / coverage.CACHE / 'syndication' / (coverage._identity(URL) + '.json')
    write(path, {'version': coverage.ARTICLE_VERSION, 'state': 'ok', 'checked_at': NOW.isoformat(),
                 'raw_body': raw, 'articles': [{'url': MP, 'match_text': '10.1234/fixture'}]})
    progress = {}
    rows, state, checked = coverage._cached_syndication(tmp_path, URL, NOW,
        lambda *_: pytest.fail('Fresh stored HTML must be reparsed locally'), progress=progress)
    assert rows == [] and state == 'ok' and checked == NOW.isoformat()
    saved = read(path)
    assert saved['parser_version'] == coverage.SYNDICATION_PARSER_VERSION and not saved['articles']
    assert saved['checked_at'] == NOW.isoformat() and progress['last_attempt_at'] == NOW.isoformat()


def test_failed_access_cache_does_not_request_network_for_parser_change(tmp_path):
    path = tmp_path / coverage.CACHE / 'syndication' / (coverage._identity(URL) + '.json')
    write(path, {'version': coverage.ARTICLE_VERSION, 'state': 'challenge', 'checked_at': NOW.isoformat()})
    assert coverage._cached_syndication(tmp_path, URL, NOW,
        lambda *_: pytest.fail('Cached challenge must retain retry backoff')) == ([], 'challenge', NOW.isoformat())
