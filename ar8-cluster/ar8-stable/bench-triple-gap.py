#!/usr/bin/env python3
"""AR8 三重差距基准 —— 性能 / 带宽 / 地域三者极大差异下的调度对照。

为什么需要这个（承接第二十九章，但维度完全不同）：
  bench-profiles.py 只建模「性能差距」（hot/warm 算力权重），数据搬移成本是
  一个与距离无关的常数 0.15。当节点变成「9600X+5070Ti vs 3 代 i5」这种算力差、
  「千兆 vs 万兆 vs 2.5G vs 3×20G USB4」这种带宽差、「北京/香港/东京/凤凰城」这种
  地域差，单值假设就崩溃了。

建模哲学（借多方真实系统的思想，不借代码）：
  1. 延迟 ≠ 带宽（云机房关键事实：「远」是光速延迟硬顶，「机房不烂」是 Gbps 出口）。
  2. 大块搬移 = 延迟 + size/带宽；小块交互 = 纯延迟（绝不跨域）。
  3. 传输 vs 重算经济学（Spark lineage 窄依赖重算 / Snowflake 下推 / CDN 边缘计算）。
  4. **数据源上行带宽才是搬移瓶颈**：数据在慢机上时，搬到快机反而不划算 ——
     因为瓶颈不在「快机算得快」，而在「慢机的窄上行把数据送出来太慢」。

关键建模：区分「可重算数据」与「不可重算数据」
  - 可重算（中间结果，~70%）：搬到快节点后重算即可，数据不构成瓶颈 → 算力引力主导；
  - 不可重算（原始数据，~30%）：必须搬数据，搬移受「数据源上行带宽」主导 → 数据引力主导。
  这与真实系统一致：Spark 里 shuffle 出来的中间数据可重算（不刻意搬），而源数据在 HDFS/本地。

统一 tick 量纲：1 tick = 100ms；weight=1 节点每 tick 消化 300 个「工作量单位」。

四方对照（同一 workload + 同一物理事实，只换「传输/地域感知」）：
  nixos-baseline  不感知传输代价（现状）
  xnu-qos         QoS 分级，偏好「最快节点」→ 不可重算大数据分片被甩跨地域 → 崩
  oh-distributed  感知 home 但只有单值 dataMoveCost，不知重算 → 对可重算数据过度保守
  geo-data        节点对传输矩阵 + 可重算/不可重算区分（目标形态）

用法:
  python3 bench-triple-gap.py --shards 48 --trials 31 --seed 20261120
  python3 bench-triple-gap.py --json bench-triple-gap.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
from typing import Any, Dict, List, Tuple

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


bp = _load("bp_triple", "bench-profiles.py")
ap = bp.ap
import ar8core  # noqa: E402
from ar8core import Router  # noqa: E402


# ===========================================================================
# 统一量纲常量
# ===========================================================================
TICK_MS = 100.0                 # 1 仿真 tick = 100ms 墙钟
PER_TICK_UNITS = 300.0          # weight=1 节点每 tick 消化的「工作量单位」
RECOMPUTE_UNITS_PER_MB = 6.0    # 重算 1MB 中间数据 ≈ 6 个工作量单位（与带宽无关）


# ---------------------------------------------------------------------------
# 1. 真实拓扑：11 节点，覆盖性能/带宽/地域三重差距
# ---------------------------------------------------------------------------
NODE_SPEC = [
    # 本地核心层：性能顶端 + 超高速互连（3×20G USB4 ≈ 60Gbps）
    ("core1", "hot",   65536, 1.00, "site-a", "core",  "usb4-3x"),   # 9600X + 5070Ti
    ("core2", "hot",   65536, 0.95, "site-a", "core",  "usb4-3x"),
    ("fast1", "hot",   32768, 0.55, "site-a", "core",  "tengbe"),    # 万兆节点
    # 本地边缘层：算力弱 + 带宽窄（数据源上行是搬移瓶颈）
    ("edge1", "warm",  16384, 0.26, "site-a", "edge",  "gbe"),       # 7 代 i5（千兆）
    ("edge2", "cold",   8192, 0.12, "site-a", "edge",  "gbe"),       # 3 代 i5（千兆）
    ("edge3", "warm",  16384, 0.26, "site-a", "edge",  "two-half"),  # 2.5G
    # 云端层：地域远（延迟硬顶）但机房带宽不差
    ("bj",    "hot",   32768, 0.60, "cn-north",    "cloud", "wan-5g"),   # 北京
    ("hk",    "hot",   32768, 0.58, "cn-hongkong", "cloud", "wan-5g"),   # 香港
    ("tokyo", "hot",   32768, 0.55, "ap-tokyo",    "cloud", "wan-2g"),   # 东京
    ("phx",   "hot",   32768, 0.50, "us-west",     "cloud", "wan-1g"),   # 凤凰城
    ("edge4", "cold",   8192, 0.10, "site-b", "edge",  "gbe"),       # 异地边缘
]

BW_MBPS = {
    "usb4-3x": 2400.0,   # 3 条 20G USB4 独享，保守取 2400 MB/s
    "tengbe": 1100.0,    # 万兆 ≈ 1250 MB/s（保守）
    "two-half": 280.0,   # 2.5 GbE
    "gbe": 110.0,        # 1 GbE
    "wan-5g": 600.0,     # 云 5 Gbps 出口
    "wan-2g": 240.0,
    "wan-1g": 120.0,
}

REGION_RTT_MS = {
    ("site-a", "site-a"): 0.05,
    ("site-a", "site-b"): 8.0,
    ("site-b", "site-b"): 0.05,
    ("site-a", "cn-north"): 20.0,
    ("site-a", "cn-hongkong"): 40.0,
    ("site-a", "ap-tokyo"): 70.0,
    ("site-a", "us-west"): 160.0,
    ("cn-north", "cn-hongkong"): 30.0,
    ("cn-north", "ap-tokyo"): 55.0,
    ("cn-north", "us-west"): 150.0,
    ("cn-hongkong", "ap-tokyo"): 45.0,
    ("cn-hongkong", "us-west"): 155.0,
    ("ap-tokyo", "us-west"): 110.0,
    ("site-b", "us-west"): 150.0,
    ("site-b", "cn-north"): 25.0,
    ("site-b", "ap-tokyo"): 65.0,
}


def _rtt(ra: str, rb: str) -> float:
    if ra == rb:
        return REGION_RTT_MS.get((ra, rb), 0.05)
    return REGION_RTT_MS.get((ra, rb), REGION_RTT_MS.get((rb, ra), 120.0))


def build_triple_nodes() -> Tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, Any]]:
    """构造三重差距拓扑，并产出 config 真值源需要的 topology 段。

    geo-distributed 已转正为 ar8-profiles.GeoDistributedRouter，它从 cfg.topology
    读「地域对 RTT 矩阵」、从 node.region / node.bwMbps 读节点地域与出口带宽。
    所以这里不再维护一份独立的 demo 路由器，而是把同一份物理事实写成真值源。
    """
    nodes = []
    region = {}
    for i, (nid, cls, mem_mib, weight, rn, loc, bw) in enumerate(NODE_SPEC):
        nodes.append({
            "id": nid, "ip": f"10.23.112.{21 + i}", "role": "worker",
            "k8sName": f"vm-ar8-{nid}", "class": cls, "memoryMib": mem_mib,
            "wan": loc == "cloud", "region": rn,
            "bwMbps": BW_MBPS.get(bw, 110.0),
            "_weight": weight,
        })
        region[nid] = rn
    regions = sorted(set(region.values()))
    topology = {
        "regions": regions,
        "rttMs": {f"{a}|{b}": v for (a, b), v in REGION_RTT_MS.items()},
    }
    return nodes, region, topology


# ---------------------------------------------------------------------------
# 2. 传输/重算的物理代价（统一折成「工作量单位」，环境层与 geo 共用同一公式）
# ---------------------------------------------------------------------------

def transfer_cost_ms(src_region: str, dst_region: str, size_mb: float,
                     bw_mbps: float) -> float:
    """搬 size_mb 从 src 到 dst 的墙钟（ms）= 延迟硬顶 + 大小/带宽。"""
    rtt = _rtt(src_region, dst_region)
    return rtt + (size_mb / max(bw_mbps, 1e-6)) * 1000.0


def data_penalty_units(sid: str, node: str,
                       home_map: Dict[str, str], size_map: Dict[str, float],
                       recompute_map: Dict[str, bool],
                       region_map: Dict[str, str], bw_map: Dict[str, float],
                       weight_map: Dict[str, float]) -> float:
    """分片落在 node 上时，为「数据可用」付出的额外工作量单位（物理事实）。

    - 可重算：重算成本 = size × R / (weight×300) 折算单位（与带宽无关）。
    - 不可重算：搬移墙钟折算单位；瓶颈 = 数据源上行与落点下行的较小带宽。
    """
    home = home_map.get(sid)
    if home is None or home == node:
        return 0.0
    size = size_map.get(sid, 0.0)
    w = max(weight_map.get(node, 1.0), 1e-6)

    if recompute_map.get(sid, False):
        # 可重算：数据可在落点重新生成，代价只与算力有关
        recompute_ticks = size * RECOMPUTE_UNITS_PER_MB / (w * PER_TICK_UNITS)
        return recompute_ticks * w * PER_TICK_UNITS  # = size * R（单位，量纲自然约掉）

    # 不可重算：必须搬。瓶颈 = 两端较小带宽（数据源上行常是短板）
    bw = min(bw_map.get(home, 110.0), bw_map.get(node, 110.0))
    move_ticks = transfer_cost_ms(region_map.get(home, ""), region_map.get(node, ""),
                                  size, bw) / TICK_MS
    return move_ticks * w * PER_TICK_UNITS


# ---------------------------------------------------------------------------
# 3. workload：可重算小数据（算力引力） + 不可重算大数据（数据引力）
# ---------------------------------------------------------------------------

def make_triple_workload(n: int, seed: int, node_ids: List[str],
                         tiers_mix: Dict[str, float]):
    """生成 n 个分片。~70% 可重算（1~100MB），~30% 不可重算（2~30GB）。"""
    state = seed & 0x7FFFFFFF
    names = list(tiers_mix.keys())
    weights = list(tiers_mix.values())
    total = sum(weights)
    shards = []
    tiers = {}
    for i in range(n):
        state, r1 = bp.BenchEnv._lcg(state)
        state, r2 = bp.BenchEnv._lcg(state)
        state, r3 = bp.BenchEnv._lcg(state)
        state, r4 = bp.BenchEnv._lcg(state)
        hi = int(20 + r1 * 1980)
        sid = f"s{i:03d}"
        tier = names[-1]
        acc = 0.0
        for t, w in zip(names, weights):
            acc += w
            if r2 * total <= acc:
                tier = t
                break
        home = node_ids[int(r3 * len(node_ids)) % len(node_ids)]
        recomputable = r4 < 0.70   # 70% 可重算
        if recomputable:
            data_mb = 1.0 + r4 / 0.70 * 99.0          # 1 ~ 100 MB
        else:
            data_mb = 2.0 + (r4 - 0.70) / 0.30 * 28000.0   # 2 GB ~ 30 GB
        shards.append({"id": sid, "lo": 2, "hi": hi, "home": home,
                       "dataSizeMb": round(data_mb, 2), "tier": tier,
                       "recomputable": recomputable})
        tiers[sid] = tier
    return shards, tiers


# ---------------------------------------------------------------------------
# 4. geo-data 调度器：正式实现已迁入 ar8-profiles.GeoDistributedRouter
# ---------------------------------------------------------------------------
# 融合点：demo 版 GeoDataRouter 已转正，不再在本文件重复维护。geo 的物理事实
# （RTT 矩阵 / 节点带宽）改由 config 真值源 topology 段承载，路由器从真值源读。
def make_geo_router(cfg: Dict[str, Any]):
    return ap.make_router("geo-distributed", cfg)


# ---------------------------------------------------------------------------
# 5. 环境：把数据代价真实计入 makespan（所有 profile 共用同一物理事实）
# ---------------------------------------------------------------------------

class TripleGapEnv(bp.BenchEnv):
    def __init__(self, cfg, *, router, system, tiers, base_capacity,
                 failure_inject=None, region_map=None, bw_map=None,
                 size_map=None, home_map=None, weight_map=None, recompute_map=None):
        super().__init__(cfg, router=router, system=system, tiers=tiers,
                         base_capacity=base_capacity, failure_inject=failure_inject)
        self.region = region_map or {}
        self.bw = bw_map or {}
        self.size_map = size_map or {}
        self.home_map = home_map or {}
        self.weight_map = weight_map or {}
        self.recompute_map = recompute_map or {}
        self.penalty: Dict[str, float] = {}

    def _speed(self, node, qos):
        return max(self.weight_map.get(node, 1.0), 0.01) * float(
            self.system.get("qosSpeed", {}).get(qos, 1.0))

    def tick(self, run, generation, shards):
        self.t += 1
        down_at = self.failure_inject.get("downAtTick")
        down_for = self.failure_inject.get("downForTicks", 0)
        down_node = self.failure_inject.get("node")
        if down_at is not None and self.t == down_at and down_node:
            self.nodes[down_node] = False
            note = getattr(self.router, "note_failure", None)
            if callable(note):
                note(down_node, self.t)
        if down_at is not None and down_node and self.t == down_at + down_for and down_for > 0:
            self.nodes[down_node] = True
            note = getattr(self.router, "note_failure", None)
            if callable(note):
                note(down_node, self.t)
        set_tick = getattr(self.router, "set_tick", None)
        if callable(set_tick):
            set_tick(self.t)
        if not self.quorate():
            return

        for node in self.order_nodes():
            if not self.nodes.get(node, False):
                self.idle_ticks[node] += 1
                continue
            pending = [j for j in self.jobs.values()
                       if j.get("node") == node and j.get("shard") not in self.finish_tick]
            if not pending:
                self.idle_ticks[node] += 1
                continue
            pending.sort(key=lambda j: j.get("uid", 0))
            job = pending[0]
            sid = job["shard"]
            qos = self.tiers.get(str(sid), "default")
            work = float(max(1, job["hi"] - job["lo"] + 1))

            if sid not in self.penalty:
                self.penalty[sid] = data_penalty_units(
                    sid, node, self.home_map, self.size_map, self.recompute_map,
                    self.region, self.bw, self.weight_map)
            total_work = work + self.penalty.get(sid, 0.0)

            gain = self._speed(node, qos) * PER_TICK_UNITS
            prev = self.progress.get(job["name"], 0.0)
            self.progress[job["name"]] = prev + gain
            self.node_work[node] += min(gain, max(0.0, total_work - prev))

            if self.progress[job["name"]] >= total_work:
                self.results[sid] = dict(uid=job["uid"], node=node,
                                         metrics=ar8core.reference_shard(job["lo"], job["hi"]),
                                         exitCode=0, generation=generation, run=run, shard=sid)
                self.finish_tick[str(sid)] = self.t
                self.jobs.pop(job["name"], None)


# ---------------------------------------------------------------------------
# 6. 单次运行
# ---------------------------------------------------------------------------

def run_triple(router_name: str, shards, tiers, cfg, seed, max_ticks=60000):
    import copy
    cfg = copy.deepcopy(cfg)
    ledger_shards = [dict(s, tier=tiers.get(s["id"])) for s in shards]

    region_map = {n["id"]: n.get("region", "site-a") for n in cfg["nodes"]}
    bw_map = {n["id"]: float(n.get("bwMbps", 110.0)) for n in cfg["nodes"]}
    weight_map = {n["id"]: float(n.get("_weight", 1.0)) for n in cfg["nodes"]}
    size_map = {s["id"]: s.get("dataSizeMb", 0.0) for s in shards}
    home_map = {s["id"]: s.get("home") for s in shards}
    recompute_map = {s["id"]: bool(s.get("recomputable", True)) for s in shards}
    cap = dict(weight_map)

    if router_name == "baseline":
        router = ar8core.Router(cfg)
    elif router_name == "xnu":
        router = ap.make_router("xnu-qos", cfg)
    elif router_name == "oh":
        router = ap.make_router("oh-distributed", cfg)
    elif router_name == "geo":
        # 正式实现：数据属性经 reconcile 透传（Ledger 保留 dataSizeMb/recomputable，
        # reconcile -> router.pick(dataSize=, recomputable=)），无需再手动塞 map。
        router = make_geo_router(cfg)
    else:
        raise ValueError(router_name)

    system = ap.system_params(
        "xnu-qos" if router_name == "xnu"
        else "geo-distributed" if router_name == "geo"
        else "oh-distributed" if router_name == "oh"
        else "nixos-baseline")
    if router_name != "xnu":
        system["qosSpeed"] = {}
        system["reservedCapacity"] = 0.0
    system["commCost"] = 0.0

    env = TripleGapEnv(cfg, router=router, system=system, tiers=tiers,
                       base_capacity=cap, region_map=region_map, bw_map=bw_map,
                       size_map=size_map, home_map=home_map, weight_map=weight_map,
                       recompute_map=recompute_map)
    env.home_map = home_map
    env.data_home = {}

    ledger = ar8core.Ledger(f"trip-{router_name}-{seed}", 1, ledger_shards)
    sm = ar8core.StateMachine("shard", cfg["stateMachines"]["shard"])
    ar8core.reconcile(env, cfg, ledger, sm, max_ticks=max_ticks)

    lat = sorted(env.finish_tick.values())
    makespan = max(lat) if lat else max_ticks
    return {"makespan": makespan, "done": len(lat), "total": len(shards),
            "total_data_units": round(sum(env.penalty.values()), 1)}


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------

ROUTERS = ["baseline", "xnu", "oh", "geo"]


def main():
    pa = argparse.ArgumentParser(description="AR8 三重差距基准")
    pa.add_argument("--shards", type=int, default=48)
    pa.add_argument("--trials", type=int, default=31)
    pa.add_argument("--seed", type=int, default=20261120)
    pa.add_argument("--json", default="")
    args = pa.parse_args()

    nodes, region, topology = build_triple_nodes()
    cfg = yaml.safe_load(open(os.path.join(HERE, "ar8.config.yaml"), encoding="utf-8"))
    cfg["nodes"] = nodes
    cfg["topology"] = topology
    node_ids = [n["id"] for n in nodes]
    tiers_mix = {"interactive": 0.2, "default": 0.3, "utility": 0.3, "background": 0.2}

    report = {"config": {"shards": args.shards, "trials": args.trials,
                         "seed": args.seed, "topology": node_ids, "routers": ROUTERS},
              "main": {}}

    print(f"== AR8 三重差距基准  shards={args.shards} trials={args.trials} ==")
    print(f"拓扑: {len(nodes)} 节点（核心×3 / 边缘×4 / 云×4，跨三大洲）")
    print("数据: ~70% 可重算(1-100MB) + ~30% 不可重算(2-30GB)")

    raw = {r: [] for r in ROUTERS}
    for r in ROUTERS:
        vals = []
        for k in range(args.trials):
            s = args.seed + k * 7919
            sh, ti = make_triple_workload(args.shards, s, node_ids, tiers_mix)
            m = run_triple(r, sh, ti, cfg, s)
            vals.append(m["makespan"])
        med = statistics.median(vals)
        raw[r] = vals
        report["main"][r] = {"makespan_median": med, "raw": vals}
        print(f"  {r:12s} makespan_median={med:10.1f}")

    print("\n== 配对显著性（geo 为基准，逐 trial 配对比）==")
    base = raw["geo"]
    from math import comb
    for r in ["baseline", "xnu", "oh"]:
        wins = sum(1 for a, b in zip(raw[r], base) if b < a)
        n = len(base)
        k = max(wins, n - wins)
        p = 2 * sum(comb(n, i) for i in range(k, n + 1)) / (2 ** n)
        report["main"][r]["geo_wins"] = wins
        report["main"][r]["geo_pair_p"] = round(p, 4)
        print(f"  geo vs {r:12s} geo 更优 {wins}/{n} 次  p≈{p:.4f}")

    if args.json:
        path = args.json if os.path.isabs(args.json) else os.path.join(HERE, args.json)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("\nwrote", path)


if __name__ == "__main__":
    main()