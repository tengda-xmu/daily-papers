"""Paper coverage command preserves cache defaults and reports long-run progress."""
import io

import pytest

from tools import collect_paper_coverage as command


@pytest.mark.parametrize('argv,refresh,ids', [
    ([], False, None),
    (['--refresh'], True, None),
    (['--refresh', '--paper-id', 'one', '--paper-id', 'two'], True, ['one', 'two']),
])
def test_collection_options_and_progress(monkeypatch, capsys, argv, refresh, ids):
    monkeypatch.setattr('src.settings.load_env', lambda: None)
    calls = []

    def collect(**options):
        calls.append(options)
        options['progress_callback'](1, 1, {'id': 'one', 'title': 'Paper\ntitle'}, {'state': 'no_match'})
        return {'papers': {'one': {'state': 'no_match'}}}

    monkeypatch.setattr(command, 'collect', collect)
    command.main(argv)
    assert len(calls) == 1
    assert calls[0]['force_refresh'] is refresh
    assert calls[0]['paper_ids'] == ids
    assert calls[0]['source_urls'] is None
    assert callable(calls[0]['index_searcher'])
    assert 'max_seconds' not in calls[0]
    output = capsys.readouterr().out
    assert '[1/1] no_match: Paper title' in output
    assert 'WeChat paper coverage: no_match=1' in output


def test_unknown_options_do_not_start_collection(monkeypatch):
    monkeypatch.setattr(command, 'collect', lambda **_: pytest.fail('Invalid options must not start collection'))
    with pytest.raises(SystemExit) as error:
        command.main(['--limit', '6'])
    assert error.value.code == 2


def test_session_import_and_source_only_options(monkeypatch, tmp_path):
    import json
    payload = {'version': 1, 'provider': 'codex_session', 'search_strategy_version': 7, 'queries': []}
    source = tmp_path / 'results.json'
    source.write_text(json.dumps(payload), encoding='utf-8')
    monkeypatch.setattr('src.settings.load_env', lambda: None)
    calls = []
    monkeypatch.setattr(command, 'collect', lambda **options: calls.append(options) or {'papers': {}})
    command.main(['--import-search-results', str(source), '--no-search'])
    assert calls[0]['imported_search_results'] == payload
    assert calls[0]['perform_search'] is False


@pytest.mark.parametrize('ids', [[], ['--paper-id', 'one', '--paper-id', 'two']])
def test_source_hint_requires_one_distinct_paper(monkeypatch, ids):
    monkeypatch.setattr(command, 'collect', lambda **_: pytest.fail('Ambiguous source must not be collected'))
    with pytest.raises(SystemExit) as error:
        command.main(ids + ['--source-url', 'https://mp.weixin.qq.com/s/example'])
    assert error.value.code == 2


def test_multiple_source_hints_are_passed_without_count_limit(monkeypatch):
    monkeypatch.setattr('src.settings.load_env', lambda: None)
    calls = []
    monkeypatch.setattr(command, 'collect', lambda **options: calls.append(options) or {'papers': {}})
    urls = [f'https://mp.weixin.qq.com/s/source-{n}' for n in range(12)]
    argv = ['--paper-id', 'one', '--paper-id', 'one']
    for url in urls:
        argv.extend(['--source-url', url])
    command.main(argv)
    assert calls[0]['source_urls'] == urls
    assert calls[0]['force_refresh'] is False
    assert 'max_seconds' not in calls[0]


@pytest.mark.parametrize('gbk_stream', [True, False])
def test_unicode_paper_titles_do_not_interrupt_collection(monkeypatch, gbk_stream):
    monkeypatch.setattr('src.settings.load_env', lambda: None)
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='gbk', errors='strict') if gbk_stream else io.StringIO()
    title = '试验结果：m²、λ、𝛼'
    completed = []

    def collect(**options):
        options['progress_callback'](1, 2, {'id': 'one', 'title': title}, {'state': 'no_match'})
        options['progress_callback'](2, 2, {'id': 'two', 'title': 'Next paper'}, {'state': 'no_match'})
        completed.append(True)
        return {'papers': {'one': {'state': 'no_match'}, 'two': {'state': 'no_match'}}}

    monkeypatch.setattr(command, 'collect', collect)
    with monkeypatch.context() as patch:
        patch.setattr(command.sys, 'stdout', stream)
        command.main([])
        stream.flush()
    output = raw.getvalue().decode('utf-8') if gbk_stream else stream.getvalue()
    assert completed == [True]
    assert f'[1/2] no_match: {title}' in output
    assert '[2/2] no_match: Next paper' in output
    assert 'WeChat paper coverage: no_match=2' in output
