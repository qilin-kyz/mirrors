# ============================================================
#  webui.nix —— 复古 BIOS 风格 Web 管理台的服务注册
# ------------------------------------------------------------
#  应用本体在 ../webui/（纯 Python stdlib，零依赖 ——
#  与系统"无包管理、不可安装"的气质一致）。
#  监听 8443，只开管理面防火墙（见 network.nix）。
#  管理口令经环境文件注入，不落进 nix store（明文不进配置）。
# ============================================================

{ config, pkgs, lib, ... }:

{
  # 口令文件：/var/lib/mirror/config/webui-password（首启时创建）
  systemd.services.mirror-webui = {
    description = "MirrorOS retro BIOS WebUI";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" "incus.service" "docker.service" "libvirtd.service" ];
    wants = [ "network-online.target" ];

    preStart = ''
      CFG=/var/lib/mirror/config
      mkdir -p "$CFG"
      if [ ! -f "$CFG/webui-password" ]; then
        echo "20120605" > "$CFG/webui-password"   # 出厂口令，首登后请修改
        chmod 600 "$CFG/webui-password"
      fi
    '';

    serviceConfig = {
      Type = "simple";
      ExecStart = "${pkgs.python3}/bin/python3 ${../webui/app.py} --host 0.0.0.0 --port 8443";
      Restart = "on-failure";
      RestartSec = 3;
      # 需要操作三栈与存储，以 root 跑但收紧能松的一切
      NoNewPrivileges = true;
      ProtectHome = true;
      PrivateTmp = true;
    };
  };
}
