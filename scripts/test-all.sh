#!/bin/sh
# Run from any directory: scripts/test-all.sh
# Uses .venv/bin/python if present, otherwise python3; set PYTHON to override.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -n "${PYTHON:-}" ]; then
    python=$PYTHON
elif [ -x "$root/.venv/bin/python" ]; then
    python=$root/.venv/bin/python
else
    python=python3
fi

test_dir=$(mktemp -d "${TMPDIR:-/tmp}/plumbline-tests.XXXXXXXX")
trap 'rm -rf "$test_dir"' EXIT HUP INT TERM
cd "$test_dir"

# Do not inherit credentials, database settings, or a local .env file.
env -i PATH="$PATH" HOME="$test_dir" TMPDIR="$test_dir" \
    PYTHONPATH="$root:$root/src" \
    PLUMBLINE_TEST_DATABASE_URL="${PLUMBLINE_TEST_DATABASE_URL:-}" \
    PLUMBLINE_AZURE_DEVOPS_PAT=test-pat PLUMBLINE_AZURE_DEVOPS_ORG=test-org PLUMBLINE_DATABASE_URL= \
    "$python" -m pytest -c "$root/pyproject.toml" "$root/tests" \
    --doctest-modules --cov="$root/src/plumbline" \
    --cov-config="$root/.coveragerc" \
    --cov-report=xml:"$root/coverage.xml" --cov-report=html:"$root/htmlcov"
