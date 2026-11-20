"""AR8 稳定核心 —— 声明式 schema / 显式状态机 / Env 抽象 / 可重入 reconcile / 不变量断言。

设计来源（源码级调研结论）：
  * PVE::HA::Env（pve-ha-manager/src/PVE/HA/Env.pm）: 把时间、锁、quorum、状态读写抽象成接口，
    同一份状态机代码在真实集群 / 回归测试 / GUI 仿真里零改动复用。
  * PVE::HA::Manager（Manager.pm:1116-1218）: 轮次驱动的可重入收敛循环，跑到无状态变化为止。
  * change_service_state（Manager.pm:543）: 每次状态变更生成新 uid 命令令牌，执行端按 uid 幂等去重。
  * PVE::JSONSchema / RESTHandler（pve-common）: 所有入口都有声明式 schema。
  * Kubernetes 声明式 API（Harvester / KubeVirt / Talos 路线）: 用 etcd 作唯一真值源，
    resourceVersion/Create 语义替代自研 flock 队列与 RPC。

本模块不依赖任何第三方库，可直接在运维机 Python 3.9+ 运行。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = "ar8/v1"


class ConfigError(Exception):
    pass


class StateError(Exception):
    pass


class InvariantViolation(Exception):
    pass


# ---------------------------------------------------------------------------
# 1. 声明式 schema：所有配置字段都有类型 / 必填 / 取值域，入口一律先校验
# ---------------------------------------------------------------------------

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _ip_like(v: str) -> bool:
    parts = v.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)



FIELD_SPEC = {
    "schemaVersion": (_is_str, "string", lambda v: v == SCHEMA_VERSION),
    "generation": (_is_int, "int >= 1", lambda v: v >= 1),
    "cluster.name": (_is_str, "string", lambda v: bool(re.match(r"^[a-z0-9-]+$", v))),
    "cluster.namespace": (_is_str, "string", lambda v: bool(re.match(r"^[a-z0-9-]+$", v))),
    "cluster.pilot": (_is_str, "IPv4", _ip_like),
    "cluster.jump": (_is_str, "user@host", lambda v: "@" in v),
    "cluster.ecsPublicIp": (_is_str, "IPv4", _ip_like),
    # 单控制面 + 至少一个 worker 即可成集群；3 节点是推荐值不是硬约束
    # （2026-11-18 重建后实际为 node1 控制面 + node2 worker）
    "nodes": (list, "list of node objects", lambda v: len(v) >= 2),
    "ports": (dict, "map", lambda v: len(set(v.values())) == len(v)),
    "features.job.enabled": (_is_bool, "bool", lambda v: True),
    "features.job.chunk": (_is_int, "int >= 100", lambda v: v >= 100),
    "features.job.backoffLimit": (_is_int, "int >= 0", lambda v: v >= 0),
    "features.job.activeDeadlineSeconds": (_is_int, "int >= 10", lambda v: v >= 10),
    "storage": (list, "list of storage classes", lambda v: len(v) >= 1),
    "stateMachines.shard.transitions": (dict, "map", lambda v: True),
    "invariants": (list, "list", lambda v: len(v) >= 1),
}




def _dig(cfg: Dict[str, Any], path: str) -> Tuple[bool, Any]:
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """返回问题列表；空列表表示配置合法。绝不静默接受未知字段的错值。"""
    issues: List[str] = []
    for path, (pred, desc, constraint) in FIELD_SPEC.items():
        present, value = _dig(cfg, path)
        if not present:
            issues.append(f"missing field: {path}")
            continue
        if not pred(value):
            issues.append(f"bad type: {path} expected {desc}, got {type(value).__name__}")
            continue
        try:
            if not constraint(value):
                issues.append(f"constraint failed: {path} ({desc})")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"constraint error: {path}: {exc}")

    # 跨字段不变量：真值源内部的自洽性
    nodes = cfg.get("nodes", [])
    ids = [n.get("id") for n in nodes if isinstance(n, dict)]
    if len(ids) != len(set(ids)):
        issues.append("duplicate node id")
    ips = [n.get("ip") for n in nodes if isinstance(n, dict)]
    if len(ips) != len(set(ips)):
        issues.append("duplicate node ip")
    roles = [n.get("role") for n in nodes if isinstance(n, dict)]
    if roles.count("control-plane") != 1:
        issues.append("exactly one control-plane node required")
    for n in nodes:
        if not isinstance(n, dict):
            continue
        for key in ("id", "ip", "role", "class", "memoryMib", "wan"):
            if key not in n:
                issues.append(f"node {n.get('id','?')} missing {key}")
        if "memoryMib" in n and (not _is_int(n["memoryMib"]) or n["memoryMib"] <= 0):
            issues.append(f"node {n.get('id','?')} bad memoryMib")
        if "wan" in n and not _is_bool(n["wan"]):
            issues.append(f"node {n.get('id','?')} bad wan flag")
    for s in cfg.get("storage", []):
        if not isinstance(s, dict):
            issues.append("storage entry is not a map")
            continue
        if s.get("kind") == "object" and s.get("mountableAsBlock", True) is not False:
            issues.append(f"object storage {s.get('name')} must declare mountableAsBlock=false")
        if s.get("clusterWideMount") != "forbidden":
            issues.append(f"storage {s.get('name')} must forbid cluster-wide mount")
    sm = cfg.get("stateMachines", {}).get("shard", {})
    if sm:
        states = set(sm.get("states", []))
        for src, dsts in sm.get("transitions", {}).items():
            if src not in states:
                issues.append(f"FSM transition from unknown state {src}")
            for d in dsts:
                if d not in states:
                    issues.append(f"FSM transition to unknown state {d}")
        if sm.get("initial") not in states:
            issues.append("FSM initial state not in states")
    return issues


def assert_config(cfg: Dict[str, Any]) -> None:
    assert_no_unknown_fields(cfg)   # 未知字段是拼写错误的第一信号，必须先拦
    issues = validate_config(cfg)
    if issues:
        raise ConfigError("config invalid:\n  - " + "\n  - ".join(issues))


# ---------------------------------------------------------------------------
# 1b. 结构化输入防线
#
# 源码来源：pve-cluster/src/pmxcfs/dcdb.c:620-641。PVE 在同步前对每个节点送来的
# memdb index 做「bytes == sizeof(header) + size * sizeof(entry)」且 size != 0 的校验，
# 注释直言：若允许 size==0 的节点参与，会让所有 follower 丢掉全部 inode。
# 这是典型的「格式合法但语义为空 -> 全集群数据丢失」灾难。
# AR8 的清单 / 状态同样要有这一层，不能只靠 schema 字段类型。
# ---------------------------------------------------------------------------

MAX_MANIFEST_BYTES = 4 * 1024 * 1024   # 对齐 pmxcfs MEMDB_MAX_FILE_SIZE 的量级
MAX_TASK_LOG_BYTES = 8 * 1024 * 1024   # append-only 日志硬上限，防止无限增长


# 配置白名单：对齐 PVE RESTHandler.pm:253-343 的注册期严格校验 + JSON Schema
# 默认拒绝未知属性。拼错字段名（如 chunkSize）必须报错，而不是静默走默认值。
FIELD_WHITELIST = {
    "cluster": {"name", "namespace", "pilot", "jump", "sshUser", "kubectl",
                "ecsPublicIp", "ecsTunnelIp", "wanBwCeilingKbit"},
    "features.admission": {"enabled", "forbidClusterWideMount",
                           "forbidObjectAsBlockVolume", "allowObjectViaGatewayReadOnly"},
    "features.job": {"enabled", "image", "imagePullPolicy", "chunk", "defaultN",
                     "ttlSeconds", "activeDeadlineSeconds", "backoffLimit",
                     "ttlSecondsAfterFinished", "spreadPolicy"},
    "features.s3": {"enabled", "endpoint", "buckets"},
    "features.bandwidth": {"enabled", "tiers", "defaultTier",
                           "nodeCeilingKbit", "allowStandardCompression"},
    "stateMachines.shard": {"initial", "states", "transitions"},
    "stateMachines.node": {"initial", "states", "transitions"},
}
NODE_FIELDS = {"id", "ip", "role", "class", "memoryMib", "wan", "k8sName"}
STORAGE_FIELDS = {"name", "tier", "kind", "provisioner", "clusterWideMount",
                  "mountableAsBlock"}


def assert_no_unknown_fields(cfg: Dict[str, Any]) -> None:
    """未知字段一律拒绝，防止拼写错误被静默忽略。"""
    bad: List[str] = []
    for path, allowed in FIELD_WHITELIST.items():
        found, value = _dig(cfg, path)
        if found and isinstance(value, dict):
            for k in sorted(set(value) - allowed):
                bad.append(f"unknown field: {path}.{k}")
    for n in cfg.get("nodes", []):
        if isinstance(n, dict):
            for k in sorted(set(n) - NODE_FIELDS):
                bad.append(f"unknown node field: {n.get('id', '?')}.{k}")
    for s in cfg.get("storage", []):
        if isinstance(s, dict):
            for k in sorted(set(s) - STORAGE_FIELDS):
                bad.append(f"unknown storage field: {s.get('name', '?')}.{k}")
    if bad:
        raise ConfigError("config has unknown fields:\n  - " + "\n  - ".join(bad))


def _is_loopback_ip(v: str) -> bool:
    return v == "::1" or v.startswith("127.")


def assert_no_loopback(cfg: Dict[str, Any]) -> None:
    """节点地址不得用 loopback。

    源码来源：pmxcfs.c:707-740 lookup_node_ip() 明确跳过 127.* 与 ::1；
    PVE 认为节点自解析成 loopback 是致命配置错误，直接 critical 退出。
    """
    for key in ("pilot", "ecsTunnelIp"):
        found, ip = _dig(cfg, f"cluster.{key}")
        if found and _is_loopback_ip(str(ip)):
            raise ConfigError(f"cluster.{key} must not be loopback: {ip}")
    for n in cfg.get("nodes", []):
        if isinstance(n, dict) and _is_loopback_ip(str(n.get("ip", ""))):
            raise ConfigError(f"node {n.get('id')} must not use loopback ip")


def validate_structure(docs: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[str]:
    """对即将提交的清单做结构化一致性校验；任何一条不通过都必须拒绝 apply。"""
    issues: List[str] = []
    if not docs:
        return ["manifest is empty (a zero-object manifest would wipe desired state)"]
    if len(docs) < len(cfg.get("nodes", [])):
        issues.append(f"manifest under-populated: objects={len(docs)} nodes={len(cfg.get('nodes', []))}")
    for i, d in enumerate(docs):
        if not isinstance(d, dict):
            issues.append(f"doc[{i}] is not a map")
            continue
        if not d.get("apiVersion") or not d.get("kind"):
            issues.append(f"doc[{i}] missing apiVersion/kind")
        meta = d.get("metadata")
        if not isinstance(meta, dict) or not meta.get("name"):
            issues.append(f"doc[{i}] missing metadata.name")
        try:
            raw = json.dumps(d, ensure_ascii=False, sort_keys=True)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"doc[{i}] not JSON serializable: {exc}")
            continue
        if len(raw.encode("utf-8")) > MAX_MANIFEST_BYTES:
            issues.append(f"doc[{i}] exceeds MAX_MANIFEST_BYTES")
        if d.get("kind") == "ConfigMap":
            gen = (d.get("data") or {}).get("generation")
            if gen is not None and str(gen) != str(cfg.get("generation")):
                issues.append(
                    f"stale generation in ConfigMap {meta.get('name')}: "
                    f"{gen} != {cfg.get('generation')}"
                )
    return issues


def assert_structure(docs: List[Dict[str, Any]], cfg: Dict[str, Any]) -> None:
    issues = validate_structure(docs, cfg)
    if issues:
        raise ConfigError("unsafe manifest:\n  - " + "\n  - ".join(issues))


def select_leader(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """确定性选主：(version, mtime) 最大者胜。

    源码来源：dcdb.c:469-496 dcdb_select_leader()。PVE 不用节点 ID 优先级，
    而是「数据最新者胜」，避免选到落后节点后把全集群拖回旧状态。
    """
    if not candidates:
        return None
    best = candidates[0]
    for c in candidates[1:]:
        cv, bv = c.get("version", 0), best.get("version", 0)
        ct, bt = c.get("mtime", 0), best.get("mtime", 0)
        if cv > bv or (cv == bv and ct > bt):
            best = c
    return best


# ---------------------------------------------------------------------------
# 2. 显式有限状态机：非法转换立即抛错，绝不静默进入未定义状态
# ---------------------------------------------------------------------------

class StateMachine:
    def __init__(self, name: str, definition: Dict[str, Any], log_sink: Optional[list] = None):
        self.name = name
        self.states = list(definition["states"])
        self.initial = definition["initial"]
        self.transitions = {k: list(v) for k, v in definition["transitions"].items()}
        self.log_sink = log_sink

    def legal(self, src: str, dst: str) -> bool:
        return src in self.transitions and dst in self.transitions[src]

    def move(self, obj: Dict[str, Any], dst: str, tick: int = 0, why: str = "") -> bool:
        src = obj["state"]
        if src == dst:
            return False
        if not self.legal(src, dst):
            raise StateError(f"{self.name}: illegal {src} -> {dst} ({why})")
        obj["state"] = dst
        obj["ticks_in_state"] = 0
        if self.log_sink is not None:
            self.log_sink.append(f"{tick:04d} {self.name} {obj.get('id','?')} {src} => {dst} {why}")
        return True


# ---------------------------------------------------------------------------
# 3. uid 命令令牌 + fencing token：幂等去重 / 陈旧代次拒绝
# ---------------------------------------------------------------------------

def command_uid(run: str, generation: int, shard: int, seq: int) -> str:
    """确定性 uid —— 与 PVE compute_new_uuid 作用相同，但去掉 time/pid，使仿真可重放。"""
    raw = f"{run}:{generation}:{shard}:{seq}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def fencing_token(run: str, generation: int, node: str) -> str:
    return hashlib.sha256(f"{run}:{generation}:{node}".encode()).hexdigest()[:12]


class Fence:
    """验证命令是否来自当前代次；旧代次/错误 run 一律拒绝。"""

    def __init__(self, run: str, generation: int):
        self.run = run
        self.generation = generation
        self.seen: set = set()
        self.rejected: List[str] = []

    def accept(self, cmd: Dict[str, Any]) -> Tuple[bool, str]:
        if cmd.get("run") != self.run:
            self.rejected.append("wrong-run")
            return False, "wrong-run"
        if cmd.get("generation") != self.generation:
            self.rejected.append("stale-generation")
            return False, "stale-generation"
        uid = cmd.get("uid")
        if not uid:
            self.rejected.append("missing-uid")
            return False, "missing-uid"
        if uid in self.seen:
            self.rejected.append("duplicate")
            return False, "duplicate"
        self.seen.add(uid)
        return True, "ok"


# ---------------------------------------------------------------------------
# 4. 独立参考实现（第三重校验）：分段埃氏筛 + 反转因子枚举，与 awk 试除完全异构
# ---------------------------------------------------------------------------

def reference_shard(lo: int, hi: int) -> Dict[str, int]:
    """返回 [lo,hi] 上 5 个确定性指标，算法与集群内 awk 逐数试除无关。"""
    if hi < 2:
        return dict(pc=0, ps=0, pf=0, pl=0, dc=0)
    lo = max(lo, 2)
    if lo > hi:
        return dict(pc=0, ps=0, pf=0, pl=0, dc=0)
    # 分段筛：只筛 [lo,hi]
    size = hi - lo + 1
    composite = bytearray(size)
    limit = int(math.isqrt(hi))
    for p in range(2, limit + 1):
        start = max(p * p, ((lo + p - 1) // p) * p)
        for m in range(start, hi + 1, p):
            composite[m - lo] = 1
    pc = ps = pf = pl = 0
    for i, c in enumerate(composite):
        if c == 0:
            x = lo + i
            pc += 1
            ps += x
            if pf == 0:
                pf = x
            pl = x
    # 反转因子枚举：sum_{x=lo..hi} d(x) = sum_{d=1..hi} (floor(hi/d)-floor((lo-1)/d))
    dc = sum(hi // d - (lo - 1) // d for d in range(1, hi + 1))
    return dict(pc=pc, ps=ps, pf=pf, pl=pl, dc=dc)


def make_shards(n: int, chunk: int) -> List[Dict[str, int]]:
    out = []
    lo = 2
    i = 0
    while lo <= n:
        hi = min(lo + chunk - 1, n)
        out.append(dict(id=i, lo=lo, hi=hi))
        lo = hi + 1
        i += 1
    return out


# ---------------------------------------------------------------------------
# 1c. 智能分片路由（把「多个节点虚拟成一台大电脑」落到调度决策上）
#
# 设计来源（只借设计，不搬实现）：
#   - XNU 的 QoS 分级：不同任务给不同权重，而不是一律平等排队；
#   - GCD / Linux CFS 的 vruntime 公平性：每个节点维护「虚拟运行时间」，
#     永远把下一个分片交给 vruntime 最小的节点，天然自平衡，无需全局锁；
#   - OpenHarmony 分布式任务调度：按设备能力（算力/内存/位置/负载）选最合适的
#     执行者，把一个任务拆到多设备上跑 —— 正是「一拆多算」的同一思路。
#
# 关键性质：全部为纯函数式打分，无随机、无隐藏状态，相同输入必得相同分配，
# 因此仿真可与真实集群共用同一份路由决策，且回放可复现。
# ---------------------------------------------------------------------------

# 节点档位权重：hot=高性能，warm=一般，cold=低功耗/边缘
NODE_CLASS_WEIGHT = {"hot": 1.0, "warm": 0.6, "cold": 0.35}


def estimate_shard_cost(lo: int, hi: int) -> float:
    """分片计算代价模型，与真实分片算法同构。

    真实 awk 分片对每个 x 试除到 sqrt(x)，代价 ≈ Σ_{x=lo..hi} sqrt(x)，
    闭式积分近似 (2/3)(hi^1.5 - (lo-1)^1.5)；
    因子枚举部分 d=1..hi 做 (hi//d - (lo-1)//d)，量级 O(hi)，给较小权重。
    这个模型让「大分片」被识别为大分片，而不是像轮询那样一视同仁。
    """
    if hi < lo:
        return 0.0
    lo = max(lo, 2)
    if lo > hi:
        return 0.0
    trial = (2.0 / 3.0) * (float(hi) ** 1.5 - float(lo - 1) ** 1.5)
    divisor = 0.15 * float(hi)
    return trial + divisor


def node_capacity(node: Dict[str, Any]) -> float:
    """节点算力权重：档位 × 内存规模（开方压缩差距，避免大内存节点独占）。"""
    cls = str(node.get("class", "warm"))
    base = NODE_CLASS_WEIGHT.get(cls, 0.5)
    mem = float(node.get("memoryMib", 2048) or 2048)
    return base * (mem / 2048.0) ** 0.5


class Router:
    """容量加权公平路由：vruntime 最小者承接下一个分片。

    与轮询的关键差别：
      1. 按节点算力加权 —— hot 节点承接更多分片，不会让弱节点拖慢整体 makespan；
      2. 按分片代价加权 —— 大分片推高该节点 vruntime，后续小分片自动流向空闲节点；
      3. 负载感知 —— 真实环境中已接近资源上限的节点被降权（避免把分片塞给快撑爆的机器）；
      4. 完全确定性 —— 平局按配置中节点顺序打破，仿真可逐字节回放。
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.nodes = {n["id"]: n for n in cfg.get("nodes", [])}
        self.order = [n["id"] for n in cfg.get("nodes", [])]
        self.capacity = {nid: node_capacity(n) for nid, n in self.nodes.items()}
        self.vruntime: Dict[str, float] = {nid: 0.0 for nid in self.order}
        self.assigned: Dict[str, int] = {nid: 0 for nid in self.order}
        # 负载降权因子（1.0 = 不降权）。XNU 的 QoS 本质是「权重」而不是「加一笔债务」：
        # 直接给 vruntime 加常数会被节点容量差异淹没，必须折算成有效容量。
        self.load_factor: Dict[str, float] = {nid: 1.0 for nid in self.order}

    def observe_load(self, metrics: Dict[str, Dict[str, float]]) -> None:
        """把节点实时资源占用折算成有效容量折扣（真实环境专用，仿真传空）。"""
        for nid, m in metrics.items():
            if nid not in self.load_factor:
                continue
            mem_ratio = float(m.get("memRatio", 0.0) or 0.0)
            cpu_ratio = float(m.get("cpuRatio", 0.0) or 0.0)
            pressure = max(mem_ratio, cpu_ratio)
            # 80% 以下不降权；80%→100% 之间线性压到 0.05（几乎不再接活）
            if pressure <= 0.80:
                self.load_factor[nid] = 1.0
            else:
                over = min(1.0, (pressure - 0.80) / 0.20)
                self.load_factor[nid] = max(0.05, 1.0 - over)

    def effective_capacity(self, nid: str) -> float:
        return max(self.capacity.get(nid, 0.5) * self.load_factor.get(nid, 1.0), 1e-9)

    def pick(self, available: Iterable[str], cost: float,
             sid: Optional[str] = None, tier: Optional[str] = None,
             home: Optional[str] = None) -> str:
        """返回承接该分片的节点；同时推进其 vruntime。

        sid/tier/home 是给可插拔调度 profile 用的可选提示（见 ar8-profiles.py）：
        nixos-baseline 忽略它们；xnu-qos 用 tier 做 QoS 提升；
        oh-distributed 用 tier/home 做能力协商与数据就近。
        home 是分片数据所在的节点（OpenHarmony 分布式数据管理的「数据本地性」）：
        把任务派到数据所在节点可省去跨节点数据搬运。默认参数保证旧调用方零改动。
        """
        cands = [nid for nid in self.order if nid in available and nid in self.capacity]
        if not cands:
            cands = [nid for nid in self.order if nid in self.capacity]
        if not cands:
            return ""
        best = min(cands, key=lambda nid: (self.vruntime[nid] / self.effective_capacity(nid),
                                           self.order.index(nid)))
        self.vruntime[best] += cost / self.effective_capacity(best)
        self.assigned[best] += 1
        return best

    def release(self, node: str, cost: float, sid: Optional[str] = None) -> None:
        """分片完成/失败后回吐负载，保持 vruntime 不单调累积。

        sid 给可插拔 Router 精确定位「哪一档账本」要回吐；基类忽略它。
        """
        if node in self.vruntime:
            self.vruntime[node] = max(
                0.0, self.vruntime[node] - cost / self.effective_capacity(node))

    def snapshot(self) -> Dict[str, Any]:
        return {"vruntime": {k: round(v, 6) for k, v in self.vruntime.items()},
                "loadFactor": {k: round(v, 3) for k, v in self.load_factor.items()},
                "assigned": dict(self.assigned)}


def aggregate(results: Iterable[Dict[str, int]]) -> Dict[str, int]:
    keys = ("pc", "ps", "pf", "pl", "dc")
    agg = {k: 0 for k in keys}
    first_prime = None
    last_prime = 0
    for r in results:
        for k in keys:
            if k in ("pf", "pl"):
                continue
            agg[k] += r[k]
        if r["pf"]:
            if first_prime is None or r["pf"] < first_prime:
                first_prime = r["pf"]
        last_prime = max(last_prime, r["pl"])
    agg["pf"] = first_prime or 0
    agg["pl"] = last_prime
    return agg


# ---------------------------------------------------------------------------
# 5. Env 抽象层：生产 / 仿真共用同一份 reconcile 逻辑
# ---------------------------------------------------------------------------

class Env(ABC):
    @abstractmethod
    def now(self) -> int:
        ...

    @abstractmethod
    def quorate(self) -> bool:
        ...

    @abstractmethod
    def node_info(self) -> Dict[str, bool]:
        """node id -> online?"""

    @abstractmethod
    def create_job(self, job: Dict[str, Any]) -> bool:
        """返回 False 表示同名已存在（幂等，不重复创建）。"""

    @abstractmethod
    def job_results(self, run: str) -> Dict[int, Dict[str, Any]]:
        """shard id -> {uid,node,metrics,exitCode,generation,run}"""

    @abstractmethod
    def delete_job(self, job_name: str) -> None:
        ...

    @abstractmethod
    def log(self, msg: str) -> None:
        ...


class SimEnv(Env):
    """确定性仿真环境：虚拟时钟、禁随机、可注入 power/network/控制面/重放类故障。"""

    def __init__(self, cfg: Dict[str, Any], work_ticks: int = 4):
        self.cfg = cfg
        self.t = 0
        self.work_ticks = work_ticks
        self.nodes = {n["id"]: True for n in cfg["nodes"]}
        self.quorum_ok = True
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.results: Dict[int, Dict[str, Any]] = {}
        self.events: List[str] = []
        self.control_skips = 0
        self.inject_duplicate = False
        self.inject_stale = False
        self.max_ticks = 400

    def now(self) -> int:
        return self.t

    def quorate(self) -> bool:
        online = sum(1 for v in self.nodes.values() if v)
        return self.quorum_ok and online > len(self.nodes) // 2

    def node_info(self) -> Dict[str, bool]:
        return dict(self.nodes)

    def create_job(self, job: Dict[str, Any]) -> bool:
        name = job["name"]
        if name in self.jobs:
            return False
        self.jobs[name] = dict(job, started=self.t)
        self.events.append(f"{self.t:04d} CREATE {name} node={job['node']} uid={job['uid']}")
        return True

    def job_results(self, run: str) -> Dict[int, Dict[str, Any]]:
        return dict(self.results)

    def delete_job(self, job_name: str) -> None:
        self.jobs.pop(job_name, None)
        # 重试前必须清掉该分片上的旧结果，否则新 uid 永远等不到匹配的结果
        try:
            sid = int(job_name.rsplit("-", 1)[1])
            self.results.pop(sid, None)
            self.results.pop(-sid - 1000000, None)
        except (ValueError, IndexError):
            pass

    def log(self, msg: str) -> None:
        self.events.append(f"{self.t:04d} {msg}")

    def apply_command(self, cmd: str) -> str:
        parts = cmd.split()
        if not parts:
            return "empty"
        op = parts[0]
        if op == "network" and len(parts) == 3:
            node, onoff = parts[1], parts[2] == "on"
            if node in self.nodes:
                self.nodes[node] = onoff
                self.events.append(f"{self.t:04d} FAULT network {node} {'on' if onoff else 'off'}")
                return "ok"
        elif op == "power" and len(parts) == 3:
            node, onoff = parts[1], parts[2] == "on"
            if node in self.nodes:
                self.nodes[node] = onoff
                self.events.append(f"{self.t:04d} FAULT power {node} {'on' if onoff else 'off'}")
                return "ok"
        elif op == "delay" and len(parts) == 2:
            self.t += int(parts[1])
            return "ok"
        elif op == "cfs" and "fail" in parts:
            self.quorum_ok = False
            self.events.append(f"{self.t:04d} FAULT cfs unavailable")
            return "ok"
        elif op == "cfs" and "work" in parts:
            self.quorum_ok = True
            self.events.append(f"{self.t:04d} HEAL cfs available")
            return "ok"
        elif op == "skip-round" and len(parts) >= 2:
            self.control_skips += 1
            self.events.append(f"{self.t:04d} FAULT skip-round {parts[1]}")
            return "ok"
        elif op == "duplicate":
            self.inject_duplicate = True
            self.events.append(f"{self.t:04d} FAULT duplicate submit")
            return "ok"
        elif op == "stale":
            self.inject_stale = True
            self.events.append(f"{self.t:04d} FAULT stale generation submit")
            return "ok"
        elif op == "kill-broker":
            self.events.append(f"{self.t:04d} FAULT control-plane restart")
            return "ok"
        elif op == "slow" and len(parts) == 2:
            self.work_ticks = int(parts[1])
            self.events.append(f"{self.t:04d} FAULT bandwidth/stall work_ticks={self.work_ticks}")
            return "ok"
        elif op == "normal":
            self.work_ticks = 4
            self.events.append(f"{self.t:04d} HEAL nominal work_ticks=4")
            return "ok"
        return "unknown-command:" + cmd

    def tick(self, run: str, generation: int, shards: List[Dict[str, Any]]) -> None:
        """推进虚拟时间，模拟 job 完成；失联节点上的 job 判失败。"""
        self.t += 1
        for name, job in list(self.jobs.items()):
            sid = job["shard"]
            if sid in self.results:
                continue
            node = job["node"]
            if not self.nodes.get(node, False) or not self.quorate():
                if self.t - job["started"] > 2:
                    self.events.append(f"{self.t:04d} LOST {name} node={node} (offline)")
                    del self.jobs[name]
                continue
            if self.t - job["started"] >= self.work_ticks:
                lo, hi = job["lo"], job["hi"]
                m = reference_shard(lo, hi)
                gen = generation - 1 if self.inject_stale else generation
                if self.inject_stale:
                    self.inject_stale = False
                self.results[sid] = dict(
                    uid=job["uid"], node=node, metrics=m, exitCode=0,
                    generation=gen, run=run, shard=sid,
                )
                if self.inject_duplicate:
                    # 同一个 uid 再投递一次，验证幂等去重
                    self.results[-sid - 1000000] = dict(self.results[sid])
                self.events.append(f"{self.t:04d} DONE {name} node={node} uid={job['uid']}")


# ---------------------------------------------------------------------------
# 6. 可重入 reconcile 循环 —— 同一份代码跑仿真与真实集群
# ---------------------------------------------------------------------------

class Ledger:
    """持久化共享状态（对齐 PVE manager_status）；崩溃后从快照重建即可继续收敛。"""

    def __init__(self, run: str, generation: int, shards: List[Dict[str, int]]):
        self.run = run
        self.generation = generation
        self.shards = {
            s["id"]: dict(id=s["id"], lo=s["lo"], hi=s["hi"], state="queued",
                          uid=None, node=None, result=None, attempts=0,
                          ticks_in_state=0, cost=0.0,
                          tier=s.get("tier"), home=s.get("home"))
            for s in shards
        }
        self.accepted: Dict[int, Dict[str, Any]] = {}
        self.events: List[str] = []
        self.seq = 0
        self.fence = Fence(run, generation)
        self.transitions = 0

    def snapshot(self) -> Dict[str, Any]:
        return dict(run=self.run, generation=self.generation,
                    shards=self.shards, accepted=self.accepted,
                    events=self.events, seq=self.seq)

    @classmethod
    def restore(cls, snap: Dict[str, Any], shards: List[Dict[str, int]]) -> "Ledger":
        led = cls(snap["run"], snap["generation"], shards)
        led.shards = snap["shards"]
        led.accepted = snap["accepted"]
        led.events = snap["events"]
        led.seq = snap["seq"]
        led.fence = Fence(snap["run"], snap["generation"])
        for uid in led.accepted.values():
            led.fence.seen.add(uid["uid"])
        return led

    def next_uid(self, shard: int) -> str:
        self.seq += 1
        return command_uid(self.run, self.generation, shard, self.seq)


def reconcile(env: Env, cfg: Dict[str, Any], ledger: Ledger,
              sm: StateMachine, max_ticks: Optional[int] = None) -> Dict[str, Any]:
    """轮次驱动收敛：每轮跑到无状态变化为止。仿真与真实集群共用。

    关键：所有决策只依据 (配置, ledger 持久化状态, env 观测)，无隐藏内存状态；
    任意一轮被中断后，下一轮从 ledger 快照重放即可继续 —— 对齐 PVE Manager::new()。
    """
    jobcfg = cfg["features"]["job"]
    limits = max_ticks or 400
    nodes = [n["id"] for n in cfg["nodes"]]
    # 智能路由：容量加权公平分配（取代原来的 available[node_idx % len(available)] 轮询）。
    # 轮询的问题是把 hot 与 warm 节点当等价机器用，弱节点拖慢整体 makespan。
    router = getattr(env, "router", None) or Router(cfg)
    ticks = 0
    converged = False

    for ticks in range(1, limits + 1):
        changed = False
        if isinstance(env, SimEnv) and env.control_skips > 0:
            env.control_skips -= 1
            env.log("SKIP reconcile round (control-plane fault)")
            env.tick(ledger.run, ledger.generation, list(ledger.shards.values()))
            continue

        if not env.quorate():
            env.log("NO-QUORUM: skip round, refuse to change desired state")
            if isinstance(env, SimEnv):
                env.tick(ledger.run, ledger.generation, list(ledger.shards.values()))
            continue

        online = env.node_info()
        available = [n for n in nodes if online.get(n, False)] or nodes
        results = env.job_results(ledger.run)

        for sid in sorted(ledger.shards):
            sh = ledger.shards[sid]
            sh["ticks_in_state"] += 1
            st = sh["state"]

            if st == "queued":
                cost = estimate_shard_cost(sh["lo"], sh["hi"])
                # 把分片 id 与 QoS 档位透传给 profile 路由器；基线实现忽略它们。
                # sh['tier'] 可由作业声明，缺省由 profile 的 infer_tier(cost) 推导。
                node = router.pick(available, cost, sid=sid, tier=sh.get("tier"),
                           home=sh.get("home"))
                if not node:
                    continue
                uid = ledger.next_uid(sid)
                job = dict(name=f"ar8-shard-{ledger.run}-{sid}", shard=sid,
                           lo=sh["lo"], hi=sh["hi"], node=node, uid=uid,
                           run=ledger.run, generation=ledger.generation,
                           cost=cost)
                if env.create_job(job):
                    sh["uid"], sh["node"] = uid, node
                    sh["cost"] = cost
                    sh["attempts"] += 1
                    changed |= sm.move(sh, "leased", ticks, f"uid={uid} node={node}")

            elif st == "leased":
                candidates = sorted(
                    [r for r in results.values()
                     if r.get("uid") == sh["uid"] and r.get("shard") in (sid, None)],
                    # 对抗性结果（旧代次）排在前面，确保 fencing 防御真的被执行到
                    key=lambda r: (r.get("generation", 0), str(r.get("node", ""))))
                if len(candidates) > 1:
                    ledger.events.append(f"{ticks:04d} DUP-SUPPRESSED shard={sid} uid={sh['uid']} copies={len(candidates)}")
                r = candidates[0] if candidates else None
                if r:
                    sh["result"] = r
                    changed |= sm.move(sh, "submitted", ticks, f"uid={sh['uid']}")
                elif sh["ticks_in_state"] > jobcfg["ttlSeconds"]:
                    # worker 级 deadline：超时即作废该 uid 并重新派单。
                    # 反面教材：PVE::ProcFSTools::upid_wait() 无 deadline，worker 卡死时
                    # HA Manager 静默停在 started（Manager.pm:1124/1611），既不自愈也不告警。
                    env.delete_job(f"ar8-shard-{ledger.run}-{sid}")
                    # 回吐负载：分片作废后该节点的 vruntime 要减掉，
                    # 否则重派时会把活偏向别的节点，违背公平性（GCD 的归还语义）。
                    router.release(sh.get("node") or "", sh.get("cost") or 0.0, sid=sid)
                    ledger.events.append(
                        f"{ticks:04d} ALERT worker-deadline shard={sid} uid={sh['uid']} "
                        f"waited={sh['ticks_in_state']} action=invalidate-and-redispatch")
                    changed |= sm.move(sh, "expired", ticks, "lease timeout")

            elif st == "expired":
                # 源码级修正（Manager.pm:1609-1611）：换 uid 前必须再确认一次旧结果是否已到。
                # 原话是 "ensure service gets started again ... but ensure also no LRM result
                # gets lost"，做法是仅在 defined($lrm_res) 时才 compute_new_uuid()。
                # 对应到 AR8：超时那一 tick 结果可能还没collected，若直接清 uid requeue，
                # 随后到达的旧结果将永远匹配不上 —— 这正是要防的"结果丢失"。
                results_now = env.job_results(ledger.run)
                late = [r for r in results_now.values()
                        if r.get("uid") == sh["uid"] and r.get("shard") in (sid, None)]
                if late:
                    sh["result"] = late[0]
                    ledger.events.append(
                        f"{ticks:04d} LATE-RESULT-RECOVERED shard={sid} uid={sh['uid']} "
                        f"action=consume-before-uid-reassign")
                    changed |= sm.move(sh, "submitted", ticks, "late result recovered")
                else:
                    sh["result"] = None
                    sh["uid"] = None
                    changed |= sm.move(sh, "queued", ticks, "requeue")

            elif st == "submitted":
                r = sh["result"] or {}
                cmd = dict(run=r.get("run"), generation=r.get("generation"), uid=r.get("uid"))
                ok, reason = ledger.fence.accept(cmd)
                metrics_ok = bool(r.get("metrics")) and r.get("exitCode") == 0
                ref = reference_shard(sh["lo"], sh["hi"])
                values_ok = metrics_ok and all(r["metrics"].get(k) == v for k, v in ref.items())
                if ok and values_ok:
                    ledger.accepted[sid] = dict(uid=r["uid"], node=r.get("node"),
                                                metrics=r["metrics"], lo=sh["lo"], hi=sh["hi"])
                    changed |= sm.move(sh, "accepted", ticks, "verified against reference")
                else:
                    ledger.events.append(f"{ticks:04d} REJECT shard={sid} reason={reason} values_ok={values_ok}")
                    env.delete_job(f"ar8-shard-{ledger.run}-{sid}")
                    router.release(sh.get("node") or "", sh.get("cost") or 0.0, sid=sid)
                    changed |= sm.move(sh, "rejected", ticks, reason or "bad-values")

            elif st == "rejected":
                sh["result"] = None
                sh["uid"] = None
                changed |= sm.move(sh, "queued", ticks, "retry with new uid")

            elif st == "accepted":
                changed |= sm.move(sh, "harvested", ticks, "result persisted")

        ledger.transitions += int(changed)
        if isinstance(env, SimEnv):
            env.tick(ledger.run, ledger.generation, list(ledger.shards.values()))

        # 给真实环境一个节流/故障注入钩子；仿真环境无此方法，行为不变
        if hasattr(env, "settle"):
            env.settle(ticks)

        if all(s["state"] == "harvested" for s in ledger.shards.values()):
            converged = True
            break

    return dict(ticks=ticks, converged=converged,
                harvested=sum(1 for s in ledger.shards.values() if s["state"] == "harvested"),
                total=len(ledger.shards), transitions=ledger.transitions,
                routing=router.snapshot())


def check_invariants(cfg: Dict[str, Any], ledger: Ledger,
                     summary: Dict[str, Any]) -> List[str]:
    """每次 reconcile 后必须成立；任何 violation 直接判失败，不靠人工读日志。"""
    bad: List[str] = []
    want = cfg.get("invariants", [])
    if "noShardLost" in want and summary["harvested"] != summary["total"]:
        bad.append(f"noShardLost: {summary['harvested']}/{summary['total']} harvested")
    if "noDuplicateAcceptedResult" in want:
        uids = [v["uid"] for v in ledger.accepted.values()]
        if len(uids) != len(set(uids)):
            bad.append("noDuplicateAcceptedResult: duplicate uid accepted")
    if "noStaleGenerationAccepted" in want:
        for sid, v in ledger.accepted.items():
            if v.get("generation", ledger.generation) != ledger.generation:
                bad.append(f"noStaleGenerationAccepted: shard {sid}")
    if "allAcceptedSumMatchesReference" in want:
        got = aggregate(v["metrics"] for v in ledger.accepted.values())
        ref = aggregate(reference_shard(s["lo"], s["hi"]) for s in ledger.shards.values())
        if got != ref:
            bad.append(f"allAcceptedSumMatchesReference: got={got} ref={ref}")
    if "noSilentStall" in want:
        stalled = [sid for sid, sh in ledger.shards.items()
                   if sh["state"] == "leased" and sh["ticks_in_state"] > 18]
        if stalled:
            bad.append(f"noSilentStall: shards {stalled} stuck in leased without deadline")
    if "nodesConvergeWithinTicks" in want and not summary["converged"]:
        bad.append(f"nodesConvergeWithinTicks: not converged in {summary['ticks']} ticks")
    if ledger.fence.rejected and "stale-generation" in ledger.fence.rejected:
        # 拒绝陈旧代次是预期防御，不是 violation
        pass
    return bad


def simulate(cfg: Dict[str, Any], scenario: Dict[str, Any]) -> Dict[str, Any]:
    """确定性仿真入口：场景即数据，返回可比对的结构化结果。"""
    assert_config(cfg)
    n = scenario.get("n", cfg["features"]["job"]["defaultN"])
    chunk = scenario.get("chunk", cfg["features"]["job"]["chunk"])
    shards = make_shards(n, chunk)
    sm = StateMachine("shard", cfg["stateMachines"]["shard"])
    env = SimEnv(cfg, work_ticks=scenario.get("work_ticks", 4))
    env.max_ticks = scenario.get("max_ticks", 200)
    ledger = Ledger(scenario["name"], cfg["generation"], shards)
    faults = sorted(scenario.get("faults", []), key=lambda f: f["at"])
    fi = 0
    ticks_done = 0
    # 故障按虚拟时刻注入：手动分阶段跑 reconcile，保持确定性
    remaining = env.max_ticks
    merged_events: List[str] = []
    converged = False
    for fault in faults + [{"at": env.max_ticks + 1, "cmd": "noop"}]:
        at = fault["at"]
        span = max(0, min(at - ticks_done, remaining))
        if span:
            res = reconcile(env, cfg, ledger, sm, max_ticks=span)
            ticks_done += res["ticks"]
            remaining -= res["ticks"]
            merged_events.extend(env.events)
            env.events = []
            converged = res["converged"]
        if converged:
            break
        cmd = fault.get("cmd", "noop")
        if cmd != "noop":
            merged_events.append(f"{ticks_done:04d} CMD {cmd}")
            env.apply_command(cmd)
        if remaining <= 0:
            break
    if not converged and remaining > 0:
        res = reconcile(env, cfg, ledger, sm, max_ticks=remaining)
        ticks_done += res["ticks"]
        merged_events.extend(env.events)
        converged = res["converged"]
    summary = dict(ticks=ticks_done, converged=converged,
                   harvested=sum(1 for s in ledger.shards.values() if s["state"] == "harvested"),
                   total=len(shards), transitions=ledger.transitions)
    bad = check_invariants(cfg, ledger, summary)
    # golden log = 环境事件 + 状态账本事件；按虚拟时刻排序，保证可重放、可比对
    all_events = merged_events + list(ledger.events)
    all_events.sort(key=lambda e: (e[:4], e))
    return dict(scenario=scenario["name"], summary=summary,
                violations=bad, rejected=list(ledger.fence.rejected),
                accepted=len(ledger.accepted), events=all_events,
                dup_suppressed=sum("DUP-SUPPRESSED" in e for e in all_events),
                fingerprint=hashlib.sha256("\n".join(all_events).encode()).hexdigest()[:16])
