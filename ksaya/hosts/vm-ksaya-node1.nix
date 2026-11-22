# 节点：KSAYA 集群控制面 node1（运行在 qilin@192.168.1.72 的 LXD KVM 中）
# 2026-11-18 重建：旧 pilot(10.23.112.16) 数据损坏下线，control-plane 迁到本节点。
# 静态 IP 由 LXD 在 lxdbr0 上分配为 10.23.112.21，与下方 nodeIP 一致。
{ config, pkgs, ... }:

{
  imports = [ ../modules/cluster-base.nix ];

  networking.hostName = "vm-ksaya-node1";

  # ---- 启动：LXD VM 自带 OVMF(UEFI)，用 systemd-boot ----
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot";

  # ---- 磁盘分区（与 pilot 同布局，按 label 挂载，可迁移）----
  fileSystems."/boot" = {
    device = "/dev/disk/by-label/EFI";
    fsType = "vfat";
  };
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  # ---- 网络：LXD 网桥 DHCP + 静态保留 IP（见上方注释）----
  networking.useDHCP = true;

  # ---- initrd 内核模块：必须显式声明（virtio 驱动，否则找不到根盘/网卡）----
  boot.initrd.availableKernelModules = [
    "virtio_pci"
    "virtio_blk"
    "virtio_scsi"
    "virtio_net"
    "ext4"
  ];
  boot.initrd.kernelModules = [
    "virtio_pci"
    "virtio_blk"
    "virtio_scsi"
    "virtio_net"
  ];

  # ---- 串口控制台 ----
  boot.kernelParams = [ "console=ttyS0,115200n8" ];

  # ---- 自动升级时间窗（与 pilot 错开）----
  system.autoUpgrade.dates = "*-*-* 04:13";

  # ---- 注入真实公钥 ----
  users.users.user.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCNW9Pf+L99HEDW0/hA+tI8r1ggd0Lz4ARaoxzCvcKA 86800543@qq.com"
  ];
  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCNW9Pf+L99HEDW0/hA+tI8r1ggd0Lz4ARaoxzCvcKA 86800543@qq.com"
  ];

  # ---- KSAYA：本节点是控制面（server），同时具备 KVM 能力 ----
  services.ksaya.enable = true;
  services.ksaya.demo.enable = true;
  services.ksaya.workload.kvm.enable = true;
  services.ksaya.workload.kubernetes = {
    enable = true;
    role = "server";           # 控制面 + 可跑负载
    clusterInit = false;       # 单 server 用默认 sqlite(kine)，不切 etcd
    token = "ksaya-arbitrary-token-2026";   # 集群共享令牌（测试明文；生产用 tokenFile/age）
    nodeIP = "10.23.112.21";   # 必须钉死（隐私 IPv6 会变 → etcd/kubelet 失联）
    nodeProfile = "control";
    registryMirrors = [
      "https://docker.1panel.live"
    ];
  };
}
