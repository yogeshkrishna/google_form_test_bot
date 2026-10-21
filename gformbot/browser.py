"""Fresh private Selenium sessions for supported installed browsers."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Literal

BrowserName = Literal["chrome", "firefox", "edge", "auto", "headless"]

_WINDOWS = sys.platform == "win32"
_MACOS = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Default-browser detection
# ---------------------------------------------------------------------------

def _default_browser_windows() -> BrowserName:
    """Read the Windows registry to figure out the default browser."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
        prog_id = prog_id.lower()
        if "firefox" in prog_id:
            return "firefox"
        if "edge" in prog_id:
            return "edge"
        if "chrome" in prog_id:
            return "chrome"
    except Exception:
        pass
    return "chrome"  # sensible default


def _default_browser_macos() -> BrowserName:
    try:
        result = subprocess.run(
            ["defaults", "read", "com.apple.LaunchServices/com.apple.launchservices.secure",
             "LSHandlers"],
            capture_output=True, text=True, timeout=5,
        )
        out = result.stdout.lower()
        if "firefox" in out:
            return "firefox"
        if "microsoft edge" in out or "msedge" in out:
            return "edge"
    except Exception:
        pass
    return "chrome"


def _default_browser_linux() -> BrowserName:
    try:
        result = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True, text=True, timeout=5,
        )
        val = result.stdout.lower()
        if "firefox" in val:
            return "firefox"
        if "chromium" in val:
            return "chrome"
        if "microsoft-edge" in val or "msedge" in val:
            return "edge"
    except Exception:
        pass
    # Try which
    for name in ("google-chrome", "chromium-browser", "chromium", "firefox"):
        if shutil.which(name):
            return "chrome" if "chrome" in name or "chromium" in name else "firefox"
    return "chrome"


def detect_default_browser() -> BrowserName:
    if _WINDOWS:
        return _default_browser_windows()
    if _MACOS:
        return _default_browser_macos()
    return _default_browser_linux()


# ---------------------------------------------------------------------------
# Driver factory
# ---------------------------------------------------------------------------

def get_driver(browser: BrowserName = "auto", headless: bool = False):
    """
    Return a configured Selenium WebDriver.

    Parameters
    ----------
    browser  : "auto" | "chrome" | "firefox" | "edge" | "headless"
    headless : Force headless mode even if browser is named.
    """
    if browser == "headless":
        headless = True
        browser = "chrome"

    if browser == "auto":
        browser = detect_default_browser()

    factories = {"chrome": _chrome_driver, "firefox": _firefox_driver, "edge": _edge_driver}
    if browser not in factories:
        raise ValueError("Browser must be auto, headless, chrome, firefox, or edge.")
    try:
        driver = factories[browser](headless)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(30)
        return driver
    except Exception as exc:
        raise RuntimeError(f"Could not start {browser}: {exc}. Check that the browser is installed.") from exc


def _chrome_driver(headless: bool):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--incognito")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--no-first-run")
    opts.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(options=opts)


def _firefox_driver(headless: bool):
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options

    opts = Options()
    opts.add_argument("-private")
    opts.set_preference("intl.accept_languages", "en-US,en")
    if headless:
        opts.add_argument("--headless")
    return webdriver.Firefox(options=opts)


def _edge_driver(headless: bool):
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options

    opts = Options()
    opts.add_argument("--inprivate")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--lang=en-US")
    opts.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
    return webdriver.Edge(options=opts)
