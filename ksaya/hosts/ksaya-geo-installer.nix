# hosts/ksaya-geo-installer.nix —— KSAYA geo-distributed 可引导安装 ISO
#
# 用途：把「第四方案 geo-distributed」打成一张可从零装机的系统盘。
#   - 引导后即是一个 NixOS 安装环境，内置 KSAYA 源码（flake 随盘）；
#   - 装机时直接 nixos-install --flake <path>#ksaya-geo-standalone，
#     装出来的系统就以 geo-distributed profile 运行（BBR + 长肥管道 + GeoData 路由）；
#   - 扩节点时同样用这张盘，把新节点 systemProfile 也设 geo-distributed 即可组多地域集群。
#
# 与 mirrors 仓库「镜 0.1-DEMO」ISO 的区别：不搞中文 TUI 向导，
# 只保留官方安装盘 + 源码随盘 + 一键装机脚本，极简、可离线、可复现。
{ config, pkgs, lib, self, ... }:

let
  # 一键装机脚本：跑分区分区（EFI + 根分区按 label）、nixos-install 指向 geo host。
  geoInstall = pkgs.writeShellScriptBin "ksaya-geo-install" ''
    set -euo pipefail
    TARGET="''${1:-}"
    if [ -z "$TARGET" ]; then
      echo "用法: ksaya-geo-install <磁盘设备>  例: ksaya-geo-install /dev/sda"
      echo "将把 $TARGET 分区为 EFI(1G) + 根(剩余)，并按 label 挂载装 KSAYA geo-distributed 系统。"
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

    # 分区设备名（NVMe 是 ''${DISK}p1 形式，SATA 是 ''${DISK}1 形式）
    if [ -e "''${DISK}p1" ]; then P="''${DISK}p"; else P="''${DISK}"; fi
    mkfs.vfat -F 32 -n EFI "''${P}1"
    mkfs.ext4 -L nixos "''${P}2"

    # 挂载 + 安装（host 已写死按 label 挂载，flake 装机无需 generate-config）
    mount /dev/disk/by-label/nixos /mnt
    mkdir -p /mnt/boot
    mount /dev/disk/by-label/EFI /mnt/boot

    # 定位 flake 源码（随盘在 /etc/ksaya-flake，若不存在则取当前源码树）
    FLAKE="''${KSAYA_FLAKE:-/etc/ksaya-flake}"
    echo "== nixos-install --flake $FLAKE#ksaya-geo-standalone =="
    nixos-install --flake "$FLAKE#ksaya-geo-standalone" "$@"
    echo "== 安装完成。umount 后 reboot 即可进入 geo-distributed 系统 =="
  '';
in
{
  # 官方安装盘基座（installation-cd-base）由 flake.nix 在最外层注入，
  # 路径依赖 nixpkgs 参数，host 文件里无法稳定引用 —— 保持与 mirrors 已验证写法一致。

  # ---- ISO 身份证（ASCII 文件名，见 mirrors build-iso 实战笔记）----
  isoImage.isoBaseName = lib.mkForce "KSAYA-geo-distributed";
  isoImage.volumeID = lib.mkForce "KSAYA-GEO";
  # 仅 UEFI 引导：基座默认 makeUsbBootable=true 会与 makeEfiBootable 同优先级冲突，
  # 必须 mkForce 压过（mirrors 三轮 CI 实测）；makeBiosBootable 直接关掉。
  isoImage.makeEfiBootable = true;
  isoImage.makeBiosBootable = false;
  isoImage.makeUsbBootable = lib.mkForce false;

  # ---- 离线装机生命线：flake 源码 + nixpkgs 随盘进 store ----
  isoImage.storeContents = [ self ];
  environment.etc."ksaya-flake".source = self;

  # ---- 网络：安装盘用 DHCP（基座默认压 useDHCP=false 且启用 NetworkManager，
  # 两处都要 mkForce 掰回来，否则求值期冲突 —— mirrors build-iso 三轮 CI 实测）----
  networking.useDHCP = lib.mkForce true;
  networking.networkmanager.enable = lib.mkForce false;
  networking.wireless.enable = false;

  # ---- 装机工具链 ----
  environment.systemPackages = with pkgs; [
    geoInstall
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
    KSAYA geo-distributed 安装环境
    装机命令：  ksaya-geo-install /dev/<磁盘>      # 分区 + 装 geo-distributed 系统
    手动路径：  mount 分区后 nixos-install --flake <flake>#ksaya-geo-standalone

  '';
}