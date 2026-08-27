# ============================================================
#  gpu-passthrough.nix —— 镜中镜：RTX 5070 Ti (GB203) PCIe 直通
# ------------------------------------------------------------
#  这台机器"为虚拟化而生"的招牌能力，就是把显卡透传给 VM。
#  本模块只负责把直通链路准备好，不算"内核定制"（不改 CONFIG_*）：
#    - 启用 AMD-Vi / IOMMU（9600X 是 AMD，amd_iommu=on）
#    - 加载 VFIO 内核模块
#    - 提供 mirror-gpu 助手：探测显卡、生成 libvirt hostdev XML、
#      在宿主 nvidia 驱动与 vfio-pci 之间按需切换
#  直通方式采用 libvirt managed='yes' 延迟绑定：
#    宿主平时用 nvidia 驱动做 Docker GPU 计算；VM 启动时 libvirt
#    自动把显卡从 nvidia 解绑、绑到 vfio-pci，关机再绑回。
#    若设备被持久守护进程占住解绑失败，用 `mirror-gpu prepare` 手动切。
# ============================================================

{ config, pkgs, lib, ... }:

let
  # RTX 5070 Ti (Blackwell / GB203) 主图形功能设备 ID
  gpuDevId = "10de:2c05";

  mirrorGpu = pkgs.writeShellScriptBin "mirror-gpu" ''
    set -euo pipefail

    gpu_addr() {
      # 返回显卡 PCI 槽位（域:总线:槽，不带 function），如 0000:01:00
      lspci -D -d ${gpuDevId} 2>/dev/null | head -1 | awk '{print $1}' | sed 's/\.[0-9]$//'
    }

    case "''${1:-help}" in
      status)
        echo "=== MirrorOS GPU passthrough status ==="
        echo "--- IOMMU ---"
        if dmesg 2>/dev/null | grep -qi "AMD-Vi.*enabled\|iommu.*enabled"; then
          echo "IOMMU: ENABLED"
        else
          echo "IOMMU: unknown (check 'dmesg | grep -i iommu')"
        fi
        echo "--- GPU device (${gpuDevId}) ---"
        lspci -nn -d ${gpuDevId} || echo "RTX 5070 Ti not found on this bus"
        echo "--- current driver binding (all functions of the slot) ---"
        ADDR=$(gpu_addr)
        if [ -n "''${ADDR:-}" ]; then
          for f in "$ADDR".*; do
            [ -e "/sys/bus/pci/devices/$f" ] || continue
            DRV=$(readlink "/sys/bus/pci/devices/$f/driver" 2>/dev/null | xargs -r basename || echo "no-driver")
            echo "  $f -> $DRV"
          done
        fi
        echo "--- vfio modules ---"
        lsmod | grep -E 'vfio' || echo "  (none loaded)"
        ;;
      xml)
        # 输出可直接 virsh attach-device 的 hostdev XML（含同槽所有 function）
        ADDR=$(gpu_addr)
        [ -n "''${ADDR:-}" ] || { echo "ERROR: RTX 5070 Ti (${gpuDevId}) not found" >&2; exit 1; }
        DOM=$(echo "$ADDR" | cut -d: -f1)
        BUS=$(echo "$ADDR" | cut -d: -f2)
        SLOT=$(echo "$ADDR" | cut -d: -f3)
        {
          echo "<hostdev mode='subsystem' type='pci' managed='yes'>"
          echo "  <source>"
          for f in "$ADDR".*; do
            [ -e "/sys/bus/pci/devices/$f" ] || continue
            FN=$(echo "$f" | sed "s/.*\.//")
            echo "    <address domain='0x''${DOM}' bus='0x''${BUS}' slot='0x''${SLOT}' function='0x''${FN}'/>"
          done
          echo "  </source>"
          echo "  <rom bar='on'/>"
          echo "</hostdev>"
        }
        ;;
      attach)
        [ -n "''${2:-}" ] || { echo "usage: mirror-gpu attach <vm-name>" >&2; exit 2; }
        XML=$(mktemp)
        "$0" xml > "$XML"
        echo "Attaching RTX 5070 Ti to VM '$2' ..."
        virsh attach-device "$2" "$XML" --live --config
        rm -f "$XML"
        ;;
      detach)
        [ -n "''${2:-}" ] || { echo "usage: mirror-gpu detach <vm-name>" >&2; exit 2; }
        XML=$(mktemp)
        "$0" xml > "$XML"
        virsh detach-device "$2" "$XML" --live --config
        rm -f "$XML"
        ;;
      prepare)
        # 手动把显卡从 nvidia 解绑、绑到 vfio-pci（直通前保底用）
        ADDR=$(gpu_addr)
        [ -n "''${ADDR:-}" ] || { echo "ERROR: RTX 5070 Ti not found" >&2; exit 1; }
        if ! lsmod | grep -q vfio_pci; then modprobe vfio-pci || true; fi
        for f in "$ADDR".*; do
          [ -e "/sys/bus/pci/devices/$f" ] || continue
          echo "unbinding $f from nvidia -> vfio-pci"
          echo "''${f}" > /sys/bus/pci/devices/"''${f}"/driver/unbind 2>/dev/null || true
          echo "vfio-pci" > /sys/bus/pci/devices/"''${f}"/driver_override 2>/dev/null || true
        done
        echo "done. Now start the VM; it will grab the GPU via vfio."
        ;;
      revert)
        # 把显卡交还给 nvidia 驱动（宿主计算模式）
        ADDR=$(gpu_addr)
        [ -n "''${ADDR:-}" ] || { echo "ERROR: RTX 5070 Ti not found" >&2; exit 1; }
        for f in "$ADDR".*; do
          [ -e "/sys/bus/pci/devices/$f" ] || continue
          echo "''${f}" > /sys/bus/pci/devices/"''${f}"/driver_override 2>/dev/null || true
          echo "1" > /sys/bus/pci/devices/"''${f}"/rescan 2>/dev/null || true
        done
        modprobe nvidia 2>/dev/null || true
        echo "reverted to nvidia driver. (persistenced will re-grab on use)"
        ;;
      help|*)
        cat <<USAGE
    mirror-gpu —— RTX 5070 Ti 直通助手

      status    IOMMU / 显卡 / 驱动绑定 / vfio 模块 一览
      xml       生成 libvirt <hostdev> XML（同槽所有 function，managed）
      attach VM  把显卡挂到运行中的 VM（live + 持久）
      detach VM  从 VM 摘除显卡
      prepare    手动解绑 nvidia、绑 vfio-pci（直通前保底）
      revert     把显卡交还 nvidia（宿主计算模式）

    典型流程：  mirror-gpu status
               mirror-gpu prepare        # 若自动解绑失败
               mirror-gpu attach win11   # 挂到目标 VM
    USAGE
        ;;
    esac
  '';
in
{
  # ---- AMD-Vi / IOMMU 打开（9600X 是 AMD，amd_iommu=on） ----
  # 用 mkAfter 追加到 kernel.nix 的同名选项之后，避免"重复定义"求值冲突
  boot.kernelParams = lib.mkAfter [
    "amd_iommu=on"
    "iommu=pt"            # passthrough 模式：性能最好，仍保证分组隔离
    "kvm.ignore_msrs=1"  # 某些 guest 固件读 MSR 友好
  ];

  # ---- VFIO 内核模块（追加，kernel.nix 已含 vfio-pci） ----
  boot.kernelModules = lib.mkAfter [ "vfio-pci" "vfio_iommu_type1" "vfio" ];
  boot.initrd.kernelModules = lib.mkAfter [ "vfio-pci" "vfio_iommu_type1" "vfio" ];

  environment.systemPackages = [ mirrorGpu ];

  # 让 mirror 用户能操作 libvirt 直通（组已在 virtualization.nix 追加）
}
