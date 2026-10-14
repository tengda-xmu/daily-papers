from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from src.coverage_discovery import local_candidate, method_names, paper_queries, sogou_search
from src.sources.wechat_public_index import parse_search_page


PAPER = {'doi': 'https://doi.org/10.1038/S42256-026-01262-4',
         'title': 'Guiding generative models to uncover diverse and novel crystals via reinforcement learning',
         'title_zh': '通过强化学习引导生成模型发现多样且新颖的晶体',
         'summary': '研究提出Chemeleon2，通过奖励引导探索晶体。', 'authors': ['Hyunsoo Park', 'Aron Walsh']}
ARTICLE = 'https://mp.weixin.qq.com/s/DXvEHDH1FQRqGQkQhBYKyw'
HEADLINE = 'npj Comput. Mater. | 晶体生成模型的新颖稳定性能协同优化：基于强化学习的未知材料主动探索研究'


def row(title=HEADLINE, href='/link?url=PRIVATE', account='Ai赋能材料'):
    return f'''<li id="sogou_vr_11002601_box_0"><h3><a href="{href}">{title}</a></h3>
    <p class="txt-info">论文介绍</p><span class="all-time-y2">{account}</span>
    <script>timeConvert('1784714400')</script></li>'''


def page(rows='', next_url=''):
    return ('<ul class="news-list">' + rows + '</ul>' +
            (f'<a id="sogou_next" href="{next_url}">下一页</a>' if next_url else '')).encode()


def test_sparse_query_fixture_retains_original_three_strategies():
    paper = {'doi': '10.1234/fixture', 'title': 'Physics informed digital twins for wind turbine fault prognosis'}
    assert paper_queries(paper) == [
        'site:mp.weixin.qq.com "10.1234/fixture"',
        'site:mp.weixin.qq.com "Physics informed digital twins for wind turbine fault prognosis"',
        'site:mp.weixin.qq.com Physics informed digital twins for wind turbine fault prognosis']


def test_chinese_and_method_queries_follow_normalized_identity_queries():
    queries = paper_queries(PAPER)
    assert queries[0] == 'site:mp.weixin.qq.com "10.1038/s42256-026-01262-4"'
    assert 'site:mp.weixin.qq.com ' + PAPER['title_zh'] in queries
    assert 'site:mp.weixin.qq.com "Chemeleon2"' in queries
    assert 'site:mp.weixin.qq.com "Chemeleon2" "Hyunsoo Park"' in queries
    assert len(set(queries)) == len(queries)
    assert method_names({'summary': 'PDF DOI P1 P12 AI 95%'}) == []


def test_method_names_exclude_case_insensitive_generic_headings():
    paper = {'abstract': 'ABSTRACT Abstract abstract REFERENCES References METHODS RESULTS Conclusion SERR PIML'}
    assert method_names(paper) == ['SERR', 'PIML']
    assert method_names({'method_names': ['Abstract', 'references', 'SERR', 'PIML']}) == ['SERR', 'PIML']
    assert all('ABSTRACT' not in query and 'REFERENCES' not in query for query in paper_queries(paper))


def test_chinese_rewording_and_method_names_admit_candidates_without_confirming_identity():
    assert local_candidate(PAPER, {'title': HEADLINE})
    assert local_candidate(PAPER, {'title': 'Chemeleon2模型的新进展'})
    assert not local_candidate(PAPER, {'title': '风电齿轮箱故障预测的数字孪生介绍'})
    assert local_candidate({'title': PAPER['title']}, {'title': 'Novel crystals and diverse generative materials exploration'})


def test_parser_preserves_result_hrefs_and_next_page_with_missing_metadata():
    parsed = parse_search_page(page(row(account=''), '/weixin?type=2&query=foo&page=2').decode())
    assert parsed['rows'][0]['href'] == '/link?url=PRIVATE'
    assert parsed['rows'][0]['account'] == ''
    assert parsed['next_url'].endswith('page=2')


def test_regular_http_redirect_resolves_article_and_strips_search_domain():
    calls = []
    def transport(url):
        calls.append(url)
        if urlsplit(url).path == '/weixin':
            assert parse_qs(urlsplit(url).query)['query'] == ['"Chemeleon2"']
            return page(row())
        return {'body': b'paper body', 'url': ARTICLE, 'status': 200}
    result = sogou_search('site:mp.weixin.qq.com "Chemeleon2"', transport=transport)
    assert result['state'] == 'ok'
    assert result['organic_results'][0]['link'] == ARTICLE
    assert len(calls) == 2
    assert 'PRIVATE' not in str(result)


@pytest.mark.parametrize('body', [f'<meta http-equiv="refresh" content="0; url={ARTICLE}">',
                                 f'<script>window.location.href = "{ARTICLE}";</script>'])
def test_literal_redirect_is_resolved_without_running_scripts(body):
    def transport(url):
        return page(row()) if urlsplit(url).path == '/weixin' else body.encode()
    result = sogou_search('Chemeleon2', transport=transport)
    assert result['organic_results'][0]['link'] == ARTICLE


def test_captcha_during_redirect_retains_all_index_candidates_and_stops_io():
    calls = []
    def transport(url):
        calls.append(url)
        if urlsplit(url).path == '/weixin':
            return page(row() + row(title='另一篇介绍', href='/link?url=SECOND') + row(title='直接链接', href=ARTICLE),
                        '/weixin?type=2&page=2')
        return {'body': b'', 'url': 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?token=PRIVATE', 'status': 200}
    result = sogou_search('Chemeleon2', transport=transport)
    assert result['state'] == 'blocked' and len(calls) == 2
    assert len(result['organic_results']) == 3
    assert result['organic_results'][0]['reason'] == 'index_only'
    assert result['organic_results'][2]['link'] == ARTICLE
    assert 'PRIVATE' not in str(result)


def test_unknown_redirect_stays_index_candidate_not_successful_article():
    def transport(url):
        return page(row()) if urlsplit(url).path == '/weixin' else b'<script>var hidden = complex();</script>'
    result = sogou_search('Chemeleon2', transport=transport)
    assert result['organic_results'][0]['reason'] == 'index_only'
    assert urlsplit(result['organic_results'][0]['link']).hostname == 'weixin.sogou.com'


@pytest.mark.parametrize('href', ['https://evil.example/path', 'http://localhost/private',
                                'https://weixin.sogou.com@evil.example/path', 'https://weixin.sogou.com:123/private'])
def test_result_redirect_cannot_fetch_unapproved_hosts(href):
    calls = []
    def transport(url):
        calls.append(url)
        return page(row(href=href))
    result = sogou_search('Chemeleon2', transport=transport)
    assert len(calls) == 1 and result['organic_results'][0]['reason'] == 'index_only'


def test_pagination_has_no_daily_or_per_paper_cutoff_and_stops_repeated_page():
    calls = []
    def transport(url):
        calls.append(url)
        number = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
        # Nine unique pages deliberately exceed old 3/day and 6/day limits.
        number = min(number, 9)
        return page(row(title='介绍' + str(number), href=ARTICLE + str(number)),
                    '/weixin?type=2&query=foo&page=' + str(number + 1))
    result = sogou_search('Chemeleon2', transport=transport)
    assert result['state'] == 'ok' and len(result['organic_results']) == 9
    assert len(calls) == 10
    assert len(sogou_search('Chemeleon2', transport=transport)['organic_results']) == 9


def test_rotating_redirect_tokens_do_not_make_a_repeated_page_look_new():
    calls = []
    def transport(url):
        calls.append(url)
        number = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
        return page(row(href='/link?token=' + str(number)), '/weixin?type=2&page=' + str(number + 1)) if '/weixin?' in url else b'<html>unresolved</html>'
    result = sogou_search('q', transport=transport)
    assert result['state'] == 'ok' and len(result['organic_results']) == 1
    assert len(calls) == 3  # Page 1, its redirect, then repeated page 2.


def test_empty_results_are_distinct_from_challenge_and_unrecognized_page():
    assert sogou_search('q', transport=lambda _: '没有找到相关结果'.encode())['state'] == 'ok'
    assert sogou_search('q', transport=lambda _: '请输入验证码'.encode())['state'] == 'blocked'
    assert sogou_search('q', transport=lambda _: b'<html>Search homepage</html>')['state'] == 'error'


def test_partial_results_survive_next_page_failure():
    def transport(url):
        if 'page=2' in url:
            raise TimeoutError('PRIVATE')
        return page(row(href=ARTICLE), '/weixin?type=2&page=2')
    result = sogou_search('q', transport=transport)
    assert result['state'] == 'error' and result['organic_results'][0]['link'] == ARTICLE
    assert 'PRIVATE' not in str(result)


def test_actual_retry_after_is_forwarded_without_provider_error_details():
    def transport(url):
        raise HTTPError(url, 429, 'PRIVATE', {'Retry-After': '3600'}, None)
    result = sogou_search('q', transport=transport)
    assert result['state'] == 'blocked' and result['retry_after'] == 3600
    assert 'PRIVATE' not in str(result)
