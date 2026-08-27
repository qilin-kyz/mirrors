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

let
  # libvirt 网络在 26.05 已不再有 virtualisation.libvirtd.networks 声明式选项，
  # 改为把网络 XML 落到 /var/lib/libvirt/qemu/networks/。这里以 writeText 固化
  # 一个「桥接到已建 br-vm」的网络（forward mode='bridge'，不接管 DHCP，
  # 实例直接吃 br-vm 上游网段），再由下方 systemd 服务 net-define + net-start。
  mirrorVmNetworkXml = pkgs.writeText "mirror-vm-network.xml" ''
    <network>
      <name>mirror-vm</name>
      <forward mode="bridge"/>
      <bridge name="br-vm"/>
    </network>
  '';
in
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
      onBoot = "ignore";          # 宿主拉电不自动拉起实例，避免风暴
      onShutdown = "shutdown";    # 宿主关机时优雅关闭实例（7x24 礼仪）
      qemu = {
        package = pkgs.qemu_kvm;
        runAsRoot = true;         # 直通场景省去权限博弈
        swtpm.enable = true;      # vTPM：Win11 类实例的前置条件
        # 注：qemu.ovmf 在该版本已被移除（OVMF 固件随 QEMU 默认就位），
        # 故不再显式配置，断言会拒绝任何非空设置。
      };
      # 实例上联：通过下方 mirror-vm 网络挂到 2.5G 实例桥 br-vm
      # （不再走 virbr0 NAT —— 实例流量应独立出网，见 network.nix）
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

  # 把 mirror-vm 网络（桥接 br-vm）注入 libvirt：定义→拉起→开机自启。
  # 用 net-info 做幂等判断，重复执行不会报错；并等待上联桥 br-vm 就绪。
  systemd.services.libvirt-net-mirror-vm = {
    description = "Define & start libvirt 'mirror-vm' network (bridge to br-vm)";
    wantedBy = [ "multi-user.target" ];
    after = [ "libvirtd.service" "systemd-networkd.service" "network.target" ];
    requires = [ "libvirtd.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      VMNET_XML=${mirrorVmNetworkXml}
      VIRSH=${pkgs.libvirt}/bin/virsh

      # 等待 libvirtd 守护就绪
      for i in $(seq 1 20); do
        $VIRSH connect qemu:///system >/dev/null 2>&1 && break
        sleep 1
      done

      # 等待上联桥 br-vm 就绪（由 systemd-networkd 建立，见 network.nix）
      for i in $(seq 1 20); do
        ${pkgs.iproute2}/bin/ip link show br-vm >/dev/null 2>&1 && break
        sleep 1
      done

      # 若尚未定义则定义（幂等）
      if ! $VIRSH net-info mirror-vm >/dev/null 2>&1; then
        $VIRSH net-define "$VMNET_XML" || true
      fi

      # 若未激活则拉起
      if ! $VIRSH net-info mirror-vm 2>/dev/null | grep -q "Active:.*yes"; then
        $VIRSH net-start mirror-vm || true
      fi

      # 设为开机自启
      $VIRSH net-autostart mirror-vm || true
    '';
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
