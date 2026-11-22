# KSAYA

> 把多台异构、跨地域的机器，虚拟成一台大电脑。

KSAYA 是一套面向**私有算力**的集群系统：底层是一份可复现的 NixOS 系统镜像，
上层是一个按「性能差距 / 带宽差距 / 地域差距」做决策的分布式调度核心。
它不假设你的节点是同一批机器——树莓派、工作站、带独显的台式机、公有云实例，
只要能引导 KSAYA 的系统盘，就能进同一个集群。

## 两个部件

| 部件 | 角色 | 位置 |
|------|------|------|
| **KSAYA Core**（早期代号 **AR8**） | 分布式调度核心：四 profile 可插拔调度、真值源、reconcile 状态机、唯一标识与心跳入群 | `ksaya/` |
| **MirrorOS（镜）** | 虚拟化宿主基座：znver5 定制内核、Incus + KVM/QEMU + Docker 三栈、只读 ISO 安装介质 | 仓库根目录（见 [MIRROROS.md](MIRROROS.md)） |

> AR8 是 KSAYA 调度核心在 1.0 之前的代号。1.0 起统一以 **KSAYA** 作为项目与产品名，
> AR8 作为该核心部件的历史代号保留在文档中。

## KSAYA Core 是什么

一个节点上跑着 `ksayactl`，它把「哪些任务该放在哪台机器上」拆成可解释的**出价**：
排队 tick + 计算 tick + 数据传输 tick。四个 profile 是四套出价策略，可以在装系统时
用一个字段切换，共用同一套 FSM、真值源、账本与不变量检查：

- `nixos-baseline` —— 基线：只按本机负载做最朴素的均衡；
- `xnu-qos` —— 借鉴 XNU 的 QoS 分级：交互档对延迟敏感、批处理档让出资源；
- `oh-distributed` —— 借鉴 OpenHarmony 的确定性时延：压低内存回收与 IO 的不确定停顿，并带信任降级；
- `geo-distributed` —— 面向极端异构：显式建模**三重差距**（性能 / 带宽 / 地域），
  区分「延迟」与「带宽」、区分「可重算数据」与「不可重算数据」，
  数据传输代价按瓶颈带宽 `min(源上行, 目标下行)` 计。

在性能、带宽、地域三项同时极端的 31 轮配对基准里，`geo-distributed` 的 makespan
显著低于其余三个 profile（详见 `ksaya/ksaya-stable/bench-triple-gap.py` 的产物）。

## 入群方式：注册即成员

每台装好 KSAYA 的机器开机后会：

1. 生成并固化一个**唯一节点标识** `/etc/ksaya/node-id`（由 `machine-id` 派生，
   写入后永不重生成）；
2. 按 systemd timer 周期向公网 collector（默认 TCP 80 的 `/report`）上报心跳与状态快照；
3. collector 按 `node_id` 落盘。**在 collector 上注册过的所有 `node_id`，就是这个集群的成员集合。**

换一台公网机只需改 `/etc/ksaya/report.conf` 里的一个地址，不用重装。

## 快速开始

需要一台装有 Nix 的 x86_64 Linux（或直接用出厂 ISO 装机）：

```bash
# 构建 KSAYA 1.0 出厂安装盘
nix build ./ksaya#iso
# 产物：result/iso/KSAYA-1.0.iso

# 引导目标机后，一键装机（GPT：EFI 1G + 根分区，按 label 挂载）
ksaya-install /dev/nvme0n1
```

装机完成后 `nixos-rebuild switch --flake /etc/ksaya-flake#ksaya-1.0` 即可切换 profile。

## 仓库结构

```
.
├── README.md              # 本文件：KSAYA 总览
├── MIRROROS.md            # MirrorOS（镜）宿主系统文档
├── ksaya/                 # KSAYA Core（原 AR8）
│   ├── flake.nix          # NixOS 系统变体 + 包 + 出厂 ISO
│   ├── hosts/             # 各节点 / 安装盘配置
│   ├── modules/           # NixOS 模块（ksaya.nix / ksaya-profile.nix / cluster-base.nix）
│   ├── pkgs/ksaya/        # KSAYA 作为 Nix 正规包的 derivation
│   ├── ksaya-stable/      # 调度核心源码 + 单测 + 基准
│   ├── ksaya-collector/   # 公网接收端点（注册表）
│   └── ksaya-demo/        # 集群 workload 清单
├── modules/               # MirrorOS 模块
├── hosts/                 # MirrorOS 宿主配置
└── webui/                 # MirrorOS 管理台
```

## 验证

- `ksaya/ksaya-stable` 下 **63 个单元测试**覆盖 schema、准入、账本、四 profile 语义与不变量；
- GitHub Actions 三条 job：NixOS 变体完整求值、1.0 系统闭包与出厂 ISO 真机构建、四版本基准；
- 1.0 出厂 ISO 由 CI 真机构建并作为 Release 资产发布。
