#!/usr/bin/env python3
"""KSAYA 稳定核心 —— 唯一操作入口。

    python3 ksayactl.py validate | plan | apply | rollback | status | doctor
    python3 ksayactl.py verify  [--n N] [--chunk C] [--fault pod-kill|node-cordon|all-pods]
    python3 ksayactl.py sim     [--scenario NAME|all] [--json]
    python3 ksayactl.py test
    python3 ksayactl.py task    [--list | --show UPID]

克制原则：
  * 所有真值来自 ksaya.config.yaml；运行时只读，禁止 kubectl set env / sed 改行为。
  * 不内嵌业务脚本到 YAML：作业逻辑由本文件生成、由 K8s 原生 Job 承载。
  * 用 etcd（K8s API）作唯一状态存储，结果经 Pod termination message 回传，
    不再有自研 broker / CGI / flock / 12 个文本账本 / 租约 TTL 脚本。
  * 每次变更事务化：先备份、后 apply、失败回滚；所有操作写 append-only 任务日志。
  * 同一份 reconcile 逻辑同时驱动确定性仿真与真实集群（PVE::HA::Env 思路）。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import glob
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

import ksayacore as core

# ksaya-bandwidth.py 文件名带连字符（非合法模块名），用 importlib 按路径加载
import importlib.util as _ilu
_bw_spec = _ilu.spec_from_file_location(
    "ksaya_bandwidth", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "ksaya-bandwidth.py"))
bandwidth = _ilu.module_from_spec(_bw_spec)
_bw_spec.loader.exec_module(bandwidth)

ROOT = os.path.dirname(os.path.abspath(__file__))
# Nix 包安装进 /nix/store 后目录是**只读**的，因此真值源与可写状态必须能外置：
#   KSAYA_CONFIG    真值源路径（只读，NixOS 下指向 /etc/ksaya/ksaya.config.yaml）
#   KSAYA_STATE_DIR 可写状态目录（任务日志/history/applied.json，NixOS 下用 /var/lib/ksaya）
# 两者都未设置时回退到脚本同级目录，保持原有「运维机直接跑」的行为不变。
CONFIG_PATH = os.environ.get("KSAYA_CONFIG") or os.path.join(ROOT, "ksaya.config.yaml")
STATE_DIR = os.environ.get("KSAYA_STATE_DIR") or os.path.join(ROOT, "state")
HISTORY_DIR = os.path.join(STATE_DIR, "history")
TASK_LOG = os.path.join(STATE_DIR, "tasks.log")
APPLIED = os.path.join(STATE_DIR, "applied.json")
SCEN_DIR = os.path.join(ROOT, "scenarios")


# ---------------------------------------------------------------------------
# 基础设施：配置、任务日志、SSH/kubectl
# ---------------------------------------------------------------------------

MAX_CONFIG_BYTES = 1024 * 1024   # 对齐 pmxcfs 对配置库设硬上限的做法
LOCK_FILE = os.path.join(STATE_DIR, ".ksaya.lock")
RESTART_FLAG = os.path.join(STATE_DIR, ".restart-flag")
LOG_SUPPRESS_AFTER = 100         # 对齐 quorum.c:170-179：重试日志每 100 次打一条，避免风暴
OP_LOCK_LEASE = 120              # 对齐 Cluster.pm:655 CFS_LOCK_TIMEOUT
OP_WORK_BUDGET = 300             # 持锁工作预算；超出必须告警（PVE 用 60 = 120 - 60 逃生余量）
_task_log_counts: Dict[Tuple[str, str], int] = {}


def _parse_quantity_mi(q: Any) -> float:
    """把 k8s 资源量解析成 MiB。支持 Ki/Mi/Gi/Ti 与裸数字。"""
    if not q:
        return 0.0
    s = str(q).strip()
    unit = {"Ki": 1 / 1024.0, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0 * 1024.0}
    for suf, mul in unit.items():
        if s.endswith(suf):
            try:
                return float(s[: -len(suf)]) * mul
            except ValueError:
                return 0.0
    try:
        return float(s) / (1024.0 * 1024.0)
    except ValueError:
        return 0.0


def _parse_quantity_milli(q: Any) -> float:
    """把 k8s CPU 资源量解析成 millicores。'2'->2000, '500m'->500。"""
    if not q:
        return 0.0
    s = str(q).strip()
    if s.endswith("m"):
        try:
            return float(s[:-1])
        except ValueError:
            return 0.0
    try:
        return float(s) * 1000.0
    except ValueError:
        return 0.0


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    size = os.path.getsize(path)
    if size > MAX_CONFIG_BYTES:
        raise core.ConfigError(f"config too large: {size} > {MAX_CONFIG_BYTES}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    """同目录临时文件 + fsync + os.replace 原子替换。

    对齐 pve-cluster/src/pmxcfs/cfs-utils.c:211-265 atomic_write_file()：
    mkstemp -> fchmod -> full_write -> close -> rename，失败一律 unlink 临时文件。
    旧实现直接 open(path,'w')，中途崩溃会留下半份配置 —— 这是 PVE 明确规避的坑。
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            buf = text.encode("utf-8")
            off = 0
            while off < len(buf):
                off += os.write(fd, buf[off:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def config_digest(cfg: Dict[str, Any]) -> str:
    return hashlib.sha256(yaml.safe_dump(cfg, sort_keys=True).encode()).hexdigest()


def ensure_dirs() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)


@contextlib.contextmanager
def op_lock() -> Iterator[None]:
    """单实例操作锁：防止并发 apply / doctor --heal 撕裂状态。

    源码来源：pmxcfs.c:855-872 —— LOCKFILE + flock(LOCK_EX|LOCK_NB)，
    10 次重试、每次 1 秒，仍拿不到就 fatal 退出。PVE 用这道锁保证同一时刻
    只有一个进程操作配置数据库；KSAYA 此前没有，并发调用会互相覆盖 state。
    """
    ensure_dirs()
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        for attempt in range(11):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == 10:
                    raise RuntimeError("unable to acquire ksaya op lock after 10 retries")
                time.sleep(1)
        t0 = time.monotonic()
        try:
            yield
        finally:
            held = time.monotonic() - t0
            if held > OP_WORK_BUDGET:
                # PVE 的核心原则：工作预算必须显著小于锁租约，留 abort 逃生余量。
                # KSAYA 用 flock（进程退出自动释放），不做强制 kill，但超预算必须可见。
                with contextlib.suppress(Exception):
                    task_log("SYSTEM",
                             f"ALERT op-lock held {held:.1f}s > budget {OP_WORK_BUDGET}s")
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def mark_restart() -> None:
    """标记一次变更开始；只有显式 clear 才代表成功提交。

    源码来源：pmxcfs.c:60,1039,1049 RESTART_FLAG_FILE —— 非正常退出留下标记，
    下次启动可检出。KSAYA 用于「apply 中途被 kill」的残留检测。
    """
    ensure_dirs()
    with open(RESTART_FLAG, "w", encoding="utf-8") as f:
        f.write(f"{time.strftime('%F %T')}\n")


def clear_restart() -> None:
    try:
        os.unlink(RESTART_FLAG)
    except FileNotFoundError:
        pass


def restart_flag_age() -> Optional[int]:
    if not os.path.exists(RESTART_FLAG):
        return None
    try:
        stamp = open(RESTART_FLAG, encoding="utf-8").read().strip()
        return int(time.time() - time.mktime(time.strptime(stamp, "%F %T")))
    except Exception:
        return -1


def new_upid(op: str) -> str:
    raw = f"{op}:{time.time_ns()}:{os.getpid()}".encode()
    tag = hashlib.sha256(raw).hexdigest()[:8]
    return f"UPID:ksaya:{tag}:{int(time.time()):08x}:{op}:"


def task_log(upid: str, msg: str) -> None:
    ensure_dirs()
    # 对齐 quorum.c:170-179：高频重复消息只打首条 + 每 LOG_SUPPRESS_AFTER 条一条摘要，避免日志风暴
    key = (upid, msg)
    seen = _task_log_counts.get(key, 0) + 1
    _task_log_counts[key] = seen
    if seen > LOG_SUPPRESS_AFTER and seen % LOG_SUPPRESS_AFTER != 0:
        return
    suffix = f" [repeated x{seen}]" if seen > 1 else ""
    with open(TASK_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%F %T')} {upid} {msg}{suffix}\n")


def task_end(upid: str, ok: bool, detail: str = "",
             status: str = "") -> None:
    """终态落盘：TASK OK / TASK ERROR / TASK TIMEOUT（对齐 UPID 末行状态协议）。"""
    tag = status or ("TASK OK " if ok else "TASK ERROR ")
    task_log(upid, tag + detail)


def task_timeout(upid: str, detail: str = "") -> None:
    task_end(upid, False, detail or "deadline exceeded", status="TASK TIMEOUT ")


def task_active(deadline_seconds: int = 300) -> List[Dict[str, Any]]:
    """列出无终态的任务，并区分 running / stuck。

    反面教材：PVE RESTEnvironment.pm:296-404 的 active_workers() 只判存活与归档，
    卡死任务会永久留在 active 列表；这里按 deadline 显式标记 stuck。
    """
    ensure_dirs()
    if not os.path.exists(TASK_LOG):
        return []
    buckets: Dict[str, Dict[str, Any]] = {}
    with open(TASK_LOG, encoding="utf-8") as f:
        for ln in f.read().splitlines():
            parts = ln.split()
            if len(parts) < 4:
                continue
            upid = next((x for x in parts if x.startswith("UPID:")), None)
            if not upid:
                continue
            b = buckets.setdefault(upid, dict(upid=upid, started=parts[0] + " " + parts[1],
                                              terminal="", stuck=False, lines=0))
            b["lines"] += 1
            if "TASK OK " in ln:
                b["terminal"] = "OK"
            elif "TASK ERROR " in ln:
                b["terminal"] = "ERROR"
            elif "TASK TIMEOUT " in ln:
                b["terminal"] = "TIMEOUT"
    now = time.time()
    out = []
    for b in buckets.values():
        if b["terminal"]:
            continue
        try:
            age = now - time.mktime(time.strptime(b["started"], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            age = 0
        b["age_seconds"] = int(age)
        b["stuck"] = age > deadline_seconds
        b["state"] = "stuck" if b["stuck"] else "running"
        out.append(b)
    return out


def wait_task(upid: str, timeout: int = 60, interval: float = 1.0) -> Tuple[bool, str]:
    """公共库式任务等待（对齐 PVE::ProcFSTools::upid_wait）。

    只判定「任务是否终结」，不判定「业务是否成功」；调用方必须再做实际状态复核，
    对齐 PVEVM.pm:137 的 `return !(-f $oldconfig)` 与 :140 的 check_running()。

    与 PVE 的关键差异：PVE::ProcFSTools::upid_wait(:595-613) 无 deadline 会无限阻塞；
    此处 timeout 必填默认值，超时写 TASK TIMEOUT 终态，不允许静默挂住。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(TASK_LOG):
            with open(TASK_LOG, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if upid in ln]
            for ln in reversed(lines):
                # 对齐 PVE::UPID::status_is_error()：只有 TASK OK / TASK WARNINGS 是成功，
                # 其余一切 TASK * 终态（含 TASK TIMEOUT 与未知终态）一律 fail-closed。
                # 原实现漏判 TASK TIMEOUT，会让超时任务在等待者眼里一直"活着"。
                if "TASK OK " in ln or "TASK WARNINGS" in ln:
                    return True, ln.strip()
                if "TASK " in ln and ("TASK ERROR " in ln or "TASK TIMEOUT " in ln
                                       or ln.rstrip().endswith("TASK ERROR")
                                       or ln.rstrip().endswith("TASK TIMEOUT")):
                    return False, ln.strip()
        time.sleep(interval)
    task_timeout(upid, f"wait_task deadline={timeout}s")
    return False, f"timeout waiting for {upid}"


def ssh_base(cfg: Dict[str, Any]) -> List[str]:
    c = cfg["cluster"]
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-J", c["jump"],
            f"{c['sshUser']}@{c['pilot']}"]


def remote(cfg: Dict[str, Any], command: str, stdin_text: Optional[str] = None,
           timeout: int = 90) -> Tuple[int, str, str]:
    p = subprocess.run(ssh_base(cfg) + [command], input=stdin_text, text=True,
                       capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def kube(cfg: Dict[str, Any], args: List[str], stdin_text: Optional[str] = None,
         timeout: int = 90) -> Tuple[int, str, str]:
    binargs = cfg["cluster"]["kubectl"].split()
    cmd = " ".join(shlex.quote(a) for a in binargs + args)
    return remote(cfg, cmd, stdin_text, timeout)


def kube_json(cfg: Dict[str, Any], args: List[str], timeout: int = 90) -> Any:
    rc, out, err = kube(cfg, args + ["-o", "json"], timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"kubectl failed: {err.strip() or out.strip()}")
    return json.loads(out)


def emit(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            print(f"{k:28s} {v}")
    else:
        print(obj)


# ---------------------------------------------------------------------------
# 单一真值源 -> 唯一清单（替代原来 9 个 YAML / 14 个 ConfigMap）
# ---------------------------------------------------------------------------

def desired_manifest(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    core.assert_config(cfg)
    ns = cfg["cluster"]["namespace"]
    docs: List[Dict[str, Any]] = [
        dict(apiVersion="v1", kind="Namespace",
             metadata=dict(name=ns, labels={"app.kubernetes.io/part-of": "ksaya"})),
        dict(apiVersion="v1", kind="ConfigMap",
             metadata=dict(name="ksaya-config", namespace=ns,
                           labels={"ksaya.io/generation": str(cfg["generation"])}),
             data={"config.yaml": yaml.safe_dump(cfg, allow_unicode=True, sort_keys=True),
                   "config.sha256": config_digest(cfg)}),
    ]
    for s in cfg["storage"]:
        docs.append(dict(
            apiVersion="storage.k8s.io/v1", kind="StorageClass",
            metadata=dict(name=s["name"], annotations={
                "ksaya.io/tier": s["tier"], "ksaya.io/kind": s["kind"],
                "ksaya.io/cluster-wide-mount": "forbidden"}),
            provisioner=s["provisioner"],
            reclaimPolicy="Retain" if s["kind"] == "object" else "Delete",
            volumeBindingMode="WaitForFirstConsumer" if s["kind"] == "block" else "Immediate",
            allowVolumeExpansion=s["kind"] == "block"))
    if cfg["features"]["admission"]["enabled"]:
        docs.append(dict(
            apiVersion="admissionregistration.k8s.io/v1",
            kind="ValidatingAdmissionPolicy",
            metadata=dict(name="ksaya-storage-guard"),
            spec=dict(failurePolicy="Fail", matchConstraints=dict(
                resourceRules=[dict(apiGroups=[""], apiVersions=["v1"],
                                    operations=["CREATE", "UPDATE"], resources=["persistentvolumeclaims"])]),
                validations=[
                    dict(expression="!has(object.spec.storageClassName) || !(object.spec.storageClassName.startsWith('ksaya-cloud-') || object.spec.storageClassName.startsWith('ksayac-cloud-')) || !has(object.spec.accessModes) || !('ReadWriteMany' in object.spec.accessModes)",
                         message="禁止以 ReadWriteMany 把云存储挂载到整个集群"),
                    dict(expression="!(has(object.metadata.annotations) && 'ksaya.io/scope' in object.metadata.annotations && object.metadata.annotations['ksaya.io/scope'] == 'cluster')",
                         message="禁止 ksaya.io/scope=cluster 整集群挂载"),
                ])))
        docs.append(dict(
            apiVersion="admissionregistration.k8s.io/v1",
            kind="ValidatingAdmissionPolicyBinding",
            metadata=dict(name="ksaya-storage-guard"),
            spec=dict(policyName="ksaya-storage-guard",
                      validationActions=["Deny"])))
    return docs


def manifest_text(docs: List[Dict[str, Any]]) -> str:
    return "\n---\n".join(json.dumps(d, ensure_ascii=False) for d in docs) + "\n"


def manifest_fingerprint(docs: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(manifest_text(docs).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 真实集群 Env：K8s 原生 Job + termination message，零自研 RPC
# ---------------------------------------------------------------------------

AWK_SHARD = r'''awk -v lo=$LO -v hi=$HI -v run=$RUN -v gen=$GEN -v id=$ID -v uid=$UID 'BEGIN{pc=0;ps=0;pf=0;pl=0;dc=0;for(x=lo;x<=hi;x++){if(x<2){dc+=1;continue}p=1;for(d=2;d*d<=x;d++)if(x%d==0){p=0;break}if(p){pc++;ps+=x;if(pf==0)pf=x;pl=x}cnt=2;for(d=2;d*d<=x;d++)if(x%d==0){cnt+=1;q=x/d;if(q!=d)cnt+=1}dc+=cnt}printf "KSAYARESULT|%s|%s|%s|%d|%d|%.0f|%.0f|%.0f|%.0f|%.0f|%s\n",run,gen,id,lo,hi,pc,ps,pf,pl,dc,uid}' > /dev/termination-log'''

AWK_VERIFY = r'''awk -v n=$N -v run=$RUN 'BEGIN{lo=2;hi=n;sz=hi-lo+1;for(i=0;i<sz;i++)c[i]=0;lim=int(sqrt(hi));for(p=2;p<=lim;p++){st=p*p;if(st<lo)st=int((lo+p-1)/p)*p;for(m=st;m<=hi;m+=p)c[m-lo]=1}pc=0;ps=0;pf=0;pl=0;for(i=0;i<sz;i++)if(c[i]==0){x=lo+i;pc++;ps+=x;if(pf==0)pf=x;pl=x}dc=0;for(d=1;d<=hi;d++)dc+=int(hi/d)-int((lo-1)/d);printf "KSAYAVERIFY|%s|%.0f|%.0f|%.0f|%.0f|%.0f\n",run,pc,ps,pf,pl,dc}' > /dev/termination-log'''


def job_manifest(cfg: Dict[str, Any], job: Dict[str, Any], script: str,
                 name: Optional[str] = None) -> Dict[str, Any]:
    jc = cfg["features"]["job"]
    ns = cfg["cluster"]["namespace"]
    run = job["run"]
    jname = name or f"ksaya-shard-{run}-{job['shard']}"
    labels = {"ksaya.io/run": run, "ksaya.io/generation": str(cfg["generation"]),
              "ksaya.io/shard": str(job["shard"]), "app": "ksaya-shard"}
    env = [{"name": k, "value": str(v)} for k, v in (
        ("LO", job["lo"]), ("HI", job["hi"]), ("RUN", run),
        ("GEN", job.get("generation", cfg["generation"])),
        ("ID", job["shard"]), ("UID", job.get("uid", "")),
        ("N", job.get("n", job["hi"])))]
    # 智能路由必须真正落到调度上：否则 router 选出的 node 只存在于 ledger 里，
    # Pod 实际落点仍由 k8s 默认调度器决定 —— 那等于没路由。
    # 这里把 ksayacore.Router 的决策固化成 nodeSelector（硬约束）+ 亲和性（软偏好）。
    pod_spec: Dict[str, Any] = dict(restartPolicy="OnFailure",
                                    containers=[dict(
                                        name="shard", image=jc["image"],
                                        imagePullPolicy=jc["imagePullPolicy"],
                                        command=["/bin/sh", "-c", script],
                                        env=env,
                                        resources=dict(
                                            requests={"cpu": "10m", "memory": "8Mi"},
                                            limits={"memory": "64Mi"}))])
    target = job.get("node")
    # 配置里节点用短名（node1），集群里是（vm-ksaya-node1）。nodeSelector 必须写真实节点名，
    # 否则 Pod 会永远 Pending。优先用调用方解析好的 nodeNames 映射，其次回退默认前缀规则。
    name_map = dict(job.get("nodeNames") or {})
    real = name_map.get(target)
    if not real:
        known = [n.get("k8sName") or n["id"] for n in cfg.get("nodes", [])]
        if target in known:
            real = target
        elif any(target and target in k for k in known):
            real = sorted(k for k in known if target in k)[0]
    if real:
        pod_spec["nodeSelector"] = {"kubernetes.io/hostname": real}
        pod_spec["affinity"] = dict(
            nodeAffinity=dict(
                preferredDuringSchedulingIgnoredDuringExecution=[dict(
                    weight=100,
                    preference=dict(matchExpressions=[dict(
                        key="ksaya.io/shard-prefer", operator="In", values=[real])]))]))
    return dict(apiVersion="batch/v1", kind="Job",
                metadata=dict(name=jname, namespace=ns, labels=labels),
                spec=dict(backoffLimit=jc["backoffLimit"],
                          activeDeadlineSeconds=jc["activeDeadlineSeconds"],
                          ttlSecondsAfterFinished=jc["ttlSecondsAfterFinished"],
                          template=dict(metadata=dict(labels=labels),
                                        spec=pod_spec)))


class RealEnv(core.Env):
    def __init__(self, cfg: Dict[str, Any], run: str, generation: int,
                 faults: Optional[Dict[int, List[str]]] = None,
                 settle_seconds: float = 1.0):
        self.cfg = cfg
        self.run = run
        self.generation = generation
        self.ns = cfg["cluster"]["namespace"]
        self.faults = faults or {}
        self.settle_seconds = settle_seconds
        self.fault_log: List[str] = []
        self._quorum_cache: Tuple[float, bool] = (0.0, True)
        self._node_cache: Tuple[float, Dict[str, bool]] = (0.0, {})
        self.ledger: Any = None  # 由 cmd_verify 挂载，供故障注入读取当前 uid
        self._retry = 0
        self._killed = False  # pod-kill 是否已真实杀到至少一个 Pod
        self.adversarial: Dict[int, Dict[str, Any]] = {}
        # 智能路由：与仿真共用同一份 ksayacore.Router；真实环境额外喂实时负载
        self.router = core.Router(cfg)
        self._load_cache: Tuple[float, Dict[str, Dict[str, float]]] = (0.0, {})
        # node id -> 真实 k8s 节点名（配置里的 id 是短名 node1，集群里是 vm-ksaya-node1）
        self.node_names: Dict[str, str] = {}  # 延迟解析，首次 create_job 时填

    # -- Env 接口 ----------------------------------------------------------
    def now(self) -> int:
        return int(time.time())

    def quorate(self) -> bool:
        now = time.time()
        if now - self._quorum_cache[0] < 5:
            return self._quorum_cache[1]
        try:
            d = kube_json(self.cfg, ["get", "nodes"])
            ready = sum(1 for it in d["items"] if any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in it.get("status", {}).get("conditions", [])))
            ok = ready > len(d["items"]) // 2
        except Exception:
            ok = self._quorum_cache[1]
        self._quorum_cache = (now, ok)
        return ok

    def _resolve_node_names(self) -> Dict[str, str]:
        """把配置里的节点 id 解析成真实 k8s 节点名。

        配置用短名（node1）方便人读，集群里是（vm-ksaya-node1）。
        解析规则：完全相等优先，其次「id 是节点名子串」。
        """
        if self.node_names:
            return self.node_names
        try:
            d = kube_json(self.cfg, ["get", "nodes"])
            names = [it["metadata"]["name"] for it in d.get("items", [])]
        except Exception:
            return {}
        m: Dict[str, str] = {}
        for n in self.cfg.get("nodes", []):
            nid = n["id"]
            if nid in names:
                m[nid] = nid
                continue
            hit = [x for x in names if nid in x]
            if hit:
                m[nid] = sorted(hit)[0]
        self.node_names = m
        return m

    def observe_load(self) -> Dict[str, Dict[str, float]]:
        """采集节点实时内存/CPU 占用，交给 Router 做负载感知降权。

        数据源是 Node.status.allocatable 与 Pod requests 之和的比值 —— 不依赖
        metrics-server（集群里没装），只用 apiserver 已有的字段，零额外组件。
        """
        now = time.time()
        if now - self._load_cache[0] < 5 and self._load_cache[1]:
            return dict(self._load_cache[1])
        names = self._resolve_node_names()
        rev = {v: k for k, v in names.items()}
        try:
            nd = kube_json(self.cfg, ["get", "nodes"])
            pd = kube_json(self.cfg, ["get", "pods", "-A"])
        except Exception:
            return dict(self._load_cache[1])
        reported: Dict[str, Dict[str, float]] = {}
        for it in nd.get("items", []):
            nm = it["metadata"]["name"]
            nid = rev.get(nm)
            if not nid:
                continue
            alloc = it.get("status", {}).get("allocatable", {})
            mem = _parse_quantity_mi(alloc.get("memory"))
            cpu = _parse_quantity_milli(alloc.get("cpu"))
            reported[nid] = {"memTotal": mem, "cpuTotal": cpu,
                             "memUsed": 0.0, "cpuUsed": 0.0}
        if reported:
            for p in pd.get("items", []):
                st = p.get("status", {})
                if st.get("phase") in ("Succeeded", "Failed"):
                    continue
                nid = rev.get(p.get("spec", {}).get("nodeName", ""))
                if nid not in reported:
                    continue
                for cs in p.get("spec", {}).get("containers", []):
                    req = (cs.get("resources", {}) or {}).get("requests", {}) or {}
                    reported[nid]["memUsed"] += _parse_quantity_mi(req.get("memory"))
                    reported[nid]["cpuUsed"] += _parse_quantity_milli(req.get("cpu"))
        out: Dict[str, Dict[str, float]] = {}
        for nid, v in reported.items():
            mt = max(v["memTotal"], 1.0)
            ct = max(v["cpuTotal"], 1.0)
            out[nid] = {"memRatio": v["memUsed"] / mt, "cpuRatio": v["cpuUsed"] / ct}
        self._load_cache = (now, out)
        return dict(out)

    def node_info(self) -> Dict[str, bool]:
        now = time.time()
        if now - self._node_cache[0] < 5 and self._node_cache[1]:
            return dict(self._node_cache[1])
        try:
            d = kube_json(self.cfg, ["get", "nodes"])
            m = {}
            for it in d["items"]:
                nm = it["metadata"]["name"]
                m[nm] = any(c["type"] == "Ready" and c["status"] == "True"
                            for c in it.get("status", {}).get("conditions", []))
                for n in self.cfg["nodes"]:
                    if n["id"] in nm:
                        m[n["id"]] = m[nm]
        except Exception:
            m = self._node_cache[1]
        self._node_cache = (now, m)
        return dict(m)

    def create_job(self, job: Dict[str, Any]) -> bool:
        self._resolve_node_names()
        # 传 id -> 真实节点名 的映射（不是值列表），供 job_manifest 生成 nodeSelector
        job = dict(job, nodeNames=dict(self.node_names))
        doc = job_manifest(self.cfg, job, AWK_SHARD)
        rc, out, err = kube(self.cfg, ["create", "-f", "-"],
                            stdin_text=json.dumps(doc) + "\n")
        if rc == 0:
            return True
        if "AlreadyExists" in (out + err):
            return False
        self.log(f"CREATE-FAILED {job['name']} rc={rc} {(out+err).strip()[:160]}")
        return False

    def job_results(self, run: str) -> Dict[int, Dict[str, Any]]:
        try:
            d = kube_json(self.cfg, ["get", "pods", "-n", self.ns,
                                     "-l", f"ksaya.io/run={run}"])
        except Exception:
            return {}
        out: Dict[int, Dict[str, Any]] = {}
        for k, v in self.adversarial.items():
            out[k] = v
        for pod in d.get("items", []):
            node = pod.get("spec", {}).get("nodeName", "")
            lab = pod.get("metadata", {}).get("labels", {})
            sid = int(lab.get("ksaya.io/shard", -1))
            for cs in pod.get("status", {}).get("containerStatuses", []):
                term = cs.get("state", {}).get("terminated")
                if not term or term.get("exitCode") != 0:
                    continue
                msg = (term.get("message") or "").strip().splitlines()[-1] if term.get("message") else ""
                p = msg.split("|")
                if len(p) != 12 or p[0] != "KSAYARESULT":
                    continue
                item = dict(
                    run=p[1], generation=int(p[2]), shard=int(p[3]),
                    lo=int(p[4]), hi=int(p[5]),
                    metrics=dict(pc=int(float(p[6])), ps=int(float(p[7])),
                                 pf=int(float(p[8])), pl=int(float(p[9])),
                                 dc=int(float(p[10]))),
                    uid=p[11], node=node, exitCode=0)
                if sid not in out:
                    out[sid] = item
                else:
                    k = sid + 1000000
                    while k in out:
                        k += 1000000
                    out[k] = item
        return out

    def delete_job(self, job_name: str) -> None:
        kube(self.cfg, ["delete", "job", "-n", self.ns, job_name,
                        "--ignore-not-found=true", "--wait=false"],
             timeout=60)

    def log(self, msg: str) -> None:
        print("  " + msg, flush=True)

    # -- 真实故障注入（原生 K8s 操作，不用 iptables / tc / watchdog） ------
    def settle(self, tick: int) -> None:
        # 每轮把真实节点负载喂给 Router：接近饱和的节点被降权，
        # 新分片自动流向空闲节点 —— 这就是「多节点拼成一台大电脑」的调度侧体现。
        try:
            self.router.observe_load(self.observe_load())
        except Exception as exc:  # 负载采集失败不得影响收敛
            self.log(f"LOAD-OBSERVE-FAILED {exc}")
        for action in self.faults.get(tick, []):
            self._inject(action, tick)
        if self.settle_seconds:
            time.sleep(self.settle_seconds)

    def _inject(self, action: str, tick: int) -> None:
        ns = self.ns
        if action in ("pod-kill", "pod-kill-retry"):
            rc, out, _ = kube(self.cfg, ["get", "pods", "-n", ns,
                                         "-l", f"ksaya.io/run={self.run}",
                                         "--field-selector=status.phase!=Succeeded",
                                         "-o", "name"], timeout=60)
            names = [x.strip() for x in out.splitlines()
                     if x.strip() and "ksaya-verify-" not in x and "ksaya-stale-" not in x][:6]
            for nm in names:
                kube(self.cfg, ["delete", "-n", ns, nm, "--grace-period=0", "--force",
                                "--wait=false"], timeout=60)
            self.fault_log.append(f"tick pod-kill deleted={len(names)}")
            self.log(f"FAULT pod-kill deleted={len(names)}")
            # 干净环境下分片可能在首次注入前已完成；只要还没真正杀到 Pod，
            # 就逐 tick 重试，保证故障路径被真实执行（而不是 deleted=0 的假通过）。
            if not self._killed:
                if names:
                    self._killed = True
                elif self._retry < 60:
                    self._retry += 1
                    self.faults.setdefault(tick + 1, []).append("pod-kill-retry")
        elif action == "all-pods":
            kube(self.cfg, ["delete", "pods", "-n", ns,
                            "-l", f"ksaya.io/run={self.run}",
                            "--grace-period=0", "--force", "--wait=false"], timeout=60)
            self.fault_log.append("tick all-pods")
            self.log("FAULT all-pods deleted")
        elif action == "node-cordon":
            kube(self.cfg, ["cordon", "vm-ksaya-node1"], timeout=60)
            self.fault_log.append("tick node-cordon vm-ksaya-node1")
            self.log("FAULT node-cordon vm-ksaya-node1")
        elif action == "node-uncordon":
            kube(self.cfg, ["uncordon", "vm-ksaya-node1"], timeout=60)
            self.fault_log.append("tick node-uncordon vm-ksaya-node1")
            self.log("HEAL node-uncordon vm-ksaya-node1")
        elif action == "stale-generation":
            # 用「相同 uid + 旧 generation」造一份结果，端到端验证 fencing token 拒绝
            uid = "deadbeef"
            if self.ledger is not None and 0 in self.ledger.shards:
                uid = self.ledger.shards[0]["uid"] or uid
            stale = dict(shard=0, lo=2, hi=3, run=self.run,
                         generation=self.generation - 1, uid=uid, n=3)
            doc = job_manifest(self.cfg, stale, AWK_SHARD,
                               name=f"ksaya-stale-{self.run}-0")
            kube(self.cfg, ["create", "-f", "-"], stdin_text=json.dumps(doc) + "\n")
            # 真实 Pod 何时完成不可控；同时回放一条同 uid、旧 generation 的结果，
            # 让 fencing 路径在每次运行中都确定地被执行到（replay 型混沌注入）。
            self.adversarial[1000000] = dict(
                run=self.run, generation=self.generation - 1, shard=0,
                lo=2, hi=3, metrics=dict(pc=2, ps=5, pf=2, pl=3, dc=5),
                uid=uid, node="replay", exitCode=0)
            self.fault_log.append(f"tick stale-generation uid={uid} replay=1")
            self.log(f"FAULT stale-generation uid={uid} replay=1")


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def cmd_validate(args, cfg) -> int:
    issues = core.validate_config(cfg)
    try:
        core.assert_no_unknown_fields(cfg)
        unknown: List[str] = []
    except core.ConfigError as exc:
        unknown = [ln.strip(" -") for ln in str(exc).splitlines()[1:]]
        issues += unknown
    res = dict(ok=not issues, issues=issues, unknownFields=unknown,
               schemaVersion=cfg.get("schemaVersion"),
               generation=cfg.get("generation"),
               nodes=len(cfg.get("nodes", [])),
               storage=len(cfg.get("storage", [])))
    emit(res, args.json)
    return 0 if not issues else 1


def applied_state() -> Dict[str, Any]:
    if os.path.exists(APPLIED):
        return json.load(open(APPLIED, encoding="utf-8"))
    return {}


def cmd_plan(args, cfg) -> int:
    docs = desired_manifest(cfg)
    fp = manifest_fingerprint(docs)
    cur = applied_state()
    changed = cur.get("fingerprint") != fp
    text = manifest_text(docs)
    if args.emit:
        with open(args.emit, "w", encoding="utf-8") as f:
            f.write(text)
    res = dict(objects=len(docs),
               kinds={k: sum(1 for d in docs if d["kind"] == k)
                      for k in sorted({d["kind"] for d in docs})},
               fingerprint=fp, applied_fingerprint=cur.get("fingerprint", "none"),
               changed=changed, generation=cfg["generation"],
               emitted=args.emit or "(dry-run, no emit)")
    emit(res, args.json)
    return 0


def verify_applied(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """启动完成握手：回读核心真值源，确认 apply 真的生效。

    源码来源：pmxcfs.c:951-980 —— 父进程只有在 pipe 收到子进程 '1'（表示 fuse_mount、
    server_start、事件循环全部就绪）后才写 pidfile 并宣告启动成功。对应到 KSAYA：
    kubectl apply 返回 0 只代表 API 接受，必须再回读 ConfigMap 摘要一致才算提交。
    """
    ns = cfg["cluster"]["namespace"]
    rc, out, err = kube(cfg, ["get", "configmap", "ksaya-config", "-n", ns,
                              "-o", "jsonpath={.data.config\\.sha256}"], timeout=30)
    if rc != 0:
        return False, f"configmap not readable: {err.strip()[:120] or out.strip()[:120]}"
    want = config_digest(cfg)
    if out.strip() != want:
        return False, f"digest mismatch: got={out.strip()[:16]} want={want[:16]}"
    return True, "configmap digest verified"


def _cmd_apply_locked(args, cfg) -> int:
    core.assert_config(cfg)
    core.assert_no_loopback(cfg)
    docs = desired_manifest(cfg)
    core.assert_structure(docs, cfg)   # dcdb.c 式结构化防线：拒绝空 / 畸形清单
    fp = manifest_fingerprint(docs)
    text = manifest_text(docs)
    ensure_dirs()
    upid = new_upid("apply")
    prev = applied_state()
    task_log(upid, f"BEGIN apply generation={cfg['generation']} fp={fp} dry_run={args.dry_run}")
    if args.dry_run:
        emit(dict(dry_run=True, fingerprint=fp, objects=len(docs)), args.json)
        return 0
    mark_restart()   # 只有显式 clear 才算成功提交；中途被 kill 会留下可检出的标记
    backup = json.dumps(prev, ensure_ascii=False) if prev else ""
    ns_doc = json.dumps(docs[0]) + "\n"
    rc, out0, err0 = kube(cfg, ["apply", "-f", "-"], stdin_text=ns_doc, timeout=90)
    if rc != 0:
        task_log(upid, f"FAIL apply/namespace rc={rc} err={err0.strip()[:200]}")
        print(f"APPLY-FAILED(namespace) rc={rc}\n{out0}\n{err0}", file=sys.stderr)
        return 1
    rest = "\n---\n".join(json.dumps(d, ensure_ascii=False) for d in docs[1:]) + "\n"
    rc, out, err = kube(cfg, ["apply", "-f", "-"], stdin_text=rest, timeout=180)
    out = out0 + out
    if rc != 0:
        task_log(upid, f"FAIL apply/objects rc={rc} err={err.strip()[:200]}")
        print(f"APPLY-FAILED rc={rc}\n{out}\n{err}", file=sys.stderr)
        return 1
    # 启动完成握手：先回读验证，再提交本地事务态（避免"说成功但没生效"）
    ok, detail = verify_applied(cfg)
    if not ok:
        task_end(upid, False, f"post-apply verification failed: {detail}")
        print(f"APPLY-VERIFY-FAILED: {detail}", file=sys.stderr)
        return 1
    # 事务提交：只有 apply 且回读验证都成功才写历史与当前状态
    atomic_write(os.path.join(HISTORY_DIR, f"g{cfg['generation']}-{fp}.yaml"), text)
    state = dict(fingerprint=fp, generation=cfg["generation"],
                 applied_at=time.strftime("%F %T"), upid=upid,
                 previous=prev or None)
    atomic_write(APPLIED, json.dumps(state, ensure_ascii=False, indent=2))
    clear_restart()
    task_log(upid, f"OK apply objects={len(docs)} backup_had_prev={bool(backup)} verify={detail}")
    task_end(upid, True, f"apply generation={cfg['generation']} objects={len(docs)}")
    emit(dict(ok=True, fingerprint=fp, objects=len(docs),
              generation=cfg["generation"], upid=upid,
              verification=detail,
              output=out.strip().splitlines()[:20]), args.json)
    return 0


def cmd_apply(args, cfg) -> int:
    """apply 的唯一入口：全程持有单实例操作锁。"""
    with op_lock():
        return _cmd_apply_locked(args, cfg)


def cmd_rollback(args, cfg) -> int:
    prev = applied_state().get("previous")
    if not prev:
        print("ROLLBACK-NO-PREVIOUS: nothing to roll back", file=sys.stderr)
        return 1
    fp = prev.get("fingerprint")
    path = os.path.join(HISTORY_DIR, f"g{prev.get('generation')}-{fp}.yaml")
    if not os.path.exists(path):
        print(f"ROLLBACK-NO-SNAPSHOT: {path}", file=sys.stderr)
        return 1
    text = open(path, encoding="utf-8").read()
    rc, out, err = kube(cfg, ["apply", "-f", "-"], stdin_text=text, timeout=180)
    upid = new_upid("rollback")
    if rc != 0:
        task_log(upid, "FAIL rollback")
        print(f"ROLLBACK-FAILED\n{out}\n{err}", file=sys.stderr)
        return 1
    atomic_write(APPLIED, json.dumps(
        dict(prev, rolled_back_at=time.strftime("%F %T"), upid=upid),
        ensure_ascii=False, indent=2))
    task_log(upid, f"OK rollback to generation={prev.get('generation')} fp={fp}")
    emit(dict(ok=True, restored_generation=prev.get("generation"), fingerprint=fp), args.json)
    return 0


def cmd_status(args, cfg) -> int:
    cur = applied_state()
    docs = desired_manifest(cfg)
    res = dict(config_generation=cfg["generation"],
               applied_generation=cur.get("generation", "never"),
               applied_fingerprint=cur.get("fingerprint", "none"),
               desired_fingerprint=manifest_fingerprint(docs),
               drift=cur.get("fingerprint") != manifest_fingerprint(docs),
               applied_at=cur.get("applied_at", "never"))
    emit(res, args.json)
    return 0


def _cmd_doctor_locked(args, cfg) -> int:
    """运行时不变量断言 + 配置漂移检测（Talos 式：漂移本身就是故障）。"""
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append(dict(check=name, ok=bool(ok), detail=detail))

    issues = core.validate_config(cfg)
    add("config-schema", not issues, ";".join(issues[:3]))
    try:
        core.assert_no_unknown_fields(cfg)
        add("no-unknown-fields", True, "all keys whitelisted")
    except core.ConfigError as exc:
        add("no-unknown-fields", False, ";".join(str(exc).splitlines()[1:3]))
    docs = desired_manifest(cfg)
    struct = core.validate_structure(docs, cfg)   # dcdb.c 式结构化输入防线
    add("manifest-structure", not struct, ";".join(struct[:3]))
    age = restart_flag_age()                      # pmxcfs.c RESTART_FLAG_FILE
    add("no-restart-residue", age is None,
        f"flag_age={age}s" if age is not None else "clean")
    drift = applied_state().get("fingerprint") != manifest_fingerprint(docs)
    add("no-config-drift", not drift, "applied == desired")
    try:
        d = kube_json(cfg, ["get", "nodes"])
        ready = sum(1 for it in d["items"] if any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in it.get("status", {}).get("conditions", [])))
        add("nodes-ready", ready == len(cfg["nodes"]), f"{ready}/{len(cfg['nodes'])}")
    except Exception as exc:  # noqa: BLE001
        add("nodes-ready", False, str(exc)[:120])
    runtime_tampered = False
    try:
        cm = kube_json(cfg, ["get", "configmap", "-n", cfg["cluster"]["namespace"], "ksaya-config"])
        want = config_digest(cfg)
        runtime_tampered = cm["data"].get("config.sha256") != want
        add("runtime-config-untampered", not runtime_tampered,
            "ConfigMap sha256 matches config")
    except Exception as exc:  # noqa: BLE001
        runtime_tampered = True
        add("runtime-config-untampered", False, str(exc)[:120])
    rc, out, _ = remote(cfg,
        "printf 'rules=%s\\n' \"$(iptables-save 2>/dev/null | grep -cE 'KSAYA-(CHAOS|RB|FF|BW)' || true)\"; "
        "printf 'tc=%s\\n' \"$(tc qdisc show 2>/dev/null | grep -cE 'tbf|netem' || true)\"; "
        "printf 'timers=%s\\n' \"$(systemctl list-timers --all --no-legend 2>/dev/null | grep -c ksaya || true)\"",
        timeout=60)
    vals = dict(x.split("=", 1) for x in out.strip().splitlines() if "=" in x)
    for k, label in (("rules", "no-iptables-residue"), ("tc", "no-tc-residue"),
                     ("timers", "no-watchdog-residue")):
        add(label, vals.get(k, "0").strip() in ("0", ""), f"{k}={vals.get(k, '?')}")
    needs_heal = drift or runtime_tampered
    if args.heal and needs_heal:
        docs = desired_manifest(cfg)
        r0, _, _ = kube(cfg, ["apply", "-f", "-"],
                        stdin_text=json.dumps(docs[0]) + "\n", timeout=90)
        r1, _, e1 = kube(cfg, ["apply", "-f", "-"],
                         stdin_text="\n---\n".join(json.dumps(d) for d in docs[1:]) + "\n",
                         timeout=180)
        healed = r0 == 0 and r1 == 0
        if healed:
            atomic_write(APPLIED, json.dumps(
                dict(fingerprint=manifest_fingerprint(docs),
                     generation=cfg["generation"],
                     applied_at=time.strftime("%F %T"),
                     upid=new_upid("heal")), indent=2))
        task_end(new_upid("doctor-heal"), healed, f"drift_healed={healed} err={e1.strip()[:100]}")
        checks = [c for c in checks if c["check"] not in ("no-config-drift", "runtime-config-untampered")]
        add("no-config-drift", healed, "auto-healed" if healed else "heal failed")
        add("runtime-config-untampered", healed, "sha256 restored" if healed else "heal failed")
    failed = [c for c in checks if not c["ok"]]
    if args.json:
        emit(dict(ok=not failed, checks=checks, failed=len(failed)), True)
    else:
        for c in checks:
            print(f"{'PASS' if c['ok'] else 'FAIL'}  {c['check']:30s} {c['detail']}")
        print(f"DOCTOR-SUMMARY pass={len(checks)-len(failed)} fail={len(failed)}")
    return 0 if not failed else 1


def cmd_doctor(args, cfg) -> int:
    """doctor 与 --heal 全程持有单实例操作锁。"""
    with op_lock():
        return _cmd_doctor_locked(args, cfg)


def cmd_bandwidth(args, cfg) -> int:
    """系统层带宽策略：查档位 / 渲染 tc 规则 / 下发到节点。

    这是取代原「语义压缩」的方案 —— 不在应用层压小流量，
    而是由调度侧按作业 tier 直接分配带宽额度，用 tc 强制执行。
    """
    spec = bandwidth.resolve_tier(cfg, args.tier)
    if args.ceiling is not None:
        spec = bandwidth.apply_node_ceiling(cfg, spec, args.ceiling)
    plan_ = bandwidth.plan(cfg, args.tier, iface=args.iface,
                           direction=args.direction,
                           node_limit_kbit=args.ceiling)
    if args.json:
        emit(plan_, True)
        return 0
    print(f"档位 {plan_['tier']}: "
          f"限速 {plan_['limitKbit']}kbit  "
          f"延迟 {plan_['latencyMs']}ms  "
          f"丢包 {plan_['lossPct']}%"
          + (f"  (被节点上限收敛, cappedBy={plan_['cappedBy']})"
             if plan_.get("cappedBy") else ""))
    if args.show:
        for c in plan_["commands"]:
            print("  " + c)
    if args.apply:
        import subprocess as sp
        node = args.node or _default_node_ip(cfg)
        host = f"root@{node}"
        for c in plan_["commands"]:
            r = sp.run(["ssh", "-o", "StrictHostKeyChecking=no", "-o",
                        "ConnectTimeout=8", host, c],
                       text=True, capture_output=True)
            if r.returncode != 0 and "Error" in (r.stderr or ""):
                print(f"  [FAIL] {c}: {r.stderr.strip()[:160]}", file=sys.stderr)
                return 4
        print(f"  已下发到 {host}")
    return 0


def _default_node_ip(cfg) -> str:
    nodes = (cfg or {}).get("nodes") or []
    for n in nodes:
        if isinstance(n, dict) and n.get("role") == "worker" and n.get("ip"):
            return str(n["ip"])
    for n in nodes:
        if isinstance(n, dict) and n.get("ip"):
            return str(n["ip"])
    return "127.0.0.1"


def cmd_sim(args, cfg) -> int:
    """内置基线场景模拟（场景文件已随混沌测试模块一并移除）。

    保留模拟器本身的价值：它是纯本地的状态机/不变量验证器，
    不依赖集群，可作为回归的一部分。
    """
    scenario = {
        "name": "builtin-baseline",
        "nodes": ["n1", "n2", "n3"],
        "actions": [
            {"tick": 1, "type": "submit", "count": 8},
            {"tick": 3, "type": "lease", "count": 4},
            {"tick": 5, "type": "submit", "count": 4},
            {"tick": 8, "type": "harvest"},
        ],
        "expect": {"complete": True},
    }
    r = core.simulate(cfg, scenario)
    exp = scenario.get("expect", {})
    ok = (not r["violations"] and r["summary"]["converged"]
          and r["summary"]["harvested"] == r["summary"]["total"]
          and (not exp.get("complete") or r["accepted"] == r["summary"]["total"]))
    if args.json:
        emit(r, True)
        return 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {r['scenario']:30s} "
          f"ticks={r['summary']['ticks']:3d} "
          f"harvest={r['summary']['harvested']}/{r['summary']['total']} "
          f"violations={r['violations']} fp={r['fingerprint']}")
    print(f"SIM-SUMMARY total=1 pass={1 if ok else 0} fail={0 if ok else 1}")
    return 0 if ok else 1


def cmd_test(args, cfg) -> int:
    p = subprocess.run([sys.executable, "-m", "unittest", "discover",
                        "-s", os.path.join(ROOT, "tests"), "-p", "test_*.py", "-v"],
                       cwd=ROOT, text=True, capture_output=True)
    print(p.stderr.strip())
    if p.stdout.strip():
        print(p.stdout.strip()[-1500:])
    print(f"TEST-SUMMARY rc={p.returncode}")
    return p.returncode


def cmd_task(args, cfg) -> int:
    ensure_dirs()
    if not os.path.exists(TASK_LOG):
        print("no tasks yet")
        return 0
    lines = open(TASK_LOG, encoding="utf-8").read().splitlines()
    if args.status:
        act = task_active(args.deadline)
        for b in act:
            print(f"{b['state']:8s} age={b['age_seconds']:6d}s {b['upid']}")
        print(f"TASK-STATUS active={len(act)} "
              f"stuck={sum(1 for b in act if b['stuck'])} "
              f"terminal={len(set(x.split()[2] for x in lines if 'UPID:' in x)) - len(act)}")
        return 0
    if args.stuck:
        for b in task_active(args.deadline):
            if b["stuck"]:
                print(f"STUCK age={b['age_seconds']}s {b['upid']}")
        return 0
    if args.show:
        for ln in lines:
            if args.show in ln:
                print(ln)
    elif args.list or True:
        for ln in lines[-args.limit:]:
            print(ln)
    return 0


def cmd_verify(args, cfg) -> int:
    """真实集群端到端：原生 Job 分片 + 三重独立校验 + 原生故障注入 + 零人工干预收敛。"""
    core.assert_config(cfg)
    run = f"v{int(time.time()) % 100000000}"
    n = args.n or cfg["features"]["job"]["defaultN"]
    chunk = args.chunk or cfg["features"]["job"]["chunk"]
    shards = core.make_shards(n, chunk)
    sm = core.StateMachine("shard", cfg["stateMachines"]["shard"])
    ledger = core.Ledger(run, cfg["generation"], shards)
    faults: Dict[int, List[str]] = {}
    if args.fault == "pod-kill":
        # tick 1 即注入：干净环境下分片完成很快，注入太晚会杀不到 Pod
        faults = {1: ["pod-kill"]}
    elif args.fault == "all-pods":
        faults = {3: ["all-pods"]}
    elif args.fault == "node-cordon":
        faults = {2: ["node-cordon"], 8: ["node-uncordon"]}
        # 干净环境下作业可能在 tick 8 前就收敛，导致 uncordon 不执行、
        # 节点残留 SchedulingDisabled。收敛后必须无条件恢复调度。
    elif args.fault == "stale-generation":
        # 必须在任何分片结果产生之前注入，否则防御路径可能已被绕过
        faults = {1: ["stale-generation"]}
    env = RealEnv(cfg, run, cfg["generation"], faults=faults,
                  settle_seconds=args.settle)
    env.ledger = ledger
    upid = new_upid("verify")
    task_log(upid, f"BEGIN verify run={run} n={n} chunk={chunk} shards={len(shards)} fault={args.fault}")
    print(f"VERIFY start run={run} shards={len(shards)} fault={args.fault}", flush=True)
    t0 = time.time()
    try:
        summary = core.reconcile(env, cfg, ledger, sm, max_ticks=args.max_ticks)
    finally:
        # 故障注入的可逆性保证：无论收敛/超时/异常，cordon 必须解除，
        # 不允许把 SchedulingDisabled 残留给下一次运行或真实负载。
        if args.fault == "node-cordon":
            kube(cfg, ["uncordon", "vm-ksaya-node1"], timeout=60)
    wall = int(time.time() - t0)

    bad = core.check_invariants(cfg, ledger, summary)
    got = core.aggregate(v["metrics"] for v in ledger.accepted.values())
    ref = core.aggregate(core.reference_shard(s["lo"], s["hi"]) for s in shards)
    if got != ref:
        bad.append(f"third-party-reference-mismatch got={got} ref={ref}")

    # 第三重：集群内独立算法（分段筛 + 反转因子枚举）整域复算
    verify_ok = False
    vjob = dict(shard=999999, lo=2, hi=n, run=run, n=n, uid="verify")
    doc = job_manifest(cfg, vjob, AWK_VERIFY, name=f"ksaya-verify-{run}")
    rc, out, err = kube(cfg, ["create", "-f", "-"], stdin_text=json.dumps(doc) + "\n", timeout=90)
    if rc == 0 or "AlreadyExists" in (out + err):
        for _ in range(30):
            d = kube_json(cfg, ["get", "pods", "-n", cfg["cluster"]["namespace"],
                                "-l", f"ksaya.io/run={run},ksaya.io/shard=999999"])
            for pod in d.get("items", []):
                for cs in pod.get("status", {}).get("containerStatuses", []):
                    term = cs.get("state", {}).get("terminated")
                    if term and term.get("exitCode") == 0:
                        msg = (term.get("message") or "").strip().splitlines()[-1]
                        p = msg.split("|")
                        if len(p) == 7 and p[0] == "KSAYAVERIFY":
                            inv = dict(pc=int(float(p[2])), ps=int(float(p[3])),
                                       pf=int(float(p[4])), pl=int(float(p[5])),
                                       dc=int(float(p[6])))
                            verify_ok = inv == ref
                            print(f"IN-CLUSTER-VERIFY {inv} match={verify_ok}", flush=True)
            if verify_ok:
                break
            time.sleep(2)
    if not verify_ok:
        bad.append("in-cluster-independent-verifier-failed")

    nodes_used = sorted({v["node"] for v in ledger.accepted.values()})
    res = dict(run=run, n=n, chunk=chunk, wall=wall,
               shards=summary["total"], harvested=summary["harvested"],
               converged=summary["converged"], ticks=summary["ticks"],
               fault=args.fault, fault_log=env.fault_log,
               nodes_used=nodes_used,
               triple_verify=dict(shard_awk=got == ref,
                                  python_reference=got == ref,
                                  in_cluster_sieve=verify_ok),
               fencing_rejected=list(ledger.fence.rejected),
               violations=bad, upid=upid)
    task_log(upid, f"END verify wall={wall}s converged={summary['converged']} violations={len(bad)}")
    task_end(upid, not bad, f"verify run={run} violations={len(bad)}")
    emit(res, args.json)
    print(f"VERIFY-SUMMARY run={run} wall={wall}s harvested={summary['harvested']}/{summary['total']} "
          f"converged={summary['converged']} violations={len(bad)} nodes={len(nodes_used)}", flush=True)
    return 0 if not bad and summary["converged"] else 1


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="ksayactl", description="KSAYA 稳定核心唯一入口")
    # 版本可被 Nix 包覆盖（安装时写入 KSAYA_VERSION），便于 ksaya-selftest 自报
    ap.add_argument("--version", action="version",
                    version=f"ksayactl {os.environ.get('KSAYA_VERSION', '0-unstable')}")
    ap.add_argument("--config", default=CONFIG_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, json_flag=True):
        if json_flag:
            p.add_argument("--json", action="store_true")

    p = sub.add_parser("validate"); common(p)
    p = sub.add_parser("plan"); common(p); p.add_argument("--emit")
    p = sub.add_parser("apply"); common(p); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("rollback"); common(p)
    p = sub.add_parser("status"); common(p)
    p = sub.add_parser("doctor"); common(p); p.add_argument("--heal", action="store_true")
    p = sub.add_parser("sim"); common(p); p.add_argument("--scenario", default="all")
    p = sub.add_parser("test"); common(p)
    p = sub.add_parser("task"); common(p, False)
    p.add_argument("--list", action="store_true"); p.add_argument("--show")
    p.add_argument("--status", action="store_true"); p.add_argument("--stuck", action="store_true")
    p.add_argument("--deadline", type=int, default=300)
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("bandwidth"); common(p)
    p.add_argument("--tier", default=None, help="作业带宽档位")
    p.add_argument("--iface", default="eth0")
    p.add_argument("--direction", default="egress", choices=["egress", "ingress"])
    p.add_argument("--ceiling", type=int, default=None, help="节点总上限 kbit")
    p.add_argument("--node", default=None, help="下发目标节点 IP")
    p.add_argument("--show", action="store_true", help="打印 tc 命令")
    p.add_argument("--apply", action="store_true", help="下发到节点执行")
    p = sub.add_parser("verify"); common(p)
    p.add_argument("--n", type=int); p.add_argument("--chunk", type=int)
    p.add_argument("--fault", choices=["none", "pod-kill", "all-pods",
                                        "node-cordon", "stale-generation"], default="none")
    p.add_argument("--settle", type=float, default=1.0)
    p.add_argument("--max-ticks", type=int, default=120)

    args = ap.parse_args()
    if args.cmd == "task":
        return cmd_task(args, load_config(args.config))
    cfg = load_config(args.config)
    handlers = dict(validate=cmd_validate, plan=cmd_plan, apply=cmd_apply,
                    rollback=cmd_rollback, status=cmd_status, doctor=cmd_doctor,
                    sim=cmd_sim, test=cmd_test, verify=cmd_verify,
                    bandwidth=cmd_bandwidth)
    try:
        return handlers[args.cmd](args, cfg)
    except core.ConfigError as exc:
        print(f"CONFIG-ERROR {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
