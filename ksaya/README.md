# KSAYA Core

KSAYA 的分布式调度核心（早期代号 **AR8**）。它回答一个问题：
**在一个性能、带宽、地域都可能相差几个数量级的集群里，这个任务应该放在哪台机器上？**

## 一、四 profile 可插拔调度

四个 profile 共用同一套真值源、reconcile 状态机、账本与不变量检查，只在「怎么给出价」上不同：

| profile | 决策依据 | 适用场景 |
|---------|----------|----------|
| `nixos-baseline` | 本机负载最朴素的均衡 | 同构、同机房的对照组 |
| `xnu-qos` | 借鉴 XNU QoS 分级：交互档对延迟敏感，批处理档让出 CPU/IO 权重 | 混部延迟敏感与批处理负载 |
| `oh-distributed` | 借鉴 OpenHarmony 确定性时延：压缩内存回收与 IO 的不确定停顿；带信任降级 | 时延抖动敏感 |
| `geo-distributed` | 显式建模**三重差距**：性能 / 带宽 / 地域 | 跨地域、异构、带宽悬殊 |

切换只改一个字段：

```nix
services.ksaya.systemProfile = "geo-distributed";
```

## 二、三重差距怎么建模

`geo-distributed` 的出价是三段 tick 之和：

```
bid = 排队 tick + 计算 tick + 数据传输 tick
```

- **性能差距**：按节点画像（CPU 核数、内存、算力档位）折算计算 tick；
- **带宽差距**：数据传输 tick 按瓶颈带宽 `min(源上行, 目标下行)` 计，不是按 RTT 猜；
- **地域差距**：`topology.rttMs` 给出节点两两 RTT，交互档对 RTT 敏感、批处理档对带宽敏感；
- **数据属性**：区分 `recomputable`（可重算数据，宁可重算不搬运）与不可重算数据（必须搬运）。

延迟不等于带宽——这是这套模型与「只按 RTT 选最近节点」的方案最本质的区别。

## 三、唯一标识与入群

- 节点标识 `/etc/ksaya/node-id`：由 `/etc/machine-id` 派生（MAC / 随机兜底），
  首次生成后固化，永不重生成；
- 心跳：`ksaya-report` 由 systemd service + timer 驱动，开机 30 秒后首报，之后按分钟级周期上报；
- 接收端：`ksaya-collector`（Python 标准库单文件 HTTP 服务，默认监听 TCP 80），
  按 `node_id` 落盘 JSON/JSONL，提供 `GET /healthz`、`GET /nodes`、`GET /nodes/<id>`、`POST /report`；
- **集群成员集合 = collector 上注册过的所有 `node_id`**。不需要每台机器都真正 join etcd。

## 四、目录

```
ksaya/
├── flake.nix                   # NixOS 系统变体 + KSAYA 包 + 出厂 ISO
├── hosts/
│   ├── ksaya-1.0.nix           # 1.0 出厂整机（默认 geo-distributed）
│   ├── ksaya-1.0-installer.nix # 1.0 出厂安装盘（ksaya-install 一键装机）
│   ├── ksaya-geo-standalone.nix
│   ├── ksaya-geo-installer.nix
│   ├── vm-ksaya-node1.nix / vm-ksaya-node2.nix
│   └── 001-thinkpad-x270.nix
├── modules/
│   ├── ksaya.nix               # 控制平面模块（k3s / 防火墙 / 上报 / 自检）
│   ├── ksaya-profile.nix       # 四 profile 的系统层参数（cgroup / sysctl / 网络栈）
│   └── cluster-base.nix        # 所有节点共享的基座策略
├── pkgs/ksaya/default.nix      # KSAYA 作为 Nix 正规包（wrapper 固化 python3 + PyYAML）
├── ksaya-stable/               # 调度核心源码（ksayacore.py / ksayactl.py / bench-*）
├── ksaya-collector/            # 公网接收端点 + systemd unit + 部署说明
└── ksaya-demo/                 # 集群 workload 清单（MC / S3 / Eaglercraft）
```

## 五、装机

```bash
# 构建出厂安装盘
nix build .#iso                 # → result/iso/KSAYA-1.0.iso

# 目标机引导后
ksaya-install /dev/nvme0n1      # 分区 + nixos-install --flake #ksaya-1.0

# 或手动
nixos-install --flake '/etc/ksaya-flake#"ksaya-1.0"'
```

装出来的系统：`systemProfile = geo-distributed`、`/etc/ksaya/version` 为 `1.0`、
开机自动向 collector 注册。

> Nix attrpath 里 `ksaya-1.0` 含点，必须写成带引号的 `"ksaya-1.0"`，
> 否则 Nix 会把它解析成 `ksaya-1` 后跟浮点字面量。

## 六、验证

```bash
cd ksaya-stable
python3 -m unittest discover -s tests -p "test_*.py" -v   # 63 个单测
python3 bench-triple-gap.py --shards 48 --trials 31        # 三重差距四方对照
```

CI（`.github/workflows/ksaya-ci.yml`）三条 job：

1. `nix-verify` —— 全部 NixOS 变体完整求值 + `nix build .#ksaya` 包自检；
2. `geo-sys` —— 真实构建 1.0 系统闭包与出厂 ISO；
3. `bench-verify` —— 63 单测 + 四版本基准（含三重差距与吞吐）。
