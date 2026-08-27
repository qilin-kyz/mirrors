# ============================================================
#  MirrorOS 主配置 —— 宿主 mirrorhost
# ------------------------------------------------------------
#  镜系命名总览（规格条目 → 实现模块）：
#    镜座(5)   驱动最小集          → modules/kernel.nix + nvidia.nix
#    镜存(1)   ISO 只读 U盘        → modules/storage.nix
#    影分身(8) ISO U盘 rw/ro 双模式 → modules/storage.nix (mirror-iso-mode)
#    镜言(3)   英文单一会话         → modules/session.nix
#    镜痕(6)   全量命令审计         → modules/security.nix
#    镜中世界(8) 系统级快照         → modules/snapshot.nix
#    三生镇魂(11) 三虚拟化栈        → modules/virtualization.nix
#    复古管理台  BIOS 风格 Web UI   → modules/webui.nix + webui/
# ============================================================

{ config, pkgs, lib, ... }:

{
  imports = [
    ./hardware-configuration.nix   # 首启时由 nixos-generate-config 生成
    ../../modules/kernel.nix
    ../../modules/nvidia.nix
    ../../modules/network.nix
    ../../modules/storage.nix
    ../../modules/virtualization.nix
    ../../modules/security.nix
    ../../modules/session.nix
    ../../modules/snapshot.nix
    ../../modules/webui.nix
  ];

  # ---- 放行 unfree：NVIDIA 闭源驱动与 CUDA 工具链均属 unfree ----
  # 本宿主须跑 NVIDIA 显卡（含 GPU 直通、nvtop 监控、计算型 CUDA），
  # 不放开则 nix eval / nixos-rebuild 会因 unfree license 拒绝求值。
  nixpkgs.config.allowUnfree = true;

  networking.hostName = "mirrorhost";
  time.timeZone = "Asia/Shanghai";

  # ---- 经济性：全系统按 Zen 5 (znver5) 微架构编译优化 ----
  # 首次构建会重编译整个系统闭包（数小时，一次性成本），
  # 换取运行期每一行代码都贴着 9600X 的流水线走。
  # 若想先快速部署，临时改成 "x86-64-v3" 即可用官方二进制缓存。
  nixpkgs.hostPlatform = {
    system = "x86_64-linux";
    gcc.arch = "znver5";
    gcc.tune = "znver5";
  };

  nix.settings = {
    experimental-features = [ "nix-command" "flakes" ];
    auto-optimise-store = true;        # 去重硬链接，系统盘更省
    # 二进制缓存依旧可用：不匹配的才本地编译
    substituters = [ "https://cache.nixos.org" ];
  };

  # 保守更新：没有自动升级、没有 channels。一切变更必须显式 flake update。
  system.autoUpgrade.enable = false;
  nix.channel.enable = false;

  # 系统状态版本：定死后永不改动（这是 NixOS 的时间锚点）
  system.stateVersion = "25.11";
}
