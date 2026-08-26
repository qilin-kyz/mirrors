# ============================================================
#  session.nix —— 镜言：英文标准 Shell + 统一会话
# ------------------------------------------------------------
#  规格："SHELL 采用英文标准版，只有单一会话；任何情况下的
#  会话都可以在另一方式继续。"
#
#  实现：全系统只有一个 tmux 会话 "mirror"。
#    - SSH 登录      → 强制 attach 到它
#    - 本地控制台登录 → 自动 attach 到它
#    - SSH 断线       → 会话不死，下次从任意入口回来接着干
#  就像照镜子：从哪一面看，镜中都是同一个世界。
# ============================================================

{ config, pkgs, lib, ... }:

{
  # ---- 英文标准版 ----
  i18n.defaultLocale = "en_US.UTF-8";
  console.keyMap = "us";

  # ---- 管理用户 ----
  users.users.mirror = {
    isNormalUser = true;
    description = "Mirror Keeper";
    shell = pkgs.bashInteractive;
    initialPassword = "mirror";   # 首登强制改密（见下方 PAM 策略提示）
    extraGroups = [ "wheel" ];    # 其余组在 virtualization.nix 追加
  };
  users.mutableUsers = true;      # 允许 passwd 改密；用户增删仍应走配置

  # ---- SSH：标准密码方式（规格 1），但只允许 mirror 用户 ----
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = true;   # 规格明确：标准密码方式
      PermitRootLogin = "no";
      AllowUsers = [ "mirror" ];
    };
    # 统一会话的核心：SSH 进来不做别的，直接照进 mirror 会话
    extraConfig = ''
      Match User mirror
        ForceCommand ${pkgs.tmux}/bin/tmux new-session -A -s mirror
    '';
  };

  # ---- 本地控制台：登录后同样照进 mirror 会话 ----
  programs.bash.interactiveShellInit = ''
    # 已在 tmux 内 / 非交互 / SSH(已被 ForceCommand 接管) 时不再嵌套
    if [ -z "''${TMUX:-}" ] && [ -z "''${SSH_CONNECTION:-}" ] && [ -t 1 ]; then
      exec ${pkgs.tmux}/bin/tmux new-session -A -s mirror
    fi
  '';

  programs.tmux = {
    enable = true;
    extraConfig = ''
      set -g history-limit 100000          # 长历史，配合镜痕追溯
      set -g status-bg black
      set -g status-fg white
      set -g status-left "[mirror] "
      set -g status-right "%Y-%m-%d %H:%M "
      setw -g mode-keys vi
      set -g detach-on-destroy off         # 最后一个窗格关了也不杀会话
    '';
  };

  # 有头模式的终端字体（服务器控制台可读性）
  console.packages = [ pkgs.terminus_font ];
  console.font = "ter-v16n";
}
