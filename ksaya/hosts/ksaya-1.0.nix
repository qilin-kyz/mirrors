# =============================================================================
# 节点：KSAYA 1.0 出厂整机 —— 「基础系统 × KSAYA」融合版（默认 geo-distributed）
#
# 这是把「KSAYA 之前那套基础系统（集群基石）」与「KSAYA 调度核心」融合成的 1.0：
#
#   【基础系统遗赠】（源自 nixos-config-questionnaire.md 的诉求，11-12~11-14 搓成）
#     - 异构硬件宽口径识别：裸机 NVMe/SATA + 虚拟机 virtio + 常见有线网卡，
#       覆盖「树莓派/工作站/NVIDIA-A100/GTX960/核显/云实例」都能入群的初衷；
#     - 轻量、稳定、安全基线：systemd-boot（最小攻击面）+ zramSwap + 仅密钥 SSH；
#     - 「这是一个集群的基石系统」——出厂即控制面，可跑 K8s 容器 + QEMU-KVM。
#
#   【KSAYA 核心】（在基座之上长出的调度系统）
#     - 四 profile 可插拔调度，出厂默认 geo-distributed（三重差距下显著最优）；
#     - 真值源 / reconcile FSM / 6 条不变量 完整随盘；
#     - 唯一标识 + 心跳上报（开机自动向公网 collector 注册入群）。
#
# 装机后：systemProfile 默认为 geo-distributed；想切 baseline/xnu-qos/oh-distributed
#         只需改 services.ksaya.systemProfile 一个字段后 nixos-rebuild switch。
# =============================================================================
{ config, pkgs, lib, ... }:

{
  imports = [ ../modules/cluster-base.nix ];

  # hostname 不能含点（NixOS 正则 ^[[:alnum:]]([[:alnum:]_-]{0,61}[[:alnum:]])?$），
  # 用连字符；1.0 品牌名保留在 ISO volumeID / flake attrpath / 文档里。
  networking.hostName = "ksaya-1-0";

  # ---- 出厂版本标识（融合后的正式版本）----
  services.ksaya.systemProfile = "geo-distributed";

  # ---- 启动：UEFI + systemd-boot（最小攻击面，符合基础系统 B4 委托）----
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot";

  # ---- 磁盘：按 label 挂载 ----
  #   /boot 的 ESP 卷标统一为 "ESP"：既对齐 ISO 装机脚本（mkfs.vfat -n ESP），
  #   也对齐 make-disk-image 的默认 ESP 卷标（mkfs.vfat -n ESP），两条装机/烧录
  #   路径共用同一 host 配置，无需分叉。
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/ESP";
    fsType = "vfat";
  };
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  # ---- 网络：DHCP，装机后按需改静态 ----
  networking.useDHCP = true;
  networking.nameservers = [ "1.1.1.1" "8.8.8.8" ];

  # ===========================================================================
  # 基础系统遗赠：异构硬件宽口径 initrd —— 让「任何硬件都能装进集群」
  #
  #   存储：NVMe(nvme) / SATA(ahci+sd_mod) / USB 盘(usb_storage)；
  #   显卡：Intel(i915) 开放驱动，AMD/NVIDIA 由 hardware.graphics 兜全（见 cluster-base）；
  #   网卡：常见板载/PCIe 有线网卡（e1000e/igb/r8169/ixgbe）+ 虚拟机 virtio_net；
  #   文件系统：ext4 + vfat（根盘 / 启动盘）。
  #
  #   未声明的模块会被 initrd 裁剪掉，找不到根盘/网卡就进不了系统（历史踩坑
  #   「Timed out waiting for device /dev/disk/by-label/nixos」）。
  # ===========================================================================
  boot.initrd.availableKernelModules = [
    # 存储
    "nvme"
    "ahci"
    "sd_mod"
    "usb_storage"
    "xhci_pci"
    # 文件系统
    "ext4"
    "vfat"
    # 显卡（开放驱动；闭源 NVIDIA/AMD 由 nixpkgs allowUnfree + 后续按需 enable 加载）
    "i915"
    # 常见有线网卡（板载/PCIe/万兆）
    "e1000e"
    "igb"
    "r8169"
    "ixgbe"
    # 虚拟机（KVM/QEMU 与公有云实例）
    "virtio_pci"
    "virtio_blk"
    "virtio_scsi"
    "virtio_net"
  ];
  boot.initrd.kernelModules = [
    "nvme"
    "ahci"
    "sd_mod"
    "usb_storage"
    "xhci_pci"
    "virtio_pci"
    "virtio_blk"
    "virtio_scsi"
    "virtio_net"
  ];

  # ---- 异构 GPU：开放驱动下尽可能识别（NVIDIA 闭源走 nixpkgs.config.allowUnfree）----
  # cluster-base.nix 已开 hardware.graphics.enable = true；这里补开启 NVIDIA 闭源驱动
  # 的开关位（若目标机无 NVIDIA 卡，此项安全无害；有卡则系统自动加载官方驱动）。
  # 注意：NVIDIA 闭源驱动会显著增大系统闭包体积，出厂 ISO 如需小体积可注释掉下面三行。
  # hardware.nvidia.open = true;
  # services.xserver.videoDrivers = [ "nvidia" ];
  # hardware.graphics.extraPackages = with pkgs; [ nvidia-vaapi-driver ];

  # ---- KSAYA：出厂即控制面（server + etcd + 可跑负载），同时具备 KVM 能力 ----
  # users.user/root 的 SSH 密钥由 cluster-base.nix 统一注入，这里不重复。
  services.ksaya.enable = true;
  services.ksaya.demo.enable = true;
  services.ksaya.workload.kvm.enable = true;
  services.ksaya.workload.kubernetes = {
    enable = true;
    role = "server";
    clusterInit = true;          # 全新单机首节点（内嵌 etcd）
    token = "ksaya-arbitrary-token-2026";
    nodeProfile = "control";
    registryMirrors = [
      "https://docker.1panel.live"
    ];
  };

  # ---- 节点上报：唯一标识 + 心跳（固定向公网 collector 注册，被公网接收）----
  # node-id 由 ksaya-report 开机首次生成并固化到 /etc/ksaya/node-id（machine-id 派生，
  # 幂等永不重生成）；心跳经 systemd timer 周期发送到 report.url。
  # 「一个集群」= 公网 collector 上注册过的所有 node_id。后期换公网机只改 url。
  services.ksaya.report = {
    enable = true;
    url = "http://182.92.237.205/report";   # 公网 collector（标准 80，可改）
  };
}