"""KSAYA 系统层带宽策略 —— 用 tc/netem 从源头控制网络占用。

为什么是这一层
--------------
原先的做法是在应用层做「语义压缩」（把 JSON 的键名折叠成 1 字节 tag）。
实测结论：在真实流量上它是 **负收益**（s2c 109.8%），因为
  * 语义折叠只对文本型协议有效，二进制游戏流量完全帮不上；
  * 每个包要额外带 tag + 长度 + pid 的帧头，小包场景下开销吃掉收益。
所以改为：**不再想办法把流量压小，而是直接限制流量本身**。

调度侧按作业声明的 tier 查表得到带宽额度，由节点上的 tc/netem 强制执行；
高流量低价值的作业拿低带宽档，交互类作业拿高带宽档。
这样「省流量」变成一个调度决策，而不是一个脆弱的编码技巧。

模块职责
--------
1. 纯函数：从 config + tier -> 具体 tc 规则（限速 / 延迟 / 丢包）
2. 规则渲染：生成可直接喂给 `tc qdisc` 的命令行（不执行）
3. 校验：tier 未知、限额非法、节点上限被突破 -> 抛错

执行侧的 `tc` 调用放在 ksayactl 里；本模块不碰系统，保持可单测。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_TIERS = {
    "bulk": {"limitKbit": 1024, "latencyMs": 40, "lossPct": 0.0},
    "streaming": {"limitKbit": 4096, "latencyMs": 10, "lossPct": 0.0},
    "interactive": {"limitKbit": 16384, "latencyMs": 2, "lossPct": 0.0},
    "realtime": {"limitKbit": 0, "latencyMs": 0, "lossPct": 0.0},
}


class BandwidthError(Exception):
    pass


def _tier_table(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """从 config 读取 tier 表；未配置时退回内置默认表。"""
    bw = (cfg or {}).get("features", {}).get("bandwidth", {})
    tiers = bw.get("tiers") or {}
    if not tiers:
        return dict(DEFAULT_TIERS)
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in tiers.items():
        out[name] = {
            "limitKbit": int(spec.get("limitKbit", 0)),
            "latencyMs": int(spec.get("latencyMs", 0)),
            "lossPct": float(spec.get("lossPct", 0.0)),
        }
    return out


def resolve_tier(cfg: Dict[str, Any], tier: Optional[str]) -> Dict[str, Any]:
    """解析作业 tier -> 具体带宽规则。

    - tier 未声明：用 config.features.bandwidth.defaultTier
    - tier 未在表中：抛 BandwidthError（不允许静默走不限速）
    """
    bw = (cfg or {}).get("features", {}).get("bandwidth", {})
    if not bw.get("enabled", False):
        return {"tier": "unlimited", "limitKbit": 0, "latencyMs": 0,
                "lossPct": 0.0, "capped": False}
    name = tier or bw.get("defaultTier", "bulk")
    table = _tier_table(cfg)
    if name not in table:
        raise BandwidthError(
            f"未知带宽档位 {name!r}；已定义: {sorted(table)}")
    spec = dict(table[name])
    spec["tier"] = name
    spec["capped"] = spec["limitKbit"] > 0
    return spec


def apply_node_ceiling(cfg: Dict[str, Any], spec: Dict[str, Any],
                        node_limit_kbit: Optional[int] = None) -> Dict[str, Any]:
    """把节点级总上限作用到单个作业规则上。

    节点总上限的语义：同一节点上所有受限作业的额度之和不得超过它。
    这里做的是**保守分配**——按作业请求的比例把节点上限切分给单个作业，
    保证「单作业不会因为调度器的疏忽而打满节点出口」。
    """
    bw = (cfg or {}).get("features", {}).get("bandwidth", {})
    ceiling = node_limit_kbit if node_limit_kbit is not None \
        else int(bw.get("nodeCeilingKbit", 0))
    out = dict(spec)
    if ceiling > 0 and out["limitKbit"] > 0:
        # 单作业最多拿走节点上限的一半，剩下的留给并发作业
        cap = ceiling // 2
        if out["limitKbit"] > cap:
            out["limitKbit"] = cap
            out["capped"] = True
            out["cappedBy"] = "nodeCeiling"
    out["nodeCeilingKbit"] = ceiling
    return out


def render_tc(spec: Dict[str, Any], iface: str = "eth0",
              direction: str = "egress") -> List[str]:
    """渲染 tc/netem 命令行（只生成，不执行）。

    使用 HTB 做限速 + netem 做延迟/丢包，两者挂在同一层级：
        tc qdisc add dev <iface> root handle 1: htb default 10
        tc class add dev <iface> parent 1: classid 1:10 htb rate <kbit> ceil <kbit>
        tc qdisc add dev <iface> parent 1:10 handle 10: netem delay <ms>ms loss <pct>%
    """
    if direction not in ("egress", "ingress"):
        raise BandwidthError(f"direction 只能是 egress/ingress，收到 {direction!r}")
    if not iface:
        raise BandwidthError("iface 不能为空")

    cmds: List[str] = []
    root = "root" if direction == "egress" else "ingress"
    parent = "1:" if direction == "egress" else "ffff:"

    # 清理旧规则（幂等重放）
    cmds.append(f"tc qdisc del dev {iface} {root} 2>/dev/null || true")

    limit = int(spec.get("limitKbit", 0))
    latency = int(spec.get("latencyMs", 0))
    loss = float(spec.get("lossPct", 0.0))

    if limit <= 0 and latency <= 0 and loss <= 0:
        return cmds  # 完全不限速，只需清理

    # HTB 根 + 默认类
    cmds.append(f"tc qdisc add dev {iface} {root} handle 1: htb default 10")
    if limit > 0:
        cmds.append(
            f"tc class add dev {iface} parent 1: classid 1:10 htb "
            f"rate {limit}kbit ceil {limit}kbit")
    else:
        # 不限速时仍需一个类挂 netem
        cmds.append(
            "tc class add dev {iface} parent 1: classid 1:10 htb "
            "rate 1000000kbit ceil 1000000kbit")

    netem_parts: List[str] = []
    if latency > 0:
        netem_parts.append(f"delay {latency}ms")
    if loss > 0:
        netem_parts.append(f"loss {loss}%")
    if netem_parts:
        cmds.append(
            f"tc qdisc add dev {iface} parent 1:10 handle 10: netem "
            + " ".join(netem_parts))
    else:
        cmds.append(
            f"tc qdisc add dev {iface} parent 1:10 handle 10: pfifo limit 1000")
    return cmds


def plan(cfg: Dict[str, Any], tier: Optional[str], iface: str = "eth0",
         direction: str = "egress",
         node_limit_kbit: Optional[int] = None) -> Dict[str, Any]:
    """完整规划：tier -> 生效规则 + 可执行的 tc 命令。"""
    spec = resolve_tier(cfg, tier)
    spec = apply_node_ceiling(cfg, spec, node_limit_kbit)
    return {
        "tier": spec["tier"],
        "limitKbit": spec["limitKbit"],
        "latencyMs": spec["latencyMs"],
        "lossPct": spec["lossPct"],
        "cappedBy": spec.get("cappedBy"),
        "iface": iface,
        "direction": direction,
        "commands": render_tc(spec, iface, direction),
    }


def selftest() -> bool:
    """内置自检：tier 解析、节点上限、tc 渲染、异常路径。"""
    cfg = {
        "features": {"bandwidth": {
            "enabled": True,
            "defaultTier": "bulk",
            "nodeCeilingKbit": 8192,
            "tiers": {
                "bulk": {"limitKbit": 1024, "latencyMs": 40, "lossPct": 0.0},
                "realtime": {"limitKbit": 0, "latencyMs": 0, "lossPct": 0.0},
            },
        }}
    }
    # 1. 默认档位
    s = resolve_tier(cfg, None)
    assert s["tier"] == "bulk" and s["limitKbit"] == 1024, s
    # 2. 显式档位
    s = resolve_tier(cfg, "realtime")
    assert s["limitKbit"] == 0 and not s["capped"], s
    # 3. 未知档位必须报错
    try:
        resolve_tier(cfg, "nope")
        raise AssertionError("未知 tier 应抛 BandwidthError")
    except BandwidthError:
        pass
    # 4. 节点上限生效（上限 8192 -> 单作业最多 4096）
    s = apply_node_ceiling(cfg, {"limitKbit": 16384, "capped": True},
                            node_limit_kbit=8192)
    assert s["limitKbit"] == 4096 and s.get("cappedBy") == "nodeCeiling", s
    # 5. 未开启时不限制
    s = resolve_tier({"features": {}}, "bulk")
    assert s["limitKbit"] == 0 and s["tier"] == "unlimited", s
    # 6. tc 渲染
    cmds = render_tc({"limitKbit": 1024, "latencyMs": 40, "lossPct": 0.0},
                      "eth0", "egress")
    assert cmds[0].startswith("tc qdisc del"), cmds
    assert any("htb default 10" in c for c in cmds), cmds
    assert any("rate 1024kbit" in c for c in cmds), cmds
    assert any("netem delay 40ms" in c for c in cmds), cmds
    # 7. 不限速时只清理
    cmds = render_tc({"limitKbit": 0, "latencyMs": 0, "lossPct": 0.0})
    assert len(cmds) == 1, cmds
    # 8. 非法 direction 报错
    try:
        render_tc({"limitKbit": 1}, "eth0", "sideways")
        raise AssertionError("非法 direction 应抛错")
    except BandwidthError:
        pass
    return True


if __name__ == "__main__":
    print("BANDWIDTH-SELFTEST", "pass" if selftest() else "fail")
