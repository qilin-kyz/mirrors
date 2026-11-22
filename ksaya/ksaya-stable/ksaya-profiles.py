#!/usr/bin/env python3
"""KSAYA 三种系统 profile 的调度策略插件。

设计边界（务必先读）：
  XNU / OpenHarmony 的内核**不能**替换 NixOS 的 Linux 内核 —— ABI 断层、
  k3s 依赖 cgroup/namespace/overlayfs、NixOS 声明式模型绑 Linux 模块体系。
  所以这里做的是「借设计，不借代码」：三个版本共用同一份 reconcile FSM、
  同一套 6 条不变量、同一份真值源，只替换 Router 决策函数 + 系统层参数。

四个版本：
  nixos-baseline   容量加权 vruntime（现有实现，ksayacore.Router）
  xnu-qos          借 XNU 的 QoS 分级：任务分 4 档，高 QoS 走高能力节点，
                   并用权重放大其执行速度；低 QoS 被让位（加 vruntime 债）
  oh-distributed   借 OpenHarmony 分布式任务调度：节点能力协商 + 出价拍卖
                   （预计完成时间 + 积压惩罚 + 通信代价），带容量预留防抖动
  geo-distributed  传输经济学调度（三重差距专用）：节点对传输矩阵（延迟≠带宽）
                   + 可重算/不可重算区分，融合 OH 的信任降级与 XNU 的吞吐/延迟分化

geo-distributed 的建模哲学（借多方真实系统思想，不借代码）：
  - 延迟 ≠ 带宽：远是光速 RTT 硬顶，「机房不烂」是 Gbps 出口 —— 二者分开建模。
  - 传输 vs 重算经济学（Spark lineage / Snowflake 下推 / CDN 边缘计算）：
    可重算中间结果甩到快节点重算；不可重算原始数据必须搬（瓶颈=源上行 vs 目标下行）。
  - 融合点：继承 OH 的节点健康信任降级（假恢复规避）；吸收 XNU 的
    吞吐/延迟分化（交互任务对 RTT 敏感、批处理任务对带宽敏感）。

全部严格确定性：无 random、无时间依赖，平局按配置节点顺序打破，可逐字节回放。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

import ksayacore


# ---------------------------------------------------------------------------
# 0. Profile 元数据：Nix 系统层参数的唯一真值源
# ---------------------------------------------------------------------------

PROFILES: Dict[str, Dict[str, Any]] = {
    "nixos-baseline": {
        "label": "NixOS 基线（现有系统）",
        "origin": "Linux CFS + cgroup v2",
        "router": "ksayacore.Router",
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
        "router": "ksaya-profiles.XnuQosRouter",
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
        "router": "ksaya-profiles.OhDistributedRouter",
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
    "geo-distributed": {
        "label": "传输经济学调度版（三重差距）",
        "origin": "Spark lineage + Snowflake 下推 + CDN 边缘 + OSPF 链路代价",
        "router": "ksaya-profiles.GeoDistributedRouter",
        "system": {
            "cpuWeight": {"batch": 50, "control": 300},
            "qosSpeed": {},
            "reservedCapacity": 0.0,
            "commCost": 0.0,
            # 传输经济学：可重算数据甩快节点重算、不可重算数据按瓶颈带宽搬移。
            # 数据代价由 GeoDistributedRouter 从 topology + node.region/bwMbps 实时
            # 计算，不在这里设单值常数 —— 这正是它区别于 OH「单值 dataMoveCost」的地方。
            "dataMoveCost": 0.0,
            "zram": "lz4/50%",
        },
    },
}

TIERS = ("background", "utility", "default", "interactive")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

# geo-distributed 的统一量纲（与 bench-triple-gap.py 保持一致，保证公平口径）：
# 1 tick = 100ms；weight=1 节点每 tick 消化 300 个工作量单位；重算 1MB ≈ 6 个单位。
TICK_MS = 100.0
PER_TICK_UNITS = 300.0
RECOMPUTE_UNITS_PER_MB = 6.0
# 节点缺省带宽（MB/s）：未声明 bwMbps 时按 1GbE 保守取值（= bench 的 "gbe" 档）。
DEFAULT_BW_MBPS = 110.0


def infer_tier(cost: float) -> str:
    """按分片代价折推 QoS 档位。

    在 KSAYA 里分片大小就是任务形态：小分片多为交互式查询/控制面请求，
    大分片是后台批处理。这与 ksaya.config.yaml 的 bandwidth.tiers 语义一致。
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

class XnuQosRouter(ksayacore.Router):
    """借 XNU 的 QoS class 思想：调度权重，不是绝对优先级。

    XNU 的关键不是「高优先级永远先跑」（那会饿死低优先级），而是：
      - 每个任务有 QoS class，class 决定它在队列里前进的速度；
      - 高 QoS 任务获得优先级提升（priority promotion），但做完就还回去；
      - 低 QoS 任务不会被永久饿死，只是让位。

    映射到 KSAYA：
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
             home: str | None = None,
             dataSize: float | None = None,
             recomputable: bool | None = None) -> str:
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

class OhDistributedRouter(ksayacore.Router):
    """借 OpenHarmony 分布式软总线 + 分布式任务调度 + 确定时延引擎。

    OpenHarmony 的做法不是中心化轮询，而是：
      - 节点通过软总线广播自己的能力（算力/内存/负载）；
      - 任务按「谁能最快做完」选执行者（能力协商 + 出价）；
      - 确定时延引擎给高实时任务预留资源，避免被大任务拖住产生长尾。

    映射到 KSAYA 的出价函数：
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
             home: str | None = None,
             dataSize: float | None = None,
             recomputable: bool | None = None) -> str:
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
# 2b. 传输经济学调度（geo-distributed）：三重差距专用
# ---------------------------------------------------------------------------

class GeoDistributedRouter(OhDistributedRouter):
    """感知「性能 / 带宽 / 地域 + 数据可重算性」的调度器。

    借多方思想（不借代码）：
      - Spark lineage / Snowflake 下推 / CDN 边缘计算：可重算中间结果甩到快节点
        重算；不可重算原始数据必须搬。这是「传输 vs 重算」经济学。
      - OSPF/BGP / CockroachDB 拓扑感知：出价里带节点对链路代价（延迟≠带宽）。

    融合（继承自 OH + 吸收自 XNU）：
      1. 继承 OH 的节点健康信任降级（假恢复规避）：刚掉线/恢复的节点在
         信任窗口内被加价，防止任务被立刻塞回又踩一脚。
      2. 吸收 XNU 的吞吐/延迟分化：交互档对跨域 RTT 额外敏感（纯吃延迟），
         批处理档由带宽主导（大数据吞吐摊平延迟）。

    出价函数：
        bid = 排队 tick + 计算 tick + 数据 tick
      - 数据 tick（可重算） = size × R / (weight × PER_TICK) —— 与带宽无关；
      - 数据 tick（不可重算） = (RTT + size / min(源上行, 目标下行)) / TICK_MS。
      瓶颈带宽 = 两端较小者，因为不可重算数据必须从「数据源窄上行」送出来。
    """

    # 融合自 XNU：交互/默认档延迟敏感，utility/background 批处理吞吐导向。
    LATENCY_TIERS = ("interactive", "default")
    THROUGHPUT_TIERS = ("utility", "background")
    # 交互任务对跨域 RTT 的额外敏感系数（>1 放大延迟惩罚，留本域优先）。
    LATENCY_RTT_WEIGHT = 1.5
    # 无 topology 时的缺省地域（退化同机房，向后兼容）。
    DEFAULT_REGION = "site-a"
    # 无明确 RTT 条目时的保守缺省（ms）。
    DEFAULT_RTT_MS = 120.0

    def __init__(self, cfg: Dict[str, Any]):
        # 直接走基类 Router 的初始化（跳过 Oh 的 __init__ 以免读到 oh 的 system），
        # 再手工补齐 Oh 的信任降级字段。
        ksayacore.Router.__init__(self, cfg)
        sys = PROFILES["geo-distributed"]["system"]
        self.reserve = float(sys["reservedCapacity"])
        self.comm = float(sys["commCost"])
        self.data_move = float(sys["dataMoveCost"])
        self.tier_map: Dict[str, str] = {}
        # 数据 home 映射（sid -> 节点）。用 home_map 作为主名（与基准环境 env.home_map
        # 及 test_57-59 的约定一致），同时保留 Oh 基类的 data_home 别名指向同一 dict，
        # 让继承来的 snapshot()（走 Oh 实现读 data_home）与 geo 自己的出价读 home_map
        # 天然一致，避免「数据所在节点」物理事实在三处漂移。
        self.home_map: Dict[str, str] = {}
        self.data_home = self.home_map
        self.node_unstable_at: Dict[str, int] = {nid: 0 for nid in self.order}
        self.now = 0

        # 传输矩阵：从真值源 topology 段读取（regions + 地域对 RTT 矩阵）。
        topo = cfg.get("topology") or {}
        self.regions: set = set(topo.get("regions", []))
        rtt: Dict[Tuple, float] = {}
        for k, v in (topo.get("rttMs") or {}).items():
            if isinstance(k, str) and "|" in k:
                a, b = k.split("|", 1)
                rtt[(a, b)] = float(v)
            elif isinstance(k, (list, tuple)) and len(k) == 2:
                a, b = k
                rtt[(a, b)] = float(v)
        self.rtt = rtt

        # 节点地域 / 带宽 / 算力权重。
        self.region: Dict[str, str] = {
            n["id"]: n.get("region", self.DEFAULT_REGION) for n in cfg.get("nodes", [])
        }
        self.bw: Dict[str, float] = {
            n["id"]: float(n.get("bwMbps") or DEFAULT_BW_MBPS) for n in cfg.get("nodes", [])
        }
        # 算力权重：优先用显式 _weight（bench 的真实机型权重），否则回落到
        # 基类 node_capacity（class × 内存开方）——保证同一路由器既能在
        # 真实 config（无 _weight）下运行，也能在基准（带 _weight）下运行。
        self.weight: Dict[str, float] = {}
        for nid in self.order:
            node = self.nodes.get(nid, {})
            w = node.get("_weight")
            self.weight[nid] = float(w) if isinstance(w, (int, float)) else self.capacity.get(nid, 0.5)

        # 数据属性账本（由 reconcile 透传的 dataSizeMb / recomputable 填充）。
        self.size_map: Dict[str, float] = {}
        self.recompute_map: Dict[str, bool] = {}

    def effective_capacity(self, nid: str) -> float:
        """统一用真实算力权重，避免基类 class×mem 与 _weight 打架。"""
        return max(self.weight.get(nid, self.capacity.get(nid, 0.5)), 1e-6)

    def _rtt_ms(self, ra: str, rb: str) -> float:
        if ra == rb:
            return self.rtt.get((ra, rb), 0.05)
        return self.rtt.get((ra, rb), self.rtt.get((rb, ra), self.DEFAULT_RTT_MS))

    def _calc_ticks(self, nid: str, cost_units: float) -> float:
        return cost_units / (self.effective_capacity(nid) * PER_TICK_UNITS)

    def _data_ticks(self, nid: str, sid: str | None) -> float:
        """分片落在 nid 上时，为「数据可用」付出的额外 tick（传输经济学）。"""
        if sid is None:
            return 0.0
        home = self.home_map.get(sid)
        if home is None or home == nid:
            return 0.0
        size = self.size_map.get(sid, 0.0)
        w = self.effective_capacity(nid)
        if self.recompute_map.get(sid, False):
            # 可重算：在落点重新生成，代价只与算力有关（与带宽/地域无关）
            return size * RECOMPUTE_UNITS_PER_MB / (w * PER_TICK_UNITS)
        # 不可重算：必须搬。瓶颈 = 数据源上行 vs 落点下行的较小带宽。
        bw = min(self.bw.get(home, DEFAULT_BW_MBPS), self.bw.get(nid, DEFAULT_BW_MBPS))
        rtt = self._rtt_ms(self.region.get(home, ""), self.region.get(nid, ""))
        return (rtt + (size / max(bw, 1e-6)) * 1000.0) / TICK_MS

    def pick(self, available: Iterable[str], cost: float,
             sid: str | None = None, tier: str | None = None,
             home: str | None = None,
             dataSize: float | None = None,
             recomputable: bool | None = None) -> str:
        qos = tier or infer_tier(cost)
        if sid is not None:
            if home:
                self.home_map[sid] = home
            if dataSize is not None:
                self.size_map[sid] = float(dataSize)
            if recomputable is not None:
                self.recompute_map[sid] = bool(recomputable)

        cands = [nid for nid in self.order
                 if nid in available and nid in self.weight]
        if not cands:
            cands = [nid for nid in self.order if nid in self.weight]
        if not cands:
            return ""

        now = getattr(self, "now", 0)

        def bid(nid: str):
            queued = self.vruntime[nid] / self.effective_capacity(nid) / PER_TICK_UNITS
            calc = self._calc_ticks(nid, cost)
            data = self._data_ticks(nid, sid)
            # 融合自 XNU：交互档对跨域 RTT 额外敏感（比批处理档更不肯跨域）。
            if qos in self.LATENCY_TIERS and sid is not None:
                home = self.home_map.get(sid)
                if home and home != nid and not self.recompute_map.get(sid, False):
                    rtt = self._rtt_ms(self.region.get(home, ""), self.region.get(nid, ""))
                    data += (self.LATENCY_RTT_WEIGHT - 1.0) * rtt / TICK_MS
            # 融合自 OH：刚异常过的节点在信任窗口内加价。
            trust = self._trust_penalty(nid, now)
            return (queued + calc + data + trust, self.order.index(nid))

        best = min(cands, key=bid)
        cap = self.effective_capacity(best)
        self.vruntime[best] += cost / cap
        self.assigned[best] += 1
        if sid is not None:
            self.tier_map[sid] = qos
        return best

    def snapshot(self) -> Dict[str, Any]:
        d = super().snapshot()
        d["geoAware"] = True
        d["regions"] = sorted(self.regions)
        d["dataHomeCount"] = len(self.home_map)
        d["sizeTracked"] = len(self.size_map)
        return d


# ---------------------------------------------------------------------------
# 3. 统一工厂
# ---------------------------------------------------------------------------

def make_router(profile: str, cfg: Dict[str, Any]):
    """按 profile 名构造 Router。未知名字直接拒绝，不静默降级。"""
    if profile == "nixos-baseline":
        return ksayacore.Router(cfg)
    if profile == "xnu-qos":
        return XnuQosRouter(cfg)
    if profile == "oh-distributed":
        return OhDistributedRouter(cfg)
    if profile == "geo-distributed":
        return GeoDistributedRouter(cfg)
    raise ValueError(f"unknown KSAYA profile: {profile!r}; "
                     f"choose from {sorted(PROFILES)}")


def system_params(profile: str) -> Dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown KSAYA profile: {profile!r}")
    # ⚠️ 必须深拷贝：qosSpeed 是嵌套 dict，浅拷贝会让所有调用方共享同一个对象。
    # 实测后果：参数扫描时改了 A 的 background 值，B 的也跟着变成 999，
    # 于是扫描结果全乱（「全 1.0 反而最慢」这种反直觉结论就是这么来的）。
    import copy
    return copy.deepcopy(PROFILES[profile]["system"])


if __name__ == "__main__":
    import json
    print(json.dumps(PROFILES, ensure_ascii=False, indent=2))
