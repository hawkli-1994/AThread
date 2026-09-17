#!/bin/bash
# kimi_retry.sh <label> <fixture> <prompt> — retries around concurrent-request 403s
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="$1"; FIX="$2"; PROMPT="$3"
for i in $(seq 1 10); do
    FIXTURE="$FIX" "$HERE/run_agent.sh" "$LABEL" kimi -p "$PROMPT"
    RC=$(python3 -c "import json;print(json.load(open('$HERE/logs/$LABEL.meta'))['rc'])" 2>/dev/null || echo 1)
    N=$(grep -c '"ev":"start"' "$HERE/logs/$LABEL.jsonl" 2>/dev/null || echo 0)
    if [ "$RC" = "0" ] && [ "$N" -gt 5 ]; then
        echo "[$LABEL] success on attempt $i ($N execs)"
        exit 0
    fi
    echo "[$LABEL] attempt $i failed (rc=$RC execs=$N), backing off 75s"
    sleep 75
done
echo "[$LABEL] gave up"
exit 1
