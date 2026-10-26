"""Check runtime contents and dependency contracts in a built distribution."""

import argparse
import hashlib
import json
import zipfile
from email.parser import BytesParser
from pathlib import Path


def check(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata = BytesParser().parsebytes(
            archive.read(next(n for n in names if n.endswith(".dist-info/METADATA")))
        )
        requires = metadata.get_all("Requires-Dist", [])
        windows = path.name.endswith("-win_amd64.whl")
        prefix = "gformbot/_vendor/selenium/"
        bundled = {n.removeprefix(prefix): archive.read(n) for n in names if n.startswith(prefix)}
        if windows:
            manifest = json.loads(bundled.pop("manifest.json"))
            actual = {name: hashlib.sha256(value).hexdigest() for name, value in bundled.items()}
            if actual != manifest["files"]:
                raise ValueError("Windows wheel does not contain the exact audited Selenium subset.")
            if any(r.lower().startswith(("selenium", "trio", "websocket")) for r in requires):
                raise ValueError("Windows wheel still requires the full protocol stack.")
        elif bundled or not any(r.lower().startswith("selenium") for r in requires):
            raise ValueError("Portable wheels must retain official Selenium and omit Windows-only data.")
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
