#!/usr/bin/env bash
# 在 k3s server（node1 控制面）节点上以 root 执行：给集群节点打「资源画像」标签。
# 说明：agent 节点本地无 API server（127.0.0.1:6443 不监听），无法 self-label，
#       因此统一由 server 节点代打。ar8.nix 的 nodeProfile 选项仅对 server 节点自打本机。
# 2026-11-18 精简：旧 pilot 与云端 ECS 已移出集群；云资源将来按单一目标专门接入。
set -euo pipefail

KUBE="k3s kubectl"

label_node () {
  local node="$1" cls="$2" tier="$3" edge="$4"
  local cores mem_kib mem_mib
  cores=$($KUBE get node "$node" -o jsonpath='{.status.allocatable.cpu}' 2>/dev/null || echo "?")
  mem_kib=$($KUBE get node "$node" -o jsonpath='{.status.allocatable.memory}' 2>/dev/null || echo "?")
  if [[ "$mem_kib" == *Ki ]]; then
    mem_mib=$(( ${mem_kib%Ki} / 1024 ))
  else
    mem_mib="$mem_kib"
  fi
  echo "labeling $node -> node-class=$cls tier=$tier cpu-cores=$cores mem-mib=$mem_mib edge=${edge:-<none>}"
  $KUBE label node "$node" --overwrite \
    node-class="$cls" tier="$tier" cpu-cores="$cores" mem-mib="$mem_mib" \
    region=home-lab ${edge:+edge="$edge"}
}

$KUBE get node vm-ar8-node1 >/dev/null 2>&1 && label_node vm-ar8-node1 control high ""
$KUBE get node vm-ar8-node2 >/dev/null 2>&1 && label_node vm-ar8-node2 standard standard ""

# 存储能力画像：对象存储不可块挂载 / 不允许整集群挂载（与准入策略一致）
echo "-- storage capability labels --"
for n in vm-ar8-node1 vm-ar8-node2; do
  $KUBE get node "$n" >/dev/null 2>&1 || continue
  $KUBE label node "$n" --overwrite \
    ar8.io/storage-hot=true \
    ar8.io/storage-warm=true \
    ar8.io/object-mount=forbidden \
    ar8.io/cluster-wide-mount=forbidden
done

echo "done. node labels:"
$KUBE get nodes --show-labels | grep -E "vm-ar8-" || true
