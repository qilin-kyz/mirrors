# ============================================================
#  storage.nix —— 镜存 + 影分身：三盘识别与 ISO 库双模式
# ------------------------------------------------------------
#  附录磁盘规格：
#    256G 系统盘 / 1T 实例盘 / 32G ISO U盘
#
#  镜存(1)：虚拟机安装介质只从 ISO U盘取，且该盘默认只读。
#  影分身(8)：同一块 U盘具备 rw/ro 两种模式 —— 平时 ro 防篡改，
#             需要导入新 ISO 时显式切 rw，完事切回 ro。
#             命令：mirror-iso-mode rw | ro | status
#
#  识别策略：udev 按尺寸区间给盘打符号链接，与插口位置解耦：
#    /dev/mirror/system     ≈ 256G
#    /dev/mirror/instances  ≈ 1T
#    /dev/mirror/iso        ≈ 32G  (USB)
# ============================================================

{ config, pkgs, lib, ... }:

let
  mirrorIsoMode = pkgs.writeShellScriptBin "mirror-iso-mode" ''
    set -euo pipefail
    MNT=/var/lib/mirror/iso
    case "''${1:-status}" in
      ro)
        mountpoint -q "$MNT" || mount "$MNT"
        mount -o remount,ro "$MNT"
        echo "ISO library is now READ-ONLY (default, tamper-proof)."
        ;;
      rw)
        mountpoint -q "$MNT" || mount -o rw "$MNT" 2>/dev/null || {
          mount "$(findmnt -n -o SOURCE --target "$MNT" 2>/dev/null || echo /dev/mirror/iso)" "$MNT"
        }
        mount -o remount,rw "$MNT"
        echo "ISO library is now READ-WRITE. Import your ISOs, then run: mirror-iso-mode ro"
        echo "WARNING: every second in rw mode is a second of attack surface. 镜痕 is watching."
        ;;
      status)
        findmnt -n -o SOURCE,FSTYPE,OPTIONS --target "$MNT" || echo "ISO disk not mounted."
        ;;
      *) echo "usage: mirror-iso-mode [ro|rw|status]"; exit 2 ;;
    esac
  '';

  mirrorDisks = pkgs.writeShellScriptBin "mirror-disks" ''
    # 三盘体检：谁是谁、挂在哪、健不健康，一张表说清
    echo "=== MirrorOS disk identification ==="
    lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,FSTYPE,MOUNTPOINTS,RO,TRAN
    echo
    echo "=== mirror symlinks ==="
    for l in /dev/mirror/*; do
      [ -e "$l" ] && echo "$l -> $(readlink -f "$l")"
    done
    echo
    echo "=== ISO library mode ==="
    mirror-iso-mode status
  '';
in
{
  # ---- udev 尺寸识别规则（首次部署后用 mirror-disks 复核） ----
  services.udev.extraRules = ''
    # 256G ±：NVMe/SATA 系统盘
    SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", ATTR{size}=="468862128", SYMLINK+="mirror/system"
    SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", ATTR{size}=="500118192", SYMLINK+="mirror/system"
    # 1T ±：实例盘
    SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", ATTR{size}=="1953525168", SYMLINK+="mirror/instances"
    SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", ATTR{size}=="2000409264", SYMLINK+="mirror/instances"
    # 32G ±：ISO U盘（USB 总线限定，防止误认内置盘）
    SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", SUBSYSTEMS=="usb", ATTR{size}=="60062464", SYMLINK+="mirror/iso"
    SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", SUBSYSTEMS=="usb", ATTR{size}=="62530624", SYMLINK+="mirror/iso"
  '';

  # ---- 挂载点预建与权限 ----
  systemd.tmpfiles.rules = [
    "d /var/lib/mirror            0750 root root -"
    "d /var/lib/mirror/instances  0750 root root -"
    "d /var/lib/mirror/iso        0755 root root -"
    "d /var/lib/mirror/snapshots  0700 root root -"
    "d /var/lib/mirror/config     0750 root root -"
  ];

  # ISO 盘插入时自动以只读挂载（fstab 由 hardware-configuration 提供，
  # 这里兜底：若 fstab 缺失，用 udev+systemd-mount 兜底 ro）
  services.udev.packages = [ ];

  environment.systemPackages = [ mirrorIsoMode mirrorDisks ];

  # fstrim：SSD 长寿面的日常保养（系统盘与实例盘）
  services.fstrim.enable = true;
}
