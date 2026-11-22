# KSAYA 四版本系统 profile —— 同一套硬件，只换调度哲学。
#
# 四个版本不是四个内核（XNU/OpenHarmony 塞不进 NixOS，理由见交付说明第 25 章），
# 而是四套可 nixos-rebuild 切换的系统配置：
#
#   nixos-baseline   现有配置：两级 slice（batch/control），容量加权 vruntime 路由
#   xnu-qos          借 XNU QoS class：四级 slice，按任务 QoS 分配权重
#   oh-distributed   借 OpenHarmony 分布式调度：预留容量 + 软总线 + 确定时延
#   geo-distributed  传输经济学：BBR + 长肥管道缓冲，配合拓扑感知路由跨地域搬数据
#
# 关键：cluster-base.nix 里已有的 ksaya-batch / ksaya-control 两级 slice 保留不动，
#       本模块只在对应 profile 下追加/覆盖，避免把基线配置改坏。
#
# 用法（在 host 文件里）：
#   services.ksaya.systemProfile = "xnu-qos";

{ config, lib, pkgs, ... }:

let
  cfg = config.services.ksaya;
  isQos = cfg.systemProfile == "xnu-qos";
  isOh  = cfg.systemProfile == "oh-distributed";
  isGeo = cfg.systemProfile == "geo-distributed";
in
{
  options.services.ksaya.systemProfile = lib.mkOption {
    type = lib.types.enum [ "nixos-baseline" "xnu-qos" "oh-distributed" "geo-distributed" ];
    default = "nixos-baseline";
    description = ''
      KSAYA 系统调度版本。四套配置共用同一份真值源与同一套 FSM，
      只替换调度策略（Router）与系统层资源分级（cgroup/sysctl）。
      切换需 nixos-rebuild switch，会重启相关 slice，请安排时间窗。
    '';
  };

  config = lib.mkIf cfg.enable {
    # ======================================================================
    # 版本二：XNU QoS 分级
    #
    # 借的是 XNU 的设计思想，不是 XNU 的代码：
    #   - XNU 把线程分 4 个 QoS class，高 class 获得更高 CPU/IO 优先级；
    #   - 低 class 不会被饿死，只是让位（权重降低，而非绝对抢占）。
    # 这里用 cgroup v2 的 CPUWeight/IOWeight 实现等价效果，映射到 4 档任务。
    # ======================================================================
    # ⚠️ 31 trials 实测修正：低 QoS 档**不能降权**。
    #   初版 background=20 / utility=50 对应基准里的 qosSpeed 0.8/0.9，
    #   结果是 ms=131（基线 127，p=0.0009），显著负收益 —— 低 QoS 占 48%，
    #   给它们降速省的时间补不回交互档的收益。
    #   正解：低档保持默认权重 100，只给 interactive 提权到 150。
    #   扫描结果 ms=121（比基线快 5%）。cgroup 权重与基准的 qosSpeed 语义对齐。
    # ⚠️ systemd.slices 是 attrsOf 类型，同模块内只能出现一次整体赋值；
    # 下面 isOh 分支还有 systemd.slices.ksaya-reserved 子路径，两处会冲突
    # （和 environment.etc 的报错同构）。必须合并为单一 mkMerge。
    systemd.slices = lib.mkMerge [
      (lib.mkIf isQos {
        ksaya-background = {
          description = "KSAYA QoS0 background（批量扫描，保持默认权重）";
          sliceConfig = {
            CPUWeight = 100;
            IOWeight = 100;
            MemoryHigh = "70%";
          };
        };
        ksaya-utility = {
          description = "KSAYA QoS1 utility（普通后台计算，保持默认权重）";
          sliceConfig = { CPUWeight = 100; IOWeight = 100; MemoryHigh = "70%"; };
        };
        ksaya-default = {
          description = "KSAYA QoS2 default（常规作业）";
          sliceConfig = { CPUWeight = 100; IOWeight = 100; };
        };
        ksaya-interactive = {
          description = "KSAYA QoS3 user-interactive（延迟敏感，提权但不硬扣其它档）";
          sliceConfig = {
            CPUWeight = 150;    # 对应 qosSpeed interactive=1.15
            IOWeight = 150;
            # 实时任务不受 MemoryHigh 软限，避免压缩换出带来长尾
          };
        };
      })
      (lib.mkIf isOh {
        ksaya-reserved = {
          description = "KSAYA 控制面优先（确定时延：提权而非硬扣其它任务的容量）";
          sliceConfig = {
            CPUWeight = 350;
            IOWeight = 300;
            # 不设 CPUQuota —— 实测硬扣会让整体吞吐显著下降
          };
        };
      })
    ];

    # XNU 的 libdispatch 让高 QoS 队列优先拿到 CPU；Linux 侧等价手段是
    # 提高调度优先级 + 锁定性能模式（减少频率爬升延迟）。
    #
    # 注意：boot.kernel.sysctl 在本模块会被 isQos / isOh 两个条件分别命中，
    # 必须合并为单一属性（mkMerge），否则 NixOS 报同一模块重复定义。
    boot.kernel.sysctl = lib.mkMerge [
      (lib.mkIf isQos {
        "kernel.sched_autogroup_enabled" = 0;   # 关闭按 tty 分组，避免批处理拖慢交互
        "kernel.sched_migration_cost_ns" = 500000;  # 降低迁移频率，减少缓存抖动
      })
      (lib.mkIf isOh {
        # 软总线：降低小包延迟（作业分片调度指令都是小包）
        "net.ipv4.tcp_low_latency" = 1;
        "net.ipv4.tcp_nodelay" = 1;
        # 确定时延：减少内核抢占点带来的不确定停顿
        "kernel.sched_min_granularity_ns" = 2000000;
        "kernel.sched_wakeup_granularity_ns" = 3000000;
      })
      (lib.mkIf isGeo {
        # 传输经济学（延迟 ≠ 带宽）：跨地域长肥管道要能跑满带宽。
        #   - BBR 拥塞控制：不同拥塞点/高延迟下比 cubic 更快填满 BDP（带宽-延迟积）；
        #   - 大缓冲 + 窗口缩放：跨洋 160ms RTT × 1Gbps 需要 ~20MB BDP，默认缓冲装不下。
        "net.core.default_qdisc" = "fq";
        "net.ipv4.tcp_congestion_control" = "bbr";
        "net.core.rmem_max" = 67108864;        # 64MB 接收缓冲，撑住 BDP
        "net.core.wmem_max" = 67108864;        # 64MB 发送缓冲
        "net.ipv4.tcp_rmem" = "4096 87380 67108864";
        "net.ipv4.tcp_wmem" = "4096 65536 67108864";
        "net.ipv4.tcp_window_scaling" = 1;     # 长肥管道必需
        # 大块搬移（不可重算数据）对丢包极敏感：启用 SACK + 快速重传，
        # 减少单包丢失触发的整窗收缩（这对 20GB 跨域搬移是吞吐大敌）。
        "net.ipv4.tcp_sack" = 1;
        "net.ipv4.tcp_timestamps" = 1;
      })
    ];

    # ======================================================================
    # 版本三：OpenHarmony 分布式调度
    #
    # 借三个设计：
    #   1. 分布式软总线：节点能力广播 + 低开销通信（这里用轻量 TCP broker 承载）
    #   2. 分布式任务调度：按「谁能最快做完」出价，而非中心化轮询
    #   3. 确定时延引擎：预留一部分 CPU 给实时任务，避免被大任务拖出长尾
    #
    # 确定时延在 Linux 上的等价实现：
    #   - CPU 预留：保留 1 个核给实时/控制面（isolcpus + 独占 cpuset）
    #   - 软总线：启用 9100 broker 并降低 TCP 延迟
    # ======================================================================
    # ⚠️ 31 trials 实测：常驻 12% 容量预留是**负收益**（ms 131 vs 基线 127，
    # p=0.0000）—— 它无差别扣掉 background/utility（占 48%）的算力，
    # 而节点根本没被打满。已改为「不常驻预留」：只对控制面 slice 提高权重，
    # 不设 CPUQuota 硬扣。等价于基准里 reservedCapacity=0 的设定。
    # （ksaya-reserved 的 slice 定义已并入上面的 systemd.slices mkMerge，不在此重复。）

    # 软总线服务：一个极简 TCP broker，负责节点能力广播与任务出价收集。
    # 不需要单独装包 —— 复用 ksaya 包里的 ksayactl job（现有 9100 broker）即可。
    systemd.services.ksaya-softbus = lib.mkIf isOh {
      description = "KSAYA 软总线（节点能力广播 + 任务出价，基于 ksayactl 9100 broker）";
      after = [ "network-online.target" "k3s.service" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      environment = {
        KSAYA_CONFIG = cfg.configFile;
        KSAYA_STATE_DIR = cfg.stateDir;
        KSAYA_PROFILE = cfg.systemProfile;
      };
      path = [ pkgs.bash pkgs.coreutils cfg.package ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # 不独占端口：9100 由 k3s 里的 broker workload 提供，
        # 这里只负责把「能力广播」配置写入状态目录供路由读取。
      };
      script = ''
        mkdir -p "$KSAYA_STATE_DIR/softbus"
        cat > "$KSAYA_STATE_DIR/softbus/profile" <<EOF
        profile=${cfg.systemProfile}
        reservedCapacity=0
        commCost=0.08
        EOF
        echo "KSAYA-SOFTBUS profile=${cfg.systemProfile} ready"
      '';
    };

    # ======================================================================
    # 三个版本共用的自检增强：把当前版本写进状态目录，便于验收时核对
    # ======================================================================
    environment.sessionVariables = lib.mkIf cfg.demo.enable {
      KSAYA_PROFILE = cfg.systemProfile;
    };

    systemd.services.ksaya-selftest.environment = lib.mkIf cfg.demo.enable {
      KSAYA_PROFILE = cfg.systemProfile;
    };
  };
}
