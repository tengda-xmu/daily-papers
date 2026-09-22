from tools.build_site import render


def test_site_renders_complete_source_and_journal_catalog():
    html = render({"core": [], "extended": [], "papers": [], "source_status": {}})
    assert "arXiv" in html
    assert "Web of Science" in html
    assert "OpenAlex" in html
    assert "Crossref" in html
    assert "Nature Communications" in html
    assert "Engineering Structures" in html
    assert "id=\"journal\"" in html
