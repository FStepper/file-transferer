#!/usr/bin/env bash
# Runs the test suite on macOS:   packaging/run_tests_mac.sh <python>
#
# Tk on macOS only works reliably for the first Tk root of a process, so every
# window test (marker "gui") runs in a process of its own; everything else runs
# together. A test stuck for longer than its timeout fails with a stack dump.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY="${1:-python3}"
failed=0

"$PY" -m pytest -v --timeout=300 --timeout-method=thread -m "not gui" || failed=1

for t in $("$PY" -m pytest --collect-only -q -m gui | grep '::'); do
    "$PY" -m pytest -v --timeout=120 --timeout-method=thread "$t" || failed=1
done

exit "$failed"
