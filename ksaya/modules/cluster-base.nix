# KSAYA 集群公共基座：所有节点共享的策略。
# 节点差异（主机名、IP、引导、CPU 微码、GPU、升级时间窗）放在 hosts/<节点>.nix。
{ config, pkgs, ... }:

{
  imports = [
    ./ksaya.nix           # KSAYA 集群控制平面模块
    ./ksaya-profile.nix   # 三版本系统 profile（baseline / xnu-qos / oh-distributed）
  ];

  # ---- 软件源策略 ----
  nixpkgs.config.allowUnfree = true;   # 闭源驱动/软件（NVIDIA 等）需要

  # ---- Nix 本身：启用 flakes ----
  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  # ---- 时区与语言（英文极简服务器）----
  time.timeZone = "Asia/Shanghai";
  i18n.defaultLocale = "en_US.UTF-8";
  console.keyMap = "us";

  # ---- 单用户 + 仅密钥登录（mutableUsers=false：完全声明式，密码不可改）----
  users.mutableUsers = false;
  users.users.user = {
    isNormalUser = true;
    description = "user";
    extraGroups = [ "wheel" ];
    openssh.authorizedKeys.keys = [
      # 站长本人公钥（~/.ssh/id_ed25519.pub，86800543@qq.com）
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCNW9Pf+L99HEDW0/hA+tI8r1ggd0Lz4ARaoxzCvcKA 86800543@qq.com"
    ];
  };
  users.users.root.openssh.authorizedKeys.keys = [
      # 站长本人公钥；如不需要 root 密钥登录，可整块删除（不影响 user + wheel 免密 sudo）
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCNW9Pf+L99HEDW0/hA+tI8r1ggd0Lz4ARaoxzCvcKA 86800543@qq.com"
  ];

  security.sudo.wheelNeedsPassword = false;   # wheel 组免密 sudo

  # ---- SSH：仅密钥，禁密码 / 禁 root 密码登录 ----
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "prohibit-password";   # root 仅密钥（你表上 S3 的要求）
    };
  };

  # ---- 自动升级：分批、不自动重启 ----
  system.autoUpgrade = {
    enable = true;
    allowReboot = false;          # 重启由各节点按批次手动执行，避免集群被打断
    flake = "/etc/nixos";         # 26.05 起 flake 选项是「flake 路径/URI」字符串，不再是布尔
    # dates 由各节点在 hosts 中覆盖，错开时间窗口，避免集群同时升级
  };

  # ---- 防火墙：默认拒绝，仅开白名单 ----
  networking.firewall.enable = true;
  networking.firewall.allowedTCPPorts = [ 22 ];   # SSH
  # TODO(部署前必填): 集群内部互访端口（KSAYA / etcd / overlay / WireGuard 等）。
  # 建议只对「受信任的内网网卡/子网」放行，而不是对全网开放，例如：
  # networking.firewall.interfaces."<内网网卡>".allowedTCPPorts = [ <端口...> ];
  # networking.firewall.trustedInterfaces = [ "<内网网卡>" ];

  # ---- 显卡：本节点 Intel 核显，启用 graphics 供 KSAYA 按需使用（无桌面、无 X）----
  hardware.graphics.enable = true;

  # ---- 基础工具：极简 ----
  environment.systemPackages = with pkgs; [
    tmux        # 你勾选的
    git         # flake / KSAYA 拉取需要
    vim         # 应急编辑（你未勾选；不需要可删）
  ];

  # ---- Git 身份（26.05 已移除 programs.git.userName/userEmail，改用 config 子模块）----
  programs.git = {
    enable = true;
    config.user.name = "user";
    config.user.email = "RTX-28168-KYZ@outlook.com";
  };

  # ---- KSAYA 集群控制平面：框架启用（后端按节点在 hosts 中开启）----
  services.ksaya.enable = true;
  # 工作负载能力（KVM / k8s）与各后端（fencing/storage/网络/HA）在 hosts/<节点>.nix 中按需开启

  # ===================================================================
  # 效率优先的系统调优（把「多节点拼成一台大电脑」做到底层）
  #
  # 设计参照（只借设计思想，不搬实现 —— 见 KSAYA-交付说明 第二十五章）：
  #   * XNU 的 QoS 分级 -> 用 cgroup v2 的 cpu.weight / io.weight 做分级资源权重；
  #   * GCD 的并发度自适应 -> 内核调度器按实际负载而非固定时片分配；
  #   * OpenHarmony 的确定性时延 -> 压缩内存回收与 IO 路径的不确定停顿。
  #
  # 说明：NixOS 的系统闭包、k3s、驱动链全部绑定 Linux ABI，
  # 无法把 XNU / LiteOS 内核直接替换进来（Apple 也未开源完整 macOS）。
  # 因此这里落地的是「可迁移的设计」，不是内核替换。
  # ===================================================================

  # ---- zram：内存压缩交换。节点内存小（node1 仅 1.7GiB），这是最直接的效率收益 ----
  # 之前是运行时手打 sysfs，重启即失效；这里固化为声明式，开机自动重建。
  zramSwap = {
    enable = true;
    algorithm = "lz4";          # lz4 比 zstd 快，压缩比在这类负载下够用
    memoryPercent = 50;         # 交换容量 = 物理内存的 50%（按压缩后折算，实际占用更小）
    priority = 100;             # 高于任何磁盘 swap，优先使用
  };

  # ---- 内存与 IO：减少不确定停顿（确定性时延）----
  boot.kernel.sysctl = {
    # 内存压力下更积极地把冷页换到 zram，避免直接 OOM 杀 Pod
    "vm.swappiness" = 100;
    # 脏页回收：降低单次回写批量，缩短 IO 尖峰持续时间
    "vm.dirty_ratio" = 10;
    "vm.dirty_background_ratio" = 5;
    # 允许内存过量承诺：k8s 的 requests 是软保证，物理上仍有 zram 兜底
    "vm.overcommit_memory" = 1;
    # 连接队列：集群内大量短连接（作业/探针）时避免丢 SYN
    "net.core.somaxconn" = 4096;
    "net.ipv4.tcp_max_syn_backlog" = 4096;
    # TCP 快速回收与复用，降低作业间连接的 TIME_WAIT 堆积
    "net.ipv4.tcp_tw_reuse" = 1;
    "net.ipv4.tcp_fin_timeout" = 15;
    # inotify：k3s/kubelet 监听大量文件，默认上限容易耗尽
    "fs.inotify.max_user_instances" = 1024;
    "fs.inotify.max_user_watches" = 524288;
    # 文件句柄：容器与 k3s 开销
    "fs.file-max" = 2097152;
    # 减少内核同一片内存的迁移抖动（NUMA/透明大页在虚机里收益不稳定）
    "vm.zone_reclaim_mode" = 0;
  };

  # ---- 调度器：按实际负载自适应，而非固定时片 ----
  boot.kernelParams = [
    # 让空闲 CPU 更快进入低功耗状态，同时保持唤醒延迟可接受
    "processor.max_cstate=1"
  ];

  # ---- cgroup v2 资源分级（XNU QoS 的等价物）----
  # 作业分片（ksaya-shard）是批处理，k3s 控制面是延迟敏感型。
  # 用 systemd 的 slice 权重把两者分开，避免批处理把控制面拖慢。
  systemd.slices.ksaya-batch = {
    description = "KSAYA 批处理作业（低权重，让出控制面）";
    sliceConfig = {
      CPUWeight = 50;          # 默认 100；批处理让一半
      IOWeight = 50;
      MemoryHigh = "70%";      # 软上限，超过后优先回收
    };
  };
  systemd.slices.ksaya-control = {
    description = "KSAYA 控制面（延迟敏感，高权重）";
    sliceConfig = {
      CPUWeight = 300;
      IOWeight = 300;
    };
  };

  system.stateVersion = "26.05";   # = 安装时版本，装完千万别改
}
