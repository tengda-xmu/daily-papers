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


def test_sparse_query_fixture_retains_domain_queries_and_adds_open_web_discovery():
    paper = {'doi': '10.1234/fixture', 'title': 'Physics informed digital twins for wind turbine fault prognosis'}
    assert paper_queries(paper) == [
        'site:mp.weixin.qq.com "10.1234/fixture"',
        'site:mp.weixin.qq.com "Physics informed digital twins for wind turbine fault prognosis"',
        'site:mp.weixin.qq.com Physics informed digital twins for wind turbine fault prognosis',
        '"10.1234/fixture"',
        '"Physics informed digital twins for wind turbine fault prognosis"',
        'Physics informed digital twins for wind turbine fault prognosis']


def test_chinese_and_method_queries_follow_normalized_identity_queries():
    queries = paper_queries(PAPER)
    assert queries[0] == 'site:mp.weixin.qq.com "10.1038/s42256-026-01262-4"'
    assert 'site:mp.weixin.qq.com ' + PAPER['title_zh'] in queries
    assert 'site:mp.weixin.qq.com "Chemeleon2"' in queries
    assert 'site:mp.weixin.qq.com "Chemeleon2" "Hyunsoo Park"' in queries
    assert len(set(queries)) == len(queries)
    assert method_names({'summary': 'PDF DOI P1 P12 AI 95%'}) == []


def test_review_subtitle_does_not_hide_concrete_chinese_subject_and_author_query():
    paper = {
        'title': 'Artificial Intelligence in Additive Manufacturing: A Critical Review of Methods, Applications and Industrial Challenges',
        'title_zh': '增材制造中的人工智能：方法、应用与工业挑战的批判性综述',
        'authors': ['Sarvesh Deshpande', 'Sahas Walvekar'],
    }
    queries = paper_queries(paper)
    assert 'site:mp.weixin.qq.com 增材制造中的人工智能' in queries
    assert 'site:mp.weixin.qq.com "Artificial Intelligence in Additive Manufacturing" "Sarvesh Deshpande"' in queries
    assert queries.index('site:mp.weixin.qq.com 增材制造中的人工智能') > queries.index('site:mp.weixin.qq.com ' + paper['title_zh'])
    assert not any(query in {'site:mp.weixin.qq.com 人工智能', 'site:mp.weixin.qq.com AI'} for query in queries)


def test_subtitle_fallback_does_not_use_generic_short_headings():
    queries = paper_queries({'title': 'AI: A review of new methods', 'title_zh': '人工智能：新方法综述', 'authors': ['A. Author']})
    assert 'site:mp.weixin.qq.com 人工智能' not in queries
    assert 'site:mp.weixin.qq.com "AI" "A. Author"' not in queries


def test_method_names_exclude_case_insensitive_generic_headings():
    paper = {'abstract': 'ABSTRACT Abstract abstract REFERENCES References METHODS RESULTS Conclusion SERR PIML'}
    assert method_names(paper) == ['SERR', 'PIML']
    assert method_names({'method_names': ['Abstract', 'references', 'SERR', 'PIML']}) == ['SERR', 'PIML']
    assert all('ABSTRACT' not in query and 'REFERENCES' not in query for query in paper_queries(paper))


def test_funding_identifiers_do_not_generate_method_searches():
    paper = {'abstract': 'Chemeleon2 uses GNN1278. This work was supported under Contract FA864923P1086, Grant No. DMR-1234567 and Award Number NSF20260123.'}
    assert method_names(paper) == ['Chemeleon2', 'GNN1278']
    assert not any('FA864923P1086' in query or 'NSF20260123' in query for query in paper_queries(paper))
    assert method_names({'method_names': ['GNN1278']}) == ['GNN1278']


def test_chinese_rewording_and_method_names_admit_candidates_without_confirming_identity():
    assert local_candidate(PAPER, {'title': HEADLINE})
    assert local_candidate(PAPER, {'title': 'Chemeleon2模型的新进展'})
    assert not local_candidate(PAPER, {'title': '风电齿轮箱故障预测的数字孪生介绍'})
    assert local_candidate({'title': PAPER['title']}, {'title': 'Novel crystals and diverse generative materials exploration'})


def test_chinese_review_candidate_requires_specific_subject_not_generic_ai_language():
    paper = {
        'title': 'Artificial Intelligence in Additive Manufacturing: A Critical Review of Methods, Applications and Industrial Challenges',
        'title_zh': '增材制造中的人工智能：方法、应用与工业挑战的批判性综述',
        'authors': ['Sarvesh Deshpande'],
    }
    assert local_candidate(paper, {'title': 'AI赋能增材制造新进展'})
    assert local_candidate(paper, {'title': '增材制造中的人工智能研究综述'})
    assert not local_candidate(paper, {'title': '外科人工智能研究综述：方法、应用与挑战'})
    assert not local_candidate(paper, {'title': '金融人工智能研究综述：方法应用及未来挑战'})
    assert not local_candidate(paper, {'title': '科学研究期刊影响因子综述', 'summary': '人工智能应用与技术挑战'})
    assert not local_candidate(PAPER, {'title': '强化学习引导生成外科人工智能模型的最新研究'})


def test_specific_topic_filter_preserves_identity_and_named_method_shortcuts():
    paper = {**PAPER, 'title_zh': '人工智能：最新研究综述'}
    assert local_candidate(paper, {'title': '新研究', 'summary': '10.1038/s42256-026-01262-4'})
    assert local_candidate(paper, {'title': PAPER['title']})
    assert local_candidate(paper, {'title': 'Chemeleon2论文解读'})
    assert not local_candidate(paper, {'title': '人工智能方法研究：应用技术与挑战'})


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


def test_result_href_spaces_and_chinese_are_encoded_without_double_encoding():
    calls = []
    def transport(url):
        calls.append(url)
        if urlsplit(url).path == '/weixin':
            return page(row(href='/link?url=TOKEN&query=%E5%A2%9E材制造 人工智能'))
        assert ' ' not in url and '材' not in url
        assert '%25E5' not in url
        assert parse_qs(urlsplit(url).query)['query'] == ['增材制造 人工智能']
        return {'body': b'paper body', 'url': ARTICLE, 'status': 200}
    result = sogou_search('q', transport=transport)
    assert result['state'] == 'ok' and result['link_resolution']['state'] == 'ok'
    assert result['organic_results'][0]['link'] == ARTICLE
    assert len(calls) == 2


@pytest.mark.parametrize('body', [f'<meta http-equiv="refresh" content="0; url={ARTICLE}">',
                                 f'<script>window.location.href = "{ARTICLE}";</script>'])
def test_literal_redirect_is_resolved_without_running_scripts(body):
    def transport(url):
        return page(row()) if urlsplit(url).path == '/weixin' else body.encode()
    result = sogou_search('Chemeleon2', transport=transport)
    assert result['organic_results'][0]['link'] == ARTICLE


def test_captcha_during_redirect_retains_candidates_and_continues_index_pages():
    calls = []
    def transport(url):
        calls.append(url)
        if urlsplit(url).path == '/weixin':
            if 'page=2' in url:
                return page(row(title='第二页介绍', href='/link?url=THIRD') + row(title='第二页直链', href=ARTICLE + '2'))
            return page(row() + row(title='另一篇介绍', href='/link?url=SECOND') + row(title='直接链接', href=ARTICLE),
                        '/weixin?type=2&page=2')
        return {'body': b'', 'url': 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?token=PRIVATE', 'status': 200}
    result = sogou_search('Chemeleon2', transport=transport)
    assert result['state'] == 'ok' and len(calls) == 3
    assert result['link_resolution'] == {'state': 'blocked', 'reason': 'verification_required', 'attempted': True}
    assert len(result['organic_results']) == 5
    assert result['organic_results'][0]['reason'] == 'index_only'
    assert result['organic_results'][2]['link'] == ARTICLE
    assert result['organic_results'][4]['link'] == ARTICLE + '2'
    assert sum('/link?' in url for url in calls) == 1
    assert result['organic_results'][0]['_redirect_url'].endswith('/link?url=PRIVATE')
    public_rows = [{k: v for k, v in r.items() if k != '_redirect_url'} for r in result['organic_results']]
    assert 'PRIVATE' not in str(public_rows)


def test_redirect_retry_after_is_separate_from_available_index_health():
    calls = []
    def transport(url):
        calls.append(url)
        if '/weixin?' in url:
            return page(row())
        raise HTTPError(url, 429, 'PRIVATE', {'Retry-After': '3600'}, None)
    result = sogou_search('q', transport=transport)
    assert result['state'] == 'ok' and 'retry_after' not in result
    assert result['link_resolution']['state'] == 'blocked'
    assert result['link_resolution']['retry_after'] == 3600
    assert result['organic_results'][0]['reason'] == 'index_only'
    assert len(calls) == 2


def test_link_resolution_cooldown_does_not_disable_index_search_or_direct_links():
    calls = []
    def transport(url):
        calls.append(url)
        assert '/weixin?' in url
        return page(row() + row(title='直接链接', href=ARTICLE))
    result = sogou_search('q', transport=transport, resolve_links=False)
    assert result['state'] == 'ok'
    assert result['link_resolution'] == {'state': 'deferred', 'reason': '', 'attempted': False}
    assert len(calls) == 1
    assert result['organic_results'][0]['reason'] == 'index_only'
    assert result['organic_results'][1]['link'] == ARTICLE


def test_index_challenge_on_next_page_blocks_index_and_preserves_previous_candidates():
    calls = []
    def transport(url):
        calls.append(url)
        if 'page=2' in url:
            return {'body': '请输入验证码', 'url': url, 'status': 429, 'headers': {'Retry-After': '1800'}}
        return page(row(href=ARTICLE), '/weixin?type=2&page=2')
    result = sogou_search('q', transport=transport)
    assert result['state'] == 'blocked' and result['retry_after'] == 1800
    assert result['link_resolution']['state'] == 'ok'
    assert result['organic_results'][0]['link'] == ARTICLE
    assert len(calls) == 2


def test_redirect_error_does_not_turn_successful_index_into_provider_error():
    calls = []
    def transport(url):
        calls.append(url)
        if '/weixin?' in url:
            return page(row() + row(title='另一篇介绍', href='/link?url=SECOND'))
        raise TimeoutError('ERROR_PRIVATE')
    result = sogou_search('q', transport=transport)
    assert result['state'] == 'ok'
    assert result['link_resolution']['state'] == 'error'
    assert len(calls) == 2 and len(result['organic_results']) == 2
    assert 'ERROR_PRIVATE' not in str(result)


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
