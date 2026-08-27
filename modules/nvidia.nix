# ============================================================
#  nvidia.nix —— RTX 5070 Ti (Blackwell/GB203) 全套官方驱动
# ------------------------------------------------------------
#  要求："包含完整的 Nvidia 非自由软件包在内的全套驱动"。
#  要点：
#   - Blackwell 世代只支持 open kernel modules (GSP 固件)，
#     即 open = true，这是 5070 Ti 的硬性要求而非选项；
#   - 驱动版本必须 ≥ 570（5070 Ti 的支持起点），latest 通道保证；
#   - nvidia-container-toolkit 让 Docker/Incus 容器直通 GPU（CDI）。
# ============================================================

{ config, pkgs, lib, ... }:

{
  # 允许非自由软件（仅此一处开口，其余系统保持自由）
  nixpkgs.config.allowUnfreePredicate = pkg:
    builtins.elem (lib.getName pkg) [
      "nvidia-x11" "nvidia-settings" "nvidia-persistenced"
    ];

  services.xserver.videoDrivers = [ "nvidia" ];  # 无 X 会话，仅确保驱动链路完整

  hardware.nvidia = {
    modesetting.enable = true;              # KMS：控制台与直通的前提
    open = true;                            # Blackwell 强制 GSP open 模块
    package = config.boot.kernelPackages.nvidiaPackages.latest;
    nvidiaSettings = false;                 # 无头系统，不需要设置面板
    nvidiaPersistenced = true;              # 持久模式：GPU 不掉线，容器随取随用
    powerManagement.enable = false;         # 7x24 不休眠，关掉一切挂起路径
    forceFullCompositionPipeline = false;   # 无显示输出，无需合成管线
  };

  hardware.graphics = {
    enable = true;                          # Vulkan/OpenGL ICD + 32位兼容库
    enable32Bit = true;
  };

  # Docker / Incus GPU 直通（CDI 方式，docker run --gpus / incus gpu 设备）
  # 注：26.05 起 hardware.nvidia-container-toolkit 模块选项已被移除，
  # 改为直接安装包，由用户/镜像脚本运行 `nvidia-ctk cdi generate` 生成 CDI 规格。
  environment.systemPackages = with pkgs; [
    nvtopPackages.nvidia       # GPU 占用监控
    cudaPackages.cudatoolkit  # CUDA 工具链（计算型实例需要）
    nvidia-container-toolkit  # nvidia-ctk / 容器运行时（CDI 直通需手动生成规格）
  ];

  # 给 libvirtd/Incus 的 GPU 设备可见性兜底
  services.udev.extraRules = ''
    KERNEL=="nvidia*", MODE="0666"
  '';
}
