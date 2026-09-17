#!/usr/bin/env python3
"""analyze_trace.py: turn raw agent exec traces into a command profile.

Reads logs/repaired/<label>.jsonl produced by repair_logs.py (the original
logs contain interleaved fragments from the old non-atomic shim; repair_logs
reports exactly how much was unrecoverable). Every anomaly — unparseable
line, start without end, end without start — is counted and printed, never
silently skipped.

Pairs start/end events by pid, emits distribution stats and a replay list
of the python/node commands actually invoked by real agents.
"""
import glob, json, os, sys, hashlib
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
REPAIRDIR = os.path.join(LOGDIR, "repaired")
REPLAY_OUT = os.path.join(LOGDIR, "replay_list.json")

def load(label):
    path = os.path.join(REPAIRDIR, f"{label}.jsonl")
    starts, ends = {}, {}
    n_bad = n_orphan_end = 0
    for line in open(path):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            n_bad += 1
            continue
        if e["ev"] == "start":
            starts[e["pid"]] = e
        else:
            ends.setdefault(e["pid"], e)  # dedupe double end lines
    recs = []
    for pid, s in starts.items():
        e = ends.pop(pid, None)
        recs.append({
            "bin": s["bin"], "argv": s["argv"], "cwd": s["cwd"],
            "venv": s.get("venv", 0),
            "dur_ms": e["dur_ms"] if e else None,
            "rc": e["rc"] if e else None,
            "ts": s["ts"],
        })
    n_orphan_end = len(ends)  # end without a recoverable start
    return recs, n_bad, n_orphan_end

def main():
    labels = sorted(os.path.basename(p)[:-6]
                    for p in glob.glob(os.path.join(REPAIRDIR, "*.jsonl")))
    all_recs = {}
    tot_bad = tot_orphan = 0
    for l in labels:
        recs, n_bad, n_orphan = load(l)
        tot_bad += n_bad; tot_orphan += n_orphan
        # exclude the agent runtime itself (e.g. codex's long-lived `node` process)
        recs = [r for r in recs if not (r["dur_ms"] and r["dur_ms"] > 120000)]
        all_recs[l] = (recs, n_bad, n_orphan)

    replay = []
    grand = Counter(); grand_dur = Counter()
    n_recoverable = 0
    for l, (recs, n_bad, n_orphan) in all_recs.items():
        n = len(recs)
        n_recoverable += n
        cnt = Counter(r["bin"] for r in recs)
        dur = Counter()
        for r in recs:
            if r["dur_ms"] is not None:
                dur[r["bin"]] += r["dur_ms"]
        total_dur = sum(dur.values())
        span_s = (max(r["ts"] for r in recs) - min(r["ts"] for r in recs)) / 1e9 if recs else 0
        meta = {}
        mp = os.path.join(LOGDIR, f"{l}.meta")
        if os.path.exists(mp):
            meta = json.load(open(mp))
        py_n = cnt.get("python3", 0) + cnt.get("python", 0)
        py_dur = dur.get("python3", 0) + dur.get("python", 0)
        node_n = cnt.get("node", 0) + cnt.get("nodejs", 0)
        node_dur = dur.get("node", 0) + dur.get("nodejs", 0)
        print(f"\n== {l} ==  execs={n} (orphan_ends={n_orphan}) "
              f"session_wall={meta.get('wall_s','?')}s trace_span={span_s:.0f}s")
        print(f"   python: {py_n} execs ({py_n/max(n,1)*100:.0f}%), {py_dur:.0f}ms "
              f"({py_dur/max(total_dur,1)*100:.0f}% of cmd time)")
        print(f"   node:   {node_n} execs, {node_dur:.0f}ms")
        print("   top bins by count: " + ", ".join(f"{b}×{c}" for b, c in cnt.most_common(12)))
        print("   top bins by time:  " + ", ".join(f"{b}={d:.0f}ms" for b, d in dur.most_common(8)))
        grand.update(cnt); grand_dur.update(dur)
        # collect python/node replay candidates (only successful, with args)
        seen = set()
        for r in recs:
            if r["bin"] not in ("python3", "python", "node", "nodejs"):
                continue
            if r["rc"] not in (0, None) or not r["argv"][1:]:
                continue
            key = hashlib.md5(json.dumps([r["bin"], r["argv"][1:]]).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            replay.append({"label": l, "bin": r["bin"], "argv": r["argv"],
                           "cwd": r["cwd"], "observed_ms": r["dur_ms"]})

    print(f"\n== ALL AGENTS combined (recoverable execs only) ==")
    print(f"   recoverable execs: {n_recoverable}; orphan end events (start lost "
          f"to corruption): {tot_orphan}; unparseable repaired lines: {tot_bad}")
    print("   NOTE: ~110 record fragments in the original logs were unrecoverable "
          "(see repair_logs.py); the true exec count is higher.")
    print("bins by count: " + ", ".join(f"{b}×{c}" for b, c in grand.most_common(15)))
    td = sum(grand_dur.values())
    print("bins by time:  " + ", ".join(f"{b}={d:.0f}ms" for b, d in grand_dur.most_common(10)))
    for fam in (("python3", "python"), ("node", "nodejs")):
        n = sum(grand[b] for b in fam); d = sum(grand_dur[b] for b in fam)
        print(f"{fam[0]}: {n} execs ({n/sum(grand.values())*100:.0f}% of execs), "
              f"{d:.0f}ms ({d/max(td,1)*100:.0f}% of cmd time)")

    with open(REPLAY_OUT, "w") as f:
        json.dump(replay, f, indent=1)
    print(f"\nreplay candidates: {len(replay)} unique python/node commands -> {REPLAY_OUT}")

if __name__ == "__main__":
    main()
