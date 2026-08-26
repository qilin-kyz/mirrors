# ============================================================
#  virtualization.nix —— 三生镇魂：Incus + KVM/QEMU + Docker
# ------------------------------------------------------------
#  三栈并存的分工：
#    Incus     → 系统容器与 VM 的统一管理层（主力，长生命周期实例）
#    KVM/QEMU  → 从 ISO 盘裸装的自由 VM（镜存的直接消费者）
#    Docker    → 应用容器（GPU 计算负载经 nvidia-container-toolkit）
#  三栈的实例数据统一落在 1T 实例盘 /var/lib/mirror/instances。
# ============================================================

{ config, pkgs, lib, ... }:

{
  virtualisation = {
    # ---- 魂之一：Incus ----
    incus = {
      enable = true;
      ui.enable = false;          # 官方 UI 不开 —— 管理面只有自研复古台
      preseed = {
        networks = [ {
          name = "incusbr0";
          type = "bridge";
          config = {
            # 实例对外统一走 br-vm（2.5G 专用口），incusbr0 仅内部/NAT 备用
            "ipv4.address" = "10.125.0.1/24";
            "ipv4.nat" = "true";
            "ipv6.address" = "none";
          };
        } ];
        storage_pools = [ {
          name = "mirror-pool";
          driver = "dir";         # 1T 实例盘上的目录池，朴素但极稳
          config.source = "/var/lib/mirror/instances/incus";
        } ];
        profiles = [ {
          name = "default";
          devices = {
            eth0 = { name = "eth0"; network = "incusbr0"; type = "nic"; };
            root = { path = "/"; pool = "mirror-pool"; type = "disk"; };
          };
        } ];
      };
    };

    # ---- 魂之二：KVM/QEMU（libvirt 管理） ----
    libvirtd = {
      enable = true;
      onBoot = "ignore";          # 宿主导完电不自动拉起实例，避免风暴
      onShutdown = "shutdown";    # 宿主关机时优雅关闭实例（7x24 礼仪）
      qemu = {
        package = pkgs.qemu_kvm;
        runAsRoot = true;         # 直通场景省去权限博弈
        swtpm.enable = true;      # vTPM：Win11 类实例的前置条件
        ovmf = {
          enable = true;          # UEFI 固件
          packages = [ (pkgs.OVMFFull.override {
            secureBoot = true;
            tpmSupport = true;
          }).fd ];
        };
      };
    };

    # ---- 魂之三：Docker ----
    docker = {
      enable = true;
      enableOnBoot = true;
      autoPrune.enable = false;   # 保守：绝不自动清理，释放空间必须人工决策
      liveRestore = true;         # dockerd 重启不杀容器（7x24 刚需）
      daemon.settings = {
        log-driver = "journald";  # 容器输出进 journal，汇入镜痕
        data-root = "/var/lib/mirror/instances/docker";
        "exec-opts" = [ "native.cgroupdriver=systemd" ];
      };
    };

    spiceUSBRedirection.enable = true;  # VM 需要时直通 USB 设备
  };

  # 虚拟化管理工具包
  environment.systemPackages = with pkgs; [
    qemu_kvm
    virt-manager        # 备用的本地图形管理（有头模式可用）
    libvirt             # virsh 等 CLI
    incus
    docker
    docker-compose
    qemu-utils          # qemu-img：镜像格式转换/qcow2 管理
  ];

  # 管理用户入组（用户在 session.nix 定义，这里只管权限归属）
  users.users.mirror.extraGroups = [ "incus-admin" "docker" "libvirtd" "kvm" ];
}
