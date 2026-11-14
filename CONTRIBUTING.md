# Contributing

Thanks for helping improve Plumbline. The independent migration is in progress;
the Python package and CLI are `plumbline` with the `PLUMBLINE_` configuration prefix.
Read the [README](README.md) for the current setup and limitations. Discuss a
substantial change in an issue before investing in it.

## Development

Use Python 3.11 or newer. CI is configured for Python 3.11 and 3.12.
From the repository root, create a virtual environment and install the development
dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Keep changes focused and use the existing patterns. Add a regression test when
there is a meaningful coverage gap. For behaviour changes, run relevant focused
tests and demonstrate the changed path with synthetic records and mocked external
transport, including independently calculated expected results. Run the repository
suite with:

```bash
scripts/test-all.sh
```

The script chooses `.venv/bin/python` when available; set `PYTHON` to choose an
interpreter. It runs in a temporary working directory with synthetic settings and
writes coverage reports in the repository. Report commands, results, failures and
checks you could not run. A mocked demonstration is not evidence of a live service.
Documentation-only changes need appropriate link and content checks, not the full
suite unless the affected build path requires it.

For the interim synthetic UI smoke, install the browser and run:

```bash
python -m playwright install chromium
python scripts/phase0_ui_smoke.py
```

All application requests in that smoke are intercepted. The baseline screenshots
use the repository HEAD assets with external font requests blocked.

For UI changes, exercise the changed journey with synthetic data and include
screenshots at 1440×900, 1366×768 and 1280×800. Check light, dark and high-contrast
themes, keyboard use and zoom where applicable; report any gaps. Capture the local
application, not reference material.

## Submitting changes

Open a focused pull request describing the problem, the change and its checks.
Explain compatibility or migration effects and update documentation when needed.
Never commit live secrets, personal information, private organisation data or
collected reports. Use synthetic fixtures and disposable credentials; do not include
sensitive logs or screenshots. Follow the [code of conduct](CODE_OF_CONDUCT.md).
Report security issues using the [security policy](SECURITY.md), not a public issue.
