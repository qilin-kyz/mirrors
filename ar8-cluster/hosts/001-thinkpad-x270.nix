# 节点：ThinkPad X270 (x86_64, UEFI, Intel 核显, 有线网卡)
# 主机名已规范化：原 "001-Thinkpad X270" 含空格属非法主机名，改为小写连字符。
{ config, pkgs, ... }:

{
  imports = [ ../modules/cluster-base.nix ];

  networking.hostName = "001-thinkpad-x270";

  # ---- 启动：UEFI 单系统，systemd-boot（攻击面小，符合 B4 委托）----
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # ---- 网络：默认 DHCP，保证 flake 可直接评估/构建 ----
  # 需要静态 IP 时，取消下面注释并填入真实网卡名与地址：
  # networking.useDHCP = false;
  # networking.interfaces."enp0s31f6".ipv4.addresses = [
  #   { address = "192.168.1.21"; prefixLength = 24; }
  # ];
  # networking.defaultGateway = "192.168.1.1";
  # ---- 网络 ----
  # 26.05 默认 networking.useDHCP = true，后端是 dhcpcd（useNetworkd = false），
  # 因此无需写 /etc/systemd/network/*.network（那是 networkd 后端专用，写了也空转）。
  networking.useDHCP = true;
  networking.nameservers = [ "1.1.1.1" "8.8.8.8" ];

  # ---- Intel 平台：微码 + KVM（提供 /dev/kvm 给 QEMU-KVM）----
  hardware.cpu.intel.updateMicrocode = true;
  boot.kernelModules = [ "kvm-intel" ];

  # ---- initrd 内核模块：必须显式声明 ----
  # 实测踩坑：nixos-install 在 chroot 中构建，硬件自动探测不可用 →
  # initrd 缺少存储/网卡驱动 → 启动时找不到根盘(/dev/disk/by-label/... 超时)。
  # ThinkPad X270：NVMe SSD + 有线网卡(iwlwifi/iwlwifi-ucode 为无线)。
  boot.initrd.availableKernelModules = [
    "nvme"
    "ahci"
    "sd_mod"
    "ext4"
    "xhci_pci"
    "i915"
  ];
  boot.initrd.kernelModules = [
    "nvme"
    "ahci"
  ];

  # ---- 磁盘分区：装机前用 lsblk -f 查真实标签/UUID 后填入 ----
  # TODO(装机前必填): 下面两项需按实际磁盘标签或 UUID 填写，否则装完无法引导启动。
  # 建议用 UUID（更抗盘序变化）：lsblk -f /dev/nvme0n1
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/EFI";
    fsType = "vfat";
  };
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };
  boot.loader.efi.efiSysMountPoint = "/boot";

  # ---- 自动升级时间窗口（与其他节点错开，降低集群风险）----
  system.autoUpgrade.dates = "*-*-* 03:17";   # 示例：每天 03:17；其他节点用不同时间

  # ---- 防火墙：本节点集群端口（TODO 待 AR8 端口确定后填）----
  # networking.firewall.interfaces."<内网网卡>".allowedTCPPorts = [ <端口> ];

  # ---- AR8 单机试点：同时具备 K8s 容器 与 KVM 虚拟化 能力 ----
  # 即"先组网成一个整体，再按节点分配成 KVM 或 K8s 容器"
  services.ar8.workload.kvm.enable = true;
  services.ar8.workload.kubernetes = {
    enable = true;
    role = "server";
    clusterInit = true;   # 单机首节点；后续扩节点时改为 role="agent" + serverAddr + token
  };
  # 存储/容灾/带外自愈/网络冗余 等后端，按阶段逐步开启
}
