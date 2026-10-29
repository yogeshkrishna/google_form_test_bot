"""Fresh private Selenium sessions for supported installed browsers."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

BrowserName = Literal["chrome", "firefox", "edge", "auto", "headless"]

_WINDOWS = sys.platform == "win32"
_MACOS = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Default-browser detection
# ---------------------------------------------------------------------------


def _default_browser_windows() -> BrowserName | None:
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
    return None


def _default_browser_macos() -> BrowserName | None:
    try:
        result = subprocess.run(
            ["defaults", "export", "com.apple.LaunchServices/com.apple.launchservices.secure", "-"],
            capture_output=True,
            timeout=5,
        )
        handlers = plistlib.loads(result.stdout).get("LSHandlers", [])
        for scheme in ("https", "http"):
            for handler in handlers:
                if handler.get("LSHandlerURLScheme", "").lower() != scheme:
                    continue
                identifier = handler.get("LSHandlerRoleAll", "").lower()
                if "firefox" in identifier:
                    return "firefox"
                if "edgemac" in identifier or "msedge" in identifier:
                    return "edge"
                if "chrome" in identifier or "chromium" in identifier:
                    return "chrome"
                return None
    except Exception:
        pass
    return None


def _default_browser_linux() -> BrowserName | None:
    try:
        result = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        val = result.stdout.lower()
        if "firefox" in val:
            return "firefox"
        if "chrome" in val or "chromium" in val:
            return "chrome"
        if "microsoft-edge" in val or "msedge" in val:
            return "edge"
    except Exception:
        pass
    return None


def available_browsers() -> list[BrowserName]:
    """Find supported local installations without launching a browser."""
    commands = {
        "chrome": ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"),
        "firefox": ("firefox",),
        "edge": ("microsoft-edge", "microsoft-edge-stable", "msedge"),
    }
    installed = {name for name, names in commands.items() if any(shutil.which(command) for command in names)}
    if _WINDOWS:
        relative_paths = {
            "chrome": "Google/Chrome/Application/chrome.exe",
            "firefox": "Mozilla Firefox/firefox.exe",
            "edge": "Microsoft/Edge/Application/msedge.exe",
        }
        roots = [
            Path(value)
            for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if (value := os.environ.get(key))
        ]
        for name, relative in relative_paths.items():
            if name not in installed and any((root / relative).is_file() for root in roots):
                installed.add(name)
    elif _MACOS:
        relative_paths = {
            "chrome": (
                "Google Chrome.app/Contents/MacOS/Google Chrome",
                "Chromium.app/Contents/MacOS/Chromium",
            ),
            "firefox": ("Firefox.app/Contents/MacOS/firefox",),
            "edge": ("Microsoft Edge.app/Contents/MacOS/Microsoft Edge",),
        }
        roots = (Path("/Applications"), Path.home() / "Applications")
        for name, relatives in relative_paths.items():
            if name not in installed and any(
                (root / relative).is_file() for root in roots for relative in relatives
            ):
                installed.add(name)
    return [name for name in commands if name in installed]


def detect_default_browser() -> BrowserName:
    available = available_browsers()
    if not available:
        return "chrome"  # Keep get_driver's actionable installation error.
    if _WINDOWS:
        preferred = _default_browser_windows()
    elif _MACOS:
        preferred = _default_browser_macos()
    else:
        preferred = _default_browser_linux()
    return preferred if preferred in available else available[0]


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
    driver = None
    try:
        driver = factories[browser](headless)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(30)
        return driver
    except Exception as exc:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass  # Preserve the original initialization error.
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise RuntimeError(
            f"Could not start {browser}: {detail}. Check that the browser is installed."
        ) from exc


def _chrome_driver(headless: bool):
    from .selenium_runtime import options, webdriver

    opts = options("chrome")
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
    from .selenium_runtime import options, webdriver

    opts = options("firefox")
    opts.add_argument("-private")
    opts.set_preference("intl.accept_languages", "en-US,en")
    if headless:
        opts.add_argument("--headless")
    return webdriver.Firefox(options=opts)


def _edge_driver(headless: bool):
    from .selenium_runtime import options, webdriver

    opts = options("edge")
    opts.add_argument("--inprivate")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--lang=en-US")
    opts.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")
    return webdriver.Edge(options=opts)
