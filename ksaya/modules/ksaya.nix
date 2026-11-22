# modules/ksaya.nix
# KSAYA 集群控制平面 —— 声明式选项骨架（2026-11-18 精简后）
#
# 用户裁决（2026-11-18）：
#   - 删除：语义压缩引擎、混沌韧性测试、Web 控制台、通用云资源兼容层、
#           自研 SigV4/S8、加密（cipher）、旧 Minecraft 裸机部署。
#   - 带宽限制改由系统层（tc）强制实施，调度按任务档位动态分配。
#   - 云资源将来按「单一目标专门接入」，不再有通用兼容层。
#   - 只保留一个最简单的 S3 API（MinIO 单实例）。
#   - Minecraft 作为集群真实 workload（StatefulSet）在 k8s 内运行。
#
# 本模块只负责：k3s 底座、防火墙、镜像加速、节点画像标签，
# 以及把 **KSAYA 作为单个 Nix 包** 注入系统（pkgs.ksaya，见 flake.nix 的 overlay
# 与 pkgs/ksaya/default.nix）。业务逻辑全部在包内，不再逐文件 environment.etc 分发。
#
# 2026-11-20 变更：KSAYA 由「散装脚本分发」升级为「Nix 定制系统中的一个正规包」：
#   - 整份 KSAYA 是一个 store 路径，内容哈希固定，可审计、可回滚、可 nix run；
#   - python3 + PyYAML 经 makeWrapper 固化，不依赖宿主 PATH 的 python3；
#   - /nix/store 只读 → 真值源装到 /etc/ksaya，可写状态走 /var/lib/ksaya。

{ config, pkgs, lib, ... }:

let
  cfg = config.services.ksaya;

  # StateDirectory 只能写相对 /var/lib 的名字，直接写死 "ksaya" 会在用户覆盖
  # stateDir 时和 tmpfiles 规则打架（两个不同目录，权限各建一份）。
  # 这里从 stateDir 推导，保证「一处声明、两处一致」。
  stateDirName =
    if lib.hasPrefix "/var/lib/" cfg.stateDir
    then lib.removePrefix "/var/lib/" cfg.stateDir
    else null;
in
{
  options.services.ksaya = {
    enable = lib.mkEnableOption "KSAYA 集群控制平面";

    # ===== 工作负载能力（节点可被分配成 KVM 或 K8s）=====
    workload = {
      kvm = {
        enable = lib.mkEnableOption "KVM / QEMU 虚拟化（libvirt 纳管）";
      };
      kubernetes = {
        enable = lib.mkEnableOption "Kubernetes 容器（k3s）";
        role = lib.mkOption {
          type = lib.types.enum [ "server" "agent" ];
          default = "server";
          description = "k3s 角色：server（含控制面，默认也跑负载）/ agent（纯工作节点）";
        };
        clusterInit = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "首个 HA server 置 true 以切到内嵌 etcd；单机或加入已有集群置 false";
        };
        token = lib.mkOption {
          type = lib.types.str;
          default = "";
          description = "加入集群的共享令牌（明文会进 Nix store，敏感请用 tokenFile）";
        };
        tokenFile = lib.mkOption {
          type = lib.types.str;
          default = "";
          description = "指向含令牌的文件（推荐，避免明文）";
        };
        serverAddr = lib.mkOption {
          type = lib.types.str;
          default = "";
          description = "agent 加入时指向 server，如 https://<ip>:6443";
        };
        registryMirrors = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [];
          description = ''
            容器镜像加速地址列表（写入 /etc/rancher/k3s/registries.yaml）。
            国内部署必填：docker.io 直连不通（registry-1.docker.io 超时）。

            ⚠️ 实测坑：
            - docker.1panel.live 对大镜像（debian/temurin）限速极狠或返回损坏内容；
              busybox 正常。MC 这类大镜像用 mc/build-mc-image.sh 本地构建 OCI 导入。
            - k3s 的 containerd 把 docker.io/rancher/* 解析到独立 endpoint，
              不走 docker.io/library 的 mirror；local-path helper 镜像要改成
              节点已有的 docker.io/library/busybox。
          '';
        };
        nodeIP = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = ''
            固定 k3s 节点地址（--node-ip）。
            实测踩坑：k3s 默认可能选中 IPv6 隐私地址（每次重启都变），
            导致 etcd 成员 ID 变化、节点永远卡 activating。必须钉死。
          '';
        };
        nodeProfile = lib.mkOption {
          type = lib.types.nullOr (lib.types.enum [ "control" "standard" "edge" ]);
          default = null;
          description = ''
            节点资源画像档位（驱动亲和性调度）。
            ⚠️ 仅 server(control-plane) 节点本地可达 API，能 self-label；
               agent 节点需由 server 节点代打（见 ksaya-demo/apply-node-labels.sh）。
          '';
        };
      };
      lxd = {
        enable = lib.mkEnableOption "LXD 容器/虚拟机（占位）";
      };
    };

    # 九、节点上报（唯一标识 + 心跳）。对应「机器开机后固定向公网机发信息，
    # 并被公网机接收」。node-id 在上报脚本里生成并固化，本模块只负责：
    #   1) 把上报地址写进 /etc/ksaya/report.conf（可改，改完 nixos-rebuild 生效）；
    #   2) 把 profile / k3s 角色写进 /etc/ksaya/ 供上报脚本读取；
    #   3) 开机跑一次 + systemd timer 周期兜底上报。
    report = {
      enable = lib.mkEnableOption "开机自动向公网/控制台节点上报心跳（含唯一 node-id）";
      url = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = ''
          上报端点，如 http://182.92.237.205/report（走标准 TCP 80）。
          空则回落到 ksaya-report.sh 内置默认值（同样指向公网 collector）。
          后期改公网机 IP 只改这里的 url 一个字段即可。
        '';
      };
      interval = lib.mkOption {
        type = lib.types.str;
        default = "*:0/1";
        description = "systemd timer OnCalendar 表达式（默认每分钟）";
      };
    };

    # 一、资源池化与异构算力调度（占位，后端分阶段接入）
    scheduler = {
      enable = lib.mkEnableOption "异构算力调度（QSV / GPU / 云端 spot）";
      defaultBackend = lib.mkOption {
        type = lib.types.enum [ "k8s-affinity" "custom" "none" ];
        default = "none";
        description = "调度后端：k8s 节点亲和 / 自研仲裁 / 关闭";
      };
    };

    # 二、物理级硬件自愈（带外 Fencing，占位）
    fencing = {
      enable = lib.mkEnableOption "带外自愈（心跳丢失→强制重启）";
      provider = lib.mkOption {
        type = lib.types.enum [ "miot" "cloud" "none" ];
        default = "none";
        description = "强制重启手段：米家 IoT / 云厂商 API / 关闭";
      };
      miot = {
        endpoint = lib.mkOption { type = lib.types.str; default = ""; description = "米家/HA API 地址"; };
        token = lib.mkOption { type = lib.types.str; default = ""; description = "API token（敏感，用 age/sops 注入）"; };
        plugEntity = lib.mkOption { type = lib.types.str; default = ""; description = "目标插座实体 / 设备 ID"; };
        powerOffDelaySec = lib.mkOption { type = lib.types.int; default = 10; };
      };
      cloud = {
        provider = lib.mkOption { type = lib.types.enum [ "aliyun" "tencent" "oracle" "none" ]; default = "none"; };
        region = lib.mkOption { type = lib.types.str; default = ""; };
        accessKey = lib.mkOption { type = lib.types.str; default = ""; description = "敏感，建议 age/sops"; };
        secretKey = lib.mkOption { type = lib.types.str; default = ""; };
        instanceId = lib.mkOption { type = lib.types.str; default = ""; };
      };
    };

    # 三、极端容灾与数据保护（占位）
    disasterRecovery = {
      enable = lib.mkEnableOption "极端容灾与云端接管";
      coldArchive = lib.mkOption { type = lib.types.str; default = ""; description = "冷归档目标（如 S3/oss 归档桶）"; };
    };

    # 四、最简 S3（MinIO 单实例）。
    #   不做多厂商通用兼容层；将来接某个具体云资源时单独写接入代码。
    storage = {
      enable = lib.mkEnableOption "最简对象存储（MinIO 单实例 S3）";
      minio = {
        enable = lib.mkEnableOption "MinIO 对象存储（单实例，最简 S3 API）";
        dataDir = lib.mkOption { type = lib.types.str; default = "/var/lib/minio"; };
      };
    };

    # 四之二、系统层带宽策略（tc/netem）。
    #   取代原「语义压缩」：不在应用层压流量，由调度按任务档位分配带宽额度，
    #   对高流量/低价值任务强制降速。策略真值源在 ksaya.config.yaml features.bandwidth，
    #   渲染与下发由 ksayactl bandwidth 完成（本模块只声明开关）。
    bandwidth = {
      enable = lib.mkEnableOption "系统层带宽策略（tc 档位限速，调度侧动态分配）";
    };

    # 五、全链路网络冗余 / 断网保命（占位）
    networkRedundancy = {
      enable = lib.mkEnableOption "多链路冗余（蜂窝哨兵）";
      cellularInterface = lib.mkOption { type = lib.types.str; default = "ppp0"; description = "4G/5G 工业模组接口"; };
      sentinel = lib.mkEnableOption "独立 ARM 哨兵节点（树莓派 + SIM）";
    };

    # 六、HA 与多集群冗余（占位）
    ha = {
      enable = lib.mkEnableOption "高可用与多集群冗余";
      multiCluster = lib.mkOption { type = lib.types.enum [ "karmada" "none" ]; default = "none"; };
    };

    # 七、稳定核心与集群 workload 分发
    demo = {
      enable = lib.mkEnableOption "把 KSAYA 稳定核心与 workload 清单分发到本机（仅 server 角色生效）";
      manifestDir = lib.mkOption {
        type = lib.types.str;
        default = "/etc/ksaya-demo";
        description = "清单落盘目录";
      };
    };

    # 八、KSAYA 包（作为 Nix 定制系统的一部分，不再散装分发）
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.ksaya;
      defaultText = lib.literalExpression "pkgs.ksaya";
      description = ''
        KSAYA 主包。默认取 overlay 注入的 pkgs.ksaya（见 flake.nix 的 overlays.default），
        也可显式指到别的版本，例如 pkgs.ksaya.override { version = "..."; }。
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/ksaya";
      description = ''
        可写状态目录（任务日志 / history / applied.json）。
        ⚠️ 必须可写：Nix store 里的包是只读的，不能把 state 放进去。
        必须与 pkgs/ksaya/default.nix 的 defaultStateDir 保持一致 —— 那边是 wrapper
        的 --set-default 兜底值。改这里要同步改那边，否则非登录 shell 会落回旧路径。
      '';
    };

    configFile = lib.mkOption {
      type = lib.types.str;
      default = "/etc/ksaya/ksaya.config.yaml";
      description = ''
        运行时真值源路径。默认由模块从包内副本安装到 /etc（便于就地查看与审计），
        但运行时以本路径为准；想改用别的配置就覆盖这个选项。
        必须与 pkgs/ksaya/default.nix 的 defaultConfig 保持一致（理由同上）。
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # ---- 工作负载能力落地 ----
    virtualisation.libvirtd.enable = lib.mkIf cfg.workload.kvm.enable true;

    services.k3s = lib.mkIf cfg.workload.kubernetes.enable {
      enable = true;
      role = cfg.workload.kubernetes.role;
      clusterInit = cfg.workload.kubernetes.clusterInit;
      token = lib.mkIf (cfg.workload.kubernetes.token != "") cfg.workload.kubernetes.token;
      tokenFile = lib.mkIf (cfg.workload.kubernetes.tokenFile != "") cfg.workload.kubernetes.tokenFile;
      serverAddr = lib.mkIf (cfg.workload.kubernetes.serverAddr != "") cfg.workload.kubernetes.serverAddr;
      nodeIP = lib.mkIf (cfg.workload.kubernetes.nodeIP != null) cfg.workload.kubernetes.nodeIP;
    };
    # ⚠️ environment.systemPackages 在本模块只允许出现一次（NixOS 同模块内重复
    # 定义同一选项会报 "already defined"）。k3s 与 KSAYA 包都必须并到这里，不能各写一处。
    # 云端 nix eval 抓出过这个错误：曾在第 235 行（k3s）与第 299 行（KSAYA 包）重复定义。
    #
    # ⚠️ 类型陷阱：systemPackages 是 listOf package，不能用 lib.mkIf 拼 `++` ——
    # lib.mkIf 返回惰性 if 结构（非 list），`++` 求值会报 "expected a list but
    # found a set: { _type = "if"; ... }"。要用 lib.optionals（返回真 list）。
    environment.systemPackages =
      lib.optionals cfg.workload.kubernetes.enable [ pkgs.k3s ]
      ++ lib.optionals cfg.demo.enable [ cfg.package ];

    # k3s 多节点所需端口（NixOS 防火墙默认全拒，必须显式放开）：
    #   6443/TCP  apiserver；2379/2380/TCP  etcd（多 server HA）；
    #   10250/TCP kubelet；30000-32767/TCP NodePort；UDP 8472 flannel VXLAN。
    # 9000：最简 S3（MinIO）。9100：作业 broker。
    #   实测坑：NixOS 的 nixos-fw 是 oneshot，is-active 显示 inactive 但规则仍生效；
    #   未放行端口时节点间 curl 超时但 ping 正常，极易误判成「防火墙没开」。
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.workload.kubernetes.enable
      [ 6443 2379 2380 10250 9000 9100 ];
    networking.firewall.allowedTCPPortRanges = lib.mkIf cfg.workload.kubernetes.enable [ { from = 30000; to = 32767; } ];
    networking.firewall.allowedUDPPorts = lib.mkIf cfg.workload.kubernetes.enable [ 8472 ];

    # 节点资源画像自打标（仅 server 节点；agent 由脚本代打）
    systemd.services.ksaya-node-profile = lib.mkIf (
      cfg.workload.kubernetes.enable
      && cfg.workload.kubernetes.role == "server"
      && cfg.workload.kubernetes.nodeProfile != null
    ) {
      description = "KSAYA 节点资源画像标签（self-label on server）";
      wantedBy = [ "multi-user.target" ];
      after = [ "k3s.service" ];
      requires = [ "k3s.service" ];
      path = [ pkgs.k3s pkgs.busybox ];
      script = ''
        for i in $(seq 1 30); do
          k3s kubectl get node "$(hostname)" >/dev/null 2>&1 && break
          sleep 2
        done
        N="$(hostname)"
        P="${cfg.workload.kubernetes.nodeProfile}"
        case "$P" in
          control)   TIER=high;     EDGE_LBL="";;
          standard)  TIER=standard; EDGE_LBL="";;
          edge)      TIER=low;      EDGE_LBL="edge=true";;
        esac
        CORES="$(nproc)"
        MEM_KIB="$(awk '/MemTotal/{print $2}' /proc/meminfo)"
        MEM_MIB=$(( MEM_KIB / 1024 ))
        k3s kubectl label node "$N" --overwrite \
          node-class="$P" tier="$TIER" cpu-cores="$CORES" mem-mib="$MEM_MIB" \
          region=home-lab $EDGE_LBL || true
      '';
    };

    # ---- 配置下发：镜像加速 + KSAYA 真值源，都用 environment.etc 这一处声明 ----
    # ⚠️ environment.etc 在本模块只允许出现一次（同模块重复定义同一选项报
    # "already defined"）。曾把 registries.yaml 与 ksaya.config.yaml 分两处写，
    # 云端 nix eval 报错；这里用 lib.mkMerge 合并，条件各自独立。
    environment.etc = lib.mkMerge [
      (lib.mkIf (cfg.workload.kubernetes.registryMirrors != [ ]) {
        "rancher/k3s/registries.yaml" = {
          text = lib.concatStringsSep "\n" (
            [ "mirrors:" "  docker.io:" "    endpoint:" ]
            ++ map (m: "      - \"${m}\"") cfg.workload.kubernetes.registryMirrors
          ) + "\n";
        };
      })
      # 真值源下发：安装位置必须跟 configFile 一致。
      # ⚠️ 不要在 environment.etc 里写死 "ksaya/ksaya.config.yaml" —— 那样一旦用户覆盖
      # configFile，副本还是装到 /etc/ksaya，运行时却去读别处，直接「配置文件不存在」。
      # 这里用 hasPrefix 判定：路径在 /etc 下就交给 environment.etc 声明式安装。
      (lib.mkIf (cfg.demo.enable && lib.hasPrefix "/etc/" cfg.configFile) {
        ${lib.removePrefix "/etc/" cfg.configFile} = {
          source = "${cfg.package}/lib/ksaya/ksaya.config.yaml";
          mode = "0444";
        };
      })
      # 节点上报：写上报地址 + 本机画像（profile / k3s 角色），供 ksaya-report 读取。
      # report.conf 是「唯一标识上报」的地址真值源，改这里一个字段就能改公网机地址。
      (lib.mkIf cfg.report.enable {
        "ksaya/report.conf" = {
          text =
            (if cfg.report.url != ""
             then "REPORT_URL=${cfg.report.url}\n"
             else "# REPORT_URL 未在此配置，ksaya-report 使用内置默认（公网 collector）\n");
          mode = "0644";
        };
        "ksaya/profile" = { text = "${cfg.systemProfile}\n"; mode = "0644"; };
        "ksaya/k3s-role" = { text = "${cfg.workload.kubernetes.role}\n"; mode = "0644"; };
      })
      # 版本标识：出厂系统自证版本（ksayactl --version 走包的 version，这里落到
      # /etc/ksaya/version 供运维/脚本免执行直接读）。version 取包的 derivation.version。
      {
        "ksaya/version" = { text = "${cfg.package.version}\n"; mode = "0444"; };
      }
    ];

    # 可写状态目录 + workload 清单软链（合并为一处，避免重复定义同一选项）
    # ⚠️ 清单只软链、不复制：内容来自 store（只读、可复现），复制副本会引入漂移。
    systemd.tmpfiles.rules = lib.mkIf cfg.demo.enable (
      [
        "d ${cfg.stateDir} 0750 root root -"
        "d ${cfg.stateDir}/history 0750 root root -"
        "d ${cfg.demo.manifestDir} 0755 root root -"
        "L+ ${cfg.demo.manifestDir}/ksaya-minecraft.yaml - - - - ${cfg.package}/share/ksaya/manifests/ksaya-minecraft.yaml"
        "L+ ${cfg.demo.manifestDir}/ksaya-demo-s3.yaml - - - - ${cfg.package}/share/ksaya/manifests/ksaya-demo-s3.yaml"
        "L+ ${cfg.demo.manifestDir}/ksaya-eaglercraft.yaml - - - - ${cfg.package}/share/ksaya/manifests/ksaya-eaglercraft.yaml"
        "L+ ${cfg.demo.manifestDir}/apply-node-labels.sh - - - - ${cfg.package}/share/ksaya/manifests/apply-node-labels.sh"
      ]
      # configFile 不在 /etc 下时，用软链补上（软链目标是只读的 store，无漂移风险）
      ++ lib.optionals (!lib.hasPrefix "/etc/" cfg.configFile) [
        "L+ ${cfg.configFile} - - - - ${cfg.package}/lib/ksaya/ksaya.config.yaml"
      ]
    );

    # 供运维/脚本使用的统一入口环境变量（登录 shell 直接能用 ksayactl）
    environment.sessionVariables = lib.mkIf cfg.demo.enable {
      KSAYA_CONFIG = cfg.configFile;
      KSAYA_STATE_DIR = cfg.stateDir;
      KSAYA_LOG = "${cfg.stateDir}/regression.log";
    };

    # ---- KSAYA 包自检：证明包真的装进了系统，且真值源/状态目录都可正常使用 ----
    # 这是「KSAYA 成为系统一部分」的可验证证据，而不是仅仅存在于 store 里。
    systemd.services.ksaya-selftest = lib.mkIf cfg.demo.enable {
      description = "KSAYA 包自检（版本 / 配置校验 / 可写状态目录）";
      wantedBy = [ "multi-user.target" ];
      after = [ "systemd-tmpfiles-setup.service" ];
      wants = [ "systemd-tmpfiles-setup.service" ];
      path = [ pkgs.coreutils pkgs.bash cfg.package ];
      environment = {
        KSAYA_CONFIG = cfg.configFile;
        KSAYA_STATE_DIR = cfg.stateDir;
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # 只有 stateDir 落在 /var/lib 下时 StateDirectory 才用得上；
        # 用户指到别处时不设置，改由下面的 tmpfiles 规则创建，
        # 避免 systemd 在 /var/lib/<name> 里多建一个没人用的目录。
      } // lib.optionalAttrs (stateDirName != null) {
        StateDirectory = stateDirName;
        StateDirectoryMode = "0750";
      };
      script = ''
        set -u
        echo "KSAYA-SELFTEST package=${cfg.package}"
        echo "KSAYA-SELFTEST version=$(ksayactl --version 2>/dev/null || echo unknown)"
        echo "KSAYA-SELFTEST config=$KSAYA_CONFIG state=$KSAYA_STATE_DIR"
        # 配置 schema 校验：失败即说明包与真值源不匹配
        ksayactl validate >/dev/null 2>&1 \
          && echo "KSAYA-SELFTEST validate=OK" \
          || echo "KSAYA-SELFTEST validate=FAILED"
        # 可写性检查：store 是只读的，这里必须能写
        if touch "$KSAYA_STATE_DIR/.selftest" 2>/dev/null; then
          rm -f "$KSAYA_STATE_DIR/.selftest"
          echo "KSAYA-SELFTEST state-writable=OK"
        else
          echo "KSAYA-SELFTEST state-writable=FAILED (dir=$KSAYA_STATE_DIR)"
        fi
      '';
    };

    # server 节点开机：打标签 + apply 核心与 workload（清单来自 KSAYA 包）
    systemd.services.ksaya-demo-apply = lib.mkIf (
      cfg.demo.enable
      && cfg.workload.kubernetes.enable
      && cfg.workload.kubernetes.role == "server"
    ) {
      description = "KSAYA：打标签 + apply 稳定核心与集群 workload（包内清单）";
      wantedBy = [ "multi-user.target" ];
      after = [ "k3s.service" "ksaya-node-profile.service" "systemd-tmpfiles-setup.service" ];
      requires = [ "k3s.service" ];
      # KSAYA 的 python3/PyYAML 已固化在包里，这里把包加进 PATH 即可用 ksayactl
      path = [ pkgs.k3s pkgs.busybox pkgs.bash cfg.package ];
      environment = {
        KSAYA_CONFIG = cfg.configFile;
        KSAYA_STATE_DIR = cfg.stateDir;
        KSAYA_LOG = "${cfg.stateDir}/regression.log";
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # 状态目录由 tmpfiles 建好；这里再兜一层，保证可写。
        # 同样只在 stateDir 位于 /var/lib 下时才交给 systemd 管。
      } // lib.optionalAttrs (stateDirName != null) {
        StateDirectory = stateDirName;
        StateDirectoryMode = "0750";
      };
      script = ''
        D="${cfg.demo.manifestDir}"
        for i in $(seq 1 60); do
          k3s kubectl get ns >/dev/null 2>&1 && break
          sleep 2
        done
        bash "$D/apply-node-labels.sh" || true
        # MC workload（镜像 ksaya.local/minecraft:26.3 需先由 build-mc-image.sh
        # 构建并 ctr images import；imagePullPolicy=Never 不会外拉）
        k3s kubectl apply -f "$D/ksaya-minecraft.yaml" || true
        k3s kubectl -n ksaya-mc rollout status statefulset/ksaya-mc --timeout=300s || true
        # 最简 S3（MinIO 单实例，钉 node1；镜像 minio/minio 需预先导入）
        k3s kubectl apply -f "$D/ksaya-demo-s3.yaml" || true
        k3s kubectl -n ksaya-s3 rollout status deployment/ksaya-s3 --timeout=300s || true
        # 浏览器版 MC（Eaglercraft，钉 node2；镜像 ksaya.local/eaglercraft:1.12.2
        # 需先在 node2 由 build-eagler-image.sh 构建并 ctr images import）
        k3s kubectl apply -f "$D/ksaya-eaglercraft.yaml" || true
        k3s kubectl -n ksaya-eagler rollout status statefulset/ksaya-eagler --timeout=300s || true
      '';
    };

    # ---- 节点上报：唯一标识 + 心跳（固定向公网机发信息，后期可改地址）----
    # 幂等：node-id 在脚本里首次生成并固化到 /etc/ksaya/node-id；service+timer
    # 重复触发只更新心跳，绝不重生成 ID。report 自适配 server/agent 双角色。
    systemd.services.ksaya-report = lib.mkIf cfg.report.enable {
      description = "KSAYA 节点心跳上报（唯一 node-id + 状态快照）";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [ pkgs.bash pkgs.coreutils pkgs.iproute2 cfg.package ];
      environment = {
        KSAYA_PROFILE = cfg.systemProfile;
      };
      serviceConfig = {
        Type = "oneshot";
        # node-id 固化到 /etc/ksaya，last-report 到 /var/lib/ksaya —— 分属两处，
        # StateDirectory 只建 /var/lib/ksaya；/etc/ksaya 由上面 environment.etc 兜住。
      } // lib.optionalAttrs (stateDirName != null) {
        StateDirectory = stateDirName;
        StateDirectoryMode = "0750";
      };
      script = ''
        exec ksaya-report
      '';
    };

    systemd.timers.ksaya-report = lib.mkIf cfg.report.enable {
      description = "KSAYA 节点心跳周期上报定时器";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "30s";
        OnCalendar = cfg.report.interval;
        Persistent = true;
        Unit = "ksaya-report.service";
      };
    };
  };
}
