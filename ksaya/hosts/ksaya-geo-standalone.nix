# 节点：KSAYA 第四方案（geo-distributed）独立整机系统 —— ISO 安装目标
#
# 这是「把 geo-distributed 打包成系统」的落地形态：一台全新单机，装机后
# 直接以 geo-distributed profile 运行 KSAYA 控制面（k3s server + KVM + KSAYA 包）。
# 与 vm-ksaya-node1 的区别：clusterInit=true（初始化全新集群）、不钉死 nodeIP、
# 不写 VM 专用串口/静态 IP —— 面向真实裸机/任意虚拟机的首次装机。
#
# 后续扩节点：把新节点的 services.ksaya.systemProfile 也设为 geo-distributed，
# 并在 ksaya.config.yaml 的 topology 段登记地域与带宽，即可组多地域集群。
{ config, pkgs, ... }:

{
  imports = [ ../modules/cluster-base.nix ];

  networking.hostName = "ksaya-geo-standalone";

  # ---- 第四方案：传输经济学调度（延迟≠带宽 + 可重算/不可重算）----
  services.ksaya.systemProfile = "geo-distributed";

  # ---- 启动：UEFI + systemd-boot ----
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot";

  # ---- 磁盘：按 label 挂载（install 脚本会建立 EFI/nixos 两个分区并打标）----
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/EFI";
    fsType = "vfat";
  };
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  # ---- 网络：DHCP，装机后按需改静态 ----
  networking.useDHCP = true;
  networking.nameservers = [ "1.1.1.1" "8.8.8.8" ];

  # ---- initrd 内核模块：覆盖裸机（NVMe/SATA）与虚拟机（virtio）两条路 ----
  # 未声明的模块会被 initrd 裁剪掉，找不到根盘/网卡就进不了系统（历史踩坑）。
  boot.initrd.availableKernelModules = [
    "nvme"
    "ahci"
    "sd_mod"
    "ext4"
    "xhci_pci"
    "i915"
    "virtio_pci"
    "virtio_blk"
    "virtio_scsi"
    "virtio_net"
  ];

  # ---- KSAYA：全新集群控制面（server + 可跑负载），同时具备 KVM 能力 ----
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

  # ---- 节点上报：唯一标识 + 心跳（固定向公网机发信息，被公网机接收）----
  # node-id 由 ksaya-report 开机首次生成并固化到 /etc/ksaya/node-id（machine-id 派生，
  # 从未重生成）；心跳经 systemd timer 周期发送到 report.url。
  # 后期换公网机只改 services.ksaya.report.url 一个字段即可，其余不动。
  services.ksaya.report = {
    enable = true;
    url = "http://182.92.237.205/report";   # 公网 collector（标准 80，可改）
  };
}