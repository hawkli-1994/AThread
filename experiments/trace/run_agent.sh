#!/bin/bash
# usage: run_agent.sh <label> <agent cmd...>
# Runs a real agent in a scratch workspace with the exec-trace shim on PATH.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="$1"; shift
WS="$HERE/workspaces/$LABEL"
LOGDIR="$HERE/logs"
SRC="${FIXTURE:-$HERE/../fixture/repo}"
mkdir -p "$LOGDIR" "$HERE/workspaces"
rm -f "$LOGDIR/$LABEL.jsonl"
rm -rf "$WS"
cp -r "$SRC" "$WS"

export ATHREAD_TRACE_LOG="$LOGDIR/$LABEL.jsonl"
ORIG_PATH="$PATH"
export ATHREAD_REAL_PATHS="$(echo "$ORIG_PATH" | tr ':' '\n' | grep -v 'trace/shim_bin' | paste -sd:)"
export PATH="$HERE/shim_bin:$ORIG_PATH"

cd "$WS"
START=$(date +%s)
timeout 900 "$@" > "$LOGDIR/$LABEL.stdout" 2> "$LOGDIR/$LABEL.stderr"
RC=$?
END=$(date +%s)
echo "{\"agent\":\"$LABEL\",\"rc\":$RC,\"wall_s\":$((END-START))}" > "$LOGDIR/$LABEL.meta"
echo "[$LABEL] rc=$RC wall=$((END-START))s cmds=$(grep -c '"ev":"start"' "$LOGDIR/$LABEL.jsonl" || true)"
