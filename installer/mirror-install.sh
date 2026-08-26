#!/usr/bin/env bash
# ============================================================
#  mirror-install —— 镜 0.1-DEMO 全中文安装向导
#  风格：Ubuntu Server 式分步 TUI，黑底白字
#  制作：纪墨绫
# ------------------------------------------------------------
#  流程：欢迎 → 磁盘识别 → 分区方案 → 管理员口令 →
#        最终确认 → 执行安装 → 完成重启
# ============================================================

set -euo pipefail

# ---- 黑底白字配色（newt/whiptail 主题） ----
export NEWT_COLORS='
root=white,black
window=white,black
border=white,black
shadow=black,black
title=white,black
button=black,white
actbutton=black,white
checkbox=white,black
actcheckbox=black,white
entry=white,black
label=white,black
listbox=white,black
actlistbox=black,white
textbox=white,black
acttextbox=black,white
helpline=white,black
roottext=white,black
emptyscale=black,black
fullscale=white,black
disentry=white,black
compactbutton=white,black
actsellistbox=black,white
sellistbox=white,black
'

TITLE="镜 0.1-DEMO · 安装向导"
BACKTITLE="镜 MirrorOS —— 为虚拟化而生的宿主系统 · 纪墨绫制作"

die() { whiptail --title "$TITLE" --backtitle "$BACKTITLE" \
        --msgbox "安装中止：$1" 10 60; exit 1; }

# ============================================================
#  第 0 步 · 欢迎
# ============================================================
whiptail --title "$TITLE" --backtitle "$BACKTITLE" --msgbox "
        ┌───────────────────────────────┐
          镜  0.1-DEMO
          MirrorOS —— 为虚拟化而生
          制作：纪墨绫
        └───────────────────────────────┘

  本向导将把「镜」安装到本机：

    目标平台    AMD Ryzen 5 9600X + RTX 5070 Ti
    系统基座    NixOS 稳定分支 · Linux 6.18 LTS
    虚拟化      Incus + KVM/QEMU + Docker（三生镇魂）

  全程只动你确认的磁盘，每一步都会先问再做。

                  按回车开始" 22 62 || exit 1

# ============================================================
#  第 1 步 · 磁盘识别（256G 系统 / 1T 实例 / 32G ISO）
# ============================================================
detect_disks() {
  SYS_DISK=""; INST_DISK=""; ISO_DISK=""
  while read -r dev size type tran; do
    [ "$type" = "disk" ] || continue
    gb=$(( size / 1024 / 1024 / 1024 ))
    if   [ "$gb" -ge 220 ] && [ "$gb" -le 280 ];  then SYS_DISK="/dev/$dev"
    elif [ "$gb" -ge 900 ] && [ "$gb" -le 1100 ]; then INST_DISK="/dev/$dev"
    elif [ "$gb" -ge 28 ]  && [ "$gb" -le 40 ] && [ "$tran" = "usb" ]; then ISO_DISK="/dev/$dev"
    fi
  done < <(lsblk -b -dn -o NAME,SIZE,TYPE,TRAN)
}

detect_disks
TABLE=$(lsblk -o NAME,MODEL,SIZE,TYPE,TRAN | sed 's/^/  /')
whiptail --title "$TITLE · 磁盘识别" --backtitle "$BACKTITLE" --msgbox "
检测到以下磁盘：

$TABLE

识别结果（按容量匹配规格）：
  系统盘（≈256G）  ${SYS_DISK:-未找到}
  实例盘（≈1T）    ${INST_DISK:-未找到}
  ISO 盘（≈32G）   ${ISO_DISK:-未找到（可稍后再插）}

识别有误时请先退出安装，用 lsblk 核对后再来。" 24 68 || exit 1

[ -n "$SYS_DISK" ]  || die "未找到 ≈256G 的系统盘。"
[ -n "$INST_DISK" ] || die "未找到 ≈1T 的实例盘。"

# ============================================================
#  第 2 步 · 分区方案确认
# ============================================================
whiptail --title "$TITLE · 分区方案" --backtitle "$BACKTITLE" --yesno "
将对磁盘执行以下操作（原有数据将全部销毁）：

  系统盘 $SYS_DISK
    ├─ 分区 1   EFI 系统分区   1 GiB   FAT32
    └─ 分区 2   镜系统根分区   剩余全部  ext4

  实例盘 $INST_DISK
    └─ 整盘单分区   XFS   → /var/lib/mirror/instances

  ISO 盘 ${ISO_DISK:-（未插入）}
    └─ 不做任何改动，保持只读镜像库角色

这是最后一个可以全身而退的界面。
是否继续？" 22 62 || exit 0

# ============================================================
#  第 3 步 · 管理员口令
# ============================================================
PW1=$(whiptail --title "$TITLE · 管理员口令" --backtitle "$BACKTITLE" \
      --passwordbox "请设置 mirror 管理员账户的口令\n（同时作为 WebUI 初始口令，首登后可分别修改）：" 12 60 3>&1 1>&2 2>&3) || exit 1
[ -n "$PW1" ] || die "口令不能为空。"

PW2=$(whiptail --title "$TITLE · 管理员口令" --backtitle "$BACKTITLE" \
      --passwordbox "请再次输入以确认：" 10 60 3>&1 1>&2 2>&3) || exit 1
[ "$PW1" = "$PW2" ] || die "两次输入的口令不一致。"

# ============================================================
#  第 4 步 · 最终确认清单
# ============================================================
whiptail --title "$TITLE · 最终确认" --backtitle "$BACKTITLE" --yesno "
即将开始安装，请最后过目：

  系统盘        $SYS_DISK   → EFI + ext4 根
  实例盘        $INST_DISK  → XFS（实例数据）
  管理员        mirror（口令已设定）
  系统内核      Linux 6.18 LTS · 9600X 专属定制
  显卡驱动      NVIDIA 官方全套（5070 Ti）
  虚拟化栈      Incus + KVM/QEMU + Docker
  管理入口      SSH(22) · WebUI(8443) · 本地控制台
  审计          镜痕全程开启

确认无误，开始安装？" 22 62 || exit 0

# ============================================================
#  第 5 步 · 执行安装
# ============================================================
{
  echo 5;   echo "XXX\n正在清空系统盘并建立分区……\nXXX"
  wipefs -af "$SYS_DISK"
  parted -s "$SYS_DISK" mklabel gpt
  parted -s "$SYS_DISK" mkpart ESP fat32 1MiB 1025MiB
  parted -s "$SYS_DISK" set 1 esp on
  parted -s "$SYS_DISK" mkpart primary 1025MiB 100%
  sleep 2

  P1="${SYS_DISK}p1"; P2="${SYS_DISK}p2"
  [ -b "$P1" ] || { P1="${SYS_DISK}1"; P2="${SYS_DISK}2"; }  # SATA 盘无 p 前缀

  echo 20;  echo "XXX\n正在格式化系统分区……\nXXX"
  mkfs.fat -F 32 -n MIRROR-BOOT "$P1"
  mkfs.ext4 -F -L mirror-root "$P2"

  echo 35;  echo "XXX\n正在格式化实例盘（XFS）……\nXXX"
  wipefs -af "$INST_DISK"
  mkfs.xfs -f -L mirror-instances "$INST_DISK"

  echo 45;  echo "XXX\n正在挂载目标系统……\nXXX"
  mount "$P2" /mnt
  mkdir -p /mnt/boot /mnt/var/lib/mirror/instances /mnt/etc/nixos
  mount "$P1" /mnt/boot
  mount "$INST_DISK" /mnt/var/lib/mirror/instances

  echo 55;  echo "XXX\n正在生成硬件配置……\nXXX"
  nixos-generate-config --root /mnt
  cp -r /etc/mirroros /mnt/etc/nixos/mirroros
  cp /mnt/etc/nixos/hardware-configuration.nix \
     /mnt/etc/nixos/mirroros/hosts/mirrorhost/hardware-configuration.nix

  echo 65;  echo "XXX\n正在注入管理员口令……\nXXX"
  mkdir -p /mnt/var/lib/mirror/config
  printf '%s' "$PW1" > /mnt/var/lib/mirror/config/webui-password
  chmod 600 /mnt/var/lib/mirror/config/webui-password

  echo 75;  echo "XXX\n正在安装镜系统（znver5 编译，请耐心等待）……\nXXX"
  nixos-install --flake /mnt/etc/nixos/mirroros#mirrorhost --no-root-passwd

  echo 95;  echo "XXX\n正在写入管理员口令……\nXXX"
  echo "mirror:$PW1" | nixos-enter --root /mnt -c 'chpasswd'

  echo 100; echo "XXX\n安装完成。\nXXX"
} | whiptail --title "$TITLE · 正在安装" --backtitle "$BACKTITLE" \
    --gauge "准备安装……" 10 62 0

# ============================================================
#  第 6 步 · 完成
# ============================================================
whiptail --title "$TITLE · 安装完成" --backtitle "$BACKTITLE" --msgbox "
        镜 0.1-DEMO 已就位。

  请取出安装介质并重启。首启后建议依次执行：

    mirror-disks              复核三盘识别
    mirror-iso-mode status    ISO 盘应为只读
    nvidia-smi                确认 5070 Ti 驱动就位
    passwd                    修改 mirror 账户口令

  管理台：https://<本机地址>:8443
  口令即刚才设置的口令。

        镜中世界，随时照见。—— 纪墨绫" 22 62

reboot
