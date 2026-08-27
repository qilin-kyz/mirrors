# ============================================================
#  security.nix —— 镜痕：凡人触镜，镜必留痕
# ------------------------------------------------------------
#  规格："一切命令与系统对于命令的后续处理全部写进系统日志"。
#  三层留痕：
#    1. auditd：全量 execve 审计（谁、何时、跑了什么、结果如何）
#    2. journald：持久化存储，所有服务的"后续处理"输出全收
#    3. 关键目录 watch：/etc、本仓库、镜像目录的任何改动
# ============================================================

{ config, pkgs, lib, ... }:

{
  # ---- audit：全命令审计（26.05 起 security.auditd 已并入 security.audit） ----
  security.audit = {
    enable = true;
    backlogLimit = 8192;        # 突发命令风暴也不丢记录
    rules = [
      # 一切命令（64/32 位双 ABI 都罩住）
      "-a always,exit -F arch=b64 -S execve -k mirror-cmd"
      "-a always,exit -F arch=b32 -S execve -k mirror-cmd"
      # 系统配置的每一次落笔
      "-w /etc -p wa -k mirror-etc"
      "-w /nix/var/nix/profiles -p wa -k mirror-generation"
      # 镜像与快照目录的每一次触碰
      "-w /var/lib/mirror/iso -p wa -k mirror-iso"
      "-w /var/lib/mirror/snapshots -p wa -k mirror-snapshot"
      # 权限变更与登录事件
      "-w /etc/passwd -p wa -k mirror-identity"
      "-w /etc/shadow -p wa -k mirror-identity"
      "-w /var/log/lastlog -p wa -k mirror-login"
    ];
  };

  # ---- journald：持久、限容、防爆盘 ----
  services.journald.extraConfig = ''
    Storage=persistent
    SystemMaxUse=4G
    SystemKeepFree=10G
    MaxRetentionSec=6month
    ForwardToSyslog=no
  '';

  # ---- 登录与权限的硬约束 ----
  security.sudo = {
    enable = true;
    wheelNeedsPassword = true;
    # sudo 的每一次使用本就进 journal；叠加 audit 双保险
  };

  # 没有其它远程服务：不装、不开、不存在（攻击面 = sshd + webui，完）
  services.openssh.settings = {
    KbdInteractiveAuthentication = false;
    X11Forwarding = false;
    AllowTcpForwarding = false;   # SSH 只做管理，不当跳板
    MaxAuthTries = 3;
    LoginGraceTime = 30;
  };

  # fail2ban：管理面唯一入口的看门犬
  services.fail2ban = {
    enable = true;
    maxretry = 3;
    bantime = "1h";
  };

  # 审计日志快捷查询工具
  environment.systemPackages = [ pkgs.audit ];
  environment.shellAliases = {
    # 镜痕速查：今天谁在镜前做了什么
    mirror-trace = "sudo ausearch -k mirror-cmd -ts today | aureport -x --summary";
  };
}
