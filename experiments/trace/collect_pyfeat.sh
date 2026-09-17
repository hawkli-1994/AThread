#!/bin/bash
# P1-A: collect exec traces of REAL agents on a python-dense task
# (fix bugs until unittest passes — the test-loop workload).
# Each agent gets a fresh copy of fixture/bugrepo; the atomic exec-trace
# shim records every tool invocation.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BUGREPO="$(cd "$HERE/../fixture/bugrepo" && pwd)"
PROMPT='Create a new module calc/stats.py implementing mean(data) and median(data) functions, and add tests for them under tests/. Follow the existing code style. Run `python3 -m unittest discover -s tests -v` repeatedly until all tests pass. You may add files under calc/ and tests/.'

run() {
    local label="$1"; shift
    FIXTURE="$BUGREPO" "$HERE/run_agent.sh" "$label" "$@" 2>&1 | tail -1
}

run pyfeat_claude claude -p "$PROMPT" --permission-mode acceptEdits
run pyfeat_kimi   kimi -p "$PROMPT"
run pyfeat_codex  codex exec "$PROMPT"
echo "collected: $(ls "$HERE/logs"/pyfeat_*.jsonl 2>/dev/null | wc -l) sessions"
