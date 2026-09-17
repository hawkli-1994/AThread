#!/usr/bin/env python3
"""repair_logs.py: recover exec records from logs corrupted by non-atomic
interleaved writes in the old trace shim.

The old shim emitted each JSON record with many dprintf() calls, so
concurrent processes interleaved fragments inside lines. All bytes are
present in the file, just merged. This script re-segments the raw byte
stream on record boundaries ({"ev":"start" / {"ev":"end"), validates every
candidate record as JSON, keeps the fully-recovered ones, and reports
exactly how much was lost. Clean lines pass through unchanged.

Outputs:
  logs/repaired/<label>.jsonl   recovered records (one JSON per line)
  per-file recovery stats printed to stdout
"""
import glob, json, os, re, sys

LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
OUTDIR = os.path.join(LOGDIR, "repaired")
MARKER = re.compile(r'\{"ev":"(start|end)"')

def parse_stream(path):
    raw = open(path, "rb").read().decode("utf-8", "replace")
    # find candidate record starts; validate each span up to the next marker
    starts = [m.start() for m in MARKER.finditer(raw)]
    good, lost_spans = [], []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(raw)
        span = raw[s:e].strip("\n")
        # a trailing partial record (no closing }) counts as lost
        try:
            rec = json.loads(span)
            if rec.get("ev") in ("start", "end") and "pid" in rec:
                good.append(rec)
                continue
        except json.JSONDecodeError:
            pass
        lost_spans.append(span[:80])
    # clean fast path sanity: count fully-parseable original lines too
    clean = sum(1 for l in raw.splitlines()
                if l.strip() and _ok(l))
    return good, lost_spans, clean

def _ok(line):
    try:
        e = json.loads(line)
        return e.get("ev") in ("start", "end") and "pid" in e
    except json.JSONDecodeError:
        return False

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    tot_good = tot_lost = 0
    for p in sorted(glob.glob(os.path.join(LOGDIR, "*.jsonl"))):
        label = os.path.basename(p)[:-6]
        if label == "replay_list":
            continue
        good, lost, clean = parse_stream(p)
        # dedupe: re-segmentation can produce a record both as a clean line
        # and as a resplit span — key on the exact JSON text
        seen, uniq = set(), []
        for r in good:
            k = json.dumps(r, sort_keys=True)
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        with open(os.path.join(OUTDIR, label + ".jsonl"), "w") as f:
            for r in uniq:
                f.write(json.dumps(r) + "\n")
        tot_good += len(uniq)
        tot_lost += len(lost)
        print(f"{label:24s} recovered={len(uniq):4d} lost_spans={len(lost):3d} clean_lines={clean:4d}")
        for s in lost[:3]:
            print(f"    lost: {s!r}")
    print(f"\nTOTAL recovered={tot_good} lost_spans={tot_lost}")

if __name__ == "__main__":
    main()
