import hashlib
from copy import deepcopy
from io import BytesIO
from urllib.parse import urlsplit
from PIL import Image

from src.figures import ROOT, figure_catalog, figure_file, figure_key, get_figure
from tools.build_site import paper_card, render


def test_verified_images_match_recorded_dimensions_and_bytes():
    entries = figure_catalog()
    assert len(entries) >= 5
    for doi, expected in entries.items():
        figure = get_figure("https://doi.org/" + doi)
        assert figure == expected
        data = figure_file(figure).read_bytes()
        with Image.open(BytesIO(data)) as image:
            assert image.format in ('PNG', 'JPEG')
            assert image.size == (figure["width"], figure["height"])
            image.verify()
        assert hashlib.sha256(data).hexdigest() == figure["sha256"]
        assert figure["modified"] is False
        assert urlsplit(figure['license_source']).hostname in ('www.nature.com', 'www.ebi.ac.uk')


def test_only_core_has_figures_and_archives_use_relative_asset_paths(monkeypatch):
    from tests.test_research_directions import direction, profile
    # Rendering behavior uses a stable DOI fixture, independent of a live
    # collector adding conference/source keys or replacing catalog entries.
    # The preceding test still verifies every real catalog entry and its bytes.
    entries = dict(list((key, value) for key, value in figure_catalog().items() if key.startswith('10.'))[:2])
    assert len(entries) == 2
    monkeypatch.setattr('src.figures.figure_catalog', lambda: entries)
    papers = [{"doi": doi, "title": "AI structural design", "source": "fixture"}
              for doi in entries]
    assert {figure_key(paper) for paper in papers} == set(entries)
    settings = profile(direction('fixture'), core=5, extended=5)
    html = render({"core": papers, "extended": papers}, current_profile=settings)
    assert html.count('<figure class="paper-figure">') == len(papers)
    assert html.count('loading="lazy"') == len(papers)
    assert html.count('class="figure-credit"') == len(papers)
    for paper in papers:
        extended = paper_card(paper, 'extended')
        assert '<figure' not in extended and 'figure-unavailable' not in extended
    assert './assets/figures/' in html
    archive = render({"core": papers}, archive_date="2026-09-23", current_profile=settings)
    assert '../assets/figures/' in archive
    assert 'src="./assets/figures/' not in archive
    assert '<figure' not in paper_card({"doi": "10.unknown/new", "title": "New paper"}, "core")


def test_untrusted_or_incomplete_figure_metadata_is_not_rendered(monkeypatch):
    doi, valid = next(iter(figure_catalog().items()))
    for field, value in (("image_path", "assets/figures/../../.env"),
                         ("source_url", "javascript:alert(1)"), ("license_url", ""),
                         ("credit", ""), ("width", '100" onerror="alert(1)')):
        figure = deepcopy(valid)
        figure[field] = value
        monkeypatch.setattr("src.figures.figure_catalog", lambda: {doi: figure})
        assert get_figure(doi) is None
        assert '<figure' not in paper_card({"doi": doi, "title": "Paper"}, "core")
