import base64
from copy import deepcopy
from datetime import datetime, timezone
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from connectors.codex_bridge.directions import DirectionManager, DirectionChange, ENDPOINT
from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN, PUBLIC_ORIGIN
from src.models import RawRecord, SourceStatus
from src.pipeline import run_pipeline, build_adapters
from src.reading_notes import curated_entries
from src.research_directions import clean_profile, load_profile, match_directions, select_tiers, profile_revision, query_plan
from tests.test_codex_bridge import FakeCodex
from tools.build_site import render


def direction(identifier, **extra):
    return {'id': identifier, 'name': identifier, 'enabled': True, 'core': True, 'extended': True,
            'weight': 1, 'keywords': [identifier], 'require_any': [], 'exclude': [], **extra}


def profile(*rows, core=3, extended=3):
    return clean_profile({'version': 1, 'core_count': core, 'extended_count': extended, 'directions': list(rows)})


def test_keyword_rules_allow_other_fields_and_prevent_false_acronyms():
    settings = profile(direction('robotics', keywords=['robotics', '机器人'], require_any=['LLM', 'large language model'], exclude=['marketing']))
    assert match_directions(RawRecord('x', '1', 'Large-language-models for robotics'), settings) == ['robotics']
    assert match_directions(RawRecord('x', '1', 'LLMs 机器人'), settings) == ['robotics']
    assert not match_directions(RawRecord('x', '1', 'Robotics marketing using LLM'), settings)
    assert not match_directions(RawRecord('x', '1', 'Robotics shellmodels'), settings)
    settings['directions'][0]['enabled'] = False
    assert not match_directions(RawRecord('x', '1', 'LLM robotics'), settings)


def test_balanced_selection_covers_fields_respects_tiers_and_never_fills_unrelated():
    settings = profile(direction('alpha', weight=3), direction('bravo'), direction('charlie', core=False), core=4, extended=3)
    rows = [{'id': str(i), 'topic_tags': ['alpha']} for i in range(10)]
    rows += [{'id': 'b', 'topic_tags': ['bravo']}, {'id': 'c', 'topic_tags': ['charlie']}, {'id': 'x', 'topic_tags': ['unrelated']}]
    core, extended = select_tiers(rows, settings)
    assert {r['recommended_direction'] for r in core[:2]} == {'alpha', 'bravo'}
    assert 'c' not in {r['id'] for r in core} and 'c' in {r['id'] for r in extended}
    assert len({r['id'] for r in core + extended}) == 7
    assert 'x' not in {r['id'] for r in core + extended}
    settings['directions'][0]['enabled'] = False
    core, extended = select_tiers(rows, settings)
    assert [r['id'] for r in core] == ['b']
    assert [r['id'] for r in extended] == ['c']


def test_queries_cover_every_direction_and_daily_adapters_use_them():
    settings = profile(*(direction('topic' + str(i)) for i in range(12)))
    plan = query_plan(settings)
    assert len(plan['bounded_boolean']) == 3
    adapters = {a.name: a for a in build_adapters(settings)}
    for i in range(12):
        term = 'topic' + str(i)
        for name in ('Elsevier', 'Google Scholar', 'OpenAlex', 'Crossref', 'Semantic Scholar', 'PubMed', 'Web of Science'):
            assert term in ' '.join(adapters[name].queries), name
        assert term in adapters['arXiv'].query_expression
        assert term in adapters['CNS 子刊专项'].config['query']
        assert term in ' '.join(adapters['ResearchGate'].index.queries)
    assert len(adapters['Web of Science'].queries) <= 3


def test_pipeline_custom_fields_and_analysis_budget_are_balanced(monkeypatch):
    settings = profile(direction('robotics'), direction('climate', core=False), core=2, extended=2)
    rows = [RawRecord('test', str(i), f'robotics material {i}', abstract='Study evidence ' * 12) for i in range(10)]
    rows += [RawRecord('test', 'climate', 'climate observations', abstract='Independent evidence ' * 12)]
    rows += [RawRecord('test', 'offtopic', 'LLM predictive maintenance', abstract='Other topic ' * 12)]
    class Source:
        name = 'test'; status = SourceStatus('test', 'ok', len(rows))
        def fetch(self, since, until): return rows
    calls = []
    def summarize(record):
        calls.append(record.title); return deepcopy(curated_entries()[0]['analysis'])
    monkeypatch.setenv('LLM_API_KEY', 'fixture')
    monkeypatch.setattr('src.pipeline.cached_analysis', lambda r: None)
    monkeypatch.setattr('src.pipeline._llm_summary', summarize)
    result = run_pipeline(adapters=[Source()], research_profile=settings)
    assert len(result['core']) == len(result['extended']) == 2
    assert any(p['title'] == 'climate observations' for p in result['extended'])
    assert len(calls) == 4
    assert all('offtopic' != p['source_id'] for p in result['papers'])
    html = render(result, archive_date='2026-09-24')
    assert 'climate' in html and '本期方向' in html
    assert '方向覆盖' not in html and 'reading-policy' not in html
    assert result['research_profile_revision'] == profile_revision(settings)


class Remote:
    def __init__(self): self.profile = load_profile(); self.writes = []
    def __call__(self, method, endpoint, body=None):
        assert endpoint in (ENDPOINT, ENDPOINT + '?ref=main')
        if method == 'GET': return {'sha': 'fixture-sha', 'content': base64.b64encode(json.dumps(self.profile).encode()).decode()}
        assert method == 'PUT' and body['sha'] == 'fixture-sha' and body['branch'] == 'main'
        self.profile = json.loads(base64.b64decode(body['content'])); self.writes.append(body)
        return {'commit': {'html_url': 'https://github.com/tengda-xmu/daily-papers/commit/abcdef'}}


def test_settings_are_persistent_conflict_checked_and_authenticated(tmp_path):
    remote = Remote(); app = create_app(tmp_path, rpc=FakeCodex()); app.state.directions.remote = remote
    with TestClient(app, base_url=LOCAL_ORIGIN) as c:
        assert c.get('/api/research-directions').status_code == 401
        token = c.post('/api/pair', json={'code': app.state.pair_code}, headers={'Origin': PUBLIC_ORIGIN}).json()['token']
        headers = {'Origin': PUBLIC_ORIGIN, 'Authorization': 'Bearer ' + token}
        before = c.get('/api/research-directions', headers=headers).json()
        changed = deepcopy(before['profile']); changed['directions'].append(direction('aerospace'))
        body = {'revision': before['revision'], 'profile': changed}
        assert c.post('/api/research-directions', json=body, headers={**headers, 'Origin': 'https://evil.test'}).status_code == 403
        assert c.post('/api/research-directions', json=body, headers=headers).status_code == 200
        assert load_profile(tmp_path) == changed == remote.profile
        assert c.post('/api/research-directions', json=body, headers=headers).status_code == 409
        assert len(remote.writes) == 1
        restarted = DirectionManager(tmp_path, remote)
        assert restarted.snapshot()['profile'] == changed


@pytest.mark.parametrize('change', [
    lambda p: p.update(core_count=100),
    lambda p: p['directions'][0].update(keywords=[]),
    lambda p: p['directions'][0].update(enabled=False),
    lambda p: p['directions'][0].update(core=False, extended=False),
    lambda p: p['directions'].append(deepcopy(p['directions'][0])),
])
def test_invalid_configuration_is_rejected(change):
    settings = profile(direction('robotics')); change(settings)
    with pytest.raises(ValueError): clean_profile(settings)
