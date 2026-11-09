"""One lazy import boundary for the private subset and portable Selenium fallback."""

import importlib
import platform
import sys
from pathlib import Path


def _bundled_available():
    arch = platform.machine().lower()
    if sys.platform == "linux":
        libc, version = platform.libc_ver()
        if libc != "glibc" or tuple(map(int, version.split("."))) < (2, 17):
            return False
    manager = {
        "win32": {"amd64": "windows", "x86_64": "windows"},
        "darwin": {"x86_64": "macos", "arm64": "macos"},
        "linux": {"x86_64": "linux-x86_64", "aarch64": "linux-arm64"},
    }.get(sys.platform, {}).get(arch)
    directory = Path(__file__).parent / "_vendor/selenium"
    executable = "selenium-manager.exe" if sys.platform == "win32" else "selenium-manager"
    return (
        manager is not None
        and (directory / "__init__.py").is_file()
        and (directory / "webdriver/common" / manager / executable).is_file()
    )


PACKAGE = "gformbot._vendor.selenium" if _bundled_available() else "selenium"

_EXPORTS = {
    "webdriver": ("webdriver", None),
    "By": ("webdriver.common.by", "By"),
    "WebDriverWait": ("webdriver.support.wait", "WebDriverWait"),
}


def options(browser):
    return importlib.import_module(f"{PACKAGE}.webdriver.{browser}.options").Options()


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    module, attribute = _EXPORTS.get(name, ("common.exceptions", name))
    imported = importlib.import_module(PACKAGE + "." + module)
    value = getattr(imported, attribute) if attribute else imported
    globals()[name] = value
    return value
