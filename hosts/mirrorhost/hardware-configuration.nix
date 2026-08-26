# ============================================================
#  hardware-configuration.nix —— 占位模板
# ------------------------------------------------------------
#  在目标硬件上安装时，用真实生成物替换本文件：
#    nixos-generate-config --root /mnt
#  然后把生成的 hardware-configuration.nix 拷回本目录。
#
#  三块盘的挂载约定（详见 modules/storage.nix，按尺寸自动识别）：
#    256G  → /            系统盘（nix store + 系统数据 + 快照）
#    1T    → /var/lib/mirror/instances   实例盘（所有 VM/容器的数据）
#    32G   → /var/lib/mirror/iso         ISO 盘（U盘，默认只读挂载）
# ============================================================

{ config, lib, pkgs, modulesPath, ... }:

{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ];

  # —— 以下为示意，安装时以 nixos-generate-config 实际探测为准 ——
  boot.initrd.availableKernelModules =
    [ "nvme" "xhci_pci" "ahci" "usb_storage" "usbhid" "sd_mod" ];

  # fileSystems."/" = {
  #   device = "/dev/disk/by-id/nvme-XXXX-system";
  #   fsType = "ext4";
  # };
  #
  # fileSystems."/var/lib/mirror/instances" = {
  #   device = "/dev/disk/by-id/nvme-XXXX-instances";
  #   fsType = "xfs";          # 大文件（磁盘镜像）友好
  # };
  #
  # # 32G ISO U盘：只读、惰性挂载（插入才挂，安全默认值）
  # fileSystems."/var/lib/mirror/iso" = {
  #   device = "/dev/disk/by-id/usb-XXXX-iso";
  #   fsType = "ext4";
  #   options = [ "ro" "noauto" "x-systemd.automount" "nofail" ];
  # };

  swapDevices = [ ];   # 虚拟化宿主不 swap：内存宁可打满也不让实例换页抖动
}
