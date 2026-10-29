"""Compare installed runtimes in isolated interpreters; output contains measurements, not user data."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

IMPORT = """
import json, time, sys
start = time.perf_counter()
import gformbot.filler
try:
    from gformbot.selenium_runtime import webdriver
except ModuleNotFoundError:
    from selenium import webdriver
classes = [webdriver.Chrome, webdriver.Edge, webdriver.Firefox]
elapsed = time.perf_counter() - start
if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in
        ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
         'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    peak = counters.PeakWorkingSetSize
else:
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
print(json.dumps({'import_seconds':elapsed, 'python_peak_rss_bytes':peak,
 'selenium_modules':sum('selenium' in n for n in sys.modules)}))
"""

SIZES = """
import importlib.metadata as m, json
result = {}
for d in m.distributions():
    size = bytecode = 0
    for f in d.files or []:
        p = d.locate_file(f)
        if p.is_file():
            if p.suffix == '.pyc': bytecode += p.stat().st_size
            else: size += p.stat().st_size
    result[d.metadata['Name']] = {'bytes': size, 'bytecode_bytes': bytecode, 'requires': d.requires or [], 'version':d.version}
print(json.dumps(result))
"""

BROWSER = r"""
import json, time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from gformbot.browser import get_driver
name = __import__('sys').argv[1]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = '<input id="text"><select id="choice"><option>A</option><option>B</option></select><button onclick="this.textContent=\'done\'">go</button>' if self.path == '/form' else '<title>second</title>' if self.path == '/second' else '<title>ready</title>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}'
start = time.perf_counter()
driver = get_driver(name)
driver.get(base + '/ready')
startup = time.perf_counter() - start
try:
    start = time.perf_counter()
    driver.get(base + '/form')
    field = driver.find_element('id', 'text')
    field.send_keys('A [literal] café')
    assert field.get_attribute('value') == 'A [literal] café'
    field.clear()
    driver.execute_script('arguments[0].value = arguments[1]', field, 'new value')
    assert field.get_attribute('value') == 'new value'
    button = driver.find_element('tag name', 'button')
    assert button.is_displayed()
    button.click()
    assert button.text == 'done'
    try:
        from gformbot.selenium_runtime import Select
    except ModuleNotFoundError:
        from selenium.webdriver.support.ui import Select
    Select(driver.find_element('id', 'choice')).select_by_visible_text('B')
    first = driver.current_window_handle
    driver.switch_to.new_window('tab')
    driver.get(base + '/second')
    assert driver.title == 'second' and len(driver.window_handles) == 2
    driver.close()
    driver.switch_to.window(first)
    operations = time.perf_counter() - start
finally:
    start = time.perf_counter()
    driver.quit()
    cleanup = time.perf_counter() - start
    server.shutdown()
    server.server_close()
print(json.dumps({'startup_seconds':startup, 'operations_seconds':operations, 'cleanup_seconds':cleanup,
                  'total_seconds': startup + operations + cleanup}))
"""


def closure(distributions):
    packages = {canonicalize_name(name): values for name, values in distributions.items()}
    needed, pending = {}, [("gformbot", frozenset())]
    visited = set()
    while pending:
        name, extras = pending.pop()
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        values = packages[name]
        needed[name] = {k: v for k, v in values.items() if k != "requires"}
        for text in values["requires"]:
            requirement = Requirement(text)
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in {"", *extras}
            ):
                pending.append((canonicalize_name(requirement.name), frozenset(requirement.extras)))
    return needed


def summarize(samples):
    return {
        name: {
            "median": statistics.median(s[name] for s in samples),
            "min": min(s[name] for s in samples),
            "max": max(s[name] for s in samples),
        }
        for name in samples[0]
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--compare-python", type=Path, help="Alternate runs against a second clean environment."
    )
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--browser-samples", type=int, default=3)
    args = parser.parse_args()
    executable = str(args.python.resolve())
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    environment["SE_AVOID_STATS"] = "true"
    with tempfile.TemporaryDirectory(prefix="gformbot-benchmark-") as directory:

        def run(code, *arguments, python=executable):
            result = subprocess.run(
                [python, "-c", code, *arguments],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr)
            return json.loads(result.stdout)

        targets = (
            {"baseline": executable, "subset": str(args.compare_python.resolve())}
            if args.compare_python
            else {"runtime": executable}
        )
        measurements = {
            label: {"installed": closure(run(SIZES, python=target)), "imports": [], "browsers": {}}
            for label, target in targets.items()
        }
        for sample in range(args.samples):
            for label in list(targets)[:: 1 if sample % 2 == 0 else -1]:
                measurements[label]["imports"].append(run(IMPORT, python=targets[label]))
        names = run(
            "import json; from gformbot.browser import available_browsers; print(json.dumps(available_browsers()))"
        )
        for name in names:
            for sample in range(args.browser_samples):
                for label in list(targets)[:: 1 if sample % 2 == 0 else -1]:
                    measurements[label]["browsers"].setdefault(name, []).append(
                        run(BROWSER, name, python=targets[label])
                    )
        reports = {}
        for label, measurement in measurements.items():
            installed = measurement["installed"]
            reports[label] = {
                "installed": installed,
                "installed_bytes": sum(p["bytes"] for p in installed.values()),
                "bytecode_bytes": sum(p["bytecode_bytes"] for p in installed.values()),
                "imports": summarize(measurement["imports"]),
                "browsers": {name: summarize(samples) for name, samples in measurement["browsers"].items()},
                "samples": args.samples,
                "browser_samples": args.browser_samples,
                "conditions": "Fresh Python processes, warm OS/driver caches; Python peak RSS excludes browsers. Paired runs alternate order.",
            }
        output = reports if args.compare_python else reports["runtime"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    label: {k: v for k, v in report.items() if k != "installed"}
                    for label, report in reports.items()
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
