# ============================================================
#  hosts/installer/configuration.nix —— 镜 0.1-DEMO 安装 ISO
# ------------------------------------------------------------
#  制作：纪墨绫
#  构建产物：可启动安装盘，引导即见"镜0.1-DEMO · 纪墨绫制作"，
#            登录后运行 mirror-install 进入全中文安装向导。
# ============================================================

{ config, pkgs, lib, self, ... }:

let
  banner = ''
    ╔══════════════════════════════════════════════════════╗
    ║                                                      ║
    ║        镜  0.1-DEMO                                  ║
    ║        MirrorOS —— 为虚拟化而生的宿主系统             ║
    ║        制作：纪墨绫                                   ║
    ║                                                      ║
    ╚══════════════════════════════════════════════════════╝
  '';

  bootBanner = pkgs.writeShellScript "mirror-boot-banner" ''
    # 内核静默启动，由我们在控制台上亲手揭幕
    for tty in /dev/console /dev/tty1; do
      [ -w "$tty" ] || continue
      printf '\033[2J\033[H' > "$tty"   # 清屏
      cat ${pkgs.writeText "banner" banner} > "$tty"
    done
  '';

  # 安装向导入口：优先 fbterm（原生控制台渲染不了中文），
  # fbterm 不可用时直接跑（向导仍是中文，需终端自身支持 CJK）
  mirrorInstall = pkgs.writeShellScriptBin "mirror-install" ''
    if [ -z "''${FBTERM:-}" ] && [ -z "''${SSH_CONNECTION:-}" ] \
       && command -v fbterm >/dev/null 2>&1; then
      exec fbterm -- env FBTERM=1 bash ${../../installer/mirror-install.sh}
    fi
    exec bash ${../../installer/mirror-install.sh}
  '';

  mirrorInstallTty = pkgs.writeShellScriptBin "mirror-install-tty" ''
    exec bash ${../../installer/mirror-install.sh}
  '';
in
{
  # 安装环境与目标系统共用同一颗 9600X 定制内核（镜座）。
  # 首轮冒烟测试实测：不导入则安装盘跑默认 6.12，定制内核压根没编译。
  imports = [ ../../modules/kernel.nix ];

  # ---- ISO 身份证 ----
  # 25.11 起文件名由 isoBaseName 拼成（isoName mkForce 实测不生效）。
  # 必须用 ASCII：NixOS 构建链会丢弃文件名首字节的非 ASCII 字符
  # （实测 "镜-0.1-DEMO" → 发布产物变成 "-0.1-DEMO.iso"），且中文
  # 文件名在 U 盘刻录/启动菜单里也易被各类工具改坏。中文名由用户下载后自取。
  isoImage.isoBaseName = lib.mkForce "MirrorOS-0.1-DEMO";
  isoImage.volumeID = lib.mkForce "JING-01-DEMO";
  # ---- 仅 UEFI 引导（用户要求：去掉 legacy BIOS 与 dd 混合启动） ----
  # 基座 installation-cd-base.nix 默认 makeUsbBootable=true，必须用 mkForce 压过，
  # 否则与 makeEfiBootable=true 同优先级定义冲突。makeBiosBootable 基座无定义，
  # 直接用默认值(false 覆盖)即可。
  isoImage.makeEfiBootable = true;
  isoImage.makeBiosBootable = false;        # 去掉 El Torito BIOS (isolinux) 启动
  isoImage.makeUsbBootable = lib.mkForce false;  # 强压基座默认 true，去掉 hybrid MBR / dd 启动

  # 离线安装的生命线：flake 源码与 nixpkgs 全部随盘进 store
  isoImage.storeContents = [ self ];
  environment.etc."mirroros".source = self;

  # ---- 加载界面：内核闭嘴，我们说话 ----
  boot.kernelParams = [ "quiet" "loglevel=3" "udev.log_level=3" ];
  boot.plymouth.enable = false;        # 不用 Plymouth，纯文本横幅更"镜"

  systemd.services.mirror-boot-banner = {
    description = "镜 0.1-DEMO boot banner";
    wantedBy = [ "sysinit.target" ];
    before = [ "getty@tty1.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = bootBanner;
      StandardInput = "null";
      StandardOutput = "null";
    };
  };

  # 登录提示页也打上印记（getty 会显示 /etc/issue）
  environment.etc."issue".text = banner + ''

    镜 0.1-DEMO 安装环境
    登录后执行：  mirror-install      启动全中文安装向导
                  mirror-install-tty  无中文控制台时的备选入口

  '';

  # ---- 中文控制台支持 ----
  # Linux 原生控制台渲染不了 CJK，上 fbterm + 文泉驿正黑；
  # fbterm 不可用的环境退回英文提示，向导脚本内有兜底。
  fonts.packages = [ pkgs.wqy_zenhei ];
  environment.systemPackages = with pkgs; [
    mirrorInstall mirrorInstallTty   # 安装向导双入口
    fbterm
    newt          # whiptail：向导的 UI 引擎
    parted gptfdisk dosfstools e2fsprogs xfsprogs
    util-linux
  ];

  # 安装环境从简：root 自动登录，抬头就是向导提示
  services.getty.autologinUser = lib.mkForce "root";
  users.users.root.initialPassword = lib.mkForce "mirror";

  # 安装介质自身无需联网也能完成全流程（DHCP 有则用）。
  # 安装盘基座默认启用 NetworkManager 并把 useDHCP 压成 false，
  # 两处都要 mkForce 掰回来，否则求值期冲突（三轮 CI 实测）。
  networking.useDHCP = lib.mkForce true;
  networking.networkmanager.enable = lib.mkForce false;
}
