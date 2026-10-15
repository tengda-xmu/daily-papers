"""A display setting must never truncate a published recommendation snapshot."""
from copy import deepcopy
import socket

from bs4 import BeautifulSoup

from src.editions import append, archive_name, read, relative_path, write
from src.research_directions import clean_profile, load_profile, profile_revision
from tools import build_site


def profile(extended):
    return clean_profile({
        'version': 1, 'core_count': 5, 'extended_count': extended,
        'directions': [{
            'id': 'robotics', 'name': '机器人', 'enabled': True,
            'core': True, 'extended': True, 'weight': 1,
            'keywords': ['robotics'], 'require_any': [], 'exclude': [],
        }],
    })


def batch(day):
    snapshot_profile = profile(5)
    papers = [{
        'id': f'{day * 100 + number:012x}',
        'title': f'Robotics fixture {day}-{number}',
        'authors': ['Fixture Author'], 'source': 'Crossref',
        'topic_tags': ['robotics'], 'published_at': f'2026-10-{day:02d}',
    } for number in range(10)]
    return {
        'generated_at': f'2026-10-{day:02d}T13:00:00+00:00',
        'update_run_id': f'fixture-{day}',
        'core': papers[:5], 'extended': papers[5:], 'papers': papers,
        'source_status': {}, 'research_profile': snapshot_profile,
        'research_profile_revision': profile_revision(snapshot_profile),
    }


def members(payload):
    return {tier: [paper['id'] for paper in payload[tier]] for tier in ('core', 'extended')}


def html_members(path):
    document = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
    return {tier: [paper['data-paper-id'] for paper in document.select(f'#{tier} .paper')]
            for tier in ('core', 'extended')}


def test_main_limits_only_current_html_and_keeps_complete_data_and_history(tmp_path, monkeypatch):
    data, out = tmp_path / 'data', tmp_path / 'site'
    previous = append(data, batch(14))
    current = append(data, batch(15))
    legacy = batch(13)
    write(data / 'archive/2026-10-13.json', legacy)
    write(data / 'daily.json', current)
    write(tmp_path / 'config/research-directions.json', profile(0))
    original = deepcopy(current)

    def source_bytes():
        return {path.relative_to(tmp_path): path.read_bytes()
                for directory in (data, tmp_path / 'config')
                for path in directory.rglob('*') if path.is_file()}

    before = source_bytes()
    monkeypatch.setattr(build_site, 'ROOT', tmp_path)
    monkeypatch.setattr(build_site, 'DATA', data)
    monkeypatch.setattr(build_site, 'OUT', out)
    monkeypatch.setattr(build_site, 'load_profile', lambda root=tmp_path: load_profile(root))

    # Keep the actual templates, homepage/archive rendering and file publishing.
    # Isolate unrelated metadata catalogs and auxiliary columns from the checkout.
    monkeypatch.setattr(build_site, 'get_figure', lambda _key: None)
    monkeypatch.setattr(build_site, 'wechat_directory', lambda: '')
    monkeypatch.setattr(build_site, 'wechat_manager', lambda: '')
    monkeypatch.setattr('src.paper_titles.DATA', data)
    monkeypatch.setattr('src.paper_titles.title_entries', lambda *args, **kwargs: {})
    monkeypatch.setattr('src.research_leads.collect', lambda *_args: {
        'entries': [], 'directions': profile(0)['directions'], 'sources': [], 'days': 90,
    })
    monkeypatch.setattr('src.ai_updates.public_index', lambda _root: {'entries': [], 'sources': []})

    def forbid_network(*args, **kwargs):
        raise AssertionError('Static display builds must not make network requests')

    monkeypatch.setattr(socket.socket, 'connect', forbid_network)
    monkeypatch.setattr(socket, 'create_connection', forbid_network)
    build_site.main()

    assert html_members(out / 'index.html') == {'core': members(current)['core'], 'extended': []}
    homepage = BeautifulSoup((out / 'index.html').read_text(encoding='utf-8'), 'html.parser')
    assert homepage.select_one('#reading')['data-total'] == '5'
    assert homepage.select_one('#extended-count').get_text(strip=True) == '0 篇'
    assert homepage.select_one('#extended-section')['data-disabled'] == 'true'
    assert homepage.select_one('#recommendation-issue')['data-display-revision'] == profile_revision(profile(0))

    # data.json is also the recovery input for local reading/update queues.
    public_data = read(out / 'data.json')
    assert members(public_data) == members(original)
    assert public_data['edition'] == original['edition']
    assert public_data['research_profile'] == profile(5)
    assert 'display_settings' not in public_data

    for payload in (previous, current):
        entry = payload['edition']
        for name in (archive_name(entry), entry['date']):
            assert html_members(out / 'archive' / (name + '.html')) == members(payload)
            archive_data = read(out / 'archive' / (name + '.json'))
            assert members(archive_data) == members(payload)
            assert archive_data['edition'] == entry
            assert archive_data['research_profile'] == profile(5)
            assert 'display_settings' not in archive_data
        published_edition = read(out / relative_path(entry))
        assert members(published_edition) == members(payload)
        assert published_edition['edition'] == entry
    assert html_members(out / 'archive/2026-10-13.html') == members(legacy)
    assert read(out / 'archive/2026-10-13.json') == legacy
    assert read(out / 'editions/index.json') == read(data / 'editions/index.json')
    assert source_bytes() == before
