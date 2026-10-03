# gformbot

> **A general-purpose Google Form auto-filler CLI.**  
> Inspect any form, supply a response pool (or let it generate synthetic answers), and submit at scale.

---

## Install

```bash
pip install gformbot
```

For clipboard URL detection (handy — just copy the form URL before running):

```bash
pip install "gformbot[clipboard]"
```

> **Requirements:** Python ≥ 3.9 · Google Chrome, Firefox, or Edge installed on your system.  
> ChromeDriver / GeckoDriver are managed automatically by Selenium Manager (no manual download needed).

> **Windows users:** If `gformbot` isn't recognized after installing, run it as `python -m gformbot` instead.  
> This happens when pip's Scripts folder isn't on your PATH (common with the Windows Store version of Python).

---

## Quick Start

### Interactive wizard (recommended for new users)

Just run:

```bash
gformbot
```

The wizard will:
1. Detect if you have a Google Form URL on your clipboard — press Enter to use it.
2. Auto-inspect the form (opens a hidden browser for ~10 seconds).
3. Ask if you have a response CSV pool.
4. Let you set count, browser, and headless preferences with arrow-key menus.
5. Show a sample response preview.
6. Submit responses with a live progress bar.

---

## Subcommands (power-user / scriptable)

### Inspect a form and generate a config

```bash
gformbot inspect "https://docs.google.com/forms/d/e/YOUR_FORM_ID/viewform"
```

This opens a headless browser, scrapes all questions and options, and writes `gformbot_config.yaml` to the current directory.

---

### Fill a form

```bash
# Using wizard-generated config (auto-detected):
gformbot fill

# Explicit URL (auto-inspects and creates config):
gformbot fill "https://docs.google.com/forms/d/e/YOUR_FORM_ID/viewform"

# With all options:
gformbot fill \
  --config my_config.yaml \
  --count 25 \
  --pool my_responses.csv \
  --browser chrome \
  --headless
```

---

### Preview responses without submitting

```bash
gformbot preview "https://..." --count 5
```

---

### View or edit the config

```bash
gformbot config --show      # print to terminal
gformbot config --edit      # open in $EDITOR
```

---

### View the run log

```bash
gformbot log
gformbot log --last 50
```

---

## Config File (`gformbot_config.yaml`)

Generated automatically by `gformbot inspect`. Customize it to set:

```yaml
url: https://docs.google.com/forms/d/e/YOUR_FORM_ID/viewform
form_title: My Survey

# Optional: path to a CSV of real responses (relative or absolute)
pool_file: response_pool.csv

run_settings:
  count: 10
  browser: auto          # auto | chrome | firefox | edge | headless
  headless: false
  min_delay: 0.25
  max_delay: 0.75
  max_runs: 500
  log_file: gformbot_log.csv

questions:
  - index: 1
    text: "What best describes your main daily activity?"
    type: radio
    options:
      - Studying
      - Mostly indoor work
      - A mix of indoor and outdoor work
      - Mostly outdoor work
      - Retired or not currently working
      - Other
    # Optional: bias weights (parallel to options list)
    weights: [85, 5, 7, 1, 1, 1]

  - index: 8
    text: "Which kinds of places felt hottest?"
    type: checkbox
    max_select: 3        # pick at most 3
    options:
      - Roads, footpaths or crossings
      - Bus stops or transport waiting areas
      - No particular hot spot / not sure

  - index: 14
    text: "Optional: Name one public place that needs more shade."
    type: textarea
    # Fixed list of texts to randomly pick from:
    text_pool:
      - ""
      - "college bus stop"
      - "campus main gate area"
```

---

## Response Pool CSV

The CSV just needs columns that loosely match your form's question texts.  
The tool auto-maps columns by matching question text prefixes — no manual mapping needed.

For multi-select questions, values in the CSV should be comma-separated:

```
Q8 column value: "Roads, footpaths or crossings, Bus stops or transport waiting areas"
```

---

## Browser Support

| Browser | Status | Notes |
|---------|--------|-------|
| Chrome / Chromium | ✅ Full | Default. Incognito mode. |
| Firefox | ✅ Full | Private mode. |
| Edge | ✅ Full | InPrivate mode. |
| Headless | ✅ Full | Fastest. No window. Recommended for servers. |
| Auto | ✅ | Detects your system default browser. Falls back to Chrome. |

---

## Tips

- **`auto` browser mode** reads your OS default browser (Windows registry / macOS defaults / `xdg-settings`) and uses that. If detection fails or the browser isn't supported, it falls back to Chrome.
- **Headless** is recommended for unattended / server runs.
- **`max_runs`** in the config is a safety cap to prevent accidental mass submissions.
- The tool stops immediately on the first error and logs it — it will never silently fail in a loop.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
