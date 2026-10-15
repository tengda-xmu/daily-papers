from urllib.error import HTTPError

from src.coverage_discovery import serpapi_search


def result(start, *, next_start=None):
    return {'organic_results': [{'link': f'https://mp.weixin.qq.com/s/article-{start}'}],
            **({'serpapi_pagination': {'next': f'https://serpapi.com/search.json?start={next_start}'}}
               if next_start is not None else {})}


def test_serpapi_follows_every_available_page_without_paper_quota():
    calls = []
    def fetch(query, key, start):
        calls.append(start)
        return result(start, next_start=start + 10 if start < 150 else None)
    payload = serpapi_search('exact query', 'secret', transport=fetch)
    assert payload['state'] == 'ok' and payload['pages_completed'] == 16
    assert calls == list(range(0, 160, 10)) and len(payload['organic_results']) == 16


def test_repeated_results_stop_even_if_offset_keeps_advancing():
    calls = []
    def fetch(query, key, start):
        calls.append(start)
        return result(0, next_start=start + 10)
    payload = serpapi_search('q', 'key', transport=fetch)
    assert calls == [0, 10] and len(payload['organic_results']) == 1


def test_later_rate_limit_retains_earlier_sources_and_retry_hint():
    def fetch(query, key, start):
        if start:
            raise HTTPError('https://serpapi.com', 429, 'limited', {'Retry-After': '123'}, None)
        return result(0, next_start=10)
    payload = serpapi_search('q', 'key', transport=fetch)
    assert payload['state'] == 'blocked' and len(payload['organic_results']) == 1
    assert payload['retry_after'] == 123


def test_continuation_cannot_forward_key_to_another_host():
    calls = []
    def fetch(query, key, start):
        calls.append(start)
        return {'organic_results': [{'link': 'https://mp.weixin.qq.com/s/article'}],
                'serpapi_pagination': {'next': 'https://evil.example/search?start=10'}}
    payload = serpapi_search('q', 'secret', transport=fetch)
    assert calls == [0] and payload['state'] == 'error'
    assert 'secret' not in str(payload)


def test_true_empty_result_and_provider_payload_error_are_distinct():
    assert serpapi_search('q', 'key', transport=lambda *_: {'organic_results': []})['state'] == 'ok'
    assert serpapi_search('q', 'key', transport=lambda *_: {'error': 'service failed'})['state'] == 'error'
