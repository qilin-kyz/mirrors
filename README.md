# MirrorOS —— 为虚拟化而生的宿主系统

> 照见一切，不留尘埃。

**目标硬件**：AMD Ryzen 5 9600X（Zen 5 / AM5）+ NVIDIA RTX 5070 Ti（Blackwell）
+ Realtek RTL8111（1GbE）+ RTL8152B（2.5GbE USB）
**系统基座**：NixOS 稳定分支 · Linux 6.18 LTS · 全系统 znver5 编译优化

## 设计逻辑（按优先级）

| # | 原则 | 机制保证 |
|---|------|---------|
| 1 | **稳定性 7×24** | 声明式单一事实来源；`nixos-rebuild` 原子切换 + 秒级回滚；Docker live-restore；libvirtd 关机时优雅停实例；无 swap |
| 2 | **经济性** | 全系统 `gcc.arch=znver5` 编译 + **9600X 专属定制内核**（裁剪无线/音频/雷电等子系统，KVM/VHOST 编译进内核，HZ=1000，SCHED_CORE，prefcore/AVIC 开启，大页/isolcpus 极致档可选）；驱动白名单制；零多余服务；amd-pstate active |
| 3 | **安全性** | 仅 NixOS 稳定分支（禁止 unstable）；管理面与实例面网卡物理分离；镜痕全量审计；ISO 盘默认只读 |

## 镜系规格映射

| 规格 | 含义 | 实现 |
|------|------|------|
| 镜座 | 只加载目标硬件驱动 | `modules/kernel.nix` 黑白名单 |
| 镜存 | 安装介质只从只读 ISO U盘取 | `modules/storage.nix` + WebUI Images 页 |
| 影分身 | ISO U盘 rw/ro 双模式 | `mirror-iso-mode ro\|rw\|status` |
| 镜言 | 英文 Shell + 单一会话随处续 | `modules/session.nix`（tmux 会话 `mirror`，SSH ForceCommand + 控制台自动接入） |
| 镜痕 | 一切命令与后果全部入日志 | `modules/security.nix`（auditd execve 全审计 + journald 持久化） |
| 镜中世界 | 快照只含系统数据不含实例数据 | `modules/snapshot.nix`（zstd tar，排除 instances，周更+手动） |
| 三生镇魂 | Incus + KVM/QEMU + Docker | `modules/virtualization.nix` |

## 目录

```
mirroros/
├── flake.nix                        # 稳定分支锁定，禁止 unstable
├── hosts/mirrorhost/
│   ├── configuration.nix            # 主配置（znver5、保守更新）
│   └── hardware-configuration.nix   # 占位：安装时以实际探测替换
├── modules/                         # 全部功能模块（见上表）
└── webui/                           # 复古 BIOS 风格管理台（纯 stdlib 零依赖）
    ├── app.py                       # 后端：三栈适配器 + mock 模式
    └── static/index.html            # Award BIOS 风格单页
```

## 打包安装 ISO（镜 0.1-DEMO · 纪墨绫制作）

在任意一台装有 Nix 的 x86_64 Linux 机器上（或目标机本身）：

```bash
nix build .#iso
# 产物：result/iso/镜-0.1-DEMO.iso
dd if=result/iso/镜-0.1-DEMO.iso of=/dev/sdX bs=4M status=progress  # 写入 U盘
```

引导体验：内核静默 → 控制台亮出"镜 0.1-DEMO · 纪墨绫制作"横幅 →
root 自动登录 → 运行 `mirror-install` 进入全中文黑底白字安装向导
（欢迎 → 磁盘识别 → 分区方案 → 口令 → 确认 → 安装 → 完成，
Ubuntu Server 式分步 TUI；原生控制台自动经 fbterm 渲染中文）。

## 部署流程（目标硬件）

```bash
# 1. 用 NixOS 最小 ISO 启动目标机，分区（256G 系统盘）
# 2. 生成真实硬件配置并替换占位文件
nixos-generate-config --root /mnt
#    按 hardware-configuration.nix 内注释挂好三块盘

# 3. 拷贝本仓库到 /mnt/etc/nixos/mirroros，安装
nixos-install --flake /mnt/etc/nixos/mirroros#mirrorhost
#    注意：znver5 全量编译首装耗时数小时（一次性）；
#    赶时间先把 configuration.nix 里 gcc.arch 改为 "x86-64-v3" 用二进制缓存

# 4. 首启体检
mirror-disks                 # 三盘识别是否正确（udev 尺寸规则）
mirror-iso-mode status       # ISO 盘应为 ro
nvidia-smi                   # 5070 Ti 驱动就位
incus list && docker ps && virsh list --all   # 三生镇魂齐活
systemctl status mirror-webui                 # 管理台 :8443

# 5. 首登必做
passwd                       # mirror 用户改密
# 修改 /var/lib/mirror/config/webui-password（WebUI 口令）
```

## 三个管理入口

1. **SSH**：`ssh mirror@<host>` —— 标准密码，进入即接入统一 tmux 会话 `mirror`；断线重连从任何入口无缝续上（镜言）。
2. **WebUI**：`https://<host>:8443` —— 复古 BIOS 黑白蓝菜单，覆盖实例全生命周期、数据释放、盘/U盘管理、快照、镜痕、电源。
3. **本地控制台**：登录后自动接入同一个 tmux 会话（有头模式）。

## 更新策略（保守）

```bash
nix flake update                       # 仅跟随稳定分支前进
nixos-rebuild build --flake .#mirrorhost   # 先构建不激活
# 观察无异常后：
nixos-rebuild switch --flake .#mirrorhost
# 任何不适：
nixos-rebuild switch --rollback        # 一秒回到上一世代
```

## 本地演示 WebUI（无 NixOS 环境时）

```bash
cd webui && python3 app.py --mock --port 8443
# 浏览器打开 http://127.0.0.1:8443，口令 mirror
```

## 已知边界（诚实清单）

- flake 未在真机构建过：首装时以 `nixos-generate-config` 实际输出为准；
- 5070 Ti 需要 NVIDIA 驱动 ≥ 570 且必须 `open = true`（Blackwell 仅有 GSP 开放模块）；
- udev 尺寸规则按常见标称容量给了两组扇区数，个别盘容量有偏差时用 `mirror-disks` 对照调整；
- Docker 不能从 ISO 启动（容器没有引导过程）——ISO 安装走 KVM/QEMU 或 Incus VM，Docker 用镜像名创建。
