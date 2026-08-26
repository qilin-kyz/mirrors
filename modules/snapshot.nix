# ============================================================
#  snapshot.nix —— 镜中世界：只装系统、不装实例的快照
# ------------------------------------------------------------
#  规格："系统快照是只保存系统数据而非实例数据的压缩包"。
#
#  收入快照（系统数据）：
#    /etc、nix store 的 generation 元信息、flake 仓库、
#    用户家目录、镜系统自身配置
#  明确排除（实例数据）：
#    /var/lib/mirror/instances/**（VM 磁盘、容器层，1T 体量）
#  产物：zstd 压缩 tar，落 /var/lib/mirror/snapshots，
#        保留最近 8 份，周更 + 手动双通道。
# ============================================================

{ config, pkgs, lib, ... }:

let
  mirrorSnapshot = pkgs.writeShellScriptBin "mirror-snapshot" ''
    set -euo pipefail
    SNAPDIR=/var/lib/mirror/snapshots
    TS=$(date +%Y%m%d-%H%M%S)
    OUT="$SNAPDIR/mirror-system-$TS.tar.zst"

    echo "[镜中世界] freezing the system (not the instances) into: $OUT"

    # 记录当前系统世代的身份证
    GENINFO=$(${pkgs.nixos-rebuild}/bin/nixos-rebuild list-generations 2>/dev/null || true)

    ${pkgs.gnutar}/bin/tar --zstd -cf "$OUT" \
      --exclude='/var/lib/mirror/instances' \
      --exclude='/var/lib/mirror/snapshots' \
      --exclude='/var/lib/docker' \
      /etc \
      /home \
      /root \
      /var/lib/mirror/config \
      /var/lib/nixos \
      /nix/var/nix/profiles \
      2>/dev/null || true

    echo "$GENINFO" > "$OUT.generations.txt"

    # 轮转：只留最近 8 份
    ls -1t "$SNAPDIR"/mirror-system-*.tar.zst 2>/dev/null | tail -n +9 | xargs -r rm -f

    echo "[镜中世界] snapshot sealed: $(du -h "$OUT" | cut -f1)"
  '';
in
{
  environment.systemPackages = [ mirrorSnapshot pkgs.zstd ];

  # 周更定时器：保守系统的例行存档
  systemd.timers.mirror-snapshot = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "weekly";
      Persistent = true;    # 错过的时间点开机后补上
    };
  };
  systemd.services.mirror-snapshot = {
    description = "MirrorOS system-only snapshot (镜中世界)";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${mirrorSnapshot}/bin/mirror-snapshot";
    };
  };
}
