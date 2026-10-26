"""One lazy import boundary for bundled Windows and official portable Selenium."""

import importlib
import sys
import sysconfig
from pathlib import Path

PACKAGE = (
    "gformbot._vendor.selenium"
    if sys.platform == "win32"
    and sysconfig.get_platform() == "win-amd64"
    and (Path(__file__).parent / "_vendor/selenium/__init__.py").is_file()
    else "selenium"
)

_EXPORTS = {
    "webdriver": ("webdriver", None),
    "By": ("webdriver.common.by", "By"),
    "WebDriverWait": ("webdriver.support.wait", "WebDriverWait"),
    "Select": ("webdriver.support.select", "Select"),
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
