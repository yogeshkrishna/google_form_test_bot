"""Check the universal wheel and source archive before installation."""

import argparse
import re
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

_REQUIRED = {"gformbot/chatgpt_http.py", "gformbot/editor.html", "gformbot/transport.py"}
_RETIRED = {
    "gformbot/browser.py",
    "gformbot/chatgpt.py",
    "gformbot/filler.py",
    "gformbot/selenium_runtime.py",
    "setup.py",
}
_EXCLUDED_PREFIXES = (".work/", ".venv/", "gformbot/_vendor/", "selenium/", "tests/", "tools/")
_BINARY_SUFFIXES = (".dll", ".dylib", ".exe", ".pyc", ".pyd", ".so", "/selenium-manager")


def _check_files(names, required=_REQUIRED):
    if len(names) != len(set(names)):
        raise ValueError("Archive contains duplicate entries.")
    if missing := required - set(names):
        raise ValueError(f"Archive is missing required files: {', '.join(sorted(missing))}.")
    for name in names:
        if name in _RETIRED or name.startswith(_EXCLUDED_PREFIXES) or name.endswith(_BINARY_SUFFIXES):
            raise ValueError(f"Archive contains a retired, private, or generated file: {name}.")


def _wheel_metadata(archive, filename):
    (name,) = [n for n in archive.namelist() if n.endswith(f".dist-info/{filename}")]
    return BytesParser().parsebytes(archive.read(name))


def check_wheel(path):
    with zipfile.ZipFile(path) as archive:
        _check_files(archive.namelist())
        wheel = _wheel_metadata(archive, "WHEEL")
        if (
            not path.name.endswith("-py3-none-any.whl")
            or wheel.get_all("Tag") != ["py3-none-any"]
            or wheel.get("Root-Is-Purelib", "").lower() != "true"
        ):
            raise ValueError("Expected one universal pure-Python wheel.")
        metadata = _wheel_metadata(archive, "METADATA")
        if set(metadata.get_all("License-File", [])) != {"LICENSE", "LICENSE-chatgpt-protocol"}:
            raise ValueError("Wheel is missing the project license or the protocol notice.")
        requirements = metadata.get_all("Requires-Dist", [])
        names = {re.split(r"[\s\[<>=!~;]", value, maxsplit=1)[0].lower() for value in requirements}
        if not {"urllib3", "certifi"} <= names:
            raise ValueError("Wheel is missing HTTPS requirements.")
        if names & {"selenium", "pysocks", "trio", "trio-websocket", "websocket-client"} or any(
            re.match(r"urllib3\s*\[", value, re.I) for value in requirements
        ):
            raise ValueError("Wheel still includes browser-transport requirements.")


def check_sdist(path):
    prefix = path.name.removesuffix(".tar.gz") + "/"
    with tarfile.open(path, "r:gz") as archive:
        names = []
        for entry in archive:
            if entry.isdir():
                continue
            if not entry.isfile() or not entry.name.startswith(prefix):
                raise ValueError(f"Source archive contains an unexpected entry: {entry.name}.")
            names.append(entry.name.removeprefix(prefix))
    _check_files(
        names,
        _REQUIRED | {"pyproject.toml", "LICENSE", "LICENSE-chatgpt-protocol", "README.md", "SECURITY.md"},
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    wheels = list(args.directory.glob("*.whl"))
    sdists = list(args.directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit("Expected exactly one wheel and one source archive.")
    for path, check in ((wheels[0], check_wheel), (sdists[0], check_sdist)):
        check(path)
        print(f"Verified {path.name}: {path.stat().st_size:,} bytes.")
