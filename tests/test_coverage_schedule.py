from pathlib import Path

import yaml


def test_coverage_schedule_does_not_depend_on_new_recommendations():
    root = Path(__file__).resolve().parents[1]
    data = yaml.load((root / '.github/workflows/coverage.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    assert data['on']['schedule'] and 'workflow_dispatch' in data['on']
    assert data['concurrency']['group'] == 'daily-papers-update'
    job = data['jobs']['supplement']
    assert 'needs' not in job and 'if' not in job
    steps = job['steps']
    assert any('tools.collect_paper_coverage' in step.get('run', '') for step in steps)
    assert any(step.get('uses') == 'actions/cache/save@v4' and step.get('if') == 'always()' for step in steps)
    upload = next(step for step in steps if step.get('uses') == 'actions/upload-pages-artifact@v3')
    assert upload['with']['path'] == 'site'
    commit = next(step for step in steps if step.get('name') == 'Commit source updates')
    assert 'git add data/paper-coverage.json data/coverage-reviews site' in commit['run']
    assert 'data/cache' not in commit['run']
