from copy import deepcopy

from bs4 import BeautifulSoup

from src.models import RawRecord, SourceStatus
from src.pipeline import run_pipeline
from src.research_directions import profile_revision
from tests.test_research_directions import direction, profile
from tools.build_site import render


def batch():
    papers = [{'id': f'{i:012x}', 'title': f'robotics study {i}', 'authors': ['Researcher'],
               'topic_tags': ['robotics'], 'landing_url': f'https://example.org/paper/{i}'}
              for i in range(10)]
    return {'generated_at': '2026-10-14T09:00:00+00:00', 'update_run_id': '123',
            'core': papers[:5], 'extended': papers[5:], 'papers': papers,
            'research_profile': profile(direction('robotics'), core=5, extended=5),
            'edition': {'id': '123', 'number': 2, 'date': '2026-10-14',
                        'core_count': 5, 'extended_count': 5}}


def test_current_counts_take_effect_without_a_new_edition_and_archive_stays_complete():
    original = batch()
    snapshot = deepcopy(original)
    for count in (1, 0, 5):
        settings = profile(direction('robotics'), core=5, extended=count)
        current = BeautifulSoup(render(original, current_profile=settings, coverage={}), 'html.parser')
        assert len(current.select('#core .paper')) == 5
        assert len(current.select('#extended .paper')) == count
        assert current.select_one('#result-count').text == f'{5 + count} 篇推荐'
        assert current.select_one('#recommendation-issue')['data-display-revision'] == profile_revision(settings)
        assert '下次更新将按新方向检索' not in current.text  # Counts alone need no new collection.
        archive = BeautifulSoup(render(original, archive_date='2026-10-14', current_profile=settings, coverage={}), 'html.parser')
        assert len(archive.select('#core .paper')) == len(archive.select('#extended .paper')) == 5
        assert archive.select_one('#recommendation-issue')['data-display-revision'] == ''
    assert original == snapshot


def test_zero_has_explicit_empty_message_and_shortage_does_not_borrow_candidates():
    original = batch()
    settings = profile(direction('robotics'), core=8, extended=0)
    page = BeautifulSoup(render(original, current_profile=settings, coverage={}), 'html.parser')
    assert len(page.select('.paper')) == 5
    assert page.select_one('#extended-section')['data-disabled'] == 'true'
    assert '扩展阅读已关闭（0 篇）' in page.select_one('#extended-empty').text
    assert '核心 8 篇、扩展 0 篇' in page.text and '保留空缺' in page.text


def test_new_pipeline_recommendations_preserve_extended_zero():
    settings = profile(direction('robotics'), core=2, extended=0)
    rows = [RawRecord('test', str(i), f'robotics study {i}', abstract='Research evidence ' * 10)
            for i in range(6)]

    class Source:
        name = 'test'
        status = SourceStatus('test', 'ok', len(rows))

        def fetch(self, since, until):
            return rows

    result = run_pipeline(adapters=[Source()], research_profile=settings)
    assert len(result['core']) == 2
    assert result['extended'] == []
    assert result['analysis_status']['target_extended'] == 0
