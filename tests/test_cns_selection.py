from copy import deepcopy
from datetime import datetime, timezone

from src.models import RawRecord, SourceStatus
from src.pipeline import run_pipeline
from src.recommendation_selection import select
from src.recommendation_heat import DEFAULTS
from src.venues import cns_journals, venue_priority

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def profile(core=6, extended=0):
    return {'version': 1, 'core_count': core, 'extended_count': extended, 'directions': [
        {'id': 'alpha', 'name': '方向甲', 'enabled': True, 'core': True, 'extended': True,
         'weight': 1, 'keywords': ['robotics'], 'require_any': [], 'exclude': []},
        {'id': 'beta', 'name': '方向乙', 'enabled': True, 'core': True, 'extended': True,
         'weight': 3, 'keywords': ['climate'], 'require_any': [], 'exclude': []}]}


def paper(identifier, venue='Nature Communications', date='2026-10-01', focus=False, tags=('alpha',)):
    return {'id': identifier, 'source_id': identifier, 'title': ('Multi-agent LLM ' if focus else '') + 'robotics ' + identifier,
            'doi': '10.test/' + identifier, 'venue': venue, 'published_at': date, 'topic_tags': list(tags)}


def test_shared_directory_has_official_issns_for_all_3_plus_16():
    journals = cns_journals()
    assert len(journals) == 19
    assert sum(j['group'] == 'CNS 正刊' for j in journals) == 3
    assert sum(j['group'] == 'CNS 子刊' for j in journals) == 16
    assert len({j['issn'] for j in journals}) == 19
    for journal in journals:
        digits = journal['issn'].replace('-', '')
        assert sum((10 if x == 'X' else int(x)) * (8 - i) for i, x in enumerate(digits)) % 11 == 0
        assert journal['issn_source'].startswith('https://')
    assert venue_priority('Nature') == venue_priority('Science Advances') > venue_priority('Engineering Structures')


def test_cns_same_tier_date_then_focus_then_weight_without_direction_quota():
    rows = [paper('ordinary', venue='Engineering Structures', date='2026-10-15', focus=True, tags=('beta',)),
            paper('old-main', venue='Nature', date='2026-09-01', focus=True, tags=('beta',)),
            paper('new-sub', date='2026-10-10'),
            paper('same-focus', date='2026-10-09', focus=True),
            paper('same-weight', date='2026-10-09', tags=('alpha', 'beta')),
            paper('same-low', date='2026-10-09')]
    original = deepcopy(rows)
    core, extended = select(rows, None, {}, {}, profile(5), NOW, DEFAULTS)
    assert [p['id'] for p in core] == ['new-sub', 'same-focus', 'same-weight', 'same-low', 'old-main']
    assert next(p for p in core if p['id'] == 'same-weight')['recommended_direction'] == 'beta'
    assert extended == [] and rows == original


def test_pipeline_does_not_reintroduce_balanced_planning_order(tmp_path):
    rows = [RawRecord.from_mapping(paper(str(i), date='2026-10-10')) for i in range(6)]
    rows.append(RawRecord('fixture', 'climate', 'climate ordinary', venue='Engineering Structures', published_at='2026-10-14'))
    class Source:
        name = 'fixture'; status = SourceStatus(name, 'ok', len(rows))
        def fetch(self, since, until): return rows
    settings = profile()
    result = run_pipeline(adapters=[Source()], research_profile=settings, until=NOW, output_path=tmp_path / 'daily.json')
    assert len(result['core']) == 6 and result['extended'] == []
    assert all(p['venue_group'] == 'CNS 子刊' and p['recommended_direction'] == 'alpha' for p in result['core'])
    assert settings == profile()
    stat = next(s for s in result['cns_statistics'] if s['journal'] == 'Nature Communications')
    assert stat['new'] == stat['relevant'] == stat['selected'] == 6
    assert stat['historical_excluded'] == 0
    # A subsequent run cannot republish the six known papers without evidence.
    again = run_pipeline(adapters=[Source()], research_profile=settings, until=NOW, output_path=tmp_path / 'daily.json')
    assert [p['source_id'] for p in again['core']] == ['climate']
    stat = next(s for s in again['cns_statistics'] if s['journal'] == 'Nature Communications')
    assert stat['new'] == stat['selected'] == 0 and stat['historical_excluded'] == 6


def test_tier_flags_and_disabled_directions_still_apply():
    settings = profile(2, 1)
    settings['directions'][1]['core'] = False
    rows = [paper('beta', date='2026-10-14', tags=('beta',)), paper('alpha'), paper('off', tags=('unknown',))]
    core, extended = select(rows, None, {}, {}, settings, NOW, DEFAULTS)
    assert [p['id'] for p in core] == ['alpha']
    assert [p['id'] for p in extended] == ['beta']


def test_other_journals_keep_method_and_topic_order_before_date():
    rows = [paper('recent', venue='Engineering Structures', date='2026-10-15', tags=('beta',)),
            paper('older-focus', venue='Engineering Structures', date='2026-08-01', focus=True),
            paper('older-two-topics', venue='Engineering Structures', date='2026-08-01', tags=('alpha', 'beta'))]
    core, _ = select(rows, None, {}, {}, profile(3), NOW, DEFAULTS)
    assert [p['id'] for p in core] == ['older-focus', 'older-two-topics', 'recent']
