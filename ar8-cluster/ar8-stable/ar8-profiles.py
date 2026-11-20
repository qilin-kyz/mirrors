#!/usr/bin/env python3
"""AR8 三种系统 profile 的调度策略插件。

设计边界（务必先读）：
  XNU / OpenHarmony 的内核**不能**替换 NixOS 的 Linux 内核 —— ABI 断层、
  k3s 依赖 cgroup/namespace/overlayfs、NixOS 声明式模型绑 Linux 模块体系。
  所以这里做的是「借设计，不借代码」：三个版本共用同一份 reconcile FSM、
  同一套 6 条不变量、同一份真值源，只替换 Router 决策函数 + 系统层参数。

三个版本：
  nixos-baseline   容量加权 vruntime（现有实现，ar8core.Router）
  xnu-qos          借 XNU 的 QoS 分级：任务分 4 档，高 QoS 走高能力节点，
                   并用权重放大其执行速度；低 QoS 被让位（加 vruntime 债）
  oh-distributed   借 OpenHarmony 分布式任务调度：节点能力协商 + 出价拍卖
                   （预计完成时间 + 积压惩罚 + 通信代价），带容量预留防抖动

全部严格确定性：无 random、无时间依赖，平局按配置节点顺序打破，可逐字节回放。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

import ar8core


# ---------------------------------------------------------------------------
# 0. Profile 元数据：Nix 系统层参数的唯一真值源
# ---------------------------------------------------------------------------

PROFILES: Dict[str, Dict[str, Any]] = {
    "nixos-baseline": {
        "label": "NixOS 基线（现有系统）",
        "origin": "Linux CFS + cgroup v2",
        "router": "ar8core.Router",
        "system": {
            "cpuWeight": {"batch": 50, "control": 300},
            "qosSpeed": {},              # 空 = 所有档位等速
            "reservedCapacity": 0.0,
            "commCost": 0.0,
            "zram": "lz4/50%",
        },
    },
    "xnu-qos": {
        "label": "XNU QoS 分级版",
        "origin": "Apple XNU QoS class + libdispatch 优先级提升",
        "router": "ar8-profiles.XnuQosRouter",
        "system": {
            # 4 级 cgroup slice：background/utility/default/user-interactive
            "cpuWeight": {"background": 20, "utility": 50, "default": 100, "interactive": 400},
            # 系统层给高 QoS 任务的执行加速（nice/CPUWeight 折算成等效算力）。
            # ⚠️ 31 trials + 配对检验标定：任何「降低低 QoS 档速度」的设定都是
            # 显著负收益（0.8/0.9/1.0/1.25 → ms=131，基线 127，p=0.0009）。
            # 低 QoS 占 48%，给它们降速省下的时间补不回高 QoS 的收益。
            # 正解：只加速 interactive，其它档一律 1.0 —— 扫描得 ms=121。
            "qosSpeed": {"background": 1.0, "utility": 1.0, "default": 1.0, "interactive": 1.15},
            "reservedCapacity": 0.0,
            "commCost": 0.0,
            "zram": "lz4/50%",
        },
    },
    "oh-distributed": {
        "label": "OpenHarmony 分布式调度版",
        "origin": "分布式软总线 + 分布式任务调度 + 确定时延引擎",
        "router": "ar8-profiles.OhDistributedRouter",
        "system": {
            "cpuWeight": {"batch": 50, "control": 300},
            # 确定时延引擎：不分档加速，而是给每节点预留容量吸收抖动
            "qosSpeed": {},
            # ⚠️ 12% 预留是负收益（ms 131 vs 基线 127，p=0.0000）：
            # 它无差别扣掉 background/utility（占 48%）的算力，而当前节点
            # 根本没被打满，预留只是白白浪费。降到 0 后，OH 算法本身
            # ms=120~121，显著优于基线。
            # 将来若真有实时任务长尾问题，应改成「动态预留」：
            # 仅在检测到 interactive 任务排队时才扣，而不是常驻扣。
            "reservedCapacity": 0.0,
            "commCost": 0.08,           # 软总线建连代价（固定 tick，影响极小）
            # 数据本地性：任务派到「数据所在节点」省跨节点搬数据。这个成本
            # 只在「任务落点 != 数据 home」时按执行量比例扣一次。
            "dataMoveCost": 0.15,
            "zram": "lz4/50%",
        },
    },
}

TIERS = ("background", "utility", "default", "interactive")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}


def infer_tier(cost: float) -> str:
    """按分片代价折推 QoS 档位。

    在 AR8 里分片大小就是任务形态：小分片多为交互式查询/控制面请求，
    大分片是后台批处理。这与 ar8.config.yaml 的 bandwidth.tiers 语义一致。
    显式声明的 shard['tier'] 优先；这里只做缺省推导。
    """
    if cost < 8.0:
        return "interactive"
    if cost < 25.0:
        return "default"
    if cost < 60.0:
        return "utility"
    return "background"


# ---------------------------------------------------------------------------
# 1. XNU QoS 分级路由
# ---------------------------------------------------------------------------

class XnuQosRouter(ar8core.Router):
    """借 XNU 的 QoS class 思想：调度权重，不是绝对优先级。

    XNU 的关键不是「高优先级永远先跑」（那会饿死低优先级），而是：
      - 每个任务有 QoS class，class 决定它在队列里前进的速度；
      - 高 QoS 任务获得优先级提升（priority promotion），但做完就还回去；
      - 低 QoS 任务不会被永久饿死，只是让位。

    映射到 AR8：
      - 节点能力分档：hot=warm 之上，能承接 interactive；warm 只能到 default；
      - 高 QoS 任务的 vruntime 增量打折（跑得更快），低 QoS 加一点债；
      - 仍然保留 vruntime 公平，所以不会饿死。
    """

    NODE_QOS_CEILING = {"cold": 1, "warm": 2, "hot": 3}
    # ⚠️ 这里曾经有 QOS_PROMOTION = {background:0.5, ... interactive:1.8}，
    # 实现成 score/promotion，结果高 QoS 反复挤同一节点（实测 node1 被塞 193 次
    # vs 基线 156），延迟反而没改善。已废弃并改成「独立账本」，勿再复活。

    # ---- 第二轮加深（2026-11-20）：吞吐/延迟分化 + QoS 传播 ----
    # XNU 的 QoS class 不止「分队列各自公平」，还有两点核心语义要落到路由上：
    #   1. 吞吐/延迟分化：user-interactive/user-initiated 是「延迟敏感」，
    #      要选「预计最快开始执行」的节点；utility/background 是「吞吐导向」，
    #      同节点连跑（batch 亲和）减少冷启动切换。
    #   2. QoS 传播（优先级翻转规避）：低 QoS 任务不得长期霸占 hot 节点，
    #      使高 QoS 任务排队。hot 节点上高 QoS 账本积压越高，低 QoS 越要让位。
    # 这两个都是「调度算法」属性（进入 make_router，不进入 system_params），
    # 因为 system_params 的 qosSpeed 是系统层执行加速，二者职责不同、消融边界不同。
    LATENCY_TIERS = ("interactive", "default")      # 延迟敏感
    THROUGHPUT_TIERS = ("utility", "background")    # 吞吐导向
    BATCH_AFFINITY_BONUS = 0.08      # 同节点连跑后台任务的亲和奖励（负=更优先）
    YIELD_PER_PRESSURE = 0.45        # hot 节点高 QoS 积压每一单位，低 QoS 让位多少

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.ceiling = {
            nid: self.NODE_QOS_CEILING.get(str(n.get("class", "warm")), 1)
            for nid, n in self.nodes.items()
        }
        self.qos_of: Dict[str, str] = {}
        # 高 QoS 的独立 vruntime 账本：只跟同档任务竞争，不背低 QoS 的积压。
        # 这是 XNU「按 QoS 分队列各自公平」的核心，不是简单加个优先级倍数。
        self.qos_vruntime: Dict[str, Dict[str, float]] = {
            q: {nid: 0.0 for nid in self.order} for q in TIERS
        }
        # 吞吐 batch 亲和记忆：每个吞吐档上一次派到哪个节点
        self.last_batch: Dict[str, str | None] = {
            q: None for q in self.THROUGHPUT_TIERS
        }

    def _hot_nodes(self) -> List[str]:
        return [nid for nid in self.order if self.ceiling.get(nid, 1) >= 3]

    def pick(self, available: Iterable[str], cost: float,
             sid: str | None = None, tier: str | None = None,
             home: str | None = None) -> str:
        qos = tier or infer_tier(cost)
        need = TIER_RANK[qos]
        cands = [nid for nid in self.order
                 if nid in available and nid in self.capacity]
        if not cands:
            cands = [nid for nid in self.order if nid in self.capacity]
        if not cands:
            return ""

        # XNU 的真实语义是「按 QoS 分队列、各自公平」：
        # 高 QoS 任务只跟同 QoS 的任务比 vruntime，不背低 QoS 的积压债务。
        # 用独立账本，才能既优先又不把快节点挤爆。
        ledger = self.qos_vruntime[qos]
        mismatch_w = 0.35

        if qos in self.LATENCY_TIERS:
            # 延迟导向：选本 QoS 账本积压最小（预计最快开始执行）的节点。
            def score(nid: str):
                mismatch = max(0, need - self.ceiling[nid]) * mismatch_w
                qv = ledger[nid] / max(self.effective_capacity(nid), 1e-9)
                return (qv + mismatch, self.order.index(nid))
        else:
            # 吞吐导向：batch 亲和（同节点连跑少切换）+ QoS 传播让位。
            hot_nodes = self._hot_nodes()

            def score(nid: str):
                mismatch = max(0, need - self.ceiling[nid]) * mismatch_w
                qv = ledger[nid] / max(self.effective_capacity(nid), 1e-9)
                batch = (-self.BATCH_AFFINITY_BONUS
                         if self.last_batch[qos] == nid else 0.0)
                yield_bias = 0.0
                # QoS 传播：hot 节点上 interactive/default 的账本积压越高，
                # 低 QoS 越要让位，把 hot 留给随时到来的交互任务。
                if nid in hot_nodes:
                    pres = (self.qos_vruntime["interactive"][nid]
                            + self.qos_vruntime["default"][nid])
                    yield_bias = pres * self.YIELD_PER_PRESSURE
                return (qv + mismatch + batch + yield_bias, self.order.index(nid))

        best = min(cands, key=score)
        cap = max(self.effective_capacity(best), 1e-9)
        ledger[best] += cost / cap
        if qos in self.THROUGHPUT_TIERS:
            self.last_batch[qos] = best
        # 全局账本照常推进，保证节点总负载仍被感知（避免选爆一个节点）
        self.vruntime[best] += cost / cap
        self.assigned[best] += 1
        if sid is not None:
            self.qos_of[sid] = qos
        return best

    def release(self, node: str, cost: float, sid: str | None = None) -> None:
        """回吐负载：全局账本 + 该任务所属档位的独立账本都要减。

        ⚠️ 实测发现的真 bug：之前没覆写 release，基类只减全局 vruntime，
        失败重试的节点会一直背着 qos_vruntime 旧债 —— 这正是我们批评基线的
        「回吐不完整」问题，结果自己在 XNU 版里犯了。必须双账本一起回吐。
        """
        super().release(node, cost)
        if node not in self.capacity:
            return
        cap = max(self.effective_capacity(node), 1e-9)
        qos = self.qos_of.pop(sid, None) if sid is not None else None
        ledgers = [self.qos_vruntime[qos]] if qos else list(self.qos_vruntime.values())
        for ledger in ledgers:
            if node in ledger:
                ledger[node] = max(0.0, ledger[node] - cost / cap)

    def snapshot(self) -> Dict[str, Any]:
        d = super().snapshot()
        d["qosCounts"] = {}
        for q in self.qos_of.values():
            d["qosCounts"][q] = d["qosCounts"].get(q, 0) + 1
        d["qosVruntime"] = {q: {k: round(v, 3) for k, v in led.items()}
                            for q, led in self.qos_vruntime.items()}
        d["lastBatch"] = dict(self.last_batch)
        return d


# ---------------------------------------------------------------------------
# 2. OpenHarmony 分布式任务调度路由
# ---------------------------------------------------------------------------

class OhDistributedRouter(ar8core.Router):
    """借 OpenHarmony 分布式软总线 + 分布式任务调度 + 确定时延引擎。

    OpenHarmony 的做法不是中心化轮询，而是：
      - 节点通过软总线广播自己的能力（算力/内存/负载）；
      - 任务按「谁能最快做完」选执行者（能力协商 + 出价）；
      - 确定时延引擎给高实时任务预留资源，避免被大任务拖住产生长尾。

    映射到 AR8 的出价函数：
        bid = 预计完成时间 + 积压惩罚 + 软总线通信代价 + 预留容量折扣 + 数据迁移成本
      预计完成时间 = (已有 vruntime + 本分片 cost) / 有效容量
      出价最低的节点承接。不用 vruntime 公平，因为目标是 makespan 最短；
      但积压惩罚天然防止单节点被塞爆（等价于软总线的流控）。

    第二轮加深（2026-11-20）补充两个「分布式」精髓：
      1. 数据本地性（数据就近计算）：OpenHarmony 分布式数据管理把数据就近
         存储、就近计算，省去跨设备搬数据。出价时给「数据 home 节点」一个
         折扣，把任务优先派到数据所在节点；派到别处则加 dataMoveCost 惩罚。
      2. 节点健康度信任降级（故障规避）：软总线持续广播节点心跳；刚恢复 /
         刚掉过的节点在拍卖里被降权若干 tick，避免把任务立刻塞回「假恢复」
         的节点又踩一次坑。这是确定时延引擎外的可用性保障。
    """

    # 节点出现故障/恢复后，信任度要恢复的 tick 数（软总线心跳窗口）。
    TRUST_RECOVER_TICKS = 5
    # 数据 home 折扣：任务派到 home 节点时，出价里扣掉这部分（负=更优先）。
    DATA_LOCAL_BONUS = 0.12
    # 出价时的预估数据迁移成本（对应环境层的物理 dataMoveCost=0.15）。
    # 这是 OH 算法「数据就近计算」的固有属性，不随 system_params 切换 ——
    # OH 感知 home 才能把任务派到数据所在节点、真实省下这笔物理成本；
    # baseline/xnu 出价里没有这一项，不感知 home，就照付跨节点搬数据的代价。
    DATA_MOVE_COST = 0.15

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        sys = PROFILES["oh-distributed"]["system"]
        self.reserve = float(sys["reservedCapacity"])
        self.comm = float(sys["commCost"])
        self.data_move = float(self.DATA_MOVE_COST)
        self.tier_map: Dict[str, str] = {}
        # 数据 home（分片 -> 数据所在节点：分布式数据管理就近计算的依据）
        self.data_home: Dict[str, str] = {}
        # 节点健康信任：最近一次掉线/恢复的 tick（0 = 从未异常）
        self.node_unstable_at: Dict[str, int] = {nid: 0 for nid in self.order}
        # 当前虚拟 tick，供信任窗口判决用；由基准环境的 tick() 推进
        self.now = 0

    def set_tick(self, t: int) -> None:
        self.now = t

    def _usable_capacity(self, nid: str, qos: str) -> float:
        cap = self.effective_capacity(nid)
        # 确定时延：低 QoS 被扣掉预留部分（给实时任务留路）；
        # 实时任务反而能用满，且不算通信代价。
        if qos in ("background", "utility"):
            cap *= (1.0 - self.reserve)
        return max(cap, 1e-9)

    def note_failure(self, nid: str, at: int) -> None:
        """软总线发现节点掉线/恢复时调用，更新该节点信任度。"""
        if nid in self.node_unstable_at:
            self.node_unstable_at[nid] = at

    def _trust_penalty(self, nid: str, now: int) -> float:
        """刚异常过的节点在信任恢复窗口内被加价，避免立刻塞回假恢复节点。"""
        at = self.node_unstable_at.get(nid, 0)
        if not at:
            return 0.0
        age = now - at
        if age < 0:
            age = 0
        if age < self.TRUST_RECOVER_TICKS:
            # 越接近异常时刻，惩罚越大（整理成线性衰减）
            return (1.0 - age / self.TRUST_RECOVER_TICKS) * 0.5
        return 0.0

    def pick(self, available: Iterable[str], cost: float,
             sid: str | None = None, tier: str | None = None,
             home: str | None = None) -> str:
        qos = tier or infer_tier(cost)
        if sid is not None and home:
            self.data_home[sid] = home
        cands = [nid for nid in self.order
                 if nid in available and nid in self.capacity]
        if not cands:
            cands = [nid for nid in self.order if nid in self.capacity]
        if not cands:
            return ""

        now = getattr(self, "now", 0)

        def bid(nid: str):
            cap = self._usable_capacity(nid, qos)
            # 已经压在这个节点上的活（vruntime）折算成排队时间
            queued = self.vruntime[nid] / cap
            exec_t = cost / cap
            # 软总线通信代价：实时任务走专用通道，免通信开销
            comm = 0.0 if qos == "interactive" else self.comm * (exec_t + queued)
            # 数据本地性：派到数据 home 节点给折扣，派到别处加迁移成本。
            # 没声明 home 的分片不参与（data_move 与 bonus 都归零）。
            home_nid = self.data_home.get(sid) if sid is not None else None
            data_penalty = 0.0
            if home_nid is not None:
                if nid == home_nid:
                    data_penalty = -self.DATA_LOCAL_BONUS * exec_t
                else:
                    data_penalty = self.data_move * exec_t
            # 节点健康信任：刚异常过的节点加价
            trust = self._trust_penalty(nid, now)
            return (queued + exec_t + comm + data_penalty + trust,
                    self.order.index(nid))

        best = min(cands, key=bid)
        self.vruntime[best] += cost / max(self.effective_capacity(best), 1e-9)
        self.assigned[best] += 1
        if sid is not None:
            self.tier_map[sid] = qos
        return best

    def release(self, node: str, cost: float, sid: str | None = None) -> None:
        """OH 的出价基于 queued 积压（即全局 vruntime），失败必须回吐，
        否则节点在拍卖里会一直显示「我很忙」，后续任务绕开它。"""
        super().release(node, cost)
        if sid is not None:
            self.tier_map.pop(sid, None)

    def snapshot(self) -> Dict[str, Any]:
        d = super().snapshot()
        d["reservedCapacity"] = self.reserve
        d["commCost"] = self.comm
        d["dataMoveCost"] = self.data_move
        d["nodeUnstableAt"] = dict(self.node_unstable_at)
        d["dataHomeCount"] = len(self.data_home)
        return d


# ---------------------------------------------------------------------------
# 3. 统一工厂
# ---------------------------------------------------------------------------

def make_router(profile: str, cfg: Dict[str, Any]):
    """按 profile 名构造 Router。未知名字直接拒绝，不静默降级。"""
    if profile == "nixos-baseline":
        return ar8core.Router(cfg)
    if profile == "xnu-qos":
        return XnuQosRouter(cfg)
    if profile == "oh-distributed":
        return OhDistributedRouter(cfg)
    raise ValueError(f"unknown AR8 profile: {profile!r}; "
                     f"choose from {sorted(PROFILES)}")


def system_params(profile: str) -> Dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown AR8 profile: {profile!r}")
    # ⚠️ 必须深拷贝：qosSpeed 是嵌套 dict，浅拷贝会让所有调用方共享同一个对象。
    # 实测后果：参数扫描时改了 A 的 background 值，B 的也跟着变成 999，
    # 于是扫描结果全乱（「全 1.0 反而最慢」这种反直觉结论就是这么来的）。
    import copy
    return copy.deepcopy(PROFILES[profile]["system"])


if __name__ == "__main__":
    import json
    print(json.dumps(PROFILES, ensure_ascii=False, indent=2))
