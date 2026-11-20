#!/usr/bin/env python3
"""AR8 能力协商与配置校验层 —— 源自 PVE 源码逐文件精读的工程结论。

这里把 Proxmox VE 在 VM/网络/存储/快照里的三个稳定模式抽象成通用规则，
用于 AR8 的多云资源准入与离谱资源兼容接口：

  1) 【单一真值源 + 拒绝覆盖】(Drive.pm:138-151)
     受管卷的 format 以存储层为准，drive 层的 format 若与之冲突直接 die，不允许覆盖。
     -> 受管资源的属性以注册表/存储层为准，请求携带的冲突属性必须拒绝（不是静默忽略）。

  2) 【能力协商降级 + 版本门禁】(Drive.pm:1213-1260 aio_cmdline_option / Snapshot.pm:43-46)
     io_uring 在 rbd(非 direct)/lvm/cifs 上不安全 -> 自动降级 native/threads；
     external snapshot 需要 QEMU machine >= 10.0，否则 fail-fast 并给出修复指引。
     -> 按底层能力选择安全实现路径；能力不足时要么降级到安全档，要么明确拒绝并给出原因。

  3) 【组合一致性 fail-closed】(Drive.pm:928-967 / Network.pm:233-253)
     mbps_max 需要 mbps、gw 需要 ip、gw 不能配 DHCP；任一不满足整条配置作废（返回 undef），
     不做部分接受。
     -> 校验字段间依赖与互斥，失败整条拒绝，不留半配置状态。

纯标准库、无副作用，可离线单测：python3 capability.py selftest
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# 模式 2：存储类型 -> 在该类型上不安全的能力 -> 降级目标
# 对应 PVE storage_allows_io_uring_default + aio_cmdline_option
UNSAFE_CAPABILITY_FALLBACK = {
    "rbd": {"io_uring": "native"},       # krbd + cache!=direct 时 io_uring 会 hang
    "lvm": {"io_uring": "threads"},      # LVM 上 io_uring 一律禁用
    "cifs": {"io_uring": "threads"},     # kernel>=5.15 的 CIFS 有已知问题
}

# 模式 3：字段依赖（option 出现时必须同时出现 requires）—— 对应 parse_drive 的 requirement 表
FIELD_REQUIRES = {
    "mbps_max": "mbps",
    "bps_max": "bps",
    "iops_max": "iops",
    "gateway": "address",
    "quota_max": "quota",
}

# 模式 3：互斥字段对 —— 对应 parse_drive 的 total/per-direction 互斥、parse_ipconfig 的 gw+dhcp
FIELD_MUTEX = [
    ("gateway", "dhcp"),
    ("mbps", "mbps_rd"),
    ("mbps", "mbps_wr"),
    ("quota", "ephemeral_quota"),
]


def negotiated_io(store_type: str, requested: str = "io_uring",
                  cache_direct: bool = True) -> Tuple[str, List[str]]:
    """模式 2：按底层存储类型协商安全的 IO 实现，返回 (选定实现, 降级说明列表)。

    对 PVE 语义做了保守化：rbd 仅在非 direct cache 时降级（与 PVE 一致），
    lvm/cifs 无条件降级。
    """
    notes: List[str] = []
    chosen = requested
    if requested == "io_uring":
        fb = UNSAFE_CAPABILITY_FALLBACK.get(store_type, {}).get("io_uring")
        unsafe = store_type == "lvm" or store_type == "cifs" or (
            store_type == "rbd" and not cache_direct)
        if fb and unsafe:
            chosen = fb if cache_direct else "threads"
            notes.append(
                f"io_uring unsafe on {store_type}"
                f"({'non-direct cache' if store_type == 'rbd' else 'known issue'}),"
                f"fallback -> {chosen}")
    return chosen, notes


def version_gate(current: Tuple[int, int], required: Tuple[int, int],
                 feature: str) -> Optional[str]:
    """模式 2（fail-fast 侧）：版本不足时返回可执行修复指引，否则 None。

    对应 Snapshot.pm external snapshot 要求 machine>=10.0 时直接 die。
    """
    if current < required:
        return (f"{feature} requires version >= {required[0]}.{required[1]}, "
                f"current={current[0]}.{current[1]}; upgrade before use")
    return None


def validate_combo(fields: Dict[str, Any]) -> List[str]:
    """模式 3：校验字段依赖与互斥，返回错误列表（空表示通过）。

    调用方应 fail-closed：只要返回非空就拒绝整条配置，不保留部分字段。
    """
    errs: List[str] = []
    present = {k for k, v in fields.items() if v not in (None, "", False, 0)}
    for opt, req in FIELD_REQUIRES.items():
        if opt in present and req not in present:
            errs.append(f"{opt} requires {req}")
    for a, b in FIELD_MUTEX:
        if a in present and b in present:
            errs.append(f"{a} and {b} are mutually exclusive")
    return errs


def managed_attr(managed_value: Any, requested_value: Any, attr: str) -> Tuple[Any, Optional[str]]:
    """模式 1：受管资源属性以注册表为准，请求值冲突时拒绝。

    对应 Drive.pm: get_path_and_format 中受管卷 format 不允许被 drive->{format} 覆盖。
    返回 (生效值, 错误信息)。requested_value=None 表示未指定，直接采用受管值。
    """
    if requested_value is None:
        return managed_value, None
    if requested_value != managed_value:
        return managed_value, (
            f"{attr}={requested_value!r} conflicts with managed value "
            f"{managed_value!r}; managed resources cannot be overridden")
    return managed_value, None


def selftest() -> bool:
    ok = True
    def check(cond, msg):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + msg)
        if not cond:
            ok = False

    # 模式 1：单一真值源
    check(managed_attr("qcow2", None, "format") == ("qcow2", None),
          "未指定属性时采用受管值")
    check(managed_attr("qcow2", "qcow2", "format") == ("qcow2", None),
          "请求值与受管值一致时通过")
    _, err = managed_attr("qcow2", "raw", "format")
    check(err is not None and "cannot be overridden" in err,
          "受管资源属性被请求覆盖时拒绝（拒绝漂移）")

    # 模式 2：能力协商降级
    check(negotiated_io("local", "io_uring")[0] == "io_uring",
          "未知/本地存储保留请求能力")
    check(negotiated_io("lvm", "io_uring")[0] == "threads",
          "LVM 上 io_uring 降级 threads")
    check(negotiated_io("rbd", "io_uring", cache_direct=False)[0] == "threads",
          "rbd 非 direct cache 降级 threads")
    check(negotiated_io("rbd", "io_uring", cache_direct=True)[0] == "io_uring",
          "rbd direct cache 保留 io_uring（与 PVE 一致）")
    check(negotiated_io("cifs", "io_uring")[0] == "threads",
          "CIFS 上 io_uring 降级 threads")

    # 模式 2：版本门禁
    check(version_gate((9, 4), (10, 0), "external snapshot") is not None,
          "版本不足时给出 fail-fast 指引")
    check("upgrade" in (version_gate((9, 4), (10, 0), "external snapshot") or ""),
          "拒绝信息含可执行修复建议")
    check(version_gate((10, 1), (10, 0), "external snapshot") is None,
          "版本满足时放行")

    # 模式 3：组合校验 fail-closed
    check(validate_combo({"mbps_max": 100}) and
          "mbps_max requires mbps" in validate_combo({"mbps_max": 100}),
          "依赖字段缺失被拒绝")
    check(not validate_combo({"mbps": 100, "mbps_max": 50}),
          "依赖齐全时通过")
    check(any("mutually exclusive" in e for e in validate_combo({"gateway": "10.0.0.1", "dhcp": True})),
          "互斥字段（gw + dhcp）被拒绝")
    check(any("mutually exclusive" in e for e in validate_combo({"mbps": 1, "mbps_rd": 1})),
          "互斥字段（总量 + 分项限速）被拒绝")
    check(not validate_combo({}), "空配置通过")

    print(f"CAPABILITY-SELFTEST {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
