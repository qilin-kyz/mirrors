#!/usr/bin/env bash
# KSAYA：在本地构建 Eaglercraft（浏览器版 MC）运行镜像 —— 不依赖外部镜像源
#
# 组成：busybox(完整多调用) + nix glibc + Temurin JRE17
#       + Paper 1.12.2 发行版（EaglerXServer/Rewind + Via 三件套 + SkinsRestorer）
#       + EaglercraftX 1.8.8 离线客户端 HTML（同容器 busybox httpd 静态服务）
#
# 版本铁律（eaglercraft-paper-deploy skill）：
#   - EaglerXServer 插件字节码是 Java 17 → JVM 必须 17（不能用 25）
#   - Paper 必须 1.12.2（更高版本协议 V4 客户端异常）
#   - 1.8.8 客户端(协议47) ↔ Paper 1.12.2(协议340) 靠 ViaVersion+Backwards+Rewind 架桥
#
# 素材（默认 /tmp，均可用环境变量覆盖）：
#   /tmp/eagler-dist.tar.gz    Paper 发行版（已配置 eula/低视距/AuthMe 停用/nogui）
#   /tmp/eagler-client.tar.gz  客户端 index.html
#   /tmp/jre17.tar.gz          Temurin JRE17 标准包
#
# 产物：OCI layout 目录 + import tar，可直接 k3s ctr images import。
set -euo pipefail

BUILD=${BUILD:-/tmp/ksaya-eaglerimage}
DIST_TARBALL=${DIST_TARBALL:-/tmp/eagler-dist.tar.gz}
CLIENT_TARBALL=${CLIENT_TARBALL:-/tmp/eagler-client.tar.gz}
JRE_TARBALL=${JRE_TARBALL:-/tmp/jre17.tar.gz}
IMAGE_NAME=${IMAGE_NAME:-ksaya.local/eaglercraft:1.12.2}

say() { printf '[eaglerimage] %s\n' "$*"; }

say "=== 准备根文件系统 ==="
rm -rf "$BUILD"
mkdir -p "$BUILD/rootfs" "$BUILD/blobs/sha256"

# 1) 完整多调用 busybox（Nix store 版是单 applet 构建，不能用）
BB=${BUSYBOX_BIN:-/tmp/bbx/bin/busybox}
if [ ! -x "$BB" ]; then
  say "提取完整 busybox（从 containerd 的 library/busybox:1.37 镜像层）"
  K3S=$(command -v k3s)
  "$K3S" ctr --address /run/k3s/containerd/containerd.sock images export /tmp/bb-export.tar docker.io/library/busybox:1.37
  mkdir -p /tmp/bbx
  tar xf /tmp/bb-export.tar -C /tmp/bbx
  for b in /tmp/bbx/blobs/sha256/*; do
    if tar tf "$b" 2>/dev/null | grep -q "^bin/busybox$"; then
      # 坑：bin/busybox 是到 bin/[ 的硬链接，单独提取会报
      # "Cannot hard link" 失败 → 必须整层解包。
      tar xf "$b" -C /tmp/bbx 2>/dev/null || true
    fi
  done
  BB=/tmp/bbx/bin/busybox
fi
# 注意：busybox 是动态链接的，NixOS 宿主上不能直接执行（stub-ld 拦截），
# 但放进带 glibc 的容器 rootfs 里完全正常 → 只检查文件存在，不做 -x 试跑。
[ -f "$BB" ] || { echo "找不到完整 busybox"; exit 1; }
say "busybox: $BB"
mkdir -p "$BUILD/rootfs/bin" "$BUILD/rootfs/usr/bin" "$BUILD/rootfs/etc" \
         "$BUILD/rootfs/lib" "$BUILD/rootfs/lib64" "$BUILD/rootfs/usr/lib" \
         "$BUILD/rootfs/proc" "$BUILD/rootfs/sys" "$BUILD/rootfs/dev" \
         "$BUILD/rootfs/tmp" "$BUILD/rootfs/root" "$BUILD/rootfs/work"
cp "$BB" "$BUILD/rootfs/bin/busybox"
chmod 755 "$BUILD/rootfs/bin/busybox"
for applet in sh ls cat cp mv rm mkdir sleep echo date id hostname \
              wget curl tar gzip gunzip touch ps kill env sed grep head tail \
              chmod chown ln df du uname tr cut sort wc awk find xargs sync \
              httpd basename dirname readlink; do
  ln -sf /bin/busybox "$BUILD/rootfs/bin/$applet"
done

# 2) glibc 运行时（校验 libc.so.6 存在，避免选中 glibc-locales）
GLIBC=""
for d in /nix/store/*-glibc-2.42-*/lib; do
  if [ -e "$d/libc.so.6" ]; then GLIBC="$d"; break; fi
done
[ -n "$GLIBC" ] || { echo "找不到真 glibc（含 libc.so.6）"; exit 1; }
say "glibc: $GLIBC"
cp -aL "$GLIBC"/. "$BUILD/rootfs/lib/"
cp -L "$GLIBC"/ld-linux-x86-64.so.2 "$BUILD/rootfs/lib64/ld-linux-x86-64.so.2"
# /lib64 补相对软链（ld-linux 必须保持实体文件，不能覆盖成自指悬空链）
( cd "$BUILD/rootfs/lib64"
  for f in ../lib/*.so*; do
    case "$(basename "$f")" in ld-linux*) continue ;; esac
    ln -sf "$f" "$(basename "$f")"
  done )
# Nix ld.so 把自身 store 目录编译为默认搜索路径 → 容器内复刻该路径软链到 /lib
GLIBC_STORE_DIR=$(dirname "$GLIBC")
mkdir -p "$BUILD/rootfs$GLIBC_STORE_DIR"
ln -sfn /lib "$BUILD/rootfs$GLIBC_STORE_DIR/lib"
say "glibc 拷贝完成（含 lib64 软链 + nix store 兼容路径）"
printf 'hosts: files dns\npasswd: files\ngroup: files\n' > "$BUILD/rootfs/etc/nsswitch.conf"
printf 'nameserver 8.8.8.8\n' > "$BUILD/rootfs/etc/resolv.conf"
printf 'root:x:0:0:root:/root:/bin/sh\n' > "$BUILD/rootfs/etc/passwd"
printf 'root:x:0:\n' > "$BUILD/rootfs/etc/group"

# 3) Temurin JRE 17（EaglerXServer 硬性要求 17；标准包顶层目录拍平）
[ -f "$JRE_TARBALL" ] || { echo "缺少 $JRE_TARBALL"; exit 1; }
say "解包 JRE17: $JRE_TARBALL"
mkdir -p "$BUILD/rootfs/opt/jre"
tar xzf "$JRE_TARBALL" -C "$BUILD/rootfs/opt/jre" --strip-components=1
[ -x "$BUILD/rootfs/opt/jre/bin/java" ] || { echo "JRE 解包后缺 bin/java"; exit 1; }
# 不要 patchelf --remove-rpath：java 依赖 $ORIGIN/../lib 找 libjli.so

# 4) Paper 发行版 + 客户端
[ -f "$DIST_TARBALL" ] || { echo "缺少 $DIST_TARBALL"; exit 1; }
[ -f "$CLIENT_TARBALL" ] || { echo "缺少 $CLIENT_TARBALL"; exit 1; }
say "解包 Paper 发行版与客户端"
mkdir -p "$BUILD/rootfs/opt/eagler" "$BUILD/rootfs/opt/www"
tar xzf "$DIST_TARBALL" -C "$BUILD/rootfs/opt/eagler"
tar xzf "$CLIENT_TARBALL" -C "$BUILD/rootfs/opt/www"
# 坑：macOS bsdtar 会塞 AppleDouble（._*）文件，Paper 把 ._Xxx.jar 当插件
# 加载并报 "Could not load"。打包侧应 COPYFILE_DISABLE=1，这里再兜底清一次。
find "$BUILD/rootfs/opt/eagler" "$BUILD/rootfs/opt/www" -name "._*" -delete
[ -f "$BUILD/rootfs/opt/eagler/paper-1.12.2.jar" ] || { echo "缺 paper jar"; exit 1; }
[ -f "$BUILD/rootfs/opt/eagler/eula.txt" ] || printf 'eula=true\n' > "$BUILD/rootfs/opt/eagler/eula.txt"
chmod 755 "$BUILD/rootfs/opt/eagler/run.sh"

# 5) 启动脚本：PVC 世界持久化 + 客户端静态服务 + Paper 前台
#    PVC 挂 /data；世界与玩家数据放 /data/world*，用软链映射回 /opt/eagler。
#    首次启动把镜像内配置骨架复制到 PVC（之后以 PVC 为准，重启不丢）。
cat > "$BUILD/rootfs/opt/eagler/start.sh" <<'EOS'
#!/bin/sh
# KSAYA Eaglercraft 容器启动脚本
set -e
cd /opt/eagler || exit 1
export HOME=/root

# --- PVC 世界持久化（/data 由 StatefulSet 挂载）---
mkdir -p /data
# 首次：把发行版骨架（除 world）搬到 PVC；此后配置改动都落在 PVC
if [ ! -f /data/.initialized ]; then
  for item in plugins plugins-disabled server.properties eula.txt run.sh \
              paper-1.12.2.jar server-icon.png; do
    [ -e "/opt/eagler/$item" ] && cp -a "/opt/eagler/$item" /data/ 2>/dev/null || true
  done
  touch /data/.initialized
fi
# 世界目录：PVC 实体 + 工作目录软链（Paper 在 /data 下运行）
cd /data

# --- 客户端静态服务（busybox httpd :8080，流式 sendfile，18MB HTML 无压力）---
busybox httpd -p 0.0.0.0:8080 -h /opt/www

# --- Paper 1.12.2（EaglerXServer 注入 WS 监听器到 25565）---
export LD_LIBRARY_PATH=/opt/jre/lib:/opt/jre/lib/server:/lib
exec /opt/jre/bin/java \
  -Xms192M -Xmx512M -XX:+UseSerialGC -XX:MaxMetaspaceSize=160M \
  -Djava.net.preferIPv4Stack=true \
  -jar /data/paper-1.12.2.jar nogui
EOS
chmod 755 "$BUILD/rootfs/opt/eagler/start.sh"

# 6) 打包 OCI layer（blob 必须是文件本身，不能是目录）
say "打包 layer"
cd "$BUILD/rootfs"
LAYER_TAR="$BUILD/layer.tar"
tar --numeric-owner --owner=0 --group=0 -cf "$LAYER_TAR" .
cd "$BUILD"
LAYER_DIGEST="sha256:$(sha256sum layer.tar | cut -d' ' -f1)"
cp layer.tar "blobs/sha256/${LAYER_DIGEST#sha256:}"
LAYER_SIZE=$(stat -c%s layer.tar)

# 7) config
cat > config.json <<EOF
{
  "architecture": "amd64",
  "config": {
    "Env": ["PATH=/bin:/usr/bin:/opt/jre/bin", "HOME=/root", "LC_ALL=C.UTF-8"],
    "Entrypoint": ["/opt/eagler/start.sh"],
    "WorkingDir": "/data",
    "Labels": {"io.ksaya.workload": "eaglercraft", "io.ksaya.version": "1.12.2"}
  },
  "rootfs": {"type": "layers", "diff_ids": ["$LAYER_DIGEST"]},
  "history": [{"created": "2026-11-19T00:00:00Z", "created_by": "ksaya eagler image build"}]
}
EOF
CONFIG_DIGEST="sha256:$(sha256sum config.json | cut -d' ' -f1)"
cp config.json "blobs/sha256/${CONFIG_DIGEST#sha256:}"
CONFIG_SIZE=$(stat -c%s config.json)

# 8) manifest
cat > manifest.json <<EOF
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
             "digest": "$CONFIG_DIGEST", "size": $CONFIG_SIZE},
  "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar",
              "digest": "$LAYER_DIGEST", "size": $LAYER_SIZE}]
}
EOF
MAN_DIGEST="sha256:$(sha256sum manifest.json | cut -d' ' -f1)"
cp manifest.json "blobs/sha256/${MAN_DIGEST#sha256:}"
MAN_SIZE=$(stat -c%s manifest.json)

# 9) index.json（io.containerd.image.name 注解，k3s import 需要）
cat > index.json <<EOF
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [{
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "digest": "$MAN_DIGEST", "size": $MAN_SIZE,
    "annotations": {
      "io.containerd.image.name": "$IMAGE_NAME",
      "org.opencontainers.image.ref.name": "$IMAGE_NAME"
    }
  }]
}
EOF
printf '{"imageLayoutVersion":"1.0.0"}' > oci-layout

# 10) import tar（只含 OCI layout 必需文件）
if [ "${PACK_TAR:-1}" = "1" ]; then
  say "打包 import tar: $BUILD.tar"
  tar -C "$BUILD" -cf "$BUILD.tar" oci-layout index.json blobs
  say "tar 大小: $(($(stat -c%s "$BUILD.tar") / 1024 / 1024)) MB"
fi

say "=== OCI layout 就绪: $BUILD ==="
say "镜像名: $IMAGE_NAME"
say "layer 大小: $((LAYER_SIZE / 1024 / 1024)) MB"
echo "$IMAGE_NAME"