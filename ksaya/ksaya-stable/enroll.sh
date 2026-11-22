#!/bin/sh
# KSAYA NixOS 零接触入群脚本（2026-11-18 精简版）
# 系统安装阶段写入集群共享令牌 /etc/ksaya/cluster.key；上电后本脚本自动：
#   1) 读取令牌与控制面地址（默认 node1 控制面，可用 KSAYA_SERVER_URL 覆盖）
#   2) 探测 apiserver /healthz（k3s 自签证书，跳过证书校验仅限内网测试）
#   3) 用令牌校验鉴权链路可用（/api 返回 401/200 均说明服务可达）
#   4) 标记入群完成 /etc/ksaya/enrolled
# 说明：真正的 join 由 k3s agent 凭同一令牌完成（见 hosts/*.nix 的
# services.k3s.token/serverAddr）；本脚本只做「装好即验证可达」的收尾，
# 不再向任何控制台上报（Web 控制台已按裁决删除）。
set -u
KEYFILE=/etc/ksaya/cluster.key
CONFFILE=/etc/ksaya/enroll.conf
DONE=/etc/ksaya/enrolled
FAILED=/etc/ksaya/enroll.failed
LOG=/var/log/ksaya-enroll.log

log(){ printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG" 2>/dev/null || true; }

log "enroll start"
rm -f "$FAILED"

[ -r "$KEYFILE" ] || { log "missing $KEYFILE"; echo "missing cluster key" > "$FAILED"; exit 2; }
KEY=$(cat "$KEYFILE")
[ -n "$KEY" ] || { log "empty cluster key"; echo "empty cluster key" > "$FAILED"; exit 2; }

# 控制面地址：默认 node1，其次配置文件/环境变量
SERVER=${KSAYA_SERVER_URL:-}
if [ -r "$CONFFILE" ]; then
  . "$CONFFILE"
fi
SERVER=${KSAYA_SERVER_URL:-https://10.23.112.21:6443}
HOSTNAME=$(hostname 2>/dev/null || echo unknown)
IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)

RC=0
for i in 1 2 3 4 5; do
  if command -v curl >/dev/null 2>&1; then
    CODE=$(curl -sk --max-time 5 -o /dev/null -w '%{http_code}' \
           -H "Authorization: Bearer $KEY" "$SERVER/healthz" 2>/dev/null) && RC=0 || RC=$?
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 5 --no-check-certificate --header="Authorization: Bearer $KEY" \
         -O /dev/null "$SERVER/healthz" 2>/dev/null && { CODE=200; RC=0; } || RC=$?
  else
    log "no curl/wget available"; echo "no http client" > "$FAILED"; exit 3
  fi
  # healthz 匿名即可 200；带令牌被拒(401/403)也证明 apiserver 活着
  case "${CODE:-}" in 200|401|403) RC=0; break;; esac
  log "attempt $i failed rc=$RC code=${CODE:-none}, retry"
  sleep 2
done

if [ "$RC" -ne 0 ]; then
  log "enroll failed after retries, server=$SERVER"
  echo "enroll failed rc=$RC server=$SERVER" > "$FAILED"
  exit 4
fi

log "enroll ok server=$SERVER node=$HOSTNAME ip=${IP:-unknown} healthz=$CODE"
printf 'enrolled at %s server=%s node=%s ip=%s\n' \
  "$(date '+%F %T')" "$SERVER" "$HOSTNAME" "${IP:-unknown}" > "$DONE"
exit 0