# Small Selenium runtimes

Selenium runs ChatGPT for text generation in a headless private browser by default. Form inspection and submission use direct HTTP. Form media is listed for the user to review. Add relevant visual details to the extra instructions if they affect the answers.

Native Windows x64, macOS Intel/Apple Silicon and Linux glibc x64/ARM64 wheels include a private, hash-locked subset of Selenium 4.51.0. Each includes the same Python import closure and exactly one matching Selenium Manager. macOS wheels retain only their native architecture from upstream's universal executable. Linux executables are statically linked. Portable fallback wheels retain official Selenium for other platforms.

The subset keeps Chrome, Edge, Firefox, waits, element controls, exceptions, scripts, certificates and proxies. It removes other OS/CPU binaries, unsupported browser adapters, generated DevTools code and unused BiDi/CDP connections. Trio, WebSocket and their unused dependencies are not installed. Driver/browser downloads and caches remain separate from the package. Headless AI generation needs no graphical desktop. If sign-in or verification is needed before any prompt is sent, the interactive wizard offers to open a fresh visible private session; that fallback needs a desktop and manual interaction. Noninteractive runs stop instead.

The subset lives in GFormBot's private namespace so it can coexist with official Selenium. Upstream license headers, LICENSE, NOTICE and Manager third-party notices are retained.

Python 3.11+ uses its standard-library `Self` type; only Python 3.10 needs `typing_extensions`.

Approximate 0.4.0 wheel sizes in decimal MB. Python, shared dependencies, browsers and driver caches are separate.

| Target | Wheel |
| --- | ---: |
| Windows x64 | 2.1 MB |
| macOS Intel | 2.4 MB |
| macOS Apple Silicon | 2.2 MB |
| Linux x64 | 2.7 MB |
| Linux ARM64 | 2.5 MB |

In the original Windows x64 benchmark with Python 3.12 and Selenium 4.51.0, the subset reduced total runtime downloads from 16.35 MB to 5.39 MB. These figures include shared dependencies and predate the switch to HTTP for form submission.

## Rebuild and check

```bash
python tools/vendor_selenium.py --check
python tools/vendor_selenium.py --wheel path/to/selenium-4.51.0-py3-none-any.whl
python -m build
python tools/check_wheel.py dist
```

The recipe checks the upstream wheel SHA-256 before selecting the import closure, applying narrow namespace/protocol patches and recording every output hash. Review the recipe when changing upstream versions; do not hand-edit generated files. The source archive includes all native managers; wheel builds select one, preserve executable permissions and write a matching manifest. Installing from source needs no separate Selenium-source download on supported systems.

Native wheel targets are Windows x64, macOS 10.12+ Intel, macOS 11+ Apple Silicon, and Linux glibc 2.17+ x64/ARM64. Browser and Python support may require newer OS versions. Other architectures and libc implementations use the upstream fallback. CI builds and installs wheels on all five targets and runs the bundled Manager. ChatGPT generation requires a supported installed browser and driver even in headless mode. Only the optional visible sign-in/verification fallback requires a graphical desktop.

To compare two clean environments built with the same Python and dependency versions:

```bash
python tools/benchmark_selenium.py --python BEFORE_PYTHON --compare-python AFTER_PYTHON --output comparison.json
```

`GFORMBOT_BUILD_FULL_SELENIUM=1` builds the portable variant for compatibility checks. `GFORMBOT_BUILD_PLATFORM` selects a supported native wheel target when preparing artifacts on another OS; this checks contents, not execution on that target. No build command publishes packages.
