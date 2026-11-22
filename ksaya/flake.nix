{
  description = "KSAYA 集群基石系统 —— NixOS 26.05，多节点可复现（KSAYA 作为系统内置包）";

  inputs = {
    # 锁定到 26.05 稳定频道，符合「无频繁更新 / 可复现」
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  };

  outputs = { self, nixpkgs }:
    let
      # 支持的多架构：新增 aarch64 节点时不必改结构
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f {
        inherit system;
        pkgs = nixpkgs.legacyPackages.${system};
      });

      # ---------------------------------------------------------------------
      # KSAYA 作为「Nix 定制系统中的一个特殊包」
      #
      # 三种消费方式，都指向同一个 derivation（内容哈希一致、可复现）：
      #   1. nix build .#ksaya   → 得到 /nix/store/...-ksaya-<ver>
      #   2. nix run   .#ksaya   → 直接执行 ksayactl
      #   3. 由 overlay 注入 pkgs.ksaya，集群内所有节点看到同一份 KSAYA
      # ---------------------------------------------------------------------
      overlay = final: prev: {
        ksaya = final.callPackage ./pkgs/ksaya { };
      };

      # ---- NixOS 系统配置变体工厂 ----
      #
      # 四版本变体：同一份 host 配置 + 不同的 services.ksaya.systemProfile。
      # 四个版本共用同一套 FSM / 真值源 / KSAYA 包，只换调度策略与系统层参数。
      # 命名规则：<host>-<profile>，baseline 变体不带后缀（保持向后兼容）。
      #
      # ⚠️ 必须放在 let 块：nixosConfigurations 要引用它，而 outputs 返回的
      # attrset 不是 rec，键之间不能互引（放那里会 undefined variable 'mkVariants'）。
      # 这是云端 nix eval 抓出来的作用域错误，一开始括号配平查不到。
      mkVariants = hostName: hostFile: {
        "${hostName}" = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            { nixpkgs.overlays = [ overlay ]; }
            hostFile
          ];
        };
        "${hostName}-xnu-qos" = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            { nixpkgs.overlays = [ overlay ]; }
            hostFile
            { services.ksaya.systemProfile = "xnu-qos"; }
          ];
        };
        "${hostName}-oh-distributed" = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            { nixpkgs.overlays = [ overlay ]; }
            hostFile
            { services.ksaya.systemProfile = "oh-distributed"; }
          ];
        };
        "${hostName}-geo-distributed" = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            { nixpkgs.overlays = [ overlay ]; }
            hostFile
            { services.ksaya.systemProfile = "geo-distributed"; }
          ];
        };
      };
    in
    {
      # ---- 包输出（nix build / nix run）----
      # iso 只对 x86_64-linux 有意义（安装盘），用 optionalAttrs 并在 forAllSystems
      # 单一定义里合并，避免同 attrset 里 packages 键出现两次（会报 already defined）。
      packages = forAllSystems ({ system, pkgs }:
        let
          p = pkgs.extend overlay;
        in
        {
          ksaya = p.ksaya;
          default = p.ksaya;
        } // nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
          # 出厂 ISO：默认产出 KSAYA 1.0 融合版；geo 版仍可用
          #   nix build .#nixosConfigurations.ksaya-geo-installer.config.system.build.isoImage
          iso = self.nixosConfigurations."ksaya-1.0-installer".config.system.build.isoImage;
        });

      # ---- overlay：让 nixpkgs 里出现 pkgs.ksaya ----
      overlays.default = overlay;

      nixosConfigurations =
        (mkVariants "001-thinkpad-x270" ./hosts/001-thinkpad-x270.nix)
        // (mkVariants "vm-ksaya-node1" ./hosts/vm-ksaya-node1.nix)
        // (mkVariants "vm-ksaya-node2" ./hosts/vm-ksaya-node2.nix)
        // {
          # ---- 第四方案独立整机：装机后就是 geo-distributed 系统 ----
          ksaya-geo-standalone = nixpkgs.lib.nixosSystem {
            system = "x86_64-linux";
            modules = [
              { nixpkgs.overlays = [ overlay ]; }
              ./hosts/ksaya-geo-standalone.nix
            ];
          };
          # ---- 第四方案安装 ISO（可引导装机盘，flake 源码随盘离线可装）----
          # 一张盘装多台：nixos-install --flake <path>#ksaya-geo-standalone
          # 或盘内入口 ksaya-geo-install /dev/<磁盘>
          ksaya-geo-installer = nixpkgs.lib.nixosSystem {
            system = "x86_64-linux";
            specialArgs = { inherit self; };   # flake 源码随盘（storeContents）
            modules = [
              { nixpkgs.overlays = [ overlay ]; }
              "${nixpkgs}/nixos/modules/installer/cd-dvd/installation-cd-base.nix"
              ./hosts/ksaya-geo-installer.nix
            ];
          };
          # ---- KSAYA 1.0 出厂整机：基础系统 × KSAYA 融合版（默认 geo-distributed）----
          # 出厂即控制面 + 异构硬件宽口径 + 唯一标识上报 + 版本标识 1.0。
          "ksaya-1.0" = nixpkgs.lib.nixosSystem {
            system = "x86_64-linux";
            modules = [
              { nixpkgs.overlays = [ overlay ]; }
              ./hosts/ksaya-1.0.nix
            ];
          };
          # ---- KSAYA 1.0 出厂安装 ISO：一张盘装出 1.0 系统，flake 随盘离线可装 ----
          # 盘内入口 ksaya-install /dev/<磁盘>，或 nixos-install --flake <path>#ksaya-1.0
          "ksaya-1.0-installer" = nixpkgs.lib.nixosSystem {
            system = "x86_64-linux";
            specialArgs = { inherit self; };   # flake 源码随盘（storeContents）
            modules = [
              { nixpkgs.overlays = [ overlay ]; }
              "${nixpkgs}/nixos/modules/installer/cd-dvd/installation-cd-base.nix"
              ./hosts/ksaya-1.0-installer.nix
            ];
          };
        };

      # 未来节点在此追加（共享同一 cluster-base 模块，只覆盖差异）：
      # 注意新节点也要带上 { nixpkgs.overlays = [ overlay ]; } 才能用 pkgs.ksaya
      # "rpi4" = nixpkgs.lib.nixosSystem {
      #   system = "aarch64-linux";
      #   modules = [ { nixpkgs.overlays = [ overlay ]; } ./hosts/rpi4.nix ];
      # };
    };
}