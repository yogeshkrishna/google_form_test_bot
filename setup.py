"""Bundle the private runtime only in native Windows x64 wheels."""

import os
import sys
import sysconfig

from setuptools import find_packages, setup
from setuptools.command.bdist_wheel import bdist_wheel

SUBSET = (
    sys.platform == "win32"
    and sysconfig.get_platform() == "win-amd64"
    and os.environ.get("GFORMBOT_BUILD_FULL_SELENIUM") != "1"
)


class Wheel(bdist_wheel):
    def get_tag(self):
        return ("py3", "none", "win_amd64") if SUBSET else super().get_tag()


setup(
    packages=find_packages(include=["gformbot*"], exclude=[] if SUBSET else ["gformbot._vendor*"]),
    install_requires=[
        "click>=8.1,<9",
        "pyyaml>=6.0,<7",
        "rich>=13.0,<15",
        "InquirerPy>=0.3.4,<0.4",
        "certifi>=2026.2.25",
        "urllib3[socks]>=2.6.3,<3",
    ]
    + (
        [
            "typing_extensions>=4.15,<5; python_version<'3.11'",
        ]
        if SUBSET
        else ["selenium>=4.25,<5"]
    ),
    include_package_data=False,
    package_data={
        "gformbot._vendor.selenium": [
            "LICENSE",
            "NOTICE",
            "manifest.json",
            "webdriver/remote/*.js",
            "webdriver/common/windows/*.exe",
            "webdriver/common/*NOTICES.txt",
        ]
    }
    if SUBSET
    else {},
    cmdclass={"bdist_wheel": Wheel},
    options={"build": {"build_base": "build/windows" if SUBSET else "build/portable"}},
)
