#!/usr/bin/env python3
"""Per-worker breakdown: model inference wait vs tool exec vs residual.

Produces the numbers in docs/harness-speed-optimization-plan.md. Select runs by
taskId from baseline-2026-09-11.json — a "latest N runs" glob does not
reproduce the frozen sample once new runs land.

    python3 docs/replay-baselines/analyze_turn_breakdown.py \
        $(python3 -c "import json;print(' '.join('worktree/%s/run.jsonl'%t for t in json.load(open('docs/replay-baselines/baseline-2026-09-11.json'))['taskIds']))")
"""
import json, sys, collections
from datetime import datetime

def ts(s): return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()
def fmt(s): return f"{s/60:.1f}m" if s>=60 else f"{s:.1f}s"

def load(path):
    out=[]
    for line in open(path):
        try: out.append(json.loads(line))
        except Exception: pass
    return out

def key(e):
    return e.get("workerId") or e.get("agentId") or e.get("actorType") or "?"

def analyze(path, verbose=True):
    ev = load(path)
    if not ev: return None
    t0, t1 = ts(ev[0]["ts"]), ts(ev[-1]["ts"])
    wall = t1-t0

    turn_open = {}                       # (actor) -> t of turn.start
    infer = collections.defaultdict(list)     # actor -> [model inference secs]
    tool_ms = collections.defaultdict(lambda: collections.defaultdict(list))  # actor->tool->[s]
    span = {}
    rpc_open = {}; rpc = collections.defaultdict(list)
    first_action = {}
    steps = collections.Counter()
    browser_calls = collections.Counter()   # actor -> n browser_call
    methods = collections.Counter()

    for e in ev:
        t = ts(e["ts"]); typ=e.get("type",""); p=e.get("payload") or {}; a=key(e)
        span.setdefault(a,[t,t]); span[a][1]=t
        if typ=="lifecycle.turn.start": turn_open[a]=t
        elif typ=="agent.model":
            st = turn_open.get(a)
            if st is not None: infer[a].append(t-st); turn_open.pop(a,None)
            steps[a]+=1
        elif typ=="lifecycle.tool.end":
            d = p.get("durationMs")
            if d is not None: tool_ms[a][p.get("toolName") or "?"].append(d/1000.0)
        elif typ=="browser.call.result":
            browser_calls[a]+=1; methods[p.get("method")]+=1
            first_action.setdefault(a, t-t0)
        elif typ.endswith(".transport.request"):
            rpc_open[(typ.split(".transport.")[0], p.get("id"))]=(t,p.get("method"))
        elif typ.endswith(".transport.response"):
            g=rpc_open.pop((typ.split(".transport.")[0], p.get("id")),None)
            if g: rpc[g[1] or "?"].append(t-g[0])

    if verbose:
        print("="*90)
        print(f"{path}\n  wall={fmt(wall)}  events={len(ev)}")
    rows=[]
    for a in sorted(span):
        sp = span[a][1]-span[a][0]
        inf = sum(infer[a]); ninf=len(infer[a])
        tl = sum(sum(v) for v in tool_ms[a].values()); ntl=sum(len(v) for v in tool_ms[a].values())
        if ninf==0 and ntl==0: continue
        rows.append((a,sp,inf,ninf,tl,ntl,browser_calls[a],first_action.get(a)))
        if verbose:
            resid = sp-inf-tl
            print(f"  {a:<22} span={fmt(sp):>7} | model={fmt(inf):>7} ({ninf:3d} turns "
                  f"{100*inf/sp if sp else 0:4.1f}%) | tools={fmt(tl):>7} ({ntl:3d} calls "
                  f"{100*tl/sp if sp else 0:4.1f}%) | resid={fmt(resid):>7} "
                  f"({100*resid/sp if sp else 0:4.1f}%) | browser_calls={browser_calls[a]:3d}"
                  f" | 1st action @{fmt(first_action[a]) if a in first_action else 'n/a'}")
    if verbose:
        print("  --- browser tool wall (all actors) ---")
        agg=collections.defaultdict(list)
        for a in tool_ms:
            for n,v in tool_ms[a].items(): agg[n].extend(v)
        for n,v in sorted(agg.items(), key=lambda kv:-sum(kv[1]))[:12]:
            s=sorted(v)
            print(f"     {n:<28} n={len(v):3d} sum={fmt(sum(v)):>7} p50={s[len(s)//2]:6.2f}s "
                  f"p95={s[int(len(s)*.95)]:7.2f}s")
        print("  --- ABCP RPC latency (wire) ---")
        for m,v in sorted(rpc.items(), key=lambda kv:-sum(kv[1]))[:10]:
            s=sorted(v)
            print(f"     {str(m):<28} n={len(v):3d} sum={fmt(sum(v)):>7} p50={s[len(s)//2]*1000:6.0f}ms "
                  f"p95={s[int(len(s)*.95)]*1000:7.0f}ms")
        allinf=sorted(x for v in infer.values() for x in v)
        if allinf:
            print(f"  --- model inference latency: n={len(allinf)} p50={allinf[len(allinf)//2]:.1f}s "
                  f"p90={allinf[int(len(allinf)*.9)]:.1f}s p99={allinf[int(len(allinf)*.99)]:.1f}s "
                  f"max={allinf[-1]:.1f}s sum={fmt(sum(allinf))}")
        print("  --- top ABCP methods called ---")
        print("     " + ", ".join(f"{m}×{n}" for m,n in methods.most_common(12)))
    return rows

for p in sys.argv[1:]:
    analyze(p)
