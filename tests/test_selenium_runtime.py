"""Exercise build target selection without invoking setuptools or downloading assets."""

import ast
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def build_helpers():
    tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8-sig"))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {"struct": struct}
    exec(compile(tree, "setup.py", "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    "system,arch,libc,expected",
    [
        ("win32", "AMD64", ("", ""), "win_amd64"),
        ("win32", "ARM64", ("", ""), None),
        ("darwin", "x86_64", ("", ""), "macosx_10_12_x86_64"),
        ("darwin", "arm64", ("", ""), "macosx_11_0_arm64"),
        ("linux", "x86_64", ("glibc", "2.17"), "manylinux_2_17_x86_64"),
        ("linux", "aarch64", ("glibc", "2.39"), "manylinux_2_17_aarch64"),
        ("linux", "x86_64", ("glibc", "2.16"), None),
        ("linux", "x86_64", ("musl", "1.2"), None),
        ("linux", "riscv64", ("glibc", "2.39"), None),
        ("freebsd", "x86_64", ("", ""), None),
    ],
)
def test_native_build_target(build_helpers, system, arch, libc, expected):
    build_helpers.update(
        sys=SimpleNamespace(platform=system),
        platform=SimpleNamespace(machine=lambda: arch, libc_ver=lambda: libc),
    )
    assert build_helpers["native_target"]() == expected


@pytest.mark.parametrize(
    "target,cpu", [("macosx_10_12_x86_64", 0x01000007), ("macosx_11_0_arm64", 0x0100000C)]
)
def test_macos_retains_exact_upstream_slice(build_helpers, target, cpu):
    path = ROOT / "gformbot/_vendor/selenium/webdriver/common/macos/selenium-manager"
    data = path.read_bytes()
    thin = build_helpers["thin_macos"](data, target)
    assert thin[:4] == b"\xcf\xfa\xed\xfe"
    assert struct.unpack_from("<I", thin, 4)[0] == cpu
    assert thin in data
    assert len(thin) < len(data)


def test_macos_rejects_wrong_container(build_helpers):
    with pytest.raises(ValueError, match="universal Mach-O"):
        build_helpers["thin_macos"](b"invalid", "macosx_11_0_arm64")


def test_macos_rejects_missing_slice(build_helpers):
    with pytest.raises(ValueError, match="no valid slice"):
        build_helpers["thin_macos"](b"\xca\xfe\xba\xbe\x00\x00\x00\x00", "macosx_11_0_arm64")


def test_editable_source_falls_back_on_unsupported_cpu(monkeypatch):
    from gformbot import selenium_runtime

    monkeypatch.setattr(selenium_runtime.platform, "machine", lambda: "riscv64")
    assert not selenium_runtime._bundled_available()


def test_missing_native_asset_does_not_select_private_runtime(monkeypatch):
    from gformbot import selenium_runtime

    original = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda path: False if "selenium-manager" in path.name else original(path)
    )
    assert not selenium_runtime._bundled_available()


@pytest.mark.parametrize("libc", [("musl", "1.2"), ("glibc", "2.16")])
def test_editable_source_uses_same_linux_fallback_as_packaging(monkeypatch, libc):
    from gformbot import selenium_runtime

    with monkeypatch.context() as patch:
        patch.setattr(selenium_runtime.sys, "platform", "linux")
        patch.setattr(selenium_runtime.platform, "machine", lambda: "x86_64")
        patch.setattr(selenium_runtime.platform, "libc_ver", lambda: libc)
        assert not selenium_runtime._bundled_available()
