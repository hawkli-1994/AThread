#!/bin/bash
# P1-A: collect exec traces of REAL agents on a python-dense task
# (fix bugs until unittest passes — the test-loop workload).
# Each agent gets a fresh copy of fixture/bugrepo; the atomic exec-trace
# shim records every tool invocation.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BUGREPO="$(cd "$HERE/../fixture/bugrepo" && pwd)"
PROMPT='This repository has a test suite under tests/ that should fully pass, but currently some tests fail. Diagnose and fix the bugs in the calc package until `python3 -m unittest discover -s tests -v` reports OK. Only modify files under calc/. Run the tests as many times as you need.'

run() {
    local label="$1"; shift
    FIXTURE="$BUGREPO" "$HERE/run_agent.sh" "$label" "$@" 2>&1 | tail -1
}

run pyfix_claude claude -p "$PROMPT" --permission-mode acceptEdits
run pyfix_kimi   kimi -p "$PROMPT"
run pyfix_codex  codex exec "$PROMPT"
echo "collected: $(ls "$HERE/logs"/pyfix_*.jsonl 2>/dev/null | wc -l) sessions"
