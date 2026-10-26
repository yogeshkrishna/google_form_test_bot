"""Rebuild GFormBot's private Windows runtime from a hash-locked upstream wheel."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "gformbot" / "_vendor" / "selenium"
VERSION = "4.51.0"
URL = "https://files.pythonhosted.org/packages/74/6d/2cae01adf4db83e2862e254bc053def829a9603a1c68431b7c9aae0ecba0/selenium-4.51.0-py3-none-any.whl"
SHA256 = "5531e99df3c60a298c4bef38de825aec4aa8ca238438ac21511d650b58dbdd87"
PREFIX = "gformbot._vendor.selenium"
ROOTS = [
    f"selenium.webdriver.{browser}.{module}"
    for browser in ("chrome", "edge", "firefox")
    for module in ("webdriver", "options", "service")
]
ROOTS += ["selenium.common.exceptions", "selenium.webdriver.common.by", "selenium.webdriver.support.ui"]
RESOURCES = [
    "webdriver/remote/getAttribute.js",
    "webdriver/remote/isDisplayed.js",
    "webdriver/remote/findElements.js",
    "webdriver/common/windows/selenium-manager.exe",
    "webdriver/common/selenium-manager-THIRD-PARTY-NOTICES.txt",
]


def remove_nodes(source, nodes):
    lines = source.splitlines(keepends=True)
    for node in sorted(nodes, key=lambda node: node.lineno, reverse=True):
        start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))]) - 1
        del lines[start : node.end_lineno]
    return "".join(lines)


def patch(source, name):
    """Keep synchronous WebDriver intact; omit the independent unused protocol feature block."""
    original = source
    if name == "selenium.webdriver.remote.webdriver":
        tree = ast.parse(source)
        driver = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WebDriver"
        )
        functions = [
            node for node in driver.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        first = next(i for i, node in enumerate(functions) if node.name == "start_devtools")
        last = next(i for i, node in enumerate(functions) if node.name == "_get_cdp_details")
        assert last - first == 14, "Upstream protocol block changed; review before rebuilding."
        nodes = functions[first : last + 1]
        nodes += [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and (
                ".common.bidi." in (node.module or "")
                or node.module
                in {
                    "selenium.webdriver.remote.bidi_connection",
                    "selenium.webdriver.remote.websocket_connection",
                }
            )
        ]
        nodes += [
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "import_cdp"
        ]
        source = remove_nodes(source, nodes)
        source = source.replace(
            '    elif browser_name in ("safari", "Safari Technology Preview"):\n'
            "        from selenium.webdriver.safari.remote_connection import SafariRemoteConnection\n\n"
            "        handler = SafariRemoteConnection\n",
            "",
        )
        assert "SafariRemoteConnection" not in source
        source = source.replace(
            "from typing_extensions import Self\n",
            "try:\n    from typing import Self\nexcept ImportError:  # Python 3.10\n    from typing_extensions import Self\n",
        )
    # Change imports and qualified module references, including lazy-import/resource strings.
    source = re.sub(r"(?m)^(\s*(?:from|import)\s+)selenium\b", rf"\g<1>{PREFIX}", source)
    source = re.sub(r"(?P<quote>['\"])selenium(?=\.[a-z_]|['\"])", rf"\g<quote>{PREFIX}", source)
    if source != original:
        source = (
            "# Modified by GFormBot for its private Windows runtime; see NOTICE and tools/vendor_selenium.py.\n"
            + source
        )
    return source


class Imports(ast.NodeVisitor):
    def __init__(self):
        self.names = set()

    def visit_If(self, node):
        if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            return
        self.generic_visit(node)

    def visit_Import(self, node):
        self.names.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node):
        if node.module:
            self.names.add(node.module)
            self.names.update(node.module + "." + alias.name for alias in node.names)


def generate(wheel):
    if hashlib.sha256(wheel).hexdigest() != SHA256:
        raise ValueError("Upstream wheel SHA-256 mismatch; refusing to vendor it.")
    archive = zipfile.ZipFile(io.BytesIO(wheel))
    sources = {}
    for filename in archive.namelist():
        if filename.startswith("selenium/") and filename.endswith(".py"):
            name = filename[:-3].replace("/", ".").removesuffix(".__init__")
            sources[name] = (filename, archive.read(filename).decode("utf-8"))
    pending, selected = list(ROOTS), {}
    while pending:
        name = pending.pop()
        if name in selected or name not in sources:
            continue
        filename, source = sources[name]
        source = patch(source, name)
        selected[name] = (filename, source)
        visitor = Imports()
        visitor.visit(ast.parse(source))
        pending.extend(n.replace(PREFIX, "selenium", 1) for n in visitor.names if n.startswith(PREFIX))
        parts = name.split(".")
        pending.extend(".".join(parts[:i]) for i in range(1, len(parts)))
    # webdriver.__init__ uses lazy string-based imports: expose only retained modules.
    name = "selenium.webdriver"
    filename, source = selected[name]
    tree = ast.parse(source)
    for node in reversed(tree.body):
        if isinstance(node, ast.Assign) and node.targets[0].id in {"_LAZY_IMPORTS", "_LAZY_SUBMODULES"}:
            values = ast.literal_eval(node.value)
            values = {
                key: value
                for key, value in values.items()
                if (value[0] if isinstance(value, tuple) else value).replace(PREFIX, "selenium", 1)
                in selected
            }
            lines = source.splitlines(keepends=True)
            lines[node.lineno - 1 : node.end_lineno] = [node.targets[0].id + " = " + repr(values) + "\n"]
            source = "".join(lines)
    selected[name] = (filename, source)
    output = {
        filename.removeprefix("selenium/"): source.encode("utf-8") for filename, source in selected.values()
    }
    output.update({path: archive.read("selenium/" + path) for path in RESOURCES})
    for name in ("LICENSE", "NOTICE"):
        output[name] = archive.read(f"selenium-{VERSION}.dist-info/{name}")
    output["NOTICE"] += (
        b"\nGFormBot modifications: private import namespace, supported browser exports, "
        b"standard-library Self on Python 3.11+, "
        b"and removal of unused BiDi/CDP protocol code and non-Windows assets. "
        b"Rebuild with tools/vendor_selenium.py from the SHA-256-locked upstream wheel.\n"
    )
    manifest = {
        "upstream": URL,
        "sha256": SHA256,
        "version": VERSION,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(output.items())},
    }
    output["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, help="Use a cached upstream wheel instead of downloading.")
    parser.add_argument("--check", action="store_true", help="Verify the checked-in subset without writing.")
    args = parser.parse_args()
    wheel = args.wheel.read_bytes() if args.wheel else urllib.request.urlopen(URL, timeout=60).read()
    output = generate(wheel)
    existing = {
        p.relative_to(DEST).as_posix(): p.read_bytes()
        for p in DEST.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    if args.check:
        if existing != output:
            raise SystemExit("Vendored Selenium differs from the pinned recipe.")
    else:
        if set(existing) - output.keys():
            raise SystemExit("Unexpected files in vendor directory; review them before regeneration.")
        for name, data in output.items():
            target = DEST / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    print(f"Verified {len(output)} files, {sum(map(len, output.values())):,} bytes.")


if __name__ == "__main__":
    main()
