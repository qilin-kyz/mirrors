{
  description = "AR8 集群基石系统 —— NixOS 26.05，多节点可复现（AR8 作为系统内置包）";

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
      # AR8 作为「Nix 定制系统中的一个特殊包」
      #
      # 三种消费方式，都指向同一个 derivation（内容哈希一致、可复现）：
      #   1. nix build .#ar8   → 得到 /nix/store/...-ar8-<ver>
      #   2. nix run   .#ar8   → 直接执行 ar8ctl
      #   3. 由 overlay 注入 pkgs.ar8，集群内所有节点看到同一份 AR8
      # ---------------------------------------------------------------------
      overlay = final: prev: {
        ar8 = final.callPackage ./pkgs/ar8 { };
      };

      # ---- NixOS 系统配置变体工厂 ----
      #
      # 三版本变体：同一份 host 配置 + 不同的 services.ar8.systemProfile。
      # 三个版本共用同一套 FSM / 真值源 / AR8 包，只换调度策略与系统层参数。
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
            { services.ar8.systemProfile = "xnu-qos"; }
          ];
        };
        "${hostName}-oh-distributed" = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            { nixpkgs.overlays = [ overlay ]; }
            hostFile
            { services.ar8.systemProfile = "oh-distributed"; }
          ];
        };
        "${hostName}-geo-distributed" = nixpkgs.lib.nixosSystem {
          system = "x86_64-linux";
          modules = [
            { nixpkgs.overlays = [ overlay ]; }
            hostFile
            { services.ar8.systemProfile = "geo-distributed"; }
          ];
        };
      };
    in
    {
      # ---- 包输出（nix build / nix run）----
      packages = forAllSystems ({ system, pkgs }:
        let
          p = pkgs.extend overlay;
        in
        {
          ar8 = p.ar8;
          default = p.ar8;
        });

      # ---- overlay：让 nixpkgs 里出现 pkgs.ar8 ----
      overlays.default = overlay;

      nixosConfigurations =
        (mkVariants "001-thinkpad-x270" ./hosts/001-thinkpad-x270.nix)
        // (mkVariants "vm-ar8-node1" ./hosts/vm-ar8-node1.nix)
        // (mkVariants "vm-ar8-node2" ./hosts/vm-ar8-node2.nix);

      # 未来节点在此追加（共享同一 cluster-base 模块，只覆盖差异）：
      # 注意新节点也要带上 { nixpkgs.overlays = [ overlay ]; } 才能用 pkgs.ar8
      # "rpi4" = nixpkgs.lib.nixosSystem {
      #   system = "aarch64-linux";
      #   modules = [ { nixpkgs.overlays = [ overlay ]; } ./hosts/rpi4.nix ];
      # };
    };
}