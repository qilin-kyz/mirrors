# hosts/ar8-1.0-installer.nix —— AR8 1.0 出厂安装 ISO
#
# 用途：把「基础系统 × AR8」融合的 1.0 打成一**张可从零装机的出厂系统盘**。
#   - 引导后即是一个 NixOS 安装环境，内置 AR8 源码（flake 随盘，离线可装）；
#   - 装机时直接 ar8-install /dev/<磁盘>（或 nixos-install --flake #ar8-1.0），
#     装出来的系统就以 geo-distributed profile 运行，且出厂自带版本标识 1.0、
#     异构硬件宽口径识别、唯一标识 + 开机自动上报。
#   - 扩节点时同样用这张盘：新节点 systemProfile 一致即可组多地域集群。
#
# 与 geo 版 ISO 的区别：装机目标是 ar8-1.0（融合版 host），装出的是 1.0 系统。
{ config, pkgs, lib, self, ... }:

let
  # 一键装机脚本：分区分区（EFI + 根分区按 label）、nixos-install 指向 ar8-1.0 host。
  ar8Install = pkgs.writeShellScriptBin "ar8-install" ''
    set -euo pipefail
    TARGET="''${1:-}"
    if [ -z "$TARGET" ]; then
      echo "用法: ar8-install <磁盘设备>  例: ar8-install /dev/sda"
      echo "将把 $TARGET 分区为 EFI(1G) + 根(剩余)，并按 label 挂载装 AR8 1.0 系统。"
      exit 1
    fi
    DISK="$TARGET"
    echo "== 目标磁盘 $DISK（原数据将全部销毁）=="
    lsblk "$DISK" || true
    read -r -p "确认继续？输入 yes 继续: " ok
    [ "$ok" = "yes" ] || { echo "已取消"; exit 0; }

    # 清空分区表 + 建 GPT + 两个分区（EFI 1G + 根 剩余）
    wipefs -a "$DISK" || true
    sgdisk --zap-all "$DISK"
    sgdisk -n 1:0:+1G -t 1:ef00 -c 1:EFI "$DISK"
    sgdisk -n 2:0:0 -t 2:8300 -c 2:nixos "$DISK"
    partprobe "$DISK" || true
    sleep 2

    # 分区设备名（NVMe 是 ${DISK}p1 形式，SATA 是 ${DISK}1 形式）
    if [ -e "''${DISK}p1" ]; then P="''${DISK}p"; else P="''${DISK}"; fi
    mkfs.vfat -F 32 -n EFI "''${P}1"
    mkfs.ext4 -L nixos "''${P}2"

    # 挂载 + 安装（host 已写死按 label 挂载，flake 装机无需 generate-config）
    mount /dev/disk/by-label/nixos /mnt
    mkdir -p /mnt/boot
    mount /dev/disk/by-label/EFI /mnt/boot

    # 定位 flake 源码（随盘在 /etc/ar8-flake，若不存在则取当前源码树）
    FLAKE="''${AR8_FLAKE:-/etc/ar8-flake}"
    echo "== nixos-install --flake $FLAKE#ar8-1.0 =="
    nixos-install --flake "$FLAKE#ar8-1.0" "$@"
    echo "== 安装完成。umount 后 reboot 即可进入 AR8 1.0 系统 =="
  '';
in
{
  # 官方安装盘基座（installation-cd-base）由 flake.nix 在最外层注入。

  # ---- ISO 身份证（ASCII 文件名）----
  isoImage.isoBaseName = lib.mkForce "AR8-1.0";
  isoImage.volumeID = lib.mkForce "AR8-1.0";
  # 仅 UEFI 引导：基座默认 makeUsbBootable=true 会与 makeEfiBootable 同优先级冲突，
  # 必须 mkForce 压过；makeBiosBootable 直接关掉。
  isoImage.makeEfiBootable = true;
  isoImage.makeBiosBootable = false;
  isoImage.makeUsbBootable = lib.mkForce false;

  # ---- 离线装机生命线：flake 源码 + nixpkgs 随盘进 store ----
  isoImage.storeContents = [ self ];
  environment.etc."ar8-flake".source = self;

  # ---- 网络：安装盘用 DHCP（基座默认压 useDHCP=false 且启用 NetworkManager，
  # 两处都要 mkForce 掰回来，否则求值期冲突）----
  networking.useDHCP = lib.mkForce true;
  networking.networkmanager.enable = lib.mkForce false;
  networking.wireless.enable = false;

  # ---- 装机工具链 ----
  environment.systemPackages = with pkgs; [
    ar8Install
    git
    vim
    gptfdisk
    parted
    e2fsprogs
    dosfstools
  ];

  # ---- root 免密登录（安装环境从简，与官方安装盘一致）----
  services.getty.autologinUser = lib.mkForce "root";
  users.users.root.initialPassword = lib.mkForce "ar8";

  # ---- 登录提示 ----
  environment.etc."issue".text = ''
    AR8 1.0 出厂安装环境（基础系统 × AR8 融合版）
    装机命令：  ar8-install /dev/<磁盘>      # 分区 + 装 AR8 1.0 系统
    手动路径：  mount 分区后 nixos-install --flake <flake>#ar8-1.0

  '';
}