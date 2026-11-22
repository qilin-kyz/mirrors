# hosts/ksaya-1.0-disk.nix —— KSAYA 1.0 出厂 DD 镜像（raw disk image）目标配置
#
# 与 ksaya-1.0.nix 是同一套系统（基础系统 × KSAYA 融合版），唯一差异：
# 关闭 canTouchEfiVariables。
#
# 为什么关：
#   DD 镜像必须「可移植」—— dd 写入任意一台机器的磁盘后，靠 ESP 里的
#   fallback 引导项 \EFI\BOOT\BOOTX64.EFI 启动（bootctl install 默认就写这个），
#   不再预写目标机固件的 NVRAM 启动项。因为 NVRAM 属于每台机器自己的
#   UEFI pflash，根本不可能打进 DD 镜像里（打进去也无效）。
#
# make-disk-image 的 boot 阶段（switch-to-configuration boot）在关闭
# canTouchEfiVariables 后不依赖 efivarfs，boot 阶段更稳、不赌 QEMU 是否
# 提供 OVMF 固件。
{ config, pkgs, lib, ... }:
{
  imports = [ ./ksaya-1.0.nix ];

  boot.loader.efi.canTouchEfiVariables = lib.mkForce false;
}