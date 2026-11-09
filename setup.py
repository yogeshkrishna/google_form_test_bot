"""Build a private Selenium subset with only the target platform's native Manager."""

import hashlib
import json
import os
import platform
import struct
import sys
import zipfile
from pathlib import Path

from setuptools import find_packages, setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py

MANAGERS = {
    "win_amd64": "windows/selenium-manager.exe",
    "macosx_10_12_x86_64": "macos/selenium-manager",
    "macosx_11_0_arm64": "macos/selenium-manager",
    "manylinux_2_17_x86_64": "linux-x86_64/selenium-manager",
    "manylinux_2_17_aarch64": "linux-arm64/selenium-manager",
}


def native_target():
    arch = platform.machine().lower()
    if sys.platform == "win32" and arch in {"amd64", "x86_64"}:
        return "win_amd64"
    if sys.platform == "darwin":
        return {"x86_64": "macosx_10_12_x86_64", "arm64": "macosx_11_0_arm64"}.get(arch)
    if sys.platform == "linux":
        libc, version = platform.libc_ver()
        if libc == "glibc" and tuple(map(int, version.split("."))) >= (2, 17):
            return {"x86_64": "manylinux_2_17_x86_64", "aarch64": "manylinux_2_17_aarch64"}.get(arch)
    return None


TARGET = os.environ.get("GFORMBOT_BUILD_PLATFORM") or native_target()
if TARGET and TARGET not in MANAGERS:
    raise ValueError(f"Unsupported GFORMBOT_BUILD_PLATFORM: {TARGET}")
SUBSET = TARGET is not None and os.environ.get("GFORMBOT_BUILD_FULL_SELENIUM") != "1"


def thin_macos(data, target):
    """Retain the upstream signed Mach-O slice unchanged, excluding the other CPU."""
    if data[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("Expected upstream universal Mach-O Manager.")
    cpu = 0x0100000C if target.endswith("arm64") else 0x01000007
    count = struct.unpack_from(">I", data, 4)[0]
    for index in range(count):
        kind, _, offset, size, _ = struct.unpack_from(">IIIII", data, 8 + index * 20)
        if kind == cpu and offset + size <= len(data):
            return data[offset : offset + size]
    raise ValueError(f"Upstream Manager has no valid slice for {target}.")


class BuildPy(build_py):
    def run(self):
        super().run()
        application = Path(self.build_lib) / "gformbot"
        source_application = Path(self.get_package_dir("gformbot"))
        for path in application.glob("*.py"):
            if not (source_application / path.name).is_file():
                path.unlink()
        if not SUBSET:
            return
        directory = Path(self.build_lib) / "gformbot/_vendor/selenium"
        source = Path(self.get_package_dir("gformbot._vendor.selenium"))
        manifest_path = directory / "manifest.json"
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        manager = "webdriver/common/" + MANAGERS[TARGET]
        manifest["files"] = {
            name: digest
            for name, digest in manifest["files"].items()
            if "selenium-manager" not in name or name == manager or name.endswith("NOTICES.txt")
        }
        # Build directories may contain modules retired by a newer vendor recipe.
        for path in directory.rglob("*"):
            if (
                path.is_file()
                and path.name != "manifest.json"
                and path.relative_to(directory).as_posix() not in manifest["files"]
            ):
                path.unlink()
        if TARGET.startswith("macosx"):
            path = directory / manager
            data = thin_macos((source / manager).read_bytes(), TARGET)
            path.write_bytes(data)
            manifest["files"][manager] = hashlib.sha256(data).hexdigest()
        manifest["platform"] = TARGET
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Wheel(bdist_wheel):
    def get_tag(self):
        return ("py3", "none", TARGET) if SUBSET else super().get_tag()

    def run(self):
        super().run()
        # Windows cannot express POSIX executable modes on disk. Set them in the
        # wheel itself so cross-built Mac/Linux packages install working Managers.
        path = Path(self.dist_dir) / (self.wheel_dist_name + "-" + "-".join(self.get_tag()) + ".whl")
        temporary = path.with_suffix(".tmp")
        try:
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as output:
                for entry in source.infolist():
                    entry.create_system = 3
                    mode = 0o100755 if entry.filename.endswith("/selenium-manager") else 0o100644
                    entry.external_attr = mode << 16
                    output.writestr(entry, source.read(entry.filename), zipfile.ZIP_DEFLATED, compresslevel=9)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


setup(
    packages=find_packages(include=["gformbot*"], exclude=[] if SUBSET else ["gformbot._vendor*"]),
    install_requires=[
        "click>=8.1,<9",
        "pyyaml>=6.0,<7",
        "rich>=13.0,<16",
        "InquirerPy>=0.3.4,<0.4",
        "certifi>=2026.2.25",
        "urllib3[socks]>=2.6.3,<3",
    ]
    + (["typing_extensions>=4.15,<5; python_version<'3.11'"] if SUBSET else ["selenium>=4.25,<5"]),
    include_package_data=False,
    package_data={
        "gformbot": ["editor.html"],
        **(
            {
                "gformbot._vendor.selenium": [
                    "LICENSE",
                    "NOTICE",
                    "manifest.json",
                    "webdriver/remote/*.js",
                    "webdriver/common/" + MANAGERS[TARGET],
                    "webdriver/common/*NOTICES.txt",
                ]
            }
            if SUBSET
            else {}
        ),
    },
    cmdclass={"bdist_wheel": Wheel, "build_py": BuildPy},
    options={"build": {"build_base": "build/" + (TARGET if SUBSET else "portable")}},
)
