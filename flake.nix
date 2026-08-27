{
  description = "MirrorOS —— 为虚拟化而生的不可变宿主系统 (Zen5 9600X + RTX 5070 Ti)";

  # ==========================================================
  #  设计逻辑（按优先级）：
  #    1. 稳定性 7x24   —— 声明式单一事实来源 + 原子切换 + 秒级回滚
  #    2. 经济性        —— znver5 全系统编译优化 + 精确驱动集，无一字节多余
  #    3. 安全性        —— 仅稳定分支（禁止 unstable）+ 网卡物理隔离 + 全量审计
  # ==========================================================

  inputs = {
    # 保守更新策略的根本：只跟随 NixOS 稳定分支。
    # 禁止改成 nixos-unstable —— 这不是建议，是系统契约。
    # 升级方式：nix flake update（跟随下一个稳定版）→ 构建 → 测试 → 切换，
    # 出问题 nixos-rebuild --rollback 一秒回到从前。
    # 注：25.11 已 EOL（冒烟测试启动警告实测），现跟随 26.05 稳定分支。
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # 注：若 6.18 尚未进入该稳定分支，modules/kernel.nix 中有回退方案。
  };

  outputs = { self, nixpkgs }: {
    nixosConfigurations = {
      # 目标宿主系统（装到硬件上跑的那个）
      mirrorhost = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          ./hosts/mirrorhost/configuration.nix
        ];
      };

      # 安装 ISO（镜 0.1-DEMO 安装盘 · 纪墨绫制作）
      installer = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        specialArgs = { inherit self; };   # flake 源码随盘，离线可装
        modules = [
          "${nixpkgs}/nixos/modules/installer/cd-dvd/installation-cd-base.nix"
          ./hosts/installer/configuration.nix
        ];
      };
    };

    # 一条命令出 ISO：
    #   nix build .#iso
    # 产物：result/iso/镜-0.1-DEMO.iso
    packages.x86_64-linux.iso =
      self.nixosConfigurations.installer.config.system.build.isoImage;
  };
}
