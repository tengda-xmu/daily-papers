import pytest
from tools.publish_researchgate import public_records


def test_publish_excludes_sessions_and_url_tokens():
    rows = public_records([{"title": "Paper", "authors": ["Author"], "cookies": "private", "raw_metadata": {"token": "private"},
                           "landing_url": "https://www.researchgate.net/publication/123_paper?token=private#tracking"}])
    assert rows == [{"title": "Paper", "authors": ["Author"], "source": "ResearchGate",
                     "landing_url": "https://www.researchgate.net/publication/123_paper"}]


def test_empty_or_login_export_cannot_replace_publications():
    with pytest.raises(ValueError):
        public_records([{"title": "Log in", "landing_url": "https://www.researchgate.net/login"}])
