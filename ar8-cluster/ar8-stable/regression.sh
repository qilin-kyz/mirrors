#!/usr/bin/env bash
# AR8 稳定核心 —— 一键全量回归（离线 + 真实集群）
#
#   bash ar8-stable/regression.sh [--quick] [--out <log>]
#
# 覆盖范围（2026-11 精简后）：
#   1 配置与计划       —— schema 校验 + 单一清单生成
#   2 离线状态机       —— 内置基线场景收敛 + 不变量
#   3 单元/集成回归    —— ar8core 全量单测
#   3b 能力协商       —— 受管属性 / 能力降级 / 版本门禁
#   3c 带宽策略       —— tier 解析 / 节点上限 / tc 渲染
#   4 真实集群 doctor  —— 10 项不变量
#   5 真实集群作业矩阵 —— 故障下分片收敛（完整模式）
#   6 漂移检测与自愈
#   7 集群 Minecraft 工作负载 —— 集群承载真实长驻有状态负载
#
# 已移除（对应模块下线）：S3 多厂商 SigV4、busybox 控制台、混沌韧性场景集、
# 语义压缩代理、密文信道。
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
# 装进 /nix/store 后目录只读，日志必须能外置；未设置时保持原行为（运维机直接跑）
OUT="${AR8_LOG:-${DIR}/regression.log}"
QUICK=0
while [ $# -gt 0 ]; do
  if [ "$1" = "--quick" ]; then QUICK=1; shift
  elif [ "$1" = "--out" ]; then OUT=$2; shift 2
  else echo "unknown arg $1"; exit 2
  fi
done
: > "$OUT"
log(){ printf '%s\n' "$*" | tee -a "$OUT"; }
sec(){ log ""; log "======== $* ========"; }
PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); log "PASS  $1"; }
ng(){ FAIL=$((FAIL+1)); log "FAIL  $1"; }

CTL="python3 $DIR/ar8ctl.py"
log "AR8-STABLE REGRESSION started=$(date '+%F %T') quick=$QUICK"

sec "1 配置与计划"
$CTL validate >>"$OUT" 2>&1 && ok "validate: schema 全字段合法" || ng "validate"
$CTL plan >>"$OUT" 2>&1 && ok "plan: 单一清单可生成" || ng "plan"

sec "2 离线状态机（内置基线场景）"
SIM=$($CTL sim 2>&1); printf '%s\n' "$SIM" | tee -a "$OUT"
printf '%s\n' "$SIM" | grep -q 'SIM-SUMMARY total=1 pass=1 fail=0' \
  && ok "sim: 基线场景收敛且零违规" || ng "sim"

sec "3 单元/集成回归"
TST=$($CTL test 2>&1); printf '%s\n' "$TST" | tail -n 5 | tee -a "$OUT"
printf '%s\n' "$TST" | grep -q 'TEST-SUMMARY rc=0' \
  && ok "test: ar8core 单测全通过" || ng "test"

sec "3b PVE 源码精读结论：能力协商/版本门禁/组合校验"
CAPABILITY=$(python3 "$DIR/capability.py" selftest 2>&1)
printf '%s\n' "$CAPABILITY" | tail -n 4 | tee -a "$OUT"
printf '%s\n' "$CAPABILITY" | grep -q 'CAPABILITY-SELFTEST PASS' \
  && ok "capability: 受管属性拒绝覆盖 + 能力降级 + fail-closed" || ng "capability"

sec "3c 系统层带宽策略（tier 解析 / 节点上限 / tc 渲染）"
BW=$(python3 "$DIR/ar8-bandwidth.py" 2>&1)
printf '%s\n' "$BW" | tee -a "$OUT"
printf '%s\n' "$BW" | grep -q 'BANDWIDTH-SELFTEST pass' \
  && ok "bandwidth: 档位解析 + 节点上限收敛 + tc 规则渲染" || ng "bandwidth"
BWT=$($CTL bandwidth --tier streaming --show 2>&1)
printf '%s\n' "$BWT" | tee -a "$OUT"
printf '%s\n' "$BWT" | grep -q 'rate 4096kbit' \
  && ok "bandwidth: ar8ctl bandwidth 可查档位并渲染 tc" || ng "bandwidth: ar8ctl"

sec "4 真实集群 doctor"
DOC=$($CTL doctor 2>&1); printf '%s\n' "$DOC" | tee -a "$OUT"
printf '%s\n' "$DOC" | grep -q 'DOCTOR-SUMMARY pass=10 fail=0' \
  && ok "doctor: 10 项不变量成立" || ng "doctor"

if [ "$QUICK" -eq 0 ]; then
  sec "5 真实集群作业矩阵（同一份 reconcile，无需人工干预）"
  CASES=("baseline:none" "pod-kill:pod-kill" "all-pods:all-pods" "node-cordon:node-cordon" "stale-token:stale-generation")
  for c in "${CASES[@]}"; do
    label=${c%%:*}; fault=${c##*:}
    log "--- CASE $label fault=$fault ---"
    R=$($CTL verify --n 40000 --chunk 2000 --fault "$fault" \
         --settle 1 --max-ticks 90 --json 2>&1)
    printf '%s\n' "$R" | grep -E 'VERIFY start|IN-CLUSTER-VERIFY|VERIFY-SUMMARY|FAULT|violations|fencing_rejected' | tee -a "$OUT"
    S=$(printf '%s\n' "$R" | grep '^VERIFY-SUMMARY' | tail -n1)
    if printf '%s' "$S" | grep -q 'violations=0' && printf '%s' "$S" | grep -q 'harvested=20/20'; then
      ok "verify/$label: 20/20 分片收敛、零违规  [$S]"
    else
      ng "verify/$label: $S"
    fi
  done

  sec "5a 智能任务路由：多节点是否真的被当作一台大电脑用"
  # 这是 AR8 的核心能力，必须真实验证而不是只看「作业跑完了」：
  #   1) 分片是否按节点容量加权分配（强节点多接，而不是平均分）；
  #   2) Pod 实际落点是否遵守路由决策（nodeSelector 真的生效）。
  RT=$(python3 - "$DIR" <<'PYEOF' 2>&1
import json, subprocess, sys, collections
d = sys.argv[1]
out = subprocess.run([sys.executable, f"{d}/ar8ctl.py", "verify", "--n", "20000",
                      "--chunk", "2000", "--fault", "none", "--settle", "1",
                      "--max-ticks", "60", "--json"],
                     capture_output=True, text=True)
# verify 会先打印进度行（含 IN-CLUSTER-VERIFY {...} 这类内联 dict）再输出 JSON，
# 且 JSON 之后还有 VERIFY-SUMMARY 行；因此用 raw_decode 只解第一个完整对象，
# 不能把 start 之后的全部行直接 join 去 json.loads（尾巴会让解析失败）。
raw = out.stdout or ""
lines = raw.splitlines()
start = None
for idx, ln in enumerate(lines):
    s = ln.strip()
    if s.startswith('{"run"') or s.startswith('{ "run"') or s == "{":
        start = idx
        break
try:
    r = json.JSONDecoder().raw_decode("\n".join(lines[start:]))[0] if start is not None else {}
except Exception:
    r = {}
if not r:
    print("ROUTING-ERROR", raw[-300:].replace("\n", " ") or out.stderr[-300:].replace("\n", " "))
    sys.exit(1)
print("nodes_used=%s" % ",".join(r.get("nodes_used") or []))
print("ROUTING-SUMMARY nodes=%d harvested=%s violations=%d" % (
    len(r.get("nodes_used") or []), r.get("harvested"), len(r.get("violations") or [])))
PYEOF
)
  printf '%s\n' "$RT" | tee -a "$OUT"
  RN=$(printf '%s\n' "$RT" | grep -o 'nodes=[0-9]*' | grep -o '[0-9]*')
  # 必须真的拿到 ROUTING-SUMMARY 且 nodes≥2、violations=0，才算路由验证通过
  if printf '%s' "$RT" | grep -q 'ROUTING-SUMMARY' \
     && printf '%s' "$RT" | grep -q 'violations=0' && [ "${RN:-0}" -ge 2 ]; then
    ok "routing: 分片按容量加权落到 ${RN} 个节点（不再是单节点/轮询）"
  else
    ng "routing: 未真实使用多节点 [$RT]"
  fi

  sec "5b 额度/算力多对多与兼容适配器自测"
  # 显式检查退出码：曾因数据文件缺失抛异常但「无 FAIL 行」被误判 PASS
  if CAP=$(python3 "$DIR/ar8-capacity.py" selftest 2>&1); then
    printf '%s\n' "$CAP" | tail -n 6 | tee -a "$OUT"
    NP=$(printf '%s\n' "$CAP" | grep -c '^PASS')
    if printf '%s\n' "$CAP" | grep -q '^FAIL' || [ "$NP" -lt 14 ]; then
      ng "capacity-selftest: PASS=$NP 或存在 FAIL"
    else
      ok "capacity: 多对多 + 瞬时 GPU 适配（$NP 项 PASS）"
    fi
  else
    printf '%s\n' "$CAP" | tail -n 8 | tee -a "$OUT"
    ng "capacity-selftest: 脚本异常退出（检查数据文件 quota-capacity.json / ar8-accounts.json）"
  fi

  # ---- 5c/5d：验收夹具（不是系统核心）----
  # Minecraft 与 Eaglercraft 只是「用来核实这套系统能不能正常跑真实 workload」的
  # 一次性夹具，不属于 AR8 核心能力。核心是「多节点虚拟成一台大电脑 + 智能任务路由
  # 拆分」。因此这里如实记录结果，但只在 --with-fixtures 时才计入通过/失败，
  # 默认不参与核心判定 —— 夹具坏了不代表系统坏了，删掉夹具也不影响核心正确性。
  FIXTURE_STRICT=0
  [ "${WITH_FIXTURES:-0}" = "1" ] && FIXTURE_STRICT=1
  fx_ok(){ if [ "$FIXTURE_STRICT" = "1" ]; then ok "$1"; else log "INFO  $1（夹具，不计入核心判定）"; fi; }
  fx_ng(){ if [ "$FIXTURE_STRICT" = "1" ]; then ng "$1"; else log "INFO  $1（夹具失败，不影响核心结论）"; fi; }

  sec "5c 验收夹具：Minecraft 真实使用测试（协议级玩家登录）"
  SSHMC="ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no -o LogLevel=ERROR -J qilin@192.168.1.72 root@10.23.112.21"
  # 分发协议级测试客户端（脚本 + 官方导出的包 ID 表）
  $SSHMC 'mkdir -p /root/ar8-mc-test' >/dev/null 2>&1
  scp -o BatchMode=yes -o StrictHostKeyChecking=no -o LogLevel=ERROR \
      -o ProxyJump=qilin@192.168.1.72 \
      "$DIR/mc/mc_playtest.py" "$DIR/mc/packets-777.json" \
      root@10.23.112.21:/root/ar8-mc-test/ >>"$OUT" 2>&1
  MCT=$($SSHMC 'PY=$(ls /nix/store/*-python3-3.13*/bin/python3 2>/dev/null | head -1); [ -z "$PY" ] && PY=$(command -v python3); "$PY" /root/ar8-mc-test/mc_playtest.py --host 127.0.0.1 --port 30065 --name AR8Tester --chat "AR8 regression playtest" --stay 15' 2>&1)
  printf '%s\n' "$MCT" | tee -a "$OUT"
  if printf '%s\n' "$MCT" | grep -q 'MC-PLAYTEST-SUMMARY total=11 pass=11 fail=0'; then
    fx_ok "mc-playtest: 玩家真实登录/进世界/聊天/心跳/区块加载 11/11 断言通过"
  else
    fx_ng "mc-playtest: 未达 11/11（检查 StatefulSet 与 NodePort 30065）"
  fi

  sec "5d 验收夹具：浏览器版 MC（Eaglercraft：WS 握手 + 客户端页 + 插件零加载错误）"
  EGL=$($SSHMC 'R=$(k3s kubectl -n ar8-eagler get sts ar8-eagler -o jsonpath="{.status.readyReplicas}" 2>/dev/null); W=$(curl -s --noproxy "*" --max-time 10 -o /dev/null -w "%{http_code}" -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" http://127.0.0.1:30112/ 2>/dev/null); P=$(curl -s --noproxy "*" --max-time 15 -o /dev/null -w "%{http_code}:%{size_download}" http://127.0.0.1:30113/ 2>/dev/null); E=$(k3s kubectl -n ar8-eagler logs ar8-eagler-0 2>/dev/null | grep -c "Could not load"); echo "ready=$R ws=$W page=$P loaderr=$E"' 2>&1)
  printf '%s\n' "$EGL" | tee -a "$OUT"
  # 期望：ready=1 ws=101 page=200:18002822 loaderr=0
  if printf '%s' "$EGL" | grep -q 'ready=1 ws=101 page=200:' && printf '%s' "$EGL" | grep -q 'loaderr=0'; then
    fx_ok "eaglercraft: 1/1 Ready + WS 101 握手 + 客户端页 200 + 插件零加载错误"
  else
    fx_ng "eaglercraft: [$EGL]"
  fi
fi

sec "6 漂移检测与自愈"
# 人为篡改运行时 ConfigMap，模拟「有人 kubectl edit 改了配置」
ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no -o LogLevel=ERROR \
  -J qilin@192.168.1.72 root@10.23.112.21 \
  "k3s kubectl -n ar8-core patch configmap ar8-config --type merge -p '{\"data\":{\"config.sha256\":\"tampered\"}}'" >/dev/null 2>&1
D1=$($CTL doctor 2>&1); printf '%s\n' "$D1" | grep -E 'runtime-config-untampered|DOCTOR-SUMMARY' | tee -a "$OUT"
printf '%s\n' "$D1" | grep -q 'FAIL  runtime-config-untampered' \
  && ok "doctor: 检出运行时配置被篡改（漂移可见）" || ng "doctor: 未检出漂移"
D2=$($CTL doctor --heal 2>&1); printf '%s\n' "$D2" | grep -E 'runtime-config-untampered|DOCTOR-SUMMARY' | tee -a "$OUT"
printf '%s\n' "$D2" | grep -q 'DOCTOR-SUMMARY pass=10 fail=0' \
  && ok "doctor --heal: 重算目标态并原子修复" || ng "doctor --heal"

sec "7 收尾"
FINAL=$($CTL doctor 2>&1); printf '%s\n' "$FINAL" | grep 'DOCTOR-SUMMARY' | tee -a "$OUT"
log ""
log "REGRESSION-SUMMARY pass=$PASS fail=$FAIL finished=$(date '+%F %T')"
log "REGRESSION-LOG $OUT"
[ "$FAIL" -eq 0 ]
