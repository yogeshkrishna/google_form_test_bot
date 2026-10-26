import hashlib
import importlib
import json
import sys
import sysconfig
from pathlib import Path

import pytest

from gformbot import browser

ROOT = Path(__file__).resolve().parents[1]


def test_windows_runtime_uses_private_namespace_without_official_selenium():
    from gformbot import selenium_runtime

    if sys.platform == "win32" and sysconfig.get_platform() == "win-amd64":
        assert selenium_runtime.PACKAGE == "gformbot._vendor.selenium"
    else:
        assert selenium_runtime.PACKAGE == "selenium"
    assert selenium_runtime.options("chrome").__class__.__module__.startswith(selenium_runtime.PACKAGE)


def test_subset_files_match_manifest_and_keep_required_notices():
    directory = ROOT / "gformbot/_vendor/selenium"
    manifest = json.loads((directory / "manifest.json").read_text())
    actual = {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file() and p.suffix != ".pyc" and p.name != "manifest.json"
    }
    assert actual == manifest["files"]
    assert {"LICENSE", "NOTICE", "webdriver/common/selenium-manager-THIRD-PARTY-NOTICES.txt"} <= actual.keys()
    assert not any(
        "/bidi/" in name
        or "/devtools/" in name
        or "/safari/" in name
        or "/linux" in name
        or "/macos/" in name
        for name in actual
    )


def test_private_browser_classes_and_resources_import_without_async_dependencies():
    prefix = "gformbot._vendor.selenium"
    for name in ("chrome", "edge", "firefox"):
        driver = importlib.import_module(f"{prefix}.webdriver.{name}.webdriver")
        assert driver.WebDriver.__module__.startswith(prefix)
    import pkgutil

    for name in ("getAttribute", "isDisplayed", "findElements"):
        assert pkgutil.get_data(prefix + ".webdriver.remote", name + ".js")


def test_all_browser_options_keep_existing_private_and_headless_flags(monkeypatch):
    from gformbot.selenium_runtime import webdriver

    for name, private in (("chrome", "--incognito"), ("edge", "--inprivate"), ("firefox", "-private")):
        captured = []
        monkeypatch.setattr(
            webdriver,
            {"chrome": "Chrome", "edge": "Edge", "firefox": "Firefox"}[name],
            lambda captured=captured, **kwargs: captured.append(kwargs["options"]),
        )
        getattr(browser, "_" + name + "_driver")(True)
        assert private in captured[0].arguments
        assert any("headless" in arg for arg in captured[0].arguments)


def test_vendor_recipe_rejects_untrusted_bytes():
    from tools.vendor_selenium import generate

    with pytest.raises(ValueError, match="SHA-256"):
        generate(b"not the pinned wheel")


@pytest.mark.parametrize(
    "address,expected",
    [("http://127.0.0.1:9", "ProxyManager"), ("socks5://127.0.0.1:9", "SOCKSProxyManager")],
)
def test_proxy_and_tls_dependencies_are_retained(address, expected):
    from gformbot._vendor.selenium.webdriver.common.proxy import Proxy
    from gformbot._vendor.selenium.webdriver.remote.client_config import ClientConfig
    from gformbot._vendor.selenium.webdriver.remote.remote_connection import RemoteConnection

    config = ClientConfig("https://example.test", proxy=Proxy({"proxyType": "manual", "sslProxy": address}))
    assert Path(config.ca_certs).is_file()
    connection = RemoteConnection(client_config=config)
    try:
        assert type(connection._conn).__name__ == expected
        assert connection._conn.connection_pool_kw["cert_reqs"] == "CERT_REQUIRED"
    finally:
        connection.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable")
def test_manager_bad_override_keeps_actionable_error(tmp_path, monkeypatch):
    from gformbot._vendor.selenium.common.exceptions import WebDriverException
    from gformbot._vendor.selenium.webdriver.common.selenium_manager import SeleniumManager

    monkeypatch.setenv("SE_MANAGER_PATH", str(tmp_path / "missing.exe"))
    with pytest.raises(WebDriverException, match="SE_MANAGER_PATH does not point to a file"):
        SeleniumManager._get_binary()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable")
def test_manager_offline_empty_cache_is_conservative(tmp_path, monkeypatch):
    from gformbot._vendor.selenium.common.exceptions import NoSuchDriverException
    from gformbot._vendor.selenium.webdriver.chrome.options import Options
    from gformbot._vendor.selenium.webdriver.chrome.service import Service
    from gformbot._vendor.selenium.webdriver.common.driver_finder import DriverFinder
    from gformbot._vendor.selenium.webdriver.common.selenium_manager import SeleniumManager

    result = SeleniumManager().binary_paths(
        [
            "--browser",
            "chrome",
            "--offline",
            "--cache-path",
            str(tmp_path),
            "--skip-driver-in-path",
            "--skip-browser-in-path",
            "--avoid-stats",
        ]
    )
    assert not result.get("driver_path")
    monkeypatch.delenv("SE_CHROMEDRIVER", raising=False)
    for key, value in {
        "SE_OFFLINE": "true",
        "SE_CACHE_PATH": str(tmp_path),
        "SE_SKIP_DRIVER_IN_PATH": "true",
        "SE_SKIP_BROWSER_IN_PATH": "true",
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(NoSuchDriverException, match="Unable to obtain driver for chrome"):
        DriverFinder(Service(), Options()).get_driver_path()
