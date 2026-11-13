#!/usr/bin/env bash
# Find lint issues, fix what Ruff can fix automatically, and format the code.
#
#   ./scripts/lint.sh          fix and format everything
#   ./scripts/lint.sh --check  only report problems, change nothing (like CI)

set -uo pipefail

cd "$(dirname "$0")/.."

RED=$'\033[31m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RESET=$'\033[0m'

if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
else
    PYTHON=python3
fi

if ! "$PYTHON" -m ruff --version >/dev/null 2>&1; then
    echo "${RED}Ruff is not installed.${RESET} Run: $PYTHON -m pip install -r requirements-dev.txt"
    exit 1
fi

RUFF=("$PYTHON" -m ruff)

if [ "${1:-}" = "--check" ]; then
    echo "Checking lint..."
    "${RUFF[@]}" check .
    lint_status=$?

    echo "Checking formatting..."
    "${RUFF[@]}" format --check .
    format_status=$?

    if [ $lint_status -ne 0 ] || [ $format_status -ne 0 ]; then
        echo "${RED}✗ Problems found.${RESET} Run ./scripts/lint.sh to fix them."
        exit 1
    fi

    echo "${GREEN}✓ No lint or formatting problems${RESET}"
    exit 0
fi

echo "Fixing lint issues..."
"${RUFF[@]}" check . --fix --quiet

echo "Formatting code..."
"${RUFF[@]}" format .

# Formatting can change line lengths, so check again for anything left.
echo "Checking for remaining issues..."
if ! "${RUFF[@]}" check .; then
    echo "${YELLOW}! Some issues can't be fixed automatically. Fix the ones above by hand.${RESET}"
    exit 1
fi

echo "${GREEN}✓ Code is lint-free and formatted${RESET}"
