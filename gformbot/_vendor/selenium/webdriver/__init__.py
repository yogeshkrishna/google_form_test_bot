# Licensed to the Software Freedom Conservancy (SFC) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The SFC licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import importlib
import logging
import os

# Enable debug logging if SE_DEBUG environment variable is set
if os.environ.get("SE_DEBUG"):
    logger = logging.getLogger("gformbot._vendor.selenium")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.warning(
        "Environment Variable `SE_DEBUG` is set; "
        "Selenium is forcing verbose logging which may override user-specified settings."
    )

__version__ = "4.51.0"

# Lazy import mapping: name -> (module_path, attribute_name)
_LAZY_IMPORTS = {'Chrome': ('gformbot._vendor.selenium.webdriver.chrome.webdriver', 'WebDriver'), 'ChromeOptions': ('gformbot._vendor.selenium.webdriver.chrome.options', 'Options'), 'ChromeService': ('gformbot._vendor.selenium.webdriver.chrome.service', 'Service'), 'Edge': ('gformbot._vendor.selenium.webdriver.edge.webdriver', 'WebDriver'), 'ChromiumEdge': ('gformbot._vendor.selenium.webdriver.edge.webdriver', 'WebDriver'), 'EdgeOptions': ('gformbot._vendor.selenium.webdriver.edge.options', 'Options'), 'EdgeService': ('gformbot._vendor.selenium.webdriver.edge.service', 'Service'), 'Firefox': ('gformbot._vendor.selenium.webdriver.firefox.webdriver', 'WebDriver'), 'FirefoxOptions': ('gformbot._vendor.selenium.webdriver.firefox.options', 'Options'), 'FirefoxProfile': ('gformbot._vendor.selenium.webdriver.firefox.firefox_profile', 'FirefoxProfile'), 'FirefoxService': ('gformbot._vendor.selenium.webdriver.firefox.service', 'Service'), 'Remote': ('gformbot._vendor.selenium.webdriver.remote.webdriver', 'WebDriver'), 'DesiredCapabilities': ('gformbot._vendor.selenium.webdriver.common.desired_capabilities', 'DesiredCapabilities'), 'Keys': ('gformbot._vendor.selenium.webdriver.common.keys', 'Keys'), 'Proxy': ('gformbot._vendor.selenium.webdriver.common.proxy', 'Proxy')}

# Submodules that can be lazily imported as modules
_LAZY_SUBMODULES = {'chrome': 'gformbot._vendor.selenium.webdriver.chrome', 'chromium': 'gformbot._vendor.selenium.webdriver.chromium', 'common': 'gformbot._vendor.selenium.webdriver.common', 'edge': 'gformbot._vendor.selenium.webdriver.edge', 'firefox': 'gformbot._vendor.selenium.webdriver.firefox', 'remote': 'gformbot._vendor.selenium.webdriver.remote', 'support': 'gformbot._vendor.selenium.webdriver.support'}


def __getattr__(name):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    if name in _LAZY_SUBMODULES:
        module = importlib.import_module(_LAZY_SUBMODULES[name])
        globals()[name] = module
        return module
    raise AttributeError(f"module 'gformbot._vendor.selenium.webdriver' has no attribute {name!r}")


def __dir__():
    return sorted(set(__all__) | set(_LAZY_SUBMODULES.keys()))


# PLE0605 wants a literal list/tuple so that the export list is statically
# readable. Type checkers read __init__.pyi, which re-exports every name
# explicitly, so deriving __all__ from _LAZY_IMPORTS here costs nothing
# statically and keeps the two from drifting apart at runtime.
__all__ = sorted(_LAZY_IMPORTS.keys())  # noqa: PLE0605
