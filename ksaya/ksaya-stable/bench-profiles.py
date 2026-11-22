#!/usr/bin/env python3
"""KSAYA 三版本公平基准 —— 同一状态机、同一 workload、同一随机种子。

为什么这个基准可信：
  1. 三个版本共用 ksayacore.reconcile 的同一份 FSM / fencing / 6 条不变量，
     只换 Router（调度策略）与 systemParams（系统层等效参数）。
  2. 复用真实 SimEnv 的 create_job/job_results/delete_job 接口，只覆写 tick()，
     把「固定 4 tick 完成」改成「按节点算力逐 tick 推进」，这样任务落在谁头上
     会真实反映到完成时间 —— 否则三个版本永远一样快，测了个寂寞。
  3. workload 用固定种子生成，三版本逐字节相同；每个版本跑多次取分布。
  4. 除主对比外还做两组消融：只换 Router / 只换系统层，用来归因差异来源。

Run:
  python3 bench-profiles.py --shards 48 --trials 7 --seed 20261120
  python3 bench-profiles.py --json bench-profiles.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
from typing import Any, Dict, List, Tuple

import ksayacore

HERE = os.path.dirname(os.path.abspath(__file__))

# 数据搬移成本是「物理事实」，固定在环境层，不随 profile 变化：
# 只要任务落点 != 数据 home 节点，就要付这笔跨节点搬数据的量级代价。
# OH profile 因为感知 home（分布式数据管理）而把任务派到 home 附近，
# 从而真实省下这笔成本；baseline/xnu 不感知 home，就会照付。
# 数值与 ksaya-profiles.OhDistributedRouter 的 dataMoveCost 出价权重对齐。
DATA_MOVE_COST = 0.15


def _load_hyphenated(name: str, filename: str):
    """按项目既有惯例加载带连字符的模块（ksaya-bandwidth.py 同款做法）。"""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod           # ← dataclass 需要能反查到本模块
    spec.loader.exec_module(mod)
    return mod


ap = _load_hyphenated("ksaya_profiles", "ksaya-profiles.py")


# ---------------------------------------------------------------------------
# 1. 基准环境：继承真实 SimEnv，只把「完成时间模型」换掉
# ---------------------------------------------------------------------------

class BenchEnv(ksayacore.SimEnv):
    """每 tick 每个在线节点处理其队列里的第一个 job，进度按有效算力累加。"""

    def __init__(self, cfg: Dict[str, Any], *,
                 router: Any,
                 system: Dict[str, Any],
                 tiers: Dict[str, str],
                 base_capacity: Dict[str, float],
                 failure_inject: Dict[str, Any] | None = None):
        # work_ticks 传大数，禁掉父类 tick() 里的固定完成逻辑（我们完全覆写）
        super().__init__(cfg, work_ticks=10 ** 9)
        self.router = router
        self.system = system
        self.tiers = tiers
        self.base_capacity = base_capacity
        self.failure_inject = failure_inject or {}
        self.progress: Dict[str, float] = {}     # job name -> 已完成的等效工作量
        self.finish_tick: Dict[str, int] = {}    # shard id -> 完成 tick
        self.node_work: Dict[str, float] = {n: 0.0 for n in base_capacity}
        self.idle_ticks: Dict[str, int] = {n: 0 for n in base_capacity}
        self.reassign_count = 0
        self._seen_uid: Dict[str, int] = {}
        # 数据本地性：job name -> (shard id, 数据 home 节点, 是否已付迁移成本)
        self.data_home: Dict[str, Tuple[str, str | None]] = {}
        # 分片数据 home 映射（sid -> 节点），由 run_one 传入；数据搬移成本是
        # workload 属性，不随 profile 变化 —— OH 靠「把任务派到 home 附近」
        # 真实省下这笔成本，baseline/xnu 不感知 home 就会真付这笔成本。
        self.home_map: Dict[str, str] = {}
        self.data_move = float(DATA_MOVE_COST)

    # -- 确定性伪随机：只用 LCG，不用 random，保证可复现 -----------------
    @staticmethod
    def _lcg(state: int) -> Tuple[int, float]:
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state, state / float(0x7FFFFFFF)

    def _speed(self, node: str, qos: str) -> float:
        """节点有效算力 × 系统层 QoS 加速 × 确定时延预留折扣。"""
        cap = self.base_capacity.get(node, 0.5)
        speed = float(self.system.get("qosSpeed", {}).get(qos, 1.0))
        if qos in ("background", "utility"):
            cap *= (1.0 - float(self.system.get("reservedCapacity", 0.0)))
        return max(cap * speed, 0.01)

    def tick(self, run: str, generation: int, shards: List[Dict[str, Any]]) -> None:
        self.t += 1
        state = self.failure_inject.get("seed", 0)

        # 故障注入：指定 tick 让某节点离线若干 tick（用来测故障回吐开销）
        down_at = self.failure_inject.get("downAtTick")
        down_for = self.failure_inject.get("downForTicks", 0)
        down_node = self.failure_inject.get("node")
        if down_at is not None and self.t == down_at and down_node:
            self.nodes[down_node] = False
            self.events.append(f"{self.t:04d} INJECT node={down_node} DOWN")
            # 软总线把掉线广播给 OH 路由器的信任账本（若它有该接口）
            note = getattr(self.router, "note_failure", None)
            if callable(note):
                note(down_node, self.t)
        if (down_at is not None and down_node
                and self.t == down_at + down_for and down_for > 0):
            self.nodes[down_node] = True
            self.events.append(f"{self.t:04d} INJECT node={down_node} UP")
            note = getattr(self.router, "note_failure", None)
            if callable(note):
                note(down_node, self.t)

        # 推进可插拔路由器的虚拟时钟（OH 的信任窗口 / XNU 不需要但接口统一）
        set_tick = getattr(self.router, "set_tick", None)
        if callable(set_tick):
            set_tick(self.t)

        if not self.quorate():
            return

        for node in self.order_nodes():
            if not self.nodes.get(node, False):
                self.idle_ticks[node] += 1
                continue
            # 每个节点同时只跑一个 job（对应 k8s Job pod 并发 = 1）
            pending = [j for j in self.jobs.values()
                       if j.get("node") == node
                       and j.get("shard") not in self.finish_tick]
            if not pending:
                self.idle_ticks[node] += 1
                continue
            pending.sort(key=lambda j: j.get("uid", 0))
            job = pending[0]
            sid = job["shard"]
            qos = self.tiers.get(str(sid), "default")

            work = float(max(1, job["hi"] - job["lo"] + 1))
            per_tick_units = 300.0     # 每 tick 每单位算力的处理能力（分片数尺度）

            # 软总线通信开销：真实的建连/协商是「固定延迟」，不是按吞吐比例
            # 持续扣速。
            comm = float(self.system.get("commCost", 0.0))
            gain = self._speed(node, qos) * per_tick_units
            jname = job["name"]
            if comm > 0 and jname not in self.progress:
                gain = max(0.0, gain - comm * 100.0)

            # 数据本地性：首次执行时，若任务落点 != 数据 home，付一次搬移成本。
            # 用 self.data_home 记录「是否已付」，保证只付一次（不逐 tick 重复扣）。
            home = self.home_map.get(sid)
            if home is not None and jname not in self.data_home:
                if node != home:
                    gain *= (1.0 - self.data_move)
                self.data_home[jname] = (sid, home)

            prev = self.progress.get(jname, 0.0)
            self.progress[jname] = prev + gain
            self.node_work[node] += min(gain, max(0.0, work - prev))

            if self.progress[job["name"]] >= work:
                lo, hi = job["lo"], job["hi"]
                self.results[sid] = dict(
                    uid=job["uid"], node=node, metrics=ksayacore.reference_shard(lo, hi),
                    exitCode=0, generation=generation, run=run, shard=sid)
                self.finish_tick[str(sid)] = self.t
                self.jobs.pop(job["name"], None)
                self.events.append(
                    f"{self.t:04d} DONE {job['name']} node={node} qos={qos} work={work:.0f}")

    def order_nodes(self) -> List[str]:
        return list(self.base_capacity.keys())

    def create_job(self, job: Dict[str, Any]) -> bool:
        # 复用父类的幂等检查，但补记 uid 变化次数用于统计故障回吐
        sid = str(job["shard"])
        if self._seen_uid.get(sid) not in (None, job["uid"]):
            self.reassign_count += 1
        self._seen_uid[sid] = job["uid"]
        return super().create_job(job)

    def delete_job(self, job_name: str) -> None:
        try:
            sid = int(job_name.rsplit("-", 1)[1])
            self.finish_tick.pop(str(sid), None)
        except (ValueError, IndexError):
            pass
        super().delete_job(job_name)


# ---------------------------------------------------------------------------
# 2. Workload 生成（固定种子，三版本逐字节相同）
# ---------------------------------------------------------------------------

def make_workload(n: int, seed: int, tiers_mix: Dict[str, float],
                  home_nodes: List[str] | None = None) -> Tuple[List[Dict[str, int]], Dict[str, str]]:
    """生成 n 个分片与其 QoS 档位。分片区间[lo,hi] 由确定性 LCG 派生。

    若给出 home_nodes，则为每个分片分配一个「数据所在节点」home ——
    这是「数据本地性」workload 的输入（分布式数据管理把数据就近存储）。
    数据 home 也用 LCG 派生，保证三版本逐字节相同。
    """
    shards: List[Dict[str, int]] = []
    tiers: Dict[str, str] = {}
    state = seed & 0x7FFFFFFF
    names = list(tiers_mix.keys())
    weights = [tiers_mix[t] for t in names]
    total = float(sum(weights))
    have_home = home_nodes is not None and len(home_nodes) > 0

    for i in range(n):
        state, r1 = BenchEnv._lcg(state)
        state, r2 = BenchEnv._lcg(state)
        state, r3 = BenchEnv._lcg(state)
        # 分片长度覆盖小/中/大：20 ~ 2000，对应 cost 约 1 ~ 100
        lo = 2
        hi = int(20 + r1 * 1980)
        sid = f"s{i:03d}"
        shard = {"id": sid, "lo": lo, "hi": hi}
        if have_home:
            # 数据 home：按 LCG 均匀落到各节点（模拟数据分散存储）
            state, r4 = BenchEnv._lcg(state)
            shard["home"] = home_nodes[int(r4 * len(home_nodes)) % len(home_nodes)]
        shards.append(shard)

        pick = r2 * total
        acc = 0.0
        qos = names[-1]
        for t, w in zip(names, weights):
            acc += w
            if pick <= acc:
                qos = t
                break
        tiers[sid] = qos
    return shards, tiers


# ---------------------------------------------------------------------------
# 3. 单次运行
# ---------------------------------------------------------------------------

def run_one(profile: str, shards: List[Dict[str, int]], tiers: Dict[str, str],
            cfg: Dict[str, Any], seed: int, max_ticks: int = 2000,
            *, router_profile: str | None = None, system_profile: str | None = None,
            failure: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """跑一次 reconcile，返回该次运行的指标。

    router_profile / system_profile 用于消融实验：
      - 都 None：用 profile 自身的完整组合（主对比）
      - 只给 router_profile：换调度算法，系统层固定
      - 只给 system_profile：换系统层，调度算法固定
    """
    rp = router_profile or profile
    sp = system_profile or profile

    cfg = json.loads(json.dumps(cfg))     # deep copy，避免不同 trial 串状态
    # 每个 shard 显式带 tier（真实集群里也可由作业声明）
    for s in cfg.setdefault("_benchShards", []):
        pass
    ledger_shards = [dict(s, tier=tiers[s["id"]]) for s in shards]

    router = ap.make_router(rp, cfg)
    system = ap.system_params(sp)
    base_cap = {n["id"]: ksayacore.node_capacity(n) for n in cfg["nodes"]}
    env = BenchEnv(cfg, router=router, system=system, tiers=tiers,
                   base_capacity=base_cap, failure_inject=failure)
    # 数据 home 映射是 workload 物理事实，注入环境层（不随 profile 变化）
    env.home_map = {s["id"]: s["home"] for s in shards if s.get("home")}

    ledger = ksayacore.Ledger(f"bench-{rp}-{sp}-{seed}", 1, ledger_shards)
    # 与 ksayactl.py:1110 完全一致的构造方式：名字 + 真值源里的状态机定义
    sm = ksayacore.StateMachine("shard", cfg["stateMachines"]["shard"])
    result = ksayacore.reconcile(env, cfg, ledger, sm, max_ticks=max_ticks)

    lat = sorted(env.finish_tick.values())
    n = len(shards)
    done = len(lat)
    makespan = max(lat) if lat else max_ticks
    node_work = dict(env.node_work)
    total_work = sum(node_work.values()) or 1.0
    shares = {k: v / total_work for k, v in node_work.items()}
    # 利用率方差：越接近 0 越均衡（归一化到 [0,1]）
    nk = len(shares) or 1
    ideal = 1.0 / nk
    imbalance = sum((v - ideal) ** 2 for v in shares.values()) / nk

    hi_qos = [v for k, v in env.finish_tick.items() if tiers.get(k) in ("interactive", "default")]
    hi_qos.sort()

    return {
        "seed": seed,
        "profile": profile,
        "router": rp,
        "system": sp,
        "done": done,
        "total": n,
        "makespan": makespan,
        "latency_mean": statistics.fmean(lat) if lat else float(max_ticks),
        "latency_p50": lat[len(lat) // 2] if lat else float(max_ticks),
        "latency_p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else float(max_ticks),
        "hi_qos_p95": hi_qos[min(len(hi_qos) - 1, int(len(hi_qos) * 0.95))] if hi_qos else 0,
        "imbalance": round(imbalance, 6),
        "node_work": {k: round(v, 2) for k, v in node_work.items()},
        "reassign": env.reassign_count,
        "events": len(env.events),
        "converged": bool(result.get("converged", False)),
    }


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------

def summarize(trials: List[Dict[str, Any]]) -> Dict[str, Any]:
    def col(key: str) -> List[float]:
        return [float(t.get(key, 0) or 0) for t in trials]

    def med(key: str) -> float:
        v = col(key)
        return round(statistics.median(v), 3) if v else 0.0

    # 保存每个 trial 的原始指标，供配对显著性检验使用。
    # ⚠️ 只存中位数会丢掉配对信息 —— 7 次 trial 用的是同一批种子，
    # 必须保留 per-trial 才能做「同一 workload 下谁赢几次」的配对检验。
    raw = [{
        "seed": t.get("seed"),
        "makespan": t.get("makespan"),
        "latency_p95": t.get("latency_p95"),
        "hi_qos_p95": t.get("hi_qos_p95"),
        "imbalance": t.get("imbalance"),
        "reassign": t.get("reassign"),
    } for t in trials]

    return {
        "trials": len(trials),
        "raw": raw,
        "makespan_median": med("makespan"),
        "makespan_min": min(col("makespan")) if trials else 0,
        "makespan_max": max(col("makespan")) if trials else 0,
        "latency_p95_median": med("latency_p95"),
        "hi_qos_p95_median": med("hi_qos_p95"),
        "latency_mean_median": med("latency_mean"),
        "imbalance_median": med("imbalance"),
        "reassign_median": med("reassign"),
        "done_median": med("done"),
        "converged_rate": round(sum(1 for t in trials if t["converged"]) / len(trials), 3) if trials else 0,
    }


def main() -> None:
    pa = argparse.ArgumentParser(description="KSAYA 三版本公平基准")
    pa.add_argument("--shards", type=int, default=48)
    pa.add_argument("--trials", type=int, default=7)
    pa.add_argument("--seed", type=int, default=20261120)
    pa.add_argument("--max-ticks", type=int, default=2000)
    pa.add_argument("--json", default="", help="把完整结果写进 JSON 文件")
    pa.add_argument("--failure", action="store_true", help="启用节点故障注入")
    pa.add_argument("--nodes", type=int, default=2,
                    help="参与基准的节点数（默认 2 = 当前真集群；设 8 测规模化）")
    # 数据本地性专测：把节点改成同构（同 class 同内存），让「数据就近」成为
    # 调度主导因素。强异构时算力差（hot/warm = 2.36x）会覆盖掉 15% 的迁移成本，
    # 「搬到快节点」永远更划算 —— 这本身是诚实结论，但不是数据本地性的主场。
    # OpenHarmony 分布式数据管理的真实场景是算力接近的多设备（手机/音箱/车机），
    # 同构才能让 OH 的「数据就近计算」可观测地体现出来。
    pa.add_argument("--homogeneous", action="store_true",
                    help="节点同构化：测数据本地性对 makespan 的独立贡献")
    args = pa.parse_args()

    with open(os.path.join(HERE, "ksaya.config.yaml"), encoding="utf-8") as f:
        cfg = yaml_safe_load(f.read())

    if args.nodes > len(cfg["nodes"]):
        # 规模化模拟：按现有 hot/warm 比例扩展到 N 节点，不改动真值源
        orig = cfg["nodes"]
        cfg["nodes"] = [dict(orig[i % len(orig)],
                             id=f"node{i + 1}",
                             ip=f"10.23.112.{21 + i}",
                             k8sName=f"vm-ksaya-node{i + 1}")
                        for i in range(args.nodes)]

    if args.homogeneous:
        # 同构：所有节点同 class + 同内存，让数据本地性成为调度主导因素。
        # 这台真机器不会变 —— 只是基准里用来隔离「数据就近」独立贡献的对照。
        for n in cfg["nodes"]:
            n["class"] = "hot"
            n["memoryMib"] = 4096

    tiers_mix = {"interactive": 0.2, "default": 0.3, "utility": 0.3, "background": 0.2}
    # 数据本地性：给每个分片一个「数据所在节点」，模拟分布式数据分散存储。
    # home 节点 = 参与基准的节点 id 列表；三版本共享同一 home 映射。
    home_nodes = [n["id"] for n in cfg["nodes"]]
    shards, tiers = make_workload(args.shards, args.seed, tiers_mix,
                                  home_nodes=home_nodes)

    failure = None
    if args.failure:
        failure = {"seed": args.seed, "node": "node1", "downAtTick": 8, "downForTicks": 6}

    report: Dict[str, Any] = {
        "config": {"shards": args.shards, "trials": args.trials, "seed": args.seed,
                   "nodes": args.nodes, "tiersMix": tiers_mix,
                   "dataLocal": True,
                   "homogeneous": bool(args.homogeneous),
                   "failureInjection": bool(args.failure)},
        "workload": {"tierCounts": {t: sum(1 for v in tiers.values() if v == t) for t in ap.TIERS}},
        "main": {},
        "ablationRouterOnly": {},
        "ablationSystemOnly": {},
    }

    print(f"== KSAYA 三版本基准  shards={args.shards} trials={args.trials} "
          f"nodes={args.nodes} seed={args.seed} dataLocal=True ==")
    print("workload tiers:", report["workload"]["tierCounts"])

    for profile in ap.PROFILES:
        trials = []
        for k in range(args.trials):
            s = args.seed + k * 7919
            sh, ti = make_workload(args.shards, s, tiers_mix, home_nodes=home_nodes)
            trials.append(run_one(profile, sh, ti, cfg, s,
                                  max_ticks=args.max_ticks, failure=failure))
        report["main"][profile] = summarize(trials)
        m = report["main"][profile]
        print(f"  {profile:15s} makespan={m['makespan_median']:6.1f}  "
              f"p95={m['latency_p95_median']:6.1f}  "
              f"hiQoS_p95={m['hi_qos_p95_median']:6.1f}  "
              f"imbalance={m['imbalance_median']:.4f}")

    # 消融 A：系统层固定为基线，只换调度算法
    print("== 消融 A：只换调度算法（系统层固定 baseline）==")
    for profile in ap.PROFILES:
        trials = []
        for k in range(args.trials):
            s = args.seed + k * 7919
            sh, ti = make_workload(args.shards, s, tiers_mix, home_nodes=home_nodes)
            trials.append(run_one(profile, sh, ti, cfg, s,
                                  max_ticks=args.max_ticks,
                                  system_profile="nixos-baseline", failure=failure))
        report["ablationRouterOnly"][profile] = summarize(trials)
        m = report["ablationRouterOnly"][profile]
        print(f"  {profile:15s} makespan={m['makespan_median']:6.1f}  p95={m['latency_p95_median']:6.1f}")

    # 消融 B：调度算法固定为基线，只换系统层
    print("== 消融 B：只换系统层（调度固定 baseline）==")
    for profile in ap.PROFILES:
        trials = []
        for k in range(args.trials):
            s = args.seed + k * 7919
            sh, ti = make_workload(args.shards, s, tiers_mix, home_nodes=home_nodes)
            trials.append(run_one(profile, sh, ti, cfg, s,
                                  max_ticks=args.max_ticks,
                                  router_profile="nixos-baseline", failure=failure))
        report["ablationSystemOnly"][profile] = summarize(trials)
        m = report["ablationSystemOnly"][profile]
        print(f"  {profile:15s} makespan={m['makespan_median']:6.1f}  p95={m['latency_p95_median']:6.1f}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("wrote", args.json)


def yaml_safe_load(text: str) -> Dict[str, Any]:
    # 复用 ksayactl 的 YAML 依赖；这里直接 import yaml，作为最小依赖
    import yaml
    return yaml.safe_load(text)


if __name__ == "__main__":
    main()
