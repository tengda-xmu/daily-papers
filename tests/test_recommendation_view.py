from copy import deepcopy

from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from connectors.codex_bridge.directions import DirectionChange, DirectionManager
from connectors.codex_bridge.server import LOCAL_ORIGIN, PUBLIC_ORIGIN, create_app
from src.public_sources import write
from src.recommendation_view import current_recommendations
from src.research_directions import clean_profile, profile_revision, select_tiers
from tests.test_codex_bridge import FakeCodex
from tests.test_research_directions import Remote, direction, profile


def settings(core=5, extended=5):
    return profile(direction('robotics'), core=core, extended=extended)


def edition(core=5, extended=5):
    original_profile = settings()
    return {'core': [{'id': f'c{i}', 'title': f'Core {i}', 'topic_tags': ['robotics']} for i in range(core)],
            'extended': [{'id': f'e{i}', 'title': f'Extended {i}', 'topic_tags': ['robotics']} for i in range(extended)],
            'papers': [{'id': 'candidate-not-selected'}],
            'edition': {'id': 'published-edition', 'date': '2026-10-14', 'number': 2,
                        'core_count': core, 'extended_count': extended},
            'generated_at': '2026-10-14T13:00:00+00:00',
            'research_profile': original_profile,
            'research_profile_revision': profile_revision(original_profile)}


def remote_profile(value):
    remote = Remote()
    remote.profile = deepcopy(value)
    return remote


def test_current_counts_round_trip_without_mutating_original_members_order_or_profile():
    source = edition()
    original = deepcopy(source)
    for count in (1, 0, 5):
        chosen = settings(extended=count)
        result = current_recommendations(source, chosen)
        assert result['core'] == original['core']
        assert result['extended'] == original['extended'][:count]
        assert result['edition'] == original['edition']
        assert result['research_profile'] == original['research_profile']
        assert result['research_profile_revision'] == original['research_profile_revision']
        assert result['papers'] == original['papers']
        assert result['display_settings'] == {
            'scope': 'current_edition', 'revision': profile_revision(chosen),
            'core_count': 5, 'extended_count': count,
            'requested': {'core_count': 5, 'extended_count': count},
            'available': {'core_count': 5, 'extended_count': 5}}
        result['core'][0]['title'] = 'Only the view changed'
        assert source == original


def test_count_limits_do_not_move_members_between_tiers_or_insert_candidates():
    source = edition(core=2, extended=5)
    result = current_recommendations(source, settings(core=10, extended=1))
    assert result['core'] == source['core'] and result['extended'] == source['extended'][:1]
    assert result['display_settings']['requested'] == {'core_count': 10, 'extended_count': 1}
    assert result['display_settings']['available'] == {'core_count': 2, 'extended_count': 5}
    assert result['display_settings']['core_count'] == 2
    assert 'candidate-not-selected' not in {p['id'] for p in result['core'] + result['extended']}


def test_zero_extended_is_valid_for_profile_and_future_selection():
    chosen = settings(extended=0)
    assert clean_profile(chosen)['extended_count'] == 0
    core, extended = select_tiers(edition()['core'] + edition()['extended'], chosen)
    assert len(core) == 5 and extended == []


@pytest.mark.parametrize('key,count', [('extended_count', -1), ('extended_count', 1.5),
    ('extended_count', True), ('extended_count', '0'), ('extended_count', 21),
    ('core_count', 0), ('core_count', -1), ('core_count', 1.5)])
def test_api_rejects_invalid_counts_without_saving_config(tmp_path, key, count):
    remote = remote_profile(settings())
    app = create_app(tmp_path, rpc=FakeCodex())
    app.state.directions.remote = remote
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        token = client.post('/api/pair', json={'code': app.state.pair_code},
                            headers={'Origin': PUBLIC_ORIGIN}).json()['token']
        headers = {'Origin': PUBLIC_ORIGIN, 'Authorization': 'Bearer ' + token}
        chosen = settings()
        chosen[key] = count
        response = client.post('/api/research-directions', headers=headers,
            json={'revision': profile_revision(remote.profile), 'profile': chosen})
        assert response.status_code == 422
        assert remote.writes == [] and remote.profile == settings()
        assert not (tmp_path / '.local/research-directions/applied.json').exists()


def test_api_save_applies_counts_from_original_runtime_edition_and_preserves_all_raw_files(tmp_path):
    remote = remote_profile(settings())
    paths = [tmp_path / '.local/codex-bridge/recommendations.json', tmp_path / 'data/daily.json',
             tmp_path / 'data/archive/2026-10-14.json', tmp_path / 'data/editions/2026-10-14-2.json']
    for path in paths:
        write(path, edition())
    original_files = {path: path.read_bytes() for path in paths}
    app = create_app(tmp_path, rpc=FakeCodex())
    app.state.directions.remote = remote
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        token = client.post('/api/pair', json={'code': app.state.pair_code},
                            headers={'Origin': PUBLIC_ORIGIN}).json()['token']
        headers = {'Origin': PUBLIC_ORIGIN, 'Authorization': 'Bearer ' + token}
        revision = profile_revision(settings())
        for count in (1, 0, 5):
            chosen = settings(extended=count)
            response = client.post('/api/research-directions', headers=headers,
                json={'revision': revision, 'profile': chosen})
            assert response.status_code == 200
            result = response.json()
            revision = result['revision']
            assert result['verified'] is True
            assert result['application'] == {
                'local_applied': True, **current_recommendations(edition(), chosen)['display_settings']}
            assert result['application']['revision'] == revision
            assert result['application']['extended_count'] == count
            page = client.get('/recommendations.html')
            assert page.status_code == 200
            document = BeautifulSoup(page.text, 'html.parser')
            assert [row['data-paper-id'] for row in document.select('#core .paper')] == [f'c{i}' for i in range(5)]
            assert [row['data-paper-id'] for row in document.select('#extended .paper')] == [f'e{i}' for i in range(count)]
            assert document.select_one('#extended-count').get_text(strip=True) == f'{count} 篇'
            assert {path: path.read_bytes() for path in paths} == original_files
        assert len(remote.writes) == 3


def test_manager_uses_custom_runtime_before_daily_then_falls_back_when_missing(tmp_path):
    remote = remote_profile(settings(extended=0))
    runtime = tmp_path / 'custom-runtime'
    write(runtime / 'recommendations.json', edition(core=2, extended=3))
    write(tmp_path / 'data/daily.json', edition(core=4, extended=5))
    manager = DirectionManager(tmp_path, remote, runtime=runtime)
    result = manager.snapshot()
    assert result['application']['available'] == {'core_count': 2, 'extended_count': 3}
    assert result['application']['core_count'] == 2 and result['application']['extended_count'] == 0
    (runtime / 'recommendations.json').unlink()
    result = manager.snapshot()
    assert result['application']['available'] == {'core_count': 4, 'extended_count': 5}


def test_local_display_application_is_independent_of_remote_config_verification(tmp_path):
    chosen = settings(extended=0)
    write(tmp_path / '.local/research-directions/applied.json', chosen)
    write(tmp_path / 'data/daily.json', edition())
    def unavailable(*_):
        raise HTTPException(503, 'Network unavailable')
    result = DirectionManager(tmp_path, unavailable).snapshot()
    assert result['verified'] is False
    assert result['application']['local_applied'] is True
    assert result['application']['extended_count'] == 0
    assert result['application']['available']['extended_count'] == 5


def test_saved_config_does_not_claim_display_was_applied_without_current_data(tmp_path):
    remote = remote_profile(settings())
    manager = DirectionManager(tmp_path, remote)
    chosen = settings(extended=0)
    result = manager.save(DirectionChange(revision=profile_revision(settings()), profile=chosen))
    assert result['verified'] is True
    assert result['application']['local_applied'] is False
    assert result['application']['available'] == {'core_count': 0, 'extended_count': 0}
    assert result['application']['requested'] == {'core_count': 5, 'extended_count': 0}
