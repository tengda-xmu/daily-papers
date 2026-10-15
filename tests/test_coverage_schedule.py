from pathlib import Path

import yaml


def workflow(name):
    root = Path(__file__).resolve().parents[1]
    return yaml.load((root / '.github/workflows' / name).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)


def test_collection_cannot_hold_the_publishing_lock():
    data = workflow('coverage.yml')
    assert data['on']['schedule'] and 'workflow_dispatch' in data['on']
    assert data['on']['workflow_run']['workflows'] == ['Daily papers']
    assert 'concurrency' not in data
    collect, publish = data['jobs']['collect'], data['jobs']['publish']
    assert collect['concurrency']['group'] == 'paper-coverage-collection'
    assert publish['concurrency']['group'] == 'daily-papers-update'
    assert collect['concurrency']['cancel-in-progress'] == 'false'
    assert publish['needs'] == 'collect'
    assert 'environment' not in collect and 'needs' not in collect
    assert any('tools.collect_paper_coverage' in step.get('run', '') for step in collect['steps'])
    assert not any('tools.collect_paper_coverage' in step.get('run', '') for step in publish['steps'])
    assert any(step.get('uses') == 'actions/cache/save@v4' and step.get('if') == 'always()'
               for step in collect['steps'])


def test_publication_uses_latest_main_and_only_processed_coverage_artifact():
    jobs = workflow('coverage.yml')['jobs']
    collect, steps = jobs['collect']['steps'], jobs['publish']['steps']
    export = next(i for i, step in enumerate(collect) if '--export' in step.get('run', ''))
    artifact_index = next(i for i, step in enumerate(collect) if step.get('uses') == 'actions/upload-artifact@v4')
    assert export < artifact_index
    artifact = collect[artifact_index]['with']
    assert artifact['path'].splitlines() == ['coverage-results/paper-coverage.json', 'coverage-results/coverage-reviews/']
    assert 'data/cache' not in artifact['path'] and 'site' not in artifact['path'] and 'daily.json' not in artifact['path']
    assert steps[0]['uses'] == 'actions/checkout@v4' and steps[0]['with']['ref'] == 'main'
    merge = next(step['run'] for step in steps if step.get('name') == 'Merge sources into the current published papers')
    assert merge.index('tools.import_coverage_reviews') < merge.index('tools.merge_coverage_results')
    assert 'connector-data' in merge and 'restore_publication' not in merge
    upload = next(step for step in steps if step.get('uses') == 'actions/upload-pages-artifact@v3')
    assert upload['with']['path'] == 'site'
    commit = next(step for step in steps if step.get('name') == 'Commit source updates')['run']
    assert 'git add data/paper-coverage.json data/coverage-reviews site' in commit
    assert 'data/cache' not in commit and '--force' not in commit


def test_daily_recommendations_do_not_wait_for_coverage_search():
    data = workflow('daily.yml')
    assert data['concurrency']['group'] == 'daily-papers-update'
    for name in ('update', 'publish'):
        commands = '\n'.join(step.get('run', '') for step in data['jobs'][name]['steps'])
        assert 'tools.collect_paper_coverage' not in commands
        assert 'tools.import_coverage_reviews' in commands
    assert not any('paper-coverage' in step.get('with', {}).get('path', '')
                   for step in data['jobs']['update']['steps']
                   if step.get('uses', '').startswith('actions/cache'))
