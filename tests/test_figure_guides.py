from xml.etree import ElementTree as ET

from src.figure_guides import guide_for, guides, paper_doi, svg, build_guides
from tools.build_site import paper_figure


def test_reviewed_guides_keep_evidence_and_non_original_labels_in_image(tmp_path):
    build_guides(tmp_path)
    for doi, row in guides().items():
        guide = guide_for({'doi': doi})
        image = (tmp_path / guide['image_path']).read_text(encoding='utf-8')
        document = ET.fromstring(image)
        assert '非原文图' in ''.join(document.itertext())
        assert row['basis'] in ''.join(document.itertext())
        assert row['source_url'].startswith('https://')
        html = paper_figure({'doi': doi}, '../')
        assert '非原文图' in html and row['source_url'] in html
        assert '../assets/figures/guide-' in html
    assert guide_for({'doi': '10.example/unknown'}) is None


def test_aliases_attach_to_verified_identity_without_changing_paper():
    doi, row = next((d, r) for d, r in guides().items() if r.get('paper_ids'))
    paper = {'id': row['paper_ids'][0], 'title': row['paper_title'], 'doi': ''}
    original = dict(paper)
    assert paper_doi(paper) == doi
    assert guide_for(paper)
    assert paper == original
    assert paper_doi({**paper, 'title': 'A different paper'}) == ''


def test_original_figure_wins_over_editorial_guide(monkeypatch):
    from src.figures import figure_catalog
    doi = next(iter(guides()))
    original = next(iter(figure_catalog().values()))
    monkeypatch.setattr('tools.build_site.get_figure', lambda identifier: original)
    html = paper_figure({'doi': doi})
    assert 'paper-figure--guide' not in html
    assert original['image_path'] in html


def test_generated_svg_escapes_text_and_title_only_diagrams_have_no_flow_arrows():
    guide = guide_for({'doi': next(iter(guides()))})
    guide['title'] = '<script>bad()</script>'
    guide['steps'][0][1] = '<foreignObject>'
    document = ET.fromstring(svg(guide))
    assert not document.findall('.//{http://www.w3.org/2000/svg}script')
    assert not document.findall('.//{http://www.w3.org/2000/svg}foreignObject')
    guide['mode'] = 'topics'
    assert 'marker-end=' not in svg(guide)
