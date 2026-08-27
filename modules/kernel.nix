# ============================================================
#  kernel.nix —— 镜座：Linux 6.18 + Zen5 优化 + 驱动最小集
# ------------------------------------------------------------
#  "默认只加载 9600X 与 5070Ti 的驱动以及网卡驱动"：
#  用黑名单逐出一切无关驱动，白名单只留必需品。
# ============================================================

{ config, pkgs, lib, ... }:

{
  # ---- Linux 6.18 LTS：9600X 专属内核，不是通用内核 ----
  # 用 kernelPatches + extraConfig（字符串片段）覆盖通用内核的模块选择：
  #   - 字符串片段不参与 NixOS 选项优先级判定 → 不与 common-config 打架
  #   - last-definition-wins → 我们的 =n/=y 压过通用内核的 =m
  #   - 用 CONFIG_ 前缀标准 Kconfig 符号，序列化器不会读坏
  #   - ignoreConfigErrors：新内核已删除的老符号（IRDA/WIMAX）直接忽略
  # 注意：必须对 linux_x_yy 内核本体 override，再用 linuxPackagesFor
  # 重新生成包集 —— 直接 .override 包集会在求值期炸掉（首轮 CI 实测）。
  boot.kernelPackages =
    let
      baseKernel =
        if pkgs ? linux_6_18
        then pkgs.linux_6_18
        else pkgs.linux_latest;
      customKernel = baseKernel.override {
        ignoreConfigErrors = true;
        kernelPatches = [
          {
            name = "mirror-9600x-trim";
            patch = null;
            extraConfig = ''
              # —— 9600X 之外的 CPU 支持：裁 ——
              CONFIG_X86_INTEL_PSTATE=n
              CONFIG_X86_AMD_PLATFORM_DEVICE=y

              # —— 无线/蓝牙/红外：这台机器没有这些器官 ——
              CONFIG_WLAN=n
              CONFIG_WIRELESS=n
              CONFIG_BT=n
              CONFIG_IRDA=n
              CONFIG_NFC=n
              CONFIG_WIMAX=n

              # —— 音频子系统：整棵砍掉 ——
              CONFIG_SOUND=n

              # —— 与本机无关的驱动族 ——
              CONFIG_INFINIBAND=n
              CONFIG_SCSI_LOWLEVEL_PCMCIA=n
              CONFIG_PCMCIA=n
              CONFIG_FIREWIRE=n
              CONFIG_THUNDERBOLT=n

              # —— 9600X 虚拟化宿主的调度取向 ——
              CONFIG_HZ_1000=y
              CONFIG_PREEMPT_VOLUNTARY=y
              CONFIG_SCHED_CORE=y
              CONFIG_CPU_FREQ_DEFAULT_GOV_PERFORMANCE=y

              # —— 虚拟化加速件，编译进内核而非模块 ——
              CONFIG_KVM=y
              CONFIG_KVM_AMD=y
              CONFIG_VHOST_NET=y
              CONFIG_VHOST_VSOCK=y
            '';
          }
        ];
      };
    in
    pkgs.linuxPackagesFor customKernel;

  # ---- Zen 5 (9600X) 运行期优化：从开机第一秒就贴着流水线走 ----
  boot.kernelParams = [
    "amd_pstate=active"        # Zen5 CPPC 主动调频：延迟最低、能效最优
    "amd_pstate_prefcore=1"    # 优先调度到体质最好的核心（CPPC preferred cores）
    "kvm_amd.nested=1"         # SVM 嵌套虚拟化（VM 里还能跑 VM）
    "kvm_amd.avic=1"           # AMD 虚拟中断控制器：实例中断直通，省一程陷入
    "kvm.report_ignored_msrs=0"
    "pcie_aspm=off"            # 7x24 稳定性优先，关 ASPM 防链路抖动
    "nowatchdog"
    "mitigations=auto"
    "transparent_hugepage=madvise"  # 大页按需，兼顾 KVM 与常规负载
    "skew_tick=1"              # 定时器中断错峰，6 核不一起醒
    # —— 极致档（默认注释，按内存与隔离需求启用）——
    # "default_hugepagesz=1G" "hugepagesz=1G" "hugepages=24"  # KVM 大页：TLB 开销归零级
    # "isolcpus=4,5" "nohz_full=4,5" "rcu_nocbs=4,5"          # 划 2 核给关键实例，tick 都不去打扰
  ];

  # ---- 虚拟化宿主向内核参数微调 ----
  # 安装盘基座 installation-device.nix 已设过 vm.overcommit_memory，
  # 逐项 mkForce 压过基座，避免"重复定义"求值报错（mirrorhost 无此冲突）。
  boot.kernel.sysctl = {
    "vm.swappiness" = lib.mkForce 1;                    # 几乎不换页（且无 swap）
    "vm.overcommit_memory" = lib.mkForce 1;             # KVM 内存超售的通行做法
    "net.ipv4.ip_forward" = lib.mkForce 1;              # 实例桥接转发
    "net.bridge.bridge-nf-call-iptables" = lib.mkForce 0;  # 桥流量不绕 iptables，省一跳
    "fs.inotify.max_user_watches" = lib.mkForce 1048576;
  };

  # ---- 驱动白名单（镜座） ----
  # 注：KVM/VHOST 已编译进内核（见上方 structuredExtraConfig），此处不再重复
  boot.kernelModules = [
    "r8169"            # Realtek RTL8111  1GbE（宿主管理口）
    "r8152"            # Realtek RTL8152B 2.5GbE USB（实例专用对外口）
    "vfio-pci"         # 备用的 PCI 直通通道（需要时整机直通显卡）
    "nvidia" "nvidia_modeset" "nvidia_uvm" "nvidia_drm"  # 详见 nvidia.nix
  ];

  # ---- 驱动黑名单：不在这台机器职责表上的，一律不准加载 ----
  boot.blacklistedKernelModules = [
    "nouveau"          # 与 NVIDIA 官方驱动互斥
    "amdgpu"           # 9600X 核显不启用（宿主无头/简易控制台足够）
    "snd_hda_intel" "snd_pcsp"     # 无音频职责
    "bluetooth" "btusb"            # 无蓝牙职责
    "sp5100_tco"       # 看门狗与 nowatchdog 策略一致关闭
    "thunderbolt"      # 无雷电外设
    "firewire_core" "firewire_ohci"
  ];

  # initrd 只带开机必需品
  boot.initrd.kernelModules = [ "nvme" "xhci_pci" "usb_storage" ];

  # CPU 微码更新：稳定性的一件大事
  hardware.cpu.amd.updateMicrocode = true;

  # 性能调节器：amd-pstate active 模式下的性能取向
  powerManagement.cpuFreqGovernor = "performance";
}
