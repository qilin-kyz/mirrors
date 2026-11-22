#!/usr/bin/env python3
"""KSAYA 账号-额度-算力多对多抽象层 + 非标准资源兼容接口。

设计要点：
  1) 账号不直接绑定算力。多个账号 -> 一个计费额度池 -> 一个算力资源，全部多对多。
  2) 兼容接口不要求厂商严格兼容 S3/IaaS；任何"离谱"资源只要实现六段协议即可接入：
       describe / acquire / invoke / status / release / bill
     Hugging Face 会员的瞬时高性能显卡实例就是典型案例：它不是一台可 SSH 的云主机，
     而是会员权益换算出的临时 GPU 秒，通过 adapter 抽象后与 ECS/ESSD/函数计算并列。
  3) 所有额度判定 fail-closed：额度不存在、账户无权、余额不足一律拒绝。

用法：
  python3 ksaya-capacity.py report
  python3 ksaya-capacity.py accounts-for-cr cr-hf-a100-spot
  python3 ksaya-capacity.py acquire cr-hf-a100-spot --seconds 60
  python3 ksaya-capacity.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
CAP_FILE = os.path.join(HERE, "quota-capacity.json")
ACCT_FILE = os.path.join(HERE, "ksaya-accounts.json")

# 兼容协议六段操作
OPERATIONS = ("describe", "acquire", "invoke", "status", "release", "bill")


@dataclass
class Quota:
    id: str
    label: str
    unit: str
    limit: float
    used: float
    reset: str
    accounts: List[str]

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.used)

    def can_draw(self, account: str, amount: float) -> bool:
        return account in self.accounts and amount > 0 and amount <= self.remaining


@dataclass
class ComputeResource:
    id: str
    label: str
    kind: str
    vendor: str
    adapter: str
    quotas: List[str]
    capacity: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass
class AdapterResult:
    ok: bool
    op: str
    resource_id: str
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


class BaseAdapter:
    """兼容性接口基类。厂商适配器只需实现自己支持的操作。"""

    name = "base"
    supports: tuple = OPERATIONS

    def __init__(self, registry: "CapacityRegistry"):
        self.registry = registry

    def describe(self, cr: ComputeResource) -> AdapterResult:
        return AdapterResult(True, "describe", cr.id,
                             {"label": cr.label, "kind": cr.kind, "vendor": cr.vendor,
                              "capacity": cr.capacity, "adapter": cr.adapter})

    def acquire(self, cr: ComputeResource, account: str, amount: float) -> AdapterResult:
        quota = self._quota_for(cr, account, amount)
        if isinstance(quota, AdapterResult):
            return quota
        lease = f"lease-{uuid.uuid4().hex[:12]}"
        quota.used += amount
        return AdapterResult(True, "acquire", cr.id,
                             {"lease": lease, "account": account, "amount": amount,
                              "unit": quota.unit, "quota": quota.id,
                              "remaining": quota.remaining, "ephemeral": bool(cr.capacity.get("ephemeral"))})

    def invoke(self, cr: ComputeResource, account: str, payload: str) -> AdapterResult:
        # 默认实现：调用也走一次额度扣减（1 单位调用额度）
        quota = self._quota_for(cr, account, 1.0)
        if isinstance(quota, AdapterResult):
            return quota
        import hashlib
        digest = hashlib.sha256(payload.encode()).hexdigest()
        quota.used += 1.0
        return AdapterResult(True, "invoke", cr.id,
                             {"account": account, "quota": quota.id,
                              "result": f"SHA256:{digest}", "remaining": quota.remaining})

    def status(self, cr: ComputeResource, lease: str = "") -> AdapterResult:
        return AdapterResult(True, "status", cr.id, {"lease": lease, "state": "READY"})

    def release(self, cr: ComputeResource, lease: str) -> AdapterResult:
        return AdapterResult(True, "release", cr.id, {"lease": lease, "state": "RELEASED"})

    def bill(self, cr: ComputeResource, account: str) -> AdapterResult:
        rows = []
        for qid in cr.quotas:
            q = self.registry.quotas.get(qid)
            if q:
                rows.append({"quota": qid, "used": q.used, "limit": q.limit,
                             "remaining": q.remaining, "unit": q.unit})
        return AdapterResult(True, "bill", cr.id, {"account": account, "quotas": rows})

    def _quota_for(self, cr: ComputeResource, account: str, amount: float):
        candidates = [self.registry.quotas[q] for q in cr.quotas if q in self.registry.quotas]
        candidates = [q for q in candidates if q.can_draw(account, amount)]
        if not candidates:
            return AdapterResult(False, "acquire", cr.id,
                                 error=f"no quota on {cr.id} for account {account} amount={amount}")
        # 优先使用剩余额度最多的池，避免小额池碎片化
        return max(candidates, key=lambda q: q.remaining)


class HfMemberSpotAdapter(BaseAdapter):
    """Hugging Face 会员瞬时 GPU 适配：非标准 IaaS，但可用同一协议调度。"""

    name = "hf-member-spot"

    def acquire(self, cr: ComputeResource, account: str, amount: float) -> AdapterResult:
        r = super().acquire(cr, account, amount)
        if r.ok:
            r.data["instanceType"] = "ephemeral-gpu"
            r.data["gpu"] = cr.capacity.get("gpu")
            r.data["maxLeaseSec"] = cr.capacity.get("maxLeaseSec")
            r.data["note"] = "会员权益换算的瞬时显卡实例，无需 SSH，按 GPU 秒计费"
        return r


class CapacityRegistry:
    def __init__(self, cap: Dict[str, Any], accounts: Optional[Dict[str, Any]] = None):
        self.quotas: Dict[str, Quota] = {
            q["id"]: Quota(q["id"], q.get("label", ""), q.get("unit", ""),
                           float(q.get("limit", 0)), float(q.get("used", 0)),
                           q.get("reset", ""), list(q.get("accounts", [])))
            for q in cap.get("quotas", [])
        }
        self.resources: Dict[str, ComputeResource] = {
            c["id"]: ComputeResource(c["id"], c.get("label", ""), c.get("kind", ""),
                                     c.get("vendor", ""), c.get("adapter", ""),
                                     list(c.get("quotas", [])), c.get("capacity", {}),
                                     c.get("notes", ""))
            for c in cap.get("computeResources", [])
        }
        self.memberships = cap.get("memberships", [])
        self.adapters_meta = {a["name"]: a for a in cap.get("adapters", [])}
        self.accounts = (accounts or {}).get("principals", [])
        self._adapters: Dict[str, BaseAdapter] = {
            "hf-member-spot": HfMemberSpotAdapter(self),
            "base": BaseAdapter(self),
        }

    @classmethod
    def from_files(cls, cap_file=CAP_FILE, acct_file=ACCT_FILE) -> "CapacityRegistry":
        with open(cap_file, encoding="utf-8") as f:
            cap = json.load(f)
        with open(acct_file, encoding="utf-8") as f:
            acct = json.load(f)
        return cls(cap, acct)

    def accounts_for_quota(self, quota_id: str) -> List[str]:
        q = self.quotas.get(quota_id)
        return list(q.accounts) if q else []

    def quotas_for_account(self, account: str) -> List[str]:
        return [q.id for q in self.quotas.values() if account in q.accounts]

    def accounts_for_cr(self, cr_id: str) -> List[str]:
        cr = self.resources.get(cr_id)
        if not cr:
            return []
        out = []
        for qid in cr.quotas:
            for a in self.accounts_for_quota(qid):
                if a not in out:
                    out.append(a)
        return out

    def resources_for_account(self, account: str) -> List[str]:
        return [cr.id for cr in self.resources.values()
                if account in self.accounts_for_cr(cr.id)]

    def adapter_for(self, cr: ComputeResource) -> BaseAdapter:
        return self._adapters.get(cr.adapter, self._adapters["base"])

    def operate(self, op: str, cr_id: str, account: str = "", amount: float = 0.0,
                payload: str = "", lease: str = "") -> AdapterResult:
        if op not in OPERATIONS:
            return AdapterResult(False, op, cr_id, error=f"unsupported operation {op}")
        cr = self.resources.get(cr_id)
        if not cr:
            return AdapterResult(False, op, cr_id, error=f"unknown compute resource {cr_id}")
        adapter = self.adapter_for(cr)
        if op not in adapter.supports:
            return AdapterResult(False, op, cr_id, error=f"adapter {adapter.name} lacks {op}")
        if op == "describe":
            return adapter.describe(cr)
        if op == "acquire":
            return adapter.acquire(cr, account, amount)
        if op == "invoke":
            return adapter.invoke(cr, account, payload)
        if op == "status":
            return adapter.status(cr, lease)
        if op == "release":
            return adapter.release(cr, lease)
        if op == "bill":
            return adapter.bill(cr, account)
        return AdapterResult(False, op, cr_id, error="unreachable")

    def report(self) -> str:
        lines = ["KSAYA 账号-额度-算力多对多报告", "=" * 60]
        lines.append("额度池:")
        for q in sorted(self.quotas.values(), key=lambda x: x.id):
            lines.append(f"  {q.id:22s} {q.unit:12s} {q.used}/{q.limit} "
                         f"剩余={q.remaining:.1f} 账户={','.join(q.accounts)}")
        lines.append("算力资源:")
        for cr in sorted(self.resources.values(), key=lambda x: x.id):
            accts = self.accounts_for_cr(cr.id)
            lines.append(f"  {cr.id:22s} {cr.kind:14s} adapter={cr.adapter:18s} "
                         f"额度={','.join(cr.quotas)} 可支取账户={','.join(accts)}")
        lines.append("会员权益:")
        for m in self.memberships:
            lines.append(f"  {m['id']:18s} {m['provider']}/{m['plan']} -> "
                         f"{','.join(m['grantsQuotas'])}")
        return "\n".join(lines)


def selftest(reg: CapacityRegistry) -> bool:
    ok = True
    def check(cond, msg):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + msg)
        if not cond:
            ok = False

    # 多对多：q-hf-pro-gpu 只授权给 HF 会员账号
    check(reg.accounts_for_quota("q-hf-pro-gpu") == ["HFMEMBERPRO0000000001"],
          "quota 多对多映射正确")
    check("ALIYUNDEV0000000000001" in reg.accounts_for_cr("cr-faas-ingest"),
          "一个算力资源可由共享额度的多个账户支取")
    check(len(reg.accounts_for_cr("cr-hf-a100-spot")) == 1,
          "HF 瞬时 GPU 由会员额度单独支撑")

    # 同一会员额度支撑多种 GPU
    check(reg.quotas_for_account("HFMEMBERPRO0000000001") == ["q-hf-pro-gpu"],
          "会员账号获得 GPU 额度")

    # acquire 扣减额度
    before = reg.quotas["q-hf-pro-gpu"].used
    r = reg.operate("acquire", "cr-hf-a100-spot", "HFMEMBERPRO0000000001", 60)
    check(r.ok and r.data.get("gpu") == "A100" and r.data.get("instanceType") == "ephemeral-gpu",
          "HF 会员瞬时 A100 租用成功（非标准资源已抽象）")
    check(reg.quotas["q-hf-pro-gpu"].used == before + 60,
          "租用后额度正确扣减")

    # 无授权账户租用失败
    bad = reg.operate("acquire", "cr-hf-a100-spot", "ARCLOUDALIYUN00000001", 60)
    check(not bad.ok and "no quota" in bad.error, "无额度账户被 fail-closed 拒绝")

    # 超额拒绝
    huge = reg.operate("acquire", "cr-hf-a100-spot", "HFMEMBERPRO0000000001", 10 ** 9)
    check(not huge.ok, "超额租用被拒绝")

    # 六段协议覆盖
    for op in OPERATIONS:
        kw = {"account": "HFMEMBERPRO0000000001", "amount": 1.0,
              "payload": "hello", "lease": "lease-test"}
        res = reg.operate(op, "cr-hf-a100-spot", **kw)
        check(res.ok, f"adapter operation {op}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["report", "accounts-for-cr", "quotas-for-account",
                                    "acquire", "invoke", "selftest"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--account", default="")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--payload", default="hello")
    ns = ap.parse_args()
    reg = CapacityRegistry.from_files()
    if ns.cmd == "report":
        print(reg.report())
        return 0
    if ns.cmd == "accounts-for-cr":
        print(",".join(reg.accounts_for_cr(ns.target)))
        return 0
    if ns.cmd == "quotas-for-account":
        print(",".join(reg.quotas_for_account(ns.target)))
        return 0
    if ns.cmd == "selftest":
        return 0 if selftest(reg) else 1
    op = "acquire" if ns.cmd == "acquire" else "invoke"
    r = reg.operate(op, ns.target, ns.account, ns.seconds, ns.payload)
    print(json.dumps({"ok": r.ok, "op": r.op, "resource": r.resource_id,
                      "data": r.data, "error": r.error}, ensure_ascii=False))
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
