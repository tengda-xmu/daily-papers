from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from src.editions import History, append, entries, enrich, migrate, read, relative_path, write
from src.auto_reading import fingerprint, public_analysis
from src.models import RawRecord
from src.pipeline import run_pipeline
from src.reading_notes import curated_entries, valid_analysis
from src.recommendation_heat import DEFAULTS, attention_evidence, citation_evidence, repeat_evidence, refresh, verified_attention
from src.recommendation_selection import select
from src.research_directions import load_profile

NOW = datetime(2026, 9, 25, 0, tzinfo=timezone.utc)


def paper(i=1):
    return {'id': f'{i:012x}', 'title': f'Neural networks for bearing fault diagnosis {i}', 'authors': ['A'],
            'doi': f'10.1234/{i}', 'venue': 'Engineering Structures', 'landing_url': f'https://doi.org/10.1234/{i}',
            'abstract': 'We evaluate neural network fault diagnosis using bearing monitoring data with controlled comparisons across operational conditions.',
            'topic_tags': ['ai_maintenance']}


def assessment(p):
    return public_analysis({'paper_id': p['id'], 'fingerprint': fingerprint(p),
        'analysis': {**deepcopy(curated_entries()[0]['analysis']), 'analysis_basis': 'abstract'},
        'evaluation': {'direction_fit': True, 'research_value': True, 'evidence_sufficient': True,
                       'reason': '该研究提供了与智能运维研究方向紧密相关的方法对照和验证证据，值得进一步重点阅读。',
                       'evidence': p['abstract'][:80]}})


def payload(run, core=(), extended=(), when=NOW):
    return {'update_run_id': str(run), 'generated_at': when.isoformat(), 'core': list(core), 'extended': list(extended)}


def observations(counts=(10, 12, 16), offsets=(29, 15, 0), source='Crossref'):
    return [{'source': source, 'count': c, 'observed_at': (NOW - timedelta(days=d)).isoformat()} for c, d in zip(counts, offsets)]


def test_editions_keep_same_day_and_retry_fixed(tmp_path):
    one = append(tmp_path, payload(1, [paper(1)]))
    two = append(tmp_path, payload(2, [paper(2)]))
    assert [e['number'] for e in entries(tmp_path)] == [1, 2]
    assert append(tmp_path, payload(1, [paper(9)])) == one
    assert read(tmp_path / relative_path(one['edition']))['core'][0]['id'] == paper(1)['id']
    assert read(tmp_path / 'archive/2026-09-25.json')['edition'] == two['edition']
    assert append(tmp_path, payload(3)) is None
    assert len(entries(tmp_path)) == 2


def test_migration_no_double_runs_and_identity_aliases(tmp_path):
    first = payload(1, [paper(1)], when=NOW - timedelta(days=1))
    migrate(tmp_path, [first, first, payload(2, [], [paper(2)])])
    migrate(tmp_path, [first])
    h = History(tmp_path)
    variant = {**paper(1), 'id': 'ffffffffffff', 'title': 'Source changed title'}
    assert h.find(variant)['id'] == paper(1)['id']
    assert len(entries(tmp_path)) == 2 and h.find(paper(1)).get('last_core')
    assert 'last_core' not in h.find(paper(2))


def test_citation_trend_same_source_and_corrections():
    last = NOW - timedelta(days=40)
    evidence = citation_evidence(observations(), last, NOW)
    assert evidence['before'] == 10 and evidence['after'] == 16
    assert citation_evidence(observations((0, 1, 5)), last, NOW)
    for invalid in (observations((10, 11, 12)), observations((100, 102, 106)),
                    observations((10, 18, 16)), observations(offsets=(10, 5, 0)),
                    observations(offsets=(29, 15, 4)), observations((10, 10, 16))):
        assert citation_evidence(invalid, last, NOW) is None
    mixed = observations(); mixed[1]['source'] = 'OpenAlex'
    assert citation_evidence(mixed, last, NOW) is None
    duplicates = [observations()[0]] * 5 + [observations()[-1]]
    assert citation_evidence(duplicates, last, NOW) is None
    assert citation_evidence(observations(), NOW - timedelta(days=10), NOW) is None
    corrected = observations((20, 10, 12, 16), (29, 22, 15, 0))
    assert citation_evidence(corrected, last, NOW)['before'] == 10


def events():
    return [{'verified': True, 'organization': source, 'story_id': source, 'url': f'https://{source}.org/paper',
             'published_at': (NOW-timedelta(days=i+1)).isoformat()} for i, source in enumerate(('journal', 'society'))]


def test_attention_independence_age_and_cooldown():
    last = NOW - timedelta(days=40)
    assert attention_evidence(events(), last, NOW)
    same = events(); same[1]['story_id'] = same[0]['story_id']
    assert attention_evidence(same, last, NOW) is None
    same = events(); same[1]['organization'] = same[0]['organization']
    assert attention_evidence(same, last, NOW) is None
    assert attention_evidence(events(), NOW, NOW) is None
    state = {'last_core': {'generated_at': (NOW-timedelta(days=29)).isoformat()}}
    assert repeat_evidence(state, {'attention': events()}, NOW) is None
    state['last_core']['generated_at'] = (NOW-timedelta(days=30)).isoformat()
    assert repeat_evidence(state, {'attention': events()}, NOW)['kind'] == 'attention'


def test_only_explicit_paper_mentions_count():
    p = paper()
    lead = {'kind': 'news', 'provider': 'official', 'title': p['title'], 'summary': p['abstract'] + ' DOI: ' + p['doi'],
            'url': 'https://society.org/research', 'published_at': NOW.isoformat()}
    assert len(verified_attention(p, [lead])) == 1
    assert verified_attention(p, [{**lead, 'kind': 'conference'}]) == []
    assert verified_attention(p, [{**lead, 'indexed': True}]) == []
    assert verified_attention(p, [{**lead, 'summary': 'A topic similar to fault diagnosis'}]) == []


def test_heat_refresh_daily_cache_and_failures(tmp_path):
    append(tmp_path, payload(1, [paper(1)]))
    h = History(tmp_path)
    calls = []
    def fetch(p):
        calls.append(p['id']); return [{'source': 'Crossref', 'count': 20}]
    a = refresh(tmp_path, h, [], [], NOW, fetch)
    b = refresh(tmp_path, h, [], [], NOW + timedelta(hours=1), fetch)
    assert len(calls) == 1 and len(b['papers'][paper(1)['id']]['citations']) == 1
    def fail(p):
        raise TimeoutError()
    c = refresh(tmp_path, h, [], [], NOW + timedelta(days=1), fail)
    assert c['status']['failed'] == 1 and c['papers'][paper(1)['id']]['citations'] == a['papers'][paper(1)['id']]['citations']


def test_shared_historical_slot_and_expanded_never_repeats(tmp_path):
    old_core, old_extended = paper(1), paper(2)
    append(tmp_path, payload(1, [old_core], [old_extended], NOW-timedelta(days=40)))
    h = History(tmp_path)
    papers = [old_core, old_extended, *[paper(i) for i in range(3, 15)]]
    analyses = {p['id']: assessment(p) for p in papers[:2]}
    heat = {'papers': {old_core['id']: {'citations': observations()}}}
    core, extended = select(papers, h, analyses, heat, load_profile(), NOW, DEFAULTS)
    assert len(core) == len(extended) == 5
    assert sum(bool(p.get('recommendation_decision')) for p in core) == 1
    assert old_core['id'] in {p['id'] for p in core}
    assert old_extended['id'] not in {p['id'] for p in extended}
    assert not ({p['id'] for p in core} & {p['id'] for p in extended})
    # The next publication resets the core cooldown and consumes the same heat.
    append(tmp_path, payload(2, core, extended))
    core2, extended2 = select(papers, History(tmp_path), analyses, heat, load_profile(), NOW+timedelta(days=1), DEFAULTS)
    assert old_core['id'] not in {p['id'] for p in core2 + extended2}
    assert next(p for p in core2 if p['id'] == old_extended['id'])['recommendation_decision']['kind'] == 'promotion'


def test_complete_notes_alone_do_not_qualify_for_promotion(tmp_path):
    p = paper()
    append(tmp_path, payload(1, [], [p]))
    a = assessment(p); a['evaluation']['evidence_sufficient'] = False
    assert select([p], History(tmp_path), {p['id']: a}, {}, load_profile(), NOW, DEFAULTS) == ([], [])
    a = assessment(p); a['evaluation']['evidence'] = 'not present in the actual article abstract' * 3
    assert select([p], History(tmp_path), {p['id']: a}, {}, load_profile(), NOW, DEFAULTS) == ([], [])


def test_enrichment_preserves_batch_decisions(tmp_path):
    p = paper()
    p['recommendation_decision'] = {'kind': 'promotion', 'reason': 'original decision'}
    original = append(tmp_path, payload(1, [p]))
    result = enrich(original, {p['id']: assessment(p)})
    assert result['edition'] == original['edition'] and result['generated_at'] == original['generated_at']
    assert result['core'][0]['recommendation_decision'] == p['recommendation_decision']
    assert valid_analysis(result['core'][0])
    assert 'deep_read' not in original['core'][0]


def test_pipeline_new_batches_no_new_and_retries(tmp_path, monkeypatch):
    class Adapter:
        status = type('Status', (), {'source': 'fixture', 'to_dict': lambda s: {'source': 'fixture', 'status': 'ok', 'count': 30}})()
        def fetch(self, since, until):
            return [RawRecord.from_mapping(paper(i)) for i in range(1, 31)]
    path = tmp_path / 'daily.json'
    batches = []
    for run in range(1, 4):
        monkeypatch.setenv('GITHUB_RUN_ID', str(run))
        batches.append(run_pipeline(until=NOW+timedelta(hours=run), adapters=[Adapter()], output_path=path))
    sets = [{p['id'] for p in b['core']+b['extended']} for b in batches]
    assert all(len(s) == 10 for s in sets)
    assert not (sets[0] & sets[1] or sets[1] & sets[2] or sets[0] & sets[2])
    monkeypatch.setenv('GITHUB_RUN_ID', '4')
    before = path.read_bytes()
    result = run_pipeline(until=NOW+timedelta(hours=4), adapters=[Adapter()], output_path=path)
    assert result['latest_update']['outcome'] == 'no_new' and path.read_bytes() == before
    assert len(entries(tmp_path)) == 3
    monkeypatch.setenv('GITHUB_RUN_ID', '3')
    retry = run_pipeline(until=NOW+timedelta(hours=5), adapters=[Adapter()], output_path=path)
    assert retry['edition'] == batches[2]['edition'] and len(entries(tmp_path)) == 3


def test_reconcile_publication_after_commit_failure(tmp_path):
    from tools.recommendation_data import reconcile
    staged = tmp_path / 'staged'; deployed = append(staged, payload(42, [paper()]))
    public = {'editions/index.json': {'editions': entries(staged)}, relative_path(deployed['edition']): deployed}
    local = tmp_path / 'local'
    reconcile(local, public.__getitem__)
    assert History(local).find(paper()) and read(local / 'daily.json')['edition']['id'] == '42'
    reconcile(local, public.__getitem__)
    assert len(entries(local)) == 1


def test_archive_groups_batches_and_mobile_link_paths(tmp_path, monkeypatch):
    from tools import build_site
    data, out = tmp_path / 'data', tmp_path / 'site'
    one = append(data, payload(1, [paper(1)]))
    p = paper(2); p['recommendation_decision'] = {'kind': 'promotion', 'previous': one['edition'], 'reason': 'worth reading'}
    append(data, payload(2, [p]))
    monkeypatch.setattr(build_site, 'DATA', data); monkeypatch.setattr(build_site, 'OUT', out)
    build_site.build_archive()
    html = (out / 'archive/index.html').read_text(encoding='utf-8')
    assert 'archive-day' in html and '2 批' in html
    assert '2026-09-25--1.html' in html and '2026-09-25--2.html' in html
    latest = (out / 'archive/2026-09-25.html').read_text(encoding='utf-8')
    assert '../archive/2026-09-25--1.html' in latest
    assert '中文精读待完成' in latest and 'class="reading-notes"' not in latest
