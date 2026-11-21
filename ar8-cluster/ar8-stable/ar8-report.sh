#!/bin/sh
# AR8 节点上报脚本（2026-11-21）
#
# 目标（对应「固定向公网机发信息，并被公网机接收」）：
#   1) 机器唯一标识码：首次运行生成并固化到 /etc/ar8/node-id（幂等，永不重生成）。
#      生成来源优先级：machine-id（/etc/machine-id，systemd 唯一）> MAC > 随机。
#   2) 收集本机状态快照（主机名 / IP / AR8 profile / k3s 角色状态 / 时钟）。
#   3) 向上报地址 POST 心跳。上报地址可配置、可后期修改：
#       优先级：命令行参数 > /etc/ar8/report.conf 的 REPORT_URL > 环境变量 > 默认值。
#   4) 支持两种运行方式：
#        a. 一次性：手动跑或 systemd oneshot；
#        b. 周期：systemd timer 每分钟/每 X 秒触发（上线后由 timer 兜底持续上报）。
#
# 设计约束：
#   - 纯 POSIX sh + curl（不依赖 python），NixOS 最小系统与公网机都能跑。
#   - 所有写操作幂等：重复运行不产生副作用，只更新 last_report 时间。
#   - node-id 一旦写入绝不覆盖（即使 machine-id 变了也保持稳定），保证「唯一且固定」。
set -u

NODE_ID_FILE=/etc/ar8/node-id
CONF_FILE=/etc/ar8/report.conf
STAMP_FILE=/var/lib/ar8/last-report
LOG=/var/log/ar8-report.log

# 默认上报地址：公网机 collector（标准 HTTP 80，可被 conf/argv/env 覆盖）
DEFAULT_URL="${REPORT_URL:-http://182.92.237.205/report}"

log(){ printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG" 2>/dev/null || true; }

# ---------- 1) 唯一标识：生成一次，永久固化 ----------
gen_node_id(){
  # 优先 machine-id：systemd 保证每台机器唯一，取前 8 位 hex 即可读
  if [ -r /etc/machine-id ] && [ -s /etc/machine-id ]; then
    ID=$(head -c 8 /etc/machine-id 2>/dev/null | tr -d '\n')
    [ -n "$ID" ] && { printf 'ar8-%s\n' "$ID"; return 0; }
  fi
  # 其次：任意物理网卡 MAC（去掉冒号，取前 8 位）
  MAC=$(ip -o link 2>/dev/null | awk '/link\/ether/{print $2; exit}' | tr -d ':' | head -c 8)
  if [ -n "$MAC" ]; then
    printf 'ar8-%s\n' "$MAC"
    return 0
  fi
  # 兜底：随机 hex
  printf 'ar8-%s\n' "$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
  return 0
}

ensure_node_id(){
  if [ -r "$NODE_ID_FILE" ] && [ -s "$NODE_ID_FILE" ]; then
    cat "$NODE_ID_FILE" | tr -d '\n'
    return 0
  fi
  mkdir -p "$(dirname "$NODE_ID_FILE")" 2>/dev/null || true
  NEW=$(gen_node_id)
  printf '%s\n' "$NEW" > "$NODE_ID_FILE" 2>/dev/null || {
    log "cannot write node-id to $NODE_ID_FILE"
    echo "$NEW"   # 至少本次返回；下次会再尝试固化
    return 0
  }
  echo "$NEW"
}

# ---------- 2) 参数与配置解析 ----------
# 统一用 REPORT_URL 这一个变量名（conf 文件 / 环境变量均叫 REPORT_URL）。
# 优先级：命令行参数 > REPORT_URL 环境变量 > conf 文件 > 内置默认值。
URL="${REPORT_URL:-$DEFAULT_URL}"
if [ -r "$CONF_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONF_FILE"   # conf 内 REPORT_URL=... 可覆盖上面默认/环境值
  URL="${REPORT_URL:-$URL}"
fi
# 命令行参数最高优先级
if [ "${1:-}" != "" ]; then
  URL="$1"
fi

# ---------- 3) 采集状态快照 ----------
NID=$(ensure_node_id)
HOST=$(hostname 2>/dev/null || echo unknown)
IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
IP=${IP:-unknown}
PROFILE="${AR8_PROFILE:-$(cat /etc/ar8/profile 2>/dev/null || echo unknown)}"
K3S_ROLE=$(cat /etc/ar8/k3s-role 2>/dev/null || echo unknown)
K3S_ACTIVE=$(systemctl is-active k3s k3s-agent 2>/dev/null | paste -sd, - 2>/dev/null || echo unknown)
UPTIME=$(cut -d' ' -f1 /proc/uptime 2>/dev/null || echo 0)
LOAD=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo unknown)
TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# 组装 JSON（手工拼，避免依赖 jq）
PAYLOAD=$(printf '{"node_id":"%s","hostname":"%s","ip":"%s","profile":"%s","k3s_role":"%s","k3s_active":"%s","uptime":%s,"load":"%s","ts":"%s"}' \
  "$NID" "$HOST" "$IP" "$PROFILE" "$K3S_ROLE" "$K3S_ACTIVE" "$UPTIME" "$LOAD" "$TS")

# ---------- 4) 上报 ----------
RC=0
if command -v curl >/dev/null 2>&1; then
  RESP=$(curl -s --max-time 8 -X POST \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" "$URL" 2>/dev/null) && RC=0 || RC=$?
elif command -v wget >/dev/null 2>&1; then
  RESP=$(wget -q -T 8 -O - --header='Content-Type: application/json' \
    --post-data="$PAYLOAD" "$URL" 2>/dev/null) && RC=0 || RC=$?
else
  log "no curl/wget"; echo "no http client" >&2; exit 3
fi

if [ "$RC" -eq 0 ]; then
  mkdir -p "$(dirname "$STAMP_FILE")" 2>/dev/null || true
  printf '%s\n' "$TS" > "$STAMP_FILE" 2>/dev/null || true
  log "report ok node=$NID url=$URL resp=$RESP"
  echo "$RESP"
  exit 0
else
  log "report failed rc=$RC url=$URL"
  echo "report failed rc=$RC" >&2
  exit 4
fi