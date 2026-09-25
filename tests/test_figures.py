import hashlib
from copy import deepcopy
from io import BytesIO
from urllib.parse import urlsplit
from PIL import Image

from src.figures import ROOT, figure_catalog, figure_file, get_figure
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


def test_core_and_extended_have_one_original_figure_and_archives_use_relative_asset_paths():
    papers = [{"doi": doi, "title": "AI structural design", "source": "fixture"}
              for doi in figure_catalog()]
    html = render({"core": papers, "extended": papers})
    assert html.count('<figure class="paper-figure">') == len(papers) * 2
    assert html.count('loading="lazy"') == len(papers) * 2
    assert html.count('class="figure-credit"') == len(papers) * 2
    assert './assets/figures/' in html
    archive = render({"core": papers}, archive_date="2026-09-23")
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
