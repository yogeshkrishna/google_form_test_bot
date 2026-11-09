"""Check platform runtime contents and dependency contracts in a built wheel."""

import argparse
import hashlib
import json
import struct
import zipfile
from email.parser import BytesParser
from pathlib import Path

MANAGERS = {
    "win_amd64": "windows/selenium-manager.exe",
    "macosx_10_12_x86_64": "macos/selenium-manager",
    "macosx_11_0_arm64": "macos/selenium-manager",
    "manylinux_2_17_x86_64": "linux-x86_64/selenium-manager",
    "manylinux_2_17_aarch64": "linux-arm64/selenium-manager",
}


def check(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Wheel contains duplicate archive entries.")
        if "gformbot/editor.html" not in names:
            raise ValueError("Wheel is missing the local response editor.")
        if "gformbot/filler.py" in names:
            raise ValueError("Wheel still contains the retired Selenium form filler.")
        metadata = BytesParser().parsebytes(
            archive.read(next(n for n in names if n.endswith(".dist-info/METADATA")))
        )
        requires = metadata.get_all("Requires-Dist", [])
        target = path.stem.rsplit("-", 1)[-1]
        prefix = "gformbot/_vendor/selenium/"
        bundled = {n.removeprefix(prefix): archive.read(n) for n in names if n.startswith(prefix)}
        if target in MANAGERS:
            manifest = json.loads(bundled.pop("manifest.json"))
            actual = {name: hashlib.sha256(value).hexdigest() for name, value in bundled.items()}
            if actual != manifest["files"] or manifest.get("platform") != target:
                raise ValueError("Wheel does not contain its exact audited Selenium subset.")
            manager = "webdriver/common/" + MANAGERS[target]
            binaries = {
                name for name in bundled if Path(name).name in {"selenium-manager", "selenium-manager.exe"}
            }
            if binaries != {manager}:
                raise ValueError("Wheel must contain exactly its own native Manager.")
            data = bundled[manager]
            if target.startswith("macosx"):
                expected = 0x0100000C if target.endswith("arm64") else 0x01000007
                if data[:4] != b"\xcf\xfa\xed\xfe" or struct.unpack_from("<I", data, 4)[0] != expected:
                    raise ValueError("Mac Manager is not the expected thin Mach-O architecture.")
            elif target.startswith("manylinux"):
                expected = 183 if target.endswith("aarch64") else 62
                if data[:4] != b"\x7fELF" or struct.unpack_from("<H", data, 18)[0] != expected:
                    raise ValueError("Linux Manager has the wrong ELF architecture.")
            elif data[:2] != b"MZ":
                raise ValueError("Windows Manager is not a PE executable.")
            if (
                not target.startswith("win")
                and not archive.getinfo(prefix + manager).external_attr >> 16 & 0o111
            ):
                raise ValueError("Native Manager is not marked executable in the wheel.")
            if any(r.lower().startswith(("selenium", "trio", "websocket")) for r in requires):
                raise ValueError("Subset wheel still requires the full protocol stack.")
        elif bundled or not any(r.lower().startswith("selenium") for r in requires):
            raise ValueError("Portable wheels must retain official Selenium and omit private assets.")
        if any(n.startswith("selenium/") or n.startswith(".work/") or n.endswith(".pyc") for n in names):
            raise ValueError("Wheel contains conflicting or generated files.")
    print(f"Verified {path.name}: {path.stat().st_size:,} bytes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    wheels = list(args.directory.glob("*.whl"))
    if not wheels:
        raise SystemExit("No wheels found.")
    for path in wheels:
        check(path)
