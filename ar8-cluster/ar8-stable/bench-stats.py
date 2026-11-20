#!/usr/bin/env python3
"""对 bench-*.json 的 per-trial 原始数据做配对显著性检验。

为什么必须做这个：
  基准只跑 7 次 trial，报告里写「xnu-qos makespan 115 vs baseline 129」，
  看起来赢了 11%，但没做检验就无法回答一个致命问题：
  **这是稳定的优势，还是 7 次里刚好赢了 5 次的运气？**

方法（全部纯标准库，无 scipy 依赖）：
  1. 配对符号检验（exact binomial sign test）：同一 seed 下 A vs B 逐对比，
     数谁赢几次，用二项分布算双尾 p 值。这比 t 检验稳健（不假设正态）。
  2. Wilcoxon 符号秩检验：比符号检验多用「赢多少」的信息，用正态近似。
  3. Bootstrap 中位数差 95% CI：重采样 10000 次，看 CI 是否跨 0。

判定标准：双尾 p < 0.05 且 bootstrap 95% CI 不跨 0，才叫「显著差异」。
7 对样本时，符号检验最小双尾 p = 2/2^7 = 0.0156（7:0），
所以「6:1」的 p = 0.125 —— **不显著**。这个下限必须让大家知道。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

PROFILE_LABELS = {
    "nixos-baseline": "baseline",
    "xnu-qos": "xnu-qos",
    "oh-distributed": "oh-dist",
}


def sign_test_p(a: List[float], b: List[float]) -> Tuple[int, int, int, float]:
    """配对符号检验（双尾精确二项）。返回 (a赢, b赢, 平局, p值)。"""
    wins_a = wins_b = ties = 0
    for x, y in zip(a, b):
        if x < y:            # 指标越低越好（makespan/延迟/不均衡度）
            wins_a += 1
        elif x > y:
            wins_b += 1
        else:
            ties += 1
    n = wins_a + wins_b
    if n == 0:
        return wins_a, wins_b, ties, 1.0
    k = min(wins_a, wins_b)
    # 双尾 p = P(X<=k) + P(X>=n-k) = 2 * P(X<=k)（对称）
    p = 2.0 * sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return wins_a, wins_b, ties, min(1.0, p)


def wilcoxon_p(a: List[float], b: List[float]) -> float:
    """Wilcoxon 符号秩检验（正态近似 + tie 修正）。"""
    diffs = []
    for x, y in zip(a, b):
        d = y - x          # 正 = A 更好
        if abs(d) > 1e-12:
            diffs.append(d)
    n = len(diffs)
    if n < 5:
        return 1.0
    ranks = sorted(range(n), key=lambda i: abs(diffs[i]))
    # 处理相同绝对值的 tie，取平均秩
    w_pos = 0.0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(diffs[ranks[j + 1]]) == abs(diffs[ranks[i]]):
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            if diffs[ranks[k]] > 0:
                w_pos += avg_rank
        i = j + 1
    w_neg = n * (n + 1) / 2.0 - w_pos
    w = min(w_pos, w_neg)
    mu = n * (n + 1) / 4.0
    sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24.0)
    if sigma == 0:
        return 1.0
    z = (w - mu) / sigma
    return min(1.0, 2.0 * 0.5 * math.erfc(abs(z) / math.sqrt(2.0)))


def bootstrap_ci(a: List[float], b: List[float], *, iters: int = 10000,
                 seed: int = 20261120) -> Tuple[float, float, float]:
    """Bootstrap **配对差值**中位数的 95% CI。返回 (点估计, lo, hi)。

    ⚠️ 踩过的坑：初版写成「分别重采样 A 和 B 再相减」，那是**非配对** bootstrap。
    本基准里 31 个 trial 用的是同一批 workload 种子，种子间的耗时波动高达 ±20%，
    非配对 bootstrap 会把这部分波动算进 CI，导致 CI 宽到跨 0，
    跟配对符号检验 p=0.0006 自相矛盾 —— 结论全错。

    正确做法：先算每个配对样本的差值 d_i = A_i - B_i，再对 d 做 bootstrap。
    这样种子间的共同波动被抵消，只剩「同一 workload 下两个策略的差」。
    """
    rng = random.Random(seed)
    n = len(a)
    if n < 2:
        return 0.0, 0.0, 0.0
    paired = [x - y for x, y in zip(a, b)]
    point = statistics.median(paired)
    meds = []
    for _ in range(iters):
        sample = [paired[rng.randrange(n)] for _ in range(n)]
        meds.append(statistics.median(sample))
    meds.sort()
    lo = meds[int(iters * 0.025)]
    hi = meds[min(iters - 1, int(iters * 0.975))]
    return point, lo, hi


def analyze_section(section: Dict[str, Any], metric: str) -> List[Dict[str, Any]]:
    """对某个 section 的三个 profile 做两两配对检验。"""
    names = list(section.keys())
    series = {
        n: [float(t[metric]) for t in section[n].get("raw", []) if metric in t]
        for n in names
    }
    # 只比较长度一致且有数据的
    out = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a_name, b_name = names[i], names[j]
            a, b = series[a_name], series[b_name]
            if len(a) < 3 or len(a) != len(b):
                continue
            wa, wb, ties, p_sign = sign_test_p(a, b)
            p_wil = wilcoxon_p(a, b)
            point, lo, hi = bootstrap_ci(a, b)
            significant = p_sign < 0.05 and not (lo <= 0 <= hi)
            out.append({
                "a": a_name, "b": b_name,
                "a_median": round(statistics.median(a), 3),
                "b_median": round(statistics.median(b), 3),
                "a_wins": wa, "b_wins": wb, "ties": ties,
                "p_sign": round(p_sign, 5),
                "p_wilcoxon": round(p_wil, 5),
                "diff": round(point, 3),
                "ci95_lo": round(lo, 3),
                "ci95_hi": round(hi, 3),
                "significant": significant,
            })
    return out


def main() -> None:
    pa = argparse.ArgumentParser(description="三版本基准的配对显著性检验")
    pa.add_argument("files", nargs="+", help="bench-*.json 文件")
    pa.add_argument("--metric", default="makespan",
                    help="要检验的指标：makespan / latency_p95 / hi_qos_p95 / imbalance")
    args = pa.parse_args()

    for f in args.files:
        path = f if os.path.isabs(f) else os.path.join(HERE, f)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = data.get("config", {})
        print(f"\n{'=' * 72}")
        print(f"{os.path.basename(f)}  nodes={cfg.get('nodes')} "
              f"failure={cfg.get('failureInjection')} metric={args.metric}")
        print('=' * 72)
        for section in ("main", "ablationRouterOnly", "ablationSystemOnly"):
            if section not in data:
                continue
            rows = analyze_section(data[section], args.metric)
            if not rows:
                continue
            label = {"main": "主对比", "ablationRouterOnly": "消融A:只换算法",
                     "ablationSystemOnly": "消融B:只换系统层"}[section]
            print(f"\n[{label}]")
            for r in rows:
                la = PROFILE_LABELS.get(r["a"], r["a"])
                lb = PROFILE_LABELS.get(r["b"], r["b"])
                flag = "显著" if r["significant"] else "不显著"
                print(f"  {la:9s}({r['a_median']:6.1f}) vs {lb:9s}({r['b_median']:6.1f}): "
                      f"胜 {r['a_wins']}:{r['b_wins']}  "
                      f"p_sign={r['p_sign']:.4f} p_wil={r['p_wilcoxon']:.4f}  "
                      f"Δ={r['diff']:+6.1f} CI=[{r['ci95_lo']:+.1f},{r['ci95_hi']:+.1f}]  "
                      f"→ {flag}")


if __name__ == "__main__":
    main()
