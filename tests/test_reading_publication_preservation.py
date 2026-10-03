"""Column-only builds and publication retries must preserve completed readings."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from src.auto_reading import fingerprint, public_analysis, public_status, READER_VERSION
from src.editions import append, read, write, relative_path
from tests.test_reading_queue import response, sample
from tools.build_site import render
from tools.recommendation_data import import_readings, import_reading_status, enrich_all
from tools.restore_publication import restore


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize('retry_old_snapshot', [False, True])
def test_publishing_without_paper_collection_keeps_latest_reading_and_batch(tmp_path, monkeypatch, retry_old_snapshot):
    git(tmp_path, 'init', '-b', 'main')
    git(tmp_path, 'config', 'user.email', 'fixture@example.invalid')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    data = tmp_path/'data'
    monkeypatch.setattr('src.paper_titles.DATA', data)
    p = sample(); other = deepcopy(p); other['id'] = 'abcdef123456'
    payload = append(data, {'generated_at':'2026-10-02T13:07:00+00:00', 'update_run_id':'kept-batch',
                            'core':[p], 'extended':[other]})
    write(data/'daily.json', payload)
    raw = response(p); raw.update(paper_id=p['id'], fingerprint=fingerprint(p))
    raw['analysis']['analysis_basis'] = 'abstract'
    old = public_analysis(raw)
    write(data/'auto-reading'/f"{p['id']}.json", old)
    stale_status = public_status({'paper_id':p['id'], 'state':'failed','basis':'full_text',
        'total':12,'covered':12,'updated_at':'2026-10-02T09:00:00+00:00'})
    write(data/'reading-status'/f"{p['id']}.json", stale_status)
    write(tmp_path/'site/data.json', payload)
    (tmp_path/'site/index.html').write_text('Old generated snapshot', encoding='utf-8')
    snapshot = tmp_path/'.publication'
    shutil.copytree(data, snapshot/'data')
    shutil.copytree(tmp_path/'site', snapshot/'site')
    private = tmp_path/'.local/private.pdf'; private.parent.mkdir(); private.write_bytes(b'private PDF')

    latest = deepcopy(old)
    latest['analysis'].update(analysis_basis='full_text', analyzed_at='2026-10-03T02:19:53+00:00',
        summary='全文逐页核对已完成，研究方法与实验条件有对应页码依据。根据原文说明验证范围，保留局限与可迁移研究建议，不将摘要解读当作全文结论。[P1]')
    latest['material']={'version':'a'*64,'total':12,'covered':12,'reader_version':READER_VERSION,
        'references':{'findings':['P1']},'covered_labels':[f'P{i}' for i in range(1,13)],'issues':[]}
    latest = public_analysis(latest)
    published = public_status({**stale_status, 'state':'published','updated_at':'2026-10-03T02:20:00+00:00'})
    write(data/'auto-reading'/f"{p['id']}.json", latest)
    write(data/'reading-status'/f"{p['id']}.json", published)
    git(tmp_path,'add','data')
    git(tmp_path,'commit','-m','Latest connector readings')
    git(tmp_path,'update-ref','refs/remotes/origin/connector-data',git(tmp_path,'rev-parse','HEAD'))

    if retry_old_snapshot:
        restore(snapshot, tmp_path)
    else:
        # The column-only checkout still contains the last committed abstract.
        write(data/'auto-reading'/f"{p['id']}.json", old)
        write(data/'reading-status'/f"{p['id']}.json", stale_status)
    assert read(data/'auto-reading'/f"{p['id']}.json")['analysis']['analysis_basis'] == 'abstract'
    assert import_readings(tmp_path) == 1
    import_reading_status(tmp_path); enrich_all(data)
    for path in [data/'daily.json', data/relative_path(payload['edition']), data/'archive/2026-10-02.json']:
        updated = read(path)
        assert updated['edition'] == payload['edition']
        assert updated['generated_at'] == payload['generated_at']
        assert updated['update_run_id'] == 'kept-batch'
        assert [v['id'] for v in updated['core']] == [p['id']]
        assert [v['id'] for v in updated['extended']] == [other['id']]
        paper = updated['core'][0]
        assert paper['analysis_basis']=='full_text' and paper['summary']==latest['analysis']['summary']
        assert paper['reading_status']['state']=='published'
        html = render(updated)
        assert '全文精读已完成' in html and '自动尝试已用尽' not in html
    assert private.read_bytes()==b'private PDF'
    assert read(data/'auto-reading'/f"{p['id']}.json")==latest


@pytest.mark.parametrize('importer', [import_readings, import_reading_status])
def test_missing_connector_ref_fails_instead_of_publishing_old_results(tmp_path, importer):
    git(tmp_path,'init')
    with pytest.raises(subprocess.CalledProcessError):
        importer(tmp_path)


def test_all_publication_paths_refresh_readings_independently_of_collection():
    root=Path(__file__).resolve().parents[1]
    daily=yaml.load((root/'.github/workflows/daily.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    update=daily['jobs']['update']['steps']
    imported=next(s for s in update if s.get('name')=='Import validated paper readings for every update')
    assert 'if' not in imported and imported.get('continue-on-error')!='true'
    assert '--import-readings' in imported['run']
    pipeline=next(s for s in update if s.get('id')=='pipeline')
    assert 'papers_needed' in pipeline['if'] and update.index(imported)<update.index(pipeline)
    publish=daily['jobs']['publish']['steps']
    preserved=next(s for s in publish if s.get('name')=='Preserve current readings when publishing or retrying a snapshot')
    restored=next(s for s in publish if s.get('name')=='Restore exact publication snapshot')
    upload=next(s for s in publish if s.get('uses')=='actions/upload-pages-artifact@v3')
    assert publish.index(restored)<publish.index(preserved)<publish.index(upload)
    assert 'if' not in preserved and preserved.get('continue-on-error')!='true'
    for command in ['git fetch origin connector-data:refs/remotes/origin/connector-data',
                    'python -m tools.recommendation_data --import-readings','python tools/build_site.py']:
        assert command in preserved['run']
    assert 'src.pipeline' not in preserved['run'] and '--refresh' not in preserved['run']
