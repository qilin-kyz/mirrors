# hosts/ksaya-1.0-installer.nix —— KSAYA 1.0 出厂安装 ISO
#
# 用途：把「基础系统 × KSAYA」融合的 1.0 打成一**张可从零装机的出厂系统盘**。
#   - 引导后即是一个 NixOS 安装环境，内置 KSAYA 源码（flake 随盘，离线可装）；
#   - 装机时直接 ksaya-install /dev/<磁盘>（或 nixos-install --flake #ksaya-1.0），
#     装出来的系统就以 geo-distributed profile 运行，且出厂自带版本标识 1.0、
#     异构硬件宽口径识别、唯一标识 + 开机自动上报。
#   - 扩节点时同样用这张盘：新节点 systemProfile 一致即可组多地域集群。
#
# 与 geo 版 ISO 的区别：装机目标是 ksaya-1.0（融合版 host），装出的是 1.0 系统。
{ config, pkgs, lib, self, ... }:

let
  # 一键装机脚本：分区分区（EFI + 根分区按 label）、nixos-install 指向 ksaya-1.0 host。
  ksayaInstall = pkgs.writeShellScriptBin "ksaya-install" ''
    set -euo pipefail
    TARGET="''${1:-}"
    if [ -z "$TARGET" ]; then
      echo "用法: ksaya-install <磁盘设备>  例: ksaya-install /dev/sda"
      echo "将把 $TARGET 分区为 EFI(1G) + 根(剩余)，并按 label 挂载装 KSAYA 1.0 系统。"
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
    sgdisk -n 1:0:+1G -t 1:ef00 -c 1:ESP "$DISK"
    sgdisk -n 2:0:0 -t 2:8300 -c 2:nixos "$DISK"
    partprobe "$DISK" || true
    sleep 2

    # 分区设备名（NVMe 是 ''${DISK}p1 形式，SATA 是 ''${DISK}1 形式）
    if [ -e "''${DISK}p1" ]; then P="''${DISK}p"; else P="''${DISK}"; fi
    mkfs.vfat -F 32 -n ESP "''${P}1"
    mkfs.ext4 -L nixos "''${P}2"

    # 挂载 + 安装（host 已写死按 label 挂载，flake 装机无需 generate-config）
    # ESP 卷标统一为 "ESP"：同时对齐 make-disk-image 硬编码的 ESP 卷标，
    # 使「ISO 装机」与「DD 烧录」两条路径产出的盘用同一 /boot 挂载（by-label/ESP）。
    mount /dev/disk/by-label/nixos /mnt
    mkdir -p /mnt/boot
    mount /dev/disk/by-label/ESP /mnt/boot

    # 定位 flake 源码（随盘在 /etc/ksaya-flake，若不存在则取当前源码树）
    FLAKE="''${KSAYA_FLAKE:-/etc/ksaya-flake}"
    echo "== nixos-install --flake $FLAKE#ksaya-1.0 =="
    nixos-install --flake "$FLAKE#\"ksaya-1.0\"" "$@"
    echo "== 安装完成。umount 后 reboot 即可进入 KSAYA 1.0 系统 =="
  '';
in
{
  # 官方安装盘基座（installation-cd-base）由 flake.nix 在最外层注入。

  # ---- ISO 身份证（ASCII 文件名）----
  isoImage.isoBaseName = lib.mkForce "KSAYA-1.0";
  isoImage.volumeID = lib.mkForce "KSAYA-1.0";
  # 仅 UEFI 引导：基座默认 makeUsbBootable=true 会与 makeEfiBootable 同优先级冲突，
  # 必须 mkForce 压过；makeBiosBootable 直接关掉。
  isoImage.makeEfiBootable = true;
  isoImage.makeBiosBootable = false;
  isoImage.makeUsbBootable = lib.mkForce false;

  # ---- 离线装机生命线：flake 源码 + nixpkgs 随盘进 store ----
  isoImage.storeContents = [ self ];
  environment.etc."ksaya-flake".source = self;

  # ---- 网络：安装盘用 DHCP（基座默认压 useDHCP=false 且启用 NetworkManager，
  # 两处都要 mkForce 掰回来，否则求值期冲突）----
  networking.useDHCP = lib.mkForce true;
  networking.networkmanager.enable = lib.mkForce false;
  networking.wireless.enable = false;

  # ---- 装机工具链 ----
  environment.systemPackages = with pkgs; [
    ksayaInstall
    git
    vim
    gptfdisk
    parted
    e2fsprogs
    dosfstools
  ];

  # ---- root 免密登录（安装环境从简，与官方安装盘一致）----
  services.getty.autologinUser = lib.mkForce "root";
  users.users.root.initialPassword = lib.mkForce "ksaya";

  # ---- 登录提示 ----
  environment.etc."issue".text = ''
    KSAYA 1.0 出厂安装环境（基础系统 × KSAYA 融合版）
    装机命令：  ksaya-install /dev/<磁盘>      # 分区 + 装 KSAYA 1.0 系统
    手动路径：  mount 分区后 nixos-install --flake <flake>#ksaya-1.0

  '';
}