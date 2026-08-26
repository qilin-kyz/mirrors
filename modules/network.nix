# ============================================================
#  network.nix —— 网卡独立分配：管理面与实例面物理分离
# ------------------------------------------------------------
#  规格："对外连接采用网卡独立分配的方式"。
#  实现：
#    RTL8111 (1GbE, r8169)  → 宿主管理面：SSH/WebUI/系统更新流量
#    RTL8152B (2.5GbE, r8152) → 实例对外面：桥接 br-vm，宿主自己不配 IP
#  效果：实例流量与宿主管理流量在网卡层面就是两张大网，
#        实例被打穿也摸不到管理面协议栈。
# ============================================================

{ config, pkgs, lib, ... }:

{
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.enable = true;

  systemd.network = {
    # 实例桥：没有宿主 IP，纯二层管道
    netdevs."10-br-vm".netdevConfig = {
      Kind = "bridge";
      Name = "br-vm";
    };

    networks = {
      # ---- 管理口：1GbE，宿主唯一的 IP 所在地 ----
      "10-mgmt" = {
        matchConfig.Driver = "r8169";
        networkConfig = {
          DHCP = "yes";            # 需要固定 IP 时改 Address/Gateway 静态配置
          IPv6AcceptRA = true;
        };
        linkConfig.RequiredForOnline = "routable";
        dhcpV4Config.RouteMetric = 100;
      };

      # ---- 实例上联口：2.5GbE，整口送给 br-vm ----
      "20-vm-uplink" = {
        matchConfig.Driver = "r8152";
        networkConfig.Bridge = "br-vm";
        linkConfig.RequiredForOnline = "enslaved";
      };

      # ---- br-vm 自身：宿主不拿地址，实例经由此桥直连外网 ----
      "30-br-vm" = {
        matchConfig.Name = "br-vm";
        linkConfig.RequiredForOnline = "carrier";
        # 若希望宿主也能经由实例网络出管理流量（不推荐），在此加 DHCP=yes
      };
    };
  };

  # ---- 防火墙：默认全拒，只对管理面开口 ----
  networking.nftables.enable = true;
  networking.firewall = {
    enable = true;
    allowedTCPPorts = [
      22     # SSH（镜言入口）
      8443   # 复古 WebUI（详见 modules/webui.nix）
    ];
    # 实例桥接口不做宿主层过滤，交给实例自治
    trustedInterfaces = [ "br-vm" ];
  };
}
