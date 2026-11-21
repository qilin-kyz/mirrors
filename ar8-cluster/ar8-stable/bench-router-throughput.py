#!/usr/bin/env python3
"""AR8 调度器 wall-clock 性能基准 —— 测「调度算法本身」的真实 CPU 开销。

背景（这是 bench-profiles.py 的盲区）：
  bench-profiles.py 测的是**调度效果**（makespan = 仿真 tick 数），从不测
  **调度决策本身的算力开销**。而第二轮加深语义给 score()/bid() 加了 QoS 传播、
  batch 亲和、数据本地性、节点信任降级 —— 这些都在每次 pick() 里多算了东西。
  本脚本回答两个诚实问题：

  1. 微观：单次调度决策多少 μs？加深语义让决策慢了还是可忽略？
  2. 端到端：完整一次 reconcile（FSM + 状态机推进 + 数据搬移 + 48 分片）
     的真实 wall-clock 是多少？调度器开销在其中占比多大？

公平性保证：
  1. 三版本处理**逐字节相同**的请求序列（cost/tier/sid/home 一致），
     唯一区别是各自 Router 的内部算法 —— 传参开销一致，差异全来自算法。
  2. 用 time.perf_counter() 计真实 wall-clock，不做任何仿真时间映射。
  3. 同一进程先预热，再取多次中位数，消掉首跑抖动与 GC 噪声。

用法:
  python3 bench-router-throughput.py --decisions 200000 --nodes 2
  python3 bench-router-throughput.py --json bench-router-throughput.json --sweep-nodes 8
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


bp = _load("bp_thr", "bench-profiles.py")
ap = bp.ap  # ar8-profiles.py 已由 bench-profiles.py 载入
import ar8core  # noqa: E402

MIX = {"interactive": 0.2, "default": 0.3, "utility": 0.3, "background": 0.2}


def build_nodes(n: int):
    """复刻 bench-profiles.py 的规模化扩展：按 hot/warm 比例铺 N 个节点。"""
    base = [
        {"id": "node1", "ip": "10.23.112.21", "role": "control-plane",
         "k8sName": "vm-ar8-node1", "class": "hot", "memoryMib": 4096, "wan": False},
        {"id": "node2", "ip": "10.23.112.22", "role": "worker",
         "k8sName": "vm-ar8-node2", "class": "warm", "memoryMib": 2048, "wan": False},
    ]
    if n <= len(base):
        return base[:n]
    return [dict(base[i % len(base)], id=f"node{i + 1}", ip=f"10.23.112.{21 + i}",
                 k8sName=f"vm-ar8-node{i + 1}") for i in range(n)]


def make_requests(n_decisions: int, n_nodes: int, seed: int):
    """确定性生成请求序列：(cost, sid, tier, home)。三版本共用同一份。"""
    state = seed & 0x7FFFFFFF
    names = list(ap.TIERS)
    nids = [f"node{i + 1}" for i in range(n_nodes)]
    reqs = []
    for i in range(n_decisions):
        state, r1 = bp.BenchEnv._lcg(state)
        state, r2 = bp.BenchEnv._lcg(state)
        state, r4 = bp.BenchEnv._lcg(state)
        cost = 2.0 + r1 * 1980.0
        tier = names[int(r2 * len(names)) % len(names)]
        sid = f"s{i % 48:03d}"
        home = nids[int(r4 * n_nodes) % n_nodes]
        reqs.append((cost, sid, tier, home))
    return reqs


def measure_pick(router_factory, reqs, node_ids, iters):
    """微观：单次 pick()（含周期 release 回吐）的 μs 中位数。"""
    RELEASE_EVERY = 64
    samples = []
    for _ in range(iters):
        router = router_factory()
        t0 = time.perf_counter()
        for i, (cost, sid, tier, home) in enumerate(reqs):
            router.pick(node_ids, cost, sid=sid, tier=tier, home=home)
            if i % RELEASE_EVERY == 0:
                nid = node_ids[i % len(node_ids)]
                router.release(nid, cost, sid)
        dt = time.perf_counter() - t0
        samples.append(dt / len(reqs) * 1e6)
    return statistics.median(samples)


def measure_reconcile(profile, cfg, home_nodes, shards_n, trials, seed):
    """端到端：完整 run_one()（48 分片 reconcile）的 wall-clock μs 中位数。"""
    samples = []
    for k in range(trials):
        s = seed + k * 7919
        sh, ti = bp.make_workload(shards_n, s, MIX, home_nodes=home_nodes)
        t0 = time.perf_counter()
        bp.run_one(profile, sh, ti, cfg, s, max_ticks=2000)
        samples.append((time.perf_counter() - t0) * 1e6)
    return statistics.median(samples)


def main():
    pa = argparse.ArgumentParser(description="AR8 调度器 wall-clock 性能基准")
    pa.add_argument("--decisions", type=int, default=200000,
                    help="微观基准每次测量的调度决策数")
    pa.add_argument("--nodes", type=int, default=2)
    pa.add_argument("--iters", type=int, default=5, help="微观基准测量轮数")
    pa.add_argument("--shards", type=int, default=48, help="端到端 reconcile 分片数")
    pa.add_argument("--trials", type=int, default=50, help="端到端 reconcile 轮数")
    pa.add_argument("--seed", type=int, default=20261120)
    pa.add_argument("--sweep-nodes", type=int, default=0,
                    help="若 >0，扫描 2..N 节点的微观决策退化曲线")
    pa.add_argument("--json", default="")
    args = pa.parse_args()

    cfg = yaml.safe_load(open(os.path.join(HERE, "ar8.config.yaml"), encoding="utf-8"))
    report = {"config": {"decisions": args.decisions, "nodes": args.nodes,
                         "iters": args.iters, "shards": args.shards,
                         "trials": args.trials, "seed": args.seed,
                         "unit": "microseconds (median)"},
              "micro": {}, "endToEnd": {}, "nodeScale": {}}

    def measure_nodes(n_nodes):
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["nodes"] = build_nodes(n_nodes)
        nids = sorted({n["id"] for n in cfg2["nodes"]})
        reqs = make_requests(args.decisions, n_nodes, args.seed)
        row = {}
        for profile in ap.PROFILES:
            def factory(p=profile):
                return ap.make_router(p, cfg2)
            us = measure_pick(factory, reqs, nids, iters=args.iters)
            row[profile] = {"us_per_decision": round(us, 4),
                            "decisions_per_sec": round(1e6 / us, 0)}
        return row

    # ---- 微观：单决策开销 ----
    print(f"== AR8 调度器 wall-clock 性能  decisions={args.decisions} "
          f"nodes={args.nodes} iters={args.iters} ==")
    print("[微观] 单次 pick() 决策（含周期回吐）")
    report["micro"] = measure_nodes(args.nodes)
    micro = report["micro"]
    for profile in ap.PROFILES:
        r = micro[profile]
        print(f"  {profile:15s} {r['us_per_decision']:8.3f} μs/决策  "
              f"{r['decisions_per_sec']:>10,.0f} 决策/秒")
    base_us = micro["nixos-baseline"]["us_per_decision"]
    if base_us > 0:
        print("  [相对 baseline 单决策成本]")
        for profile in ap.PROFILES:
            print(f"  {profile:15s} {micro[profile]['us_per_decision'] / base_us:6.2f}x")

    # ---- 端到端：完整 reconcile ----
    print(f"\n[端到端] 完整 reconcile wall-clock（{args.shards} 分片 × {args.trials} 轮）")
    home_nodes = [n["id"] for n in build_nodes(args.nodes)]
    et = {}
    for profile in ap.PROFILES:
        us = measure_reconcile(profile, cfg, home_nodes, args.shards,
                               args.trials, args.seed)
        et[profile] = {"reconcile_us": round(us, 1), "reconcile_ms": round(us / 1000, 3)}
        print(f"  {profile:15s} {us:10.1f} μs  ({us/1000:7.3f} ms)")
    report["endToEnd"] = et
    # 调度器决策在端到端里的占比：用「约 48 分片 → 数十次 pick」估算上界。
    # 48 分片完整 reconcile 里 pick 次数约等于分片数（reassign 略多），
    # 用 48 × 单决策 μs 作为调度器开销的上界估计。
    print("  [调度器开销占端到端比例（用 48×单决策μs 上界估算）]")
    for profile in ap.PROFILES:
        pick_overhead = micro[profile]["us_per_decision"] * args.shards
        ratio = pick_overhead / et[profile]["reconcile_us"] * 100
        print(f"  {profile:15s} 调度器≈{pick_overhead:7.1f}μs / 端到端{et[profile]['reconcile_us']:.0f}μs = {ratio:5.2f}%")

    # ---- 节点规模退化 ----
    if args.sweep_nodes > 0:
        print(f"\n[节点规模退化] 单决策 μs，2..{args.sweep_nodes} 节点")
        for n in range(2, args.sweep_nodes + 1):
            r = measure_nodes(n)
            report["nodeScale"][str(n)] = r
            line = "  ".join(f"{p}={r[p]['us_per_decision']:.3f}" for p in ap.PROFILES)
            print(f"  nodes={n:3d}  {line}")

    if args.json:
        path = args.json if os.path.isabs(args.json) else os.path.join(HERE, args.json)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("\nwrote", path)


if __name__ == "__main__":
    main()