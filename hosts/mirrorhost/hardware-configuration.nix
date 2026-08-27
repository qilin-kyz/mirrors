# ============================================================
#  hardware-configuration.nix —— 占位桩（仅用于 CI 求值）
# ------------------------------------------------------------
#  真实部署时由 `nixos-generate-config` 在目标硬件（9600X + RTX 5070 Ti）
#  上生成，并覆盖到本仓库 hosts/mirrorhost/ 下。
#  此桩唯一用途：让 `nix eval .#mirrorhost` 在云端 CI 中可求值，
#  从而验证 modules/*.nix 的语法与选项正确。
#  ⚠️ 请勿直接用于真实装机 —— 设备名与分区均为占位，装机前务必替换。
# ============================================================
{ config, lib, pkgs, modulesPath, ... }:

{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ];

  boot.initrd.availableKernelModules = [ "xhci_pci" "ahci" "nvme" "usb_storage" "usbhid" "sd_mod" "usbhid" ];
  boot.initrd.kernelModules = [ ];
  boot.kernelModules = [ "kvm-amd" "vfio-pci" "r8169" "r8152" ];

  boot.loader.grub.enable = true;
  boot.loader.grub.device = "/dev/sda";
  boot.loader.efi.canTouchEfiVariables = true;

  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  swapDevices = [ ];

  powerManagement.cpuFreqGovernor = lib.mkDefault "performance";
}
