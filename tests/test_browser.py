"""Browser selection used by ChatGPT generation, without launching a browser."""

from types import SimpleNamespace

import pytest


def test_available_browsers_detects_path_without_launching(monkeypatch):
    from gformbot import browser as factory

    monkeypatch.setattr(factory, "_WINDOWS", False)
    monkeypatch.setattr(factory, "_MACOS", False)
    monkeypatch.setattr(
        factory.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in {"chromium", "microsoft-edge"} else None,
    )
    assert factory.available_browsers() == ["chrome", "edge"]


def test_available_browsers_detects_standard_windows_paths(monkeypatch):
    from gformbot import browser as factory

    monkeypatch.setattr(factory, "_WINDOWS", True)
    monkeypatch.setattr(factory, "_MACOS", False)
    monkeypatch.setattr(factory.shutil, "which", lambda name: None)
    for key, value in [
        ("PROGRAMFILES", "C:/Mock/Program Files"),
        ("PROGRAMFILES(X86)", "C:/Mock/Program Files (x86)"),
        ("LOCALAPPDATA", "C:/Mock/Local"),
    ]:
        monkeypatch.setenv(key, value)
    installed = {
        factory.Path("C:/Mock/Program Files/Google/Chrome/Application/chrome.exe"),
        factory.Path("C:/Mock/Local/Mozilla Firefox/firefox.exe"),
        factory.Path("C:/Mock/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    }
    monkeypatch.setattr(factory.Path, "is_file", lambda self: self in installed)
    assert factory.available_browsers() == ["chrome", "firefox", "edge"]


def test_available_browsers_detects_standard_macos_paths(monkeypatch):
    from gformbot import browser as factory

    monkeypatch.setattr(factory, "_WINDOWS", False)
    monkeypatch.setattr(factory, "_MACOS", True)
    monkeypatch.setattr(factory.shutil, "which", lambda name: None)
    monkeypatch.setattr(factory.Path, "home", classmethod(lambda cls: factory.Path("/Users/example")))
    installed = {
        factory.Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        factory.Path("/Users/example/Applications/Firefox.app/Contents/MacOS/firefox"),
    }
    monkeypatch.setattr(factory.Path, "is_file", lambda self: self in installed)
    assert factory.available_browsers() == ["chrome", "firefox"]


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
@pytest.mark.parametrize(
    "available,preferred,expected",
    [
        (["chrome", "firefox", "edge"], "edge", "edge"),
        (["firefox", "edge"], "chrome", "firefox"),
        (["firefox", "edge"], None, "firefox"),
        ([], "firefox", "chrome"),
    ],
)
def test_default_browser_only_selects_available_installations(
    monkeypatch, platform, available, preferred, expected
):
    from gformbot import browser as factory

    monkeypatch.setattr(factory, "_WINDOWS", platform == "windows")
    monkeypatch.setattr(factory, "_MACOS", platform == "macos")
    monkeypatch.setattr(factory, "available_browsers", lambda: available)
    monkeypatch.setattr(factory, f"_default_browser_{platform}", lambda: preferred)
    assert factory.detect_default_browser() == expected


def test_macos_default_browser_uses_https_handler_not_other_file_associations(monkeypatch):
    from gformbot import browser as factory

    exported = factory.plistlib.dumps(
        {
            "LSHandlers": [
                {"LSHandlerURLScheme": "mailto", "LSHandlerRoleAll": "org.mozilla.firefox"},
                {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "com.google.chrome"},
            ]
        }
    )
    monkeypatch.setattr(factory.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=exported))
    assert factory._default_browser_macos() == "chrome"
