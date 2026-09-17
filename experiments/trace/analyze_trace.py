#!/usr/bin/env python3
"""analyze_trace.py: turn raw agent exec traces into a command profile.

Pairs start/end events by pid, emits distribution stats and a replay list
of the python/node commands actually invoked by real agents.
"""
import glob, json, os, sys, hashlib
from collections import Counter, defaultdict

LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
REPLAY_OUT = os.path.join(LOGDIR, "replay_list.json")

def load(label):
    path = os.path.join(LOGDIR, f"{label}.jsonl")
    starts, ends = {}, {}
    recs = []
    for line in open(path):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e["ev"] == "start":
            starts[e["pid"]] = e
        else:
            ends.setdefault(e["pid"], e)  # dedupe double end lines
    for pid, s in starts.items():
        e = ends.get(pid)
        recs.append({
            "bin": s["bin"], "argv": s["argv"], "cwd": s["cwd"],
            "venv": s.get("venv", 0),
            "dur_ms": e["dur_ms"] if e else None,
            "rc": e["rc"] if e else None,
            "ts": s["ts"],
        })
    return recs

def main():
    labels = sorted(os.path.basename(p)[:-6]
                    for p in glob.glob(os.path.join(LOGDIR, "*.jsonl")))
    labels = [l for l in labels if l not in ("replay_list",)]
    all_recs = {}
    for l in labels:
        recs = load(l)
        # exclude the agent runtime itself (e.g. codex's long-lived `node` process)
        recs = [r for r in recs if not (r["dur_ms"] and r["dur_ms"] > 120000)]
        all_recs[l] = recs

    replay = []
    grand = Counter(); grand_dur = Counter()
    for l, recs in all_recs.items():
        done = [r for r in recs if r["dur_ms"] is not None]
        n = len(recs)
        cnt = Counter(r["bin"] for r in recs)
        dur = Counter()
        for r in done:
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
        print(f"\n== {l} ==  execs={n} session_wall={meta.get('wall_s','?')}s trace_span={span_s:.0f}s")
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

    print(f"\n== ALL AGENTS combined ==")
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
