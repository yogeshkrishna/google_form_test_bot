import pytest

from gformbot import previews


@pytest.fixture(autouse=True)
def selected_fixture_browser(request, monkeypatch):
    """Run the same interaction fixtures against each supported Windows browser in CI."""
    import os

    selected = os.environ.get("GFORMBOT_TEST_BROWSER")
    if request.node.get_closest_marker("browser") and selected:
        from gformbot import browser

        factory = browser.get_driver
        monkeypatch.setattr(
            browser,
            "get_driver",
            lambda name="auto", headless=False, **kw: factory(selected, headless=headless),
        )


@pytest.fixture(autouse=True)
def isolated_preview_index(monkeypatch, tmp_path):
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "state" / "previews.json")
