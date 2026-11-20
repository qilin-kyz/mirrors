#!/usr/bin/env bash
# AR8：在本地构建 Minecraft 运行镜像（不依赖任何外部镜像源）
#
# 背景：国内网络下 docker.io 大镜像（debian-slim / eclipse-temurin）拉不动，
#       但 busybox 镜像可用。于是自建镜像：
#         busybox(静态 shell) + nix glibc(运行时) + Temurin JRE25 + MC server.jar
#       全部素材来自本机已有文件，无外网依赖。
#
# 产物：OCI layout 目录，可直接 k3s ctr images import。
set -euo pipefail

BUILD=${BUILD:-/tmp/ar8-mcimage}
JRE_TARBALL=${JRE_TARBALL:-/tmp/jre25-std.tar.gz}
MC_SERVER_JAR=${MC_SERVER_JAR:-/opt/mc/server.jar}
MC_PROPS=${MC_PROPS:-/opt/mc/server.properties}
IMAGE_NAME=${IMAGE_NAME:-ar8.local/minecraft:26.3}

say() { printf '[mcimage] %s\n' "$*"; }

say "=== 准备根文件系统 ==="
rm -rf "$BUILD"
mkdir -p "$BUILD/rootfs" "$BUILD/blobs/sha256"

# 1) 完整多调用 busybox 作为 shell / 基础工具
#    坑：Nix store 的 busybox 是单 applet 构建（argv[0] 分发被裁掉），
#    软链 ls/cat 过去全部报 "applet not found"。必须用 docker.io/library/busybox
#    镜像层里的完整版（先 ctr images export 再解层提取）。
BB=${BUSYBOX_BIN:-/tmp/bbx/bin/busybox}
if [ ! -x "$BB" ]; then
  say "提取完整 busybox（从 containerd 的 library/busybox:1.37 镜像层）"
  K3S=$(command -v k3s)
  "$K3S" ctr --address /run/k3s/containerd/containerd.sock images export /tmp/bb-export.tar docker.io/library/busybox:1.37
  mkdir -p /tmp/bbx
  tar xf /tmp/bb-export.tar -C /tmp/bbx
  for b in /tmp/bbx/blobs/sha256/*; do
    if tar tf "$b" 2>/dev/null | grep -q "^bin/busybox$"; then
      tar xf "$b" -C /tmp/bbx bin/busybox
    fi
  done
  BB=/tmp/bbx/bin/busybox
fi
[ -x "$BB" ] || { echo "找不到完整 busybox"; exit 1; }
say "busybox: $BB"
mkdir -p "$BUILD/rootfs/bin" "$BUILD/rootfs/usr/bin" "$BUILD/rootfs/etc" \
         "$BUILD/rootfs/lib" "$BUILD/rootfs/lib64" "$BUILD/rootfs/usr/lib" \
         "$BUILD/rootfs/proc" "$BUILD/rootfs/sys" "$BUILD/rootfs/dev" \
         "$BUILD/rootfs/tmp" "$BUILD/rootfs/root" "$BUILD/rootfs/work"
cp "$BB" "$BUILD/rootfs/bin/busybox"
chmod 755 "$BUILD/rootfs/bin/busybox"
for applet in sh ls cat cp mv rm mkdir sleep echo date id hostname \
              wget curl tar gzip gunzip touch ps kill env sed grep head tail \
              chmod chown ln df du uname tr cut sort wc awk find xargs sync; do
  ln -sf /bin/busybox "$BUILD/rootfs/bin/$applet"
done

# 2) glibc 运行时（从 nix store 取）
#    坑：glob *-glibc-2.42-* 会先匹配到 glibc-locales（只有 locale 数据没有库），
#    必须校验 libc.so.6 存在才算选中真 glibc。
GLIBC=""
for d in /nix/store/*-glibc-2.42-*/lib; do
  if [ -e "$d/libc.so.6" ]; then GLIBC="$d"; break; fi
done
[ -n "$GLIBC" ] || { echo "找不到真 glibc（含 libc.so.6）"; exit 1; }
say "glibc: $GLIBC"
cp -aL "$GLIBC"/. "$BUILD/rootfs/lib/"
# 动态链接器放 /lib64（java 等标准 ELF 的 INTERP 指向这里）
cp -L "$GLIBC"/ld-linux-x86-64.so.2 "$BUILD/rootfs/lib64/ld-linux-x86-64.so.2"
# 坑：ld.so 默认搜索路径是 /lib64 而不是 /lib；库全在 /lib 时
# 容器内报 "libm.so.6: cannot open shared object file"。
# 必须在 /lib64 给每个库补相对软链。
# 注意排除 ld-linux：/lib64/ld-linux-x86-64.so.2 必须是实体文件，
# 若被 ln -sf ../lib/ld-linux... 覆盖会形成自指悬空链（java/sh 全部起不来）
( cd "$BUILD/rootfs/lib64"
  for f in ../lib/*.so*; do
    case "$(basename "$f")" in ld-linux*) continue ;; esac
    ln -sf "$f" "$(basename "$f")"
  done )
# 关键坑：Nix 构建的 ld.so 把「自身所在 nix store 目录」编译为默认搜索路径，
# 标准 /lib64 /lib 反而不在默认路径里（--library-path 显式指定才有效）。
# 解法：在容器 rootfs 里复刻该 store 路径并软链到 /lib。
GLIBC_STORE_DIR=$(dirname "$GLIBC")
mkdir -p "$BUILD/rootfs$GLIBC_STORE_DIR"
ln -sfn /lib "$BUILD/rootfs$GLIBC_STORE_DIR/lib"
say "glibc 拷贝完成（含 lib64 软链 + nix store 兼容路径）"
# nsswitch / resolv 兜底
printf 'hosts: files dns\npasswd: files\ngroup: files\n' > "$BUILD/rootfs/etc/nsswitch.conf"
printf 'nameserver 8.8.8.8\nnameserver 2001:4860:4860::8888\n' > "$BUILD/rootfs/etc/resolv.conf"
printf 'root:x:0:0:root:/root:/bin/sh\n' > "$BUILD/rootfs/etc/passwd"
printf 'root:x:0:\n' > "$BUILD/rootfs/etc/group"

# 3) Temurin JRE 25
[ -f "$JRE_TARBALL" ] || { echo "缺少 $JRE_TARBALL"; exit 1; }
say "解包 JRE: $JRE_TARBALL"
mkdir -p "$BUILD/rootfs/opt/jre"
# 标准 Adoptium 包内含顶层目录 jdk-<ver>-jre/，用 --strip-components=1 拍平
tar xzf "$JRE_TARBALL" -C "$BUILD/rootfs/opt/jre" --strip-components=1
say "JRE 解包完成"
[ -x "$BUILD/rootfs/opt/jre/bin/java" ] || { echo "JRE 解包后缺 bin/java"; exit 1; }
# 注意：不要 patchelf --remove-rpath！标准 Temurin 的 java 依赖
# $ORIGIN/../lib 的 RPATH 找 libjli.so，清掉后容器内起不来。
# 启动脚本里用 LD_LIBRARY_PATH 兜底（见下方 start.sh）。

# 4) Minecraft 服务端
[ -f "$MC_SERVER_JAR" ] || { echo "缺少 $MC_SERVER_JAR"; exit 1; }
say "复制 server.jar"
mkdir -p "$BUILD/rootfs/opt/mc"
cp "$MC_SERVER_JAR" "$BUILD/rootfs/opt/mc/server.jar"
[ -f "$MC_PROPS" ] && cp "$MC_PROPS" "$BUILD/rootfs/opt/mc/server.properties"
printf 'eula=true\n' > "$BUILD/rootfs/opt/mc/eula.txt"

# 5) 启动脚本
cat > "$BUILD/rootfs/opt/mc/start.sh" <<'EOS'
#!/bin/sh
# AR8 Minecraft 容器启动脚本
cd /opt/mc || exit 1
export HOME=/root
# Nix ld.so 默认搜索路径不含 /lib64；JRE 的 $ORIGIN RPATH 在容器内
# 也可能解析异常。显式 LD_LIBRARY_PATH 兜底，保证 libjli/libjvm 可加载。
export LD_LIBRARY_PATH=/opt/jre/lib:/opt/jre/lib/server:/lib
exec /opt/jre/bin/java \
  -Xms256M -Xmx700M -XX:+UseSerialGC -XX:MaxMetaspaceSize=192M \
  -Djava.net.preferIPv4Stack=true \
  -jar /opt/mc/server.jar nogui
EOS
chmod 755 "$BUILD/rootfs/opt/mc/start.sh"

# 6) 打包成 OCI layer
say "打包 layer"
cd "$BUILD/rootfs"
LAYER_TAR="$BUILD/layer.tar"
tar --numeric-owner --owner=0 --group=0 -cf "$LAYER_TAR" .
cd "$BUILD"
LAYER_DIGEST="sha256:$(sha256sum layer.tar | cut -d' ' -f1)"
# OCI layout：blobs/sha256/<hex> 必须是「文件本身」，不能是目录（否则 ctr 报 unrecognized image format）
cp layer.tar "blobs/sha256/${LAYER_DIGEST#sha256:}"
LAYER_SIZE=$(stat -c%s layer.tar)

# 7) config
cat > config.json <<EOF
{
  "architecture": "amd64",
  "config": {
    "Env": ["PATH=/bin:/usr/bin:/opt/jre/bin", "HOME=/root", "LC_ALL=C.UTF-8"],
    "Entrypoint": ["/opt/mc/start.sh"],
    "WorkingDir": "/opt/mc",
    "Labels": {"io.ar8.workload": "minecraft", "io.ar8.version": "26.3"}
  },
  "rootfs": {"type": "layers", "diff_ids": ["$LAYER_DIGEST"]},
  "history": [{"created": "2026-11-17T00:00:00Z", "created_by": "ar8 mc image build"}]
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

# 9) index.json（带 io.containerd.image.name 注解，k3s import 需要）
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

# 10) 打包成可 import 的 tar（只含 OCI layout 必需文件；
#     不要把 rootfs/ 和散落的 layer.tar 打进去，体积翻倍且易混淆格式）
if [ "${PACK_TAR:-1}" = "1" ]; then
  say "打包 import tar: $BUILD.tar"
  tar -C "$BUILD" -cf "$BUILD.tar" oci-layout index.json blobs
  say "tar 大小: $(($(stat -c%s "$BUILD.tar") / 1024 / 1024)) MB"
fi

say "=== OCI layout 就绪: $BUILD ==="
say "镜像名: $IMAGE_NAME"
say "layer 大小: $((LAYER_SIZE / 1024 / 1024)) MB"
echo "$IMAGE_NAME"
