# ============================================================
#  kernel.nix —— 镜座：Linux 6.18 + Zen5 优化 + 驱动最小集
# ------------------------------------------------------------
#  "默认只加载 9600X 与 5070Ti 的驱动以及网卡驱动"：
#  用黑名单逐出一切无关驱动，白名单只留必需品。
# ============================================================

{ config, pkgs, lib, ... }:

{
  # ---- Linux 6.18 LTS：9600X 专属内核，不是通用内核 ----
  # 在通用 6.18 之上叠加 structuredExtraConfig：
  # 不为这台机器服务的子系统，直接从内核二进制里消失
  # （不是"不加载"，是"不存在"——攻击面与体积同步归零）。
  # 注意：必须对 linux_x_yy 内核本体 override，再用 linuxPackagesFor
  # 重新生成包集 —— 直接 .override 包集会在求值期炸掉（首轮 CI 实测）。
  boot.kernelPackages =
    let
      baseKernel =
        if pkgs ? linux_6_18
        then pkgs.linux_6_18
        else pkgs.linux_latest;
      customKernel = baseKernel.override {
        structuredExtraConfig = with lib.kernel; {
          # —— 9600X 之外的 CPU 支持：裁 ——
          X86_INTEL_PSTATE = no;          # Intel 调速器，与本机无关
          X86_AMD_PLATFORM_DEVICE = yes;  # AMD 平台设备（zen 必需）

          # —— 无线/蓝牙/红外：这台机器没有这些器官 ——
          WLAN = no;
          WIRELESS = no;
          BT = no;
          IRDA = no;
          NFC = no;
          WIMAX = no;

          # —— 音频子系统：整棵砍掉（黑名单只是不加载，这里是编译期消失）——
          SOUND = no;

          # —— 与本机无关的驱动族 ——
          INFINIBAND = no;
          SCSI_LOWLEVEL_PCMCIA = no;
          PCMCIA = no;
          FIREWIRE = no;
          THUNDERBOLT = no;

          # —— 9600X 虚拟化宿主的调度取向 ——
          HZ_1000 = yes;                  # 1ms tick：实例调度响应拉满
          HZ = freeform "1000";
          PREEMPT_VOLUNTARY = yes;        # 吞吐与延迟的平衡点（全抢占对宿主得不偿失）
          SCHED_CORE = yes;               # SMT 兄弟核调度感知（9600X 12 线程）
          CPU_FREQ_DEFAULT_GOV_PERFORMANCE = yes;  # 出生即满频

          # —— 虚拟化加速件，编译进内核而非模块 ——
          KVM = yes;
          KVM_AMD = yes;
          VHOST_NET = yes;                # virtio-net 内核态数据面，省用户态往返
          VHOST_VSOCK = yes;
        };
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
  boot.kernel.sysctl = {
    "vm.swappiness" = 1;                    # 几乎不换页（且无 swap）
    "vm.overcommit_memory" = 1;             # KVM 内存超售的通行做法
    "net.ipv4.ip_forward" = 1;              # 实例桥接转发
    "net.bridge.bridge-nf-call-iptables" = 0;  # 桥流量不绕 iptables，省一跳
    "fs.inotify.max_user_watches" = 1048576;
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
