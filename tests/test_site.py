from tools.build_site import render, source_status_panel


def test_site_renders_complete_source_and_journal_catalog():
    html = render({"core": [], "extended": [], "papers": [], "source_status": {}})
    assert "arXiv" in html
    assert "Web of Science" in html
    assert "OpenAlex" in html
    assert "Crossref" in html
    assert "Nature Communications" in html
    assert "Engineering Structures" in html
    assert "id=\"journal\"" in html
    assert "手动更新" in html
    assert 'href="./setup.html#sources"' in html
    assert 'id="sources"' not in html
    assert "actions/workflows/daily.yml" in source_status_panel(
        {"source_status": {}}, reading_url="./"
    )
