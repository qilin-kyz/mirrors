#!/usr/bin/env python3
# ============================================================
#  MirrorOS WebUI —— 复古 BIOS 风格管理台（纯 stdlib，零依赖）
# ------------------------------------------------------------
#  适配器层自动探测三栈（incus / docker / virsh）；
#  任何一个不在，就对该栈降级；全部不在 → MOCK 模式（开发演示用）。
#
#  覆盖规格管理面：实例全生命周期、数据释放、硬盘/U盘管理、
#  系统镜像（快照）保存、镜痕日志、电源。
# ============================================================

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ISO_DIR = os.environ.get("MIRROR_ISO_DIR", "/var/lib/mirror/iso")
SNAP_DIR = os.environ.get("MIRROR_SNAP_DIR", "/var/lib/mirror/snapshots")
PASSWORD_FILE = os.environ.get("MIRROR_UI_PASSWORD_FILE",
                               "/var/lib/mirror/config/webui-password")
MOCK = False
SESSIONS = {}          # token -> expire
SESSION_TTL = 3600 * 8


# ------------------------------------------------------------ 工具 --
def run(cmd, timeout=30):
    """执行命令并返回 (rc, stdout)。一切外部触达都过这一扇门。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def have(binary):
    return shutil.which(binary) is not None


def read_password():
    try:
        with open(PASSWORD_FILE) as f:
            return f.read().strip()
    except OSError:
        return "20120605"  # 出厂口令（mock/开发环境）


# ------------------------------------------------------------ 实例三栈 --
def list_instances():
    """统一返回 [{engine, name, state, info}]，engine ∈ incus/qemu/docker"""
    out = []

    if have("incus") and not MOCK:
        rc, s = run(["incus", "list", "--format", "json"])
        if rc == 0:
            try:
                for i in json.loads(s):
                    out.append({
                        "engine": "incus",
                        "name": i.get("name"),
                        "state": i.get("status", "unknown").lower(),
                        "info": i.get("config", {}).get("image.description", ""),
                    })
            except json.JSONDecodeError:
                pass
    if have("docker") and not MOCK:
        rc, s = run(["docker", "ps", "-a", "--format",
                     "{{.Names}}|{{.State}}|{{.Image}}"])
        if rc == 0:
            for line in s.strip().splitlines():
                if "|" in line:
                    name, state, image = line.split("|", 2)
                    out.append({"engine": "docker", "name": name,
                                "state": state, "info": image})
    if have("virsh") and not MOCK:
        rc, s = run(["virsh", "list", "--all", "--name"])
        if rc == 0:
            for name in s.strip().splitlines():
                name = name.strip()
                if not name:
                    continue
                _, st = run(["virsh", "domstate", name])
                out.append({"engine": "qemu", "name": name,
                            "state": st.strip(), "info": "KVM/QEMU"})

    if not out and MOCK:
        out = [
            {"engine": "incus", "name": "gw-router", "state": "running",
             "info": "Debian 13 (container)"},
            {"engine": "incus", "name": "win11-work", "state": "stopped",
             "info": "Windows 11 VM (GPU passthrough)"},
            {"engine": "qemu", "name": "freebsd-nas", "state": "running",
             "info": "KVM/QEMU · installed from /var/lib/mirror/iso"},
            {"engine": "docker", "name": "ollama-gpu", "state": "running",
             "info": "ollama:latest · CUDA on 5070 Ti"},
            {"engine": "docker", "name": "caddy-edge", "state": "exited",
             "info": "caddy:2"},
        ]
    return sorted(out, key=lambda x: (x["engine"], x["name"]))


def instance_action(engine, name, action):
    if MOCK:
        return True, f"[mock] {engine}/{name}: {action} ok"
    table = {
        "incus": {"start": ["incus", "start", name],
                  "stop": ["incus", "stop", name],
                  "restart": ["incus", "restart", name],
                  "delete": ["incus", "delete", "--force", name]},
        "docker": {"start": ["docker", "start", name],
                   "stop": ["docker", "stop", name],
                   "restart": ["docker", "restart", name],
                   "delete": ["docker", "rm", "-f", name]},
        "qemu": {"start": ["virsh", "start", name],
                 "stop": ["virsh", "shutdown", name],
                 "restart": ["virsh", "reboot", name],
                 "delete": ["virsh", "undefine", name, "--remove-all-storage"]},
    }
    cmd = table.get(engine, {}).get(action)
    if not cmd:
        return False, f"unsupported: {engine}/{action}"
    rc, s = run(cmd, timeout=120)
    return rc == 0, s.strip() or f"{action} done"


import re
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


def create_instance(spec):
    """新建实例向导：
       qemu   → 从 ISO 盘安装（镜存的唯一消费通道）
       incus  → 从镜像启动（images:xxx 远程镜像）
       docker → 拉取镜像即跑（用时拉取，可挂 5070 Ti GPU）
    """
    engine = spec.get("engine", "")
    name = (spec.get("name") or "").strip()
    if not NAME_RE.match(name):
        return False, f"illegal instance name: {name!r}"

    if MOCK:
        detail = {"qemu": f"qcow2 {spec.get('disk_gb', 64)}G on instances disk, "
                          f"cdrom={spec.get('iso', '?')}",
                  "incus": f"launch {spec.get('image', '?')}"
                           + (" + 5070 Ti GPU (gpu device added)" if spec.get("gpu") else ""),
                  "docker": f"pull & run {spec.get('image', '?')} (no GPU — Incus only)"}
        return True, f"[mock] {engine}/{name}: created — {detail.get(engine, '')}"

    if engine == "qemu":
        iso = os.path.basename(spec.get("iso", ""))
        if not iso.lower().endswith(".iso") or not os.path.isfile(
                os.path.join(ISO_DIR, iso)):
            return False, f"ISO not found in read-only library: {iso}"
        cmd = ["virt-install", "--name", name,
               "--memory", str(int(spec.get("memory_mb", 4096))),
               "--vcpus", str(int(spec.get("vcpus", 4))),
               "--disk", f"path=/var/lib/mirror/instances/qemu/{name}.qcow2,"
                         f"size={int(spec.get('disk_gb', 64))},format=qcow2",
               "--cdrom", os.path.join(ISO_DIR, iso),
               "--os-variant", "generic",
               "--network", "bridge=br-vm,model=virtio",
               "--graphics", "none", "--noautoconsole",
               "--boot", "uefi"]
        rc, s = run(cmd, timeout=300)
        return rc == 0, s.strip() or f"{name} defined, installer running"

    if engine == "incus":
        image = spec.get("image", "").strip()
        if not image:
            return False, "image required (e.g. images:debian/13)"
        rc, s = run(["incus", "launch", image, name], timeout=600)
        if rc == 0 and spec.get("gpu"):
            run(["incus", "config", "device", "add", name, "gpu0", "gpu"])
        return rc == 0, s.strip() or f"{name} launched"

    if engine == "docker":
        image = spec.get("image", "").strip()
        if not image:
            return False, "image required (e.g. ollama/ollama:latest)"
        rc, s = run(["docker", "pull", image], timeout=900)   # 用时拉取
        if rc != 0:
            return False, f"pull failed: {s.strip()}"
        # 5070 Ti 共享只对 Incus 开放（规格），docker 一律不挂显卡
        cmd = ["docker", "run", "-d", "--name", name,
               "--restart", "unless-stopped", image]
        rc, s = run(cmd, timeout=120)
        return rc == 0, s.strip() or f"{name} running"

    return False, f"unknown engine: {engine}"



# ------------------------------------------------------------ 镜像/存储/快照 --
def list_isos():
    if MOCK:
        return [
            {"name": "debian-13.1.0-amd64-netinst.iso", "size_mb": 640},
            {"name": "windows11-24h2-zh-cn.iso", "size_mb": 5900},
            {"name": "freebsd-14.2-amd64-disc1.iso", "size_mb": 1100},
        ]
    try:
        return sorted(
            {"name": f, "size_mb": round(os.path.getsize(
                os.path.join(ISO_DIR, f)) / 1048576)}
            for f in os.listdir(ISO_DIR) if f.lower().endswith(".iso"))
    except OSError:
        return []


def storage_info():
    if MOCK:
        return {
            "disks": [
                {"role": "system", "dev": "/dev/mirror/system",
                 "model": "NVMe 256G", "mount": "/", "used": "41%", "ro": False},
                {"role": "instances", "dev": "/dev/mirror/instances",
                 "model": "NVMe 1T", "mount": "/var/lib/mirror/instances",
                 "used": "23%", "ro": False},
                {"role": "iso", "dev": "/dev/mirror/iso",
                 "model": "USB 32G", "mount": "/var/lib/mirror/iso",
                 "used": "58%", "ro": True},
            ],
            "iso_mode": "ro",
        }
    rc, s = run(["mirror-disks"])
    rc2, mode = run(["findmnt", "-n", "-o", "OPTIONS", ISO_DIR])
    return {"raw": s, "iso_mode": "ro" if "ro" in mode else "rw"}


def set_iso_mode(mode):
    if MOCK:
        return True, f"[mock] ISO library -> {mode}"
    rc, s = run(["mirror-iso-mode", mode])
    return rc == 0, s.strip()


def list_snapshots():
    if MOCK:
        return [{"name": "mirror-system-20260823-030001.tar.zst", "size_mb": 96},
                {"name": "mirror-system-20260816-030002.tar.zst", "size_mb": 94}]
    try:
        return sorted(
            ({"name": f, "size_mb": round(os.path.getsize(
                os.path.join(SNAP_DIR, f)) / 1048576, 1)}
             for f in os.listdir(SNAP_DIR) if f.endswith(".tar.zst")),
            key=lambda x: x["name"], reverse=True)
    except OSError:
        return []


def make_snapshot():
    if MOCK:
        return True, "[mock] snapshot sealed (system only, instances excluded)"
    rc, s = run(["mirror-snapshot"], timeout=600)
    return rc == 0, s.strip()


def delete_snapshot(name):
    if "/" in name or ".." in name:
        return False, "illegal name"
    if MOCK:
        return True, f"[mock] released {name}"
    try:
        os.remove(os.path.join(SNAP_DIR, name))
        return True, f"released {name}"
    except OSError as e:
        return False, str(e)


# ------------------------------------------------------------ 系统/日志/电源 --
def overview():
    if MOCK:
        return {
            "host": "mirrorhost", "kernel": "6.18.2-LTS (znver5 build)",
            "uptime": "17 days, 04:12", "load": "0.42 0.31 0.27",
            "cpu": "AMD Ryzen 5 9600X · 6C12T · amd-pstate/active",
            "gpu": "RTX 5070 Ti · driver 575.64 · persistence ON",
            "mem": "11.2G / 64G",
            "net": "mgmt r8169 1G · vm-uplink r8152 2.5G (br-vm, host-less)",
            "engines": ["incus", "qemu", "docker"],
        }
    _, kernel = run(["uname", "-r"])
    _, uptime = run(["uptime", "-p"])
    _, load = run(["sh", "-c", "cut -d' ' -f1-3 /proc/loadavg"])
    _, gpu = run(["nvidia-smi", "--query-gpu=name,driver_version",
                  "--format=csv,noheader"])
    _, mem = run(["sh", "-c",
                  "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}"
                  "END{printf \"%.1fG / %.1fG\", (t-a)/1048576, t/1048576}' "
                  "/proc/meminfo"])
    _, disk = run(["df", "-h", "/", "/var/lib/mirror/instances",
                   "--output=target,pcent"])
    engines = [e for e in ("incus", "virsh", "docker") if have(e)]
    return {"host": os.uname().nodename, "kernel": kernel.strip(),
            "uptime": uptime.strip(), "load": load.strip(),
            "cpu": "AMD Ryzen 5 9600X (Zen 5)",
            "gpu": gpu.strip(), "mem": mem.strip(),
            "disk": disk.strip(), "engines": engines}


def audit_log():
    if MOCK:
        return ("[2026-08-25 15:31] mirror uid=1000 execve(/usr/bin/incus stop gw-router) rc=0\n"
                "[2026-08-25 15:33] mirror uid=1000 execve(/usr/bin/mirror-iso-mode rw) rc=0\n"
                "[2026-08-25 15:34] audit watch /var/lib/mirror/iso: windows11-24h2-zh-cn.iso created\n"
                "[2026-08-25 15:35] mirror uid=1000 execve(/usr/bin/mirror-iso-mode ro) rc=0\n"
                "[2026-08-25 15:40] sshd: session opened for mirror, attached to tmux 'mirror'")
    rc, s = run(["ausearch", "-k", "mirror-cmd", "-ts", "recent"], timeout=15)
    if rc != 0 or not s.strip():
        _, s = run(["journalctl", "-n", "100", "--no-pager"])
    return s[-8000:]


def system_power(what):
    if MOCK:
        return True, f"[mock] {what} acknowledged"
    if what == "reboot":
        run(["systemctl", "reboot"])
    elif what == "poweroff":
        run(["systemctl", "poweroff"])
    return True, f"{what} issued"


# ------------------------------------------------------------ HTTP --
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # -- 认证：令牌双通道（请求头优先，Cookie 兜底） --
    # 背景：管理台常被放进 iframe/反向代理预览，第三方 Cookie
    # 会被浏览器拦截导致"登录后仍 401"。请求头方案免疫此问题。
    def _token(self):
        header = self.headers.get("X-Mirror-Token")
        if header:
            return header
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("mirror_token="):
                return part.split("=", 1)[1]
        return None

    def _authed(self):
        tok = self._token()
        return tok in SESSIONS and SESSIONS[tok] > time.time()

    def _send(self, code, obj, ctype="application/json", headers=None):
        body = obj if isinstance(obj, (str, bytes)) else json.dumps(obj, ensure_ascii=False)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # -- 路由 --
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path == "/api/login" or not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        routes = {
            "/api/overview": overview,
            "/api/instances": list_instances,
            "/api/isos": list_isos,
            "/api/storage": storage_info,
            "/api/snapshots": list_snapshots,
            "/api/audit": lambda: {"log": audit_log()},
        }
        fn = routes.get(path)
        if fn:
            self._send(200, fn())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}

        if path == "/api/login":
            if body.get("password") == read_password():
                tok = secrets.token_hex(16)
                SESSIONS[tok] = time.time() + SESSION_TTL
                # 令牌同时走响应体与 Cookie：前端优先用请求头携带
                self._send(200, {"ok": True, "token": tok},
                           headers={"Set-Cookie":
                                    f"mirror_token={tok}; HttpOnly; SameSite=Strict; Path=/"})
            else:
                self._send(403, {"error": "wrong password"})
            return

        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return

        if path == "/api/instance/create":
            ok, msg = create_instance(body)
            self._send(200 if ok else 400, {"ok": ok, "message": msg})
        elif path == "/api/instance/action":
            ok, msg = instance_action(body.get("engine"), body.get("name"),
                                      body.get("action"))
            self._send(200 if ok else 400, {"ok": ok, "message": msg})
        elif path == "/api/storage/isomode":
            ok, msg = set_iso_mode(body.get("mode", "ro"))
            self._send(200 if ok else 400, {"ok": ok, "message": msg})
        elif path == "/api/snapshot/create":
            ok, msg = make_snapshot()
            self._send(200 if ok else 500, {"ok": ok, "message": msg})
        elif path == "/api/snapshot/delete":
            ok, msg = delete_snapshot(body.get("name", ""))
            self._send(200 if ok else 400, {"ok": ok, "message": msg})
        elif path == "/api/power":
            ok, msg = system_power(body.get("what"))
            self._send(200, {"ok": ok, "message": msg})
        else:
            self._send(404, {"error": "not found"})


def main():
    global MOCK
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--mock", action="store_true", help="演示模式：虚构三栈数据")
    args = ap.parse_args()
    MOCK = args.mock or not any(have(b) for b in ("incus", "docker", "virsh"))

    mode = "MOCK (demo data)" if MOCK else "LIVE (real engines detected)"
    print(f"MirrorOS WebUI on http://{args.host}:{args.port}  [{mode}]")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
