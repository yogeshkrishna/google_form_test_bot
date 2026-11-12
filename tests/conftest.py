import pytest

from gformbot import previews


@pytest.fixture(autouse=True)
def isolated_preview_index(monkeypatch, tmp_path):
    monkeypatch.setattr(previews, "index_path", lambda: tmp_path / "state" / "previews.json")
