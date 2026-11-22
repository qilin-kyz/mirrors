#!/usr/bin/env python3
"""QoS 混合比例敏感性分析 —— 回答「换 workload 结论会不会反转」。

背景：
  单次基准只用一种 QoS 比例（interactive 20/default 30/utility 30/background 20）。
  但 XNU profile 的设计就是「为交互任务加速」，只在一种比例下测就下结论，
  等于拿自己的主场当普遍真理。这个脚本扫 5 种极端比例。

用法:
  python3 bench-sensitivity.py --trials 15 --json bench-sensitivity.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))

MIXES = {
    "全交互": {"interactive": 1.0, "default": 0.0, "utility": 0.0, "background": 0.0},
    "重交互": {"interactive": 0.6, "default": 0.3, "utility": 0.1, "background": 0.0},
    "均衡": {"interactive": 0.25, "default": 0.25, "utility": 0.25, "background": 0.25},
    "重后台": {"interactive": 0.05, "default": 0.15, "utility": 0.3, "background": 0.5},
    "全后台": {"interactive": 0.0, "default": 0.0, "utility": 0.2, "background": 0.8},
}


def load_bench():
    """带连字符的 bench-profiles.py 用 importlib 加载（项目既有惯例）。"""
    spec = importlib.util.spec_from_file_location("bp", os.path.join(HERE, "bench-profiles.py"))
    bp = importlib.util.module_from_spec(spec)
    sys.modules["bp"] = bp
    spec.loader.exec_module(bp)
    return bp


def main():
    pa = argparse.ArgumentParser(description="KSAYA QoS 比例敏感性分析")
    pa.add_argument("--shards", type=int, default=48)
    pa.add_argument("--trials", type=int, default=15)
    pa.add_argument("--seed", type=int, default=20261120)
    pa.add_argument("--json", default="")
    pa.add_argument("--homogeneous", action="store_true",
                    help="节点同构化：隔离数据本地性的独立贡献")
    args = pa.parse_args()

    bp = load_bench()
    with open(os.path.join(HERE, "ksaya.config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if args.homogeneous:
        for n in cfg["nodes"]:
            n["class"] = "hot"
            n["memoryMib"] = 4096

    home_nodes = [n["id"] for n in cfg["nodes"]]

    report = {"config": {"shards": args.shards, "trials": args.trials,
                         "seed": args.seed, "homogeneous": bool(args.homogeneous)},
              "mixes": {}}
    print(f"== QoS 比例敏感性  shards={args.shards} trials={args.trials} "
          f"homogeneous={args.homogeneous} ==")

    for name, mix in MIXES.items():
        row = {}
        print(f"\n[{name}] {mix}")
        for profile in bp.ap.PROFILES:
            vals = []
            for k in range(args.trials):
                seed = args.seed + k * 7919
                shards, tiers = bp.make_workload(args.shards, seed, mix,
                                                 home_nodes=home_nodes)
                m = bp.run_one(profile, shards, tiers, cfg, seed, max_ticks=2000)
                vals.append({
                    "makespan": m["makespan"],
                    "p95": m["latency_p95"],
                    "hi_qos_p95": m["hi_qos_p95"],
                })
            med = statistics.median([v["makespan"] for v in vals])
            row[profile] = {"makespan_median": med, "raw": vals}
            print(f"  {profile:15s} ms={med:6.1f}")
        report["mixes"][name] = {"tiersMix": mix, "profiles": row}

    if args.json:
        path = args.json if os.path.isabs(args.json) else os.path.join(HERE, args.json)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("\nwrote", path)


if __name__ == "__main__":
    main()
