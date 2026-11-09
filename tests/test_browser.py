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


@pytest.mark.parametrize("name", ["chrome", "edge"])
@pytest.mark.parametrize(
    "agent",
    [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HeadlessChrome/156.1.2.3 Safari/537.36",
        "Mozilla/5.0 (X11; Linux aarch64) HeadlessChrome/157.4.5.6 Safari/537.36 Edg/157.4.5.6",
    ],
)
def test_headless_chromium_keeps_actual_platform_and_browser_version(monkeypatch, name, agent):
    from gformbot import browser as factory

    overrides = []
    driver = SimpleNamespace(
        set_page_load_timeout=lambda timeout: None,
        set_script_timeout=lambda timeout: None,
        execute_script=lambda script: agent,
        execute_cdp_cmd=lambda command, params: overrides.append((command, params)),
    )
    monkeypatch.setattr(factory, f"_{name}_driver", lambda **kwargs: driver)
    assert factory.get_driver(name) is driver
    assert overrides == [
        ("Network.setUserAgentOverride", {"userAgent": agent.replace("HeadlessChrome/", "Chrome/")})
    ]


@pytest.mark.parametrize(
    "name,headless,agent,reads",
    [
        ("chrome", True, "Mozilla/5.0 Chrome/156.1.2.3 Safari/537.36", 1),
        ("edge", True, "Mozilla/5.0 Chrome/156.1.2.3 Safari/537.36 Edg/156.1.2.3", 1),
        ("firefox", True, "Mozilla/5.0 Firefox/147.0", 0),
        ("chrome", False, "Mozilla/5.0 HeadlessChrome/156.1.2.3 Safari/537.36", 0),
    ],
)
def test_normal_agents_and_non_chromium_sessions_are_not_overridden(
    monkeypatch, name, headless, agent, reads
):
    from gformbot import browser as factory

    queries = []
    driver = SimpleNamespace(
        set_page_load_timeout=lambda timeout: None,
        set_script_timeout=lambda timeout: None,
        execute_script=lambda script: queries.append(script) or agent,
        execute_cdp_cmd=lambda *args: pytest.fail("This session must not receive an agent override"),
    )
    monkeypatch.setattr(factory, f"_{name}_driver", lambda **kwargs: driver)
    assert factory.get_driver(name, headless=headless) is driver
    assert len(queries) == reads


@pytest.mark.parametrize("failure_stage", ["read", "override"])
def test_failed_headless_setup_closes_driver_and_preserves_error(monkeypatch, failure_stage):
    from gformbot import browser as factory

    closed = []

    def adjust(stage):
        if stage == failure_stage:
            raise RuntimeError("Headless setup failed")
        return "Mozilla/5.0 HeadlessChrome/156.1.2.3 Safari/537.36"

    driver = SimpleNamespace(
        set_page_load_timeout=lambda timeout: None,
        set_script_timeout=lambda timeout: None,
        execute_script=lambda script: adjust("read"),
        execute_cdp_cmd=lambda *args: adjust("override"),
        quit=lambda: closed.append(True),
    )
    monkeypatch.setattr(factory, "_chrome_driver", lambda **kwargs: driver)
    with pytest.raises(RuntimeError, match="Headless setup failed"):
        factory.get_driver("chrome")
    assert closed == [True]
