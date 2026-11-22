#!/usr/bin/env python3
"""KSAYA collector —— 公网机接收端点（心跳上报汇聚）。

设计目标（对应「固定向公网机发信息，并被公网机接收」）：
  1. 纯 Python 标准库（http.server + threading），不依赖任何第三方包，
     在 Ubuntu 24.04 / Python 3.12 上开箱即跑。
  2. 监听一个端口，接收各节点 POST 上来的心跳 JSON，按 node_id 落盘：
       - 最新状态   ->  <DATA_DIR>/nodes/<node_id>.json
       - 追加历史   ->  <DATA_DIR>/history/<node_id>.jsonl
  3. 提供只读查询端点，供确认「公网机真的收到了」：
       GET /healthz                 -> ok
       GET /nodes                   -> 所有节点最新状态（JSON）
       GET /nodes/<node_id>         -> 单节点最新状态（JSON）
       POST /report                 -> 上报入口（application/json）

环境变量（全部可覆盖）：
  KSAYA_COLLECTOR_PORT   监听端口，默认 80
  KSAYA_COLLECTOR_HOST   绑定地址，默认 0.0.0.0
  KSAYA_COLLECTOR_DIR    数据目录，默认 /var/lib/ksaya-collector

写入节点侧提示：节点 heartbeat 可通过：
   curl -s -X POST -H 'Content-Type: application/json' \
        -d '{"node_id":"ksaya-xxxx","hostname":"...","ip":"...","ts":"..."}' \
        http://<公网IP>:<PORT>/report
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

HOST = os.environ.get("KSAYA_COLLECTOR_HOST", "0.0.0.0")
# 默认走 TCP 80：节点上报走标准 HTTP 端口，利好看一眼、过安全组也少一个口。
# 需要特权(<1024)才能绑 80；collector 以 root 运行（见 systemd unit）。
PORT = int(os.environ.get("KSAYA_COLLECTOR_PORT", "80"))
DATA_DIR = os.environ.get("KSAYA_COLLECTOR_DIR", "/var/lib/ksaya-collector")
NODES_DIR = os.path.join(DATA_DIR, "nodes")
HIST_DIR = os.path.join(DATA_DIR, "history")

# 上天时做一次目录初始化；权限放开一点方便运维查看，但数据本身由 root 写。
for _d in (NODES_DIR, HIST_DIR):
    os.makedirs(_d, mode=0o755, exist_ok=True)


def _safe_id(raw: str) -> str:
    """把 node_id 过滤成安全文件名，防止路径穿越。"""
    if not raw:
        return ""
    keep = [c for c in raw if c.isalnum() or c in "-_."]
    s = "".join(keep)[:64]
    return s


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


class Handler(BaseHTTPRequestHandler):
    server_version = "ksaya-collector/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 走 stdout 便于 systemd journal 采集
        sys.stdout.write("[%s] %s\n" % (_now(), fmt % args))
        sys.stdout.flush()

    def do_GET(self):
        path = unquote(self.path.split("?")[0]).rstrip("/")
        if path == "/healthz":
            self._send(200, {"ok": True, "ts": _now()})
            return
        if path == "/nodes":
            listing = []
            if os.path.isdir(NODES_DIR):
                for fn in sorted(os.listdir(NODES_DIR)):
                    if not fn.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(NODES_DIR, fn), "r", encoding="utf-8") as f:
                            listing.append(json.load(f))
                    except Exception:
                        continue
            self._send(200, {"nodes": listing, "count": len(listing)})
            return
        if path.startswith("/nodes/"):
            nid = _safe_id(path[len("/nodes/"):])
            if not nid:
                self._send(400, {"error": "bad node_id"})
                return
            fp = os.path.join(NODES_DIR, nid + ".json")
            if not os.path.isfile(fp):
                self._send(404, {"error": "node not found", "node_id": nid})
                return
            with open(fp, "r", encoding="utf-8") as f:
                self._send(200, json.load(f))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = unquote(self.path.split("?")[0]).rstrip("/")
        if path != "/report":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            self._send(400, {"error": "empty body"})
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send(400, {"error": "bad json: %s" % e})
            return
        nid = _safe_id(str(data.get("node_id", "")))
        if not nid:
            self._send(400, {"error": "missing node_id"})
            return

        # 接收时间戳打点：记录公网机什么时刻收到（与节点自报 ts 区分）。
        data["received_at"] = _now()
        data["received_unixts"] = int(time.time())

        try:
            with open(os.path.join(NODES_DIR, nid + ".json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            with open(os.path.join(HIST_DIR, nid + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception as e:
            self._send(500, {"error": "persist failed: %s" % e})
            return

        self._send(200, {"ok": True, "node_id": nid, "received_at": data["received_at"]})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("KSAYA collector listening on %s:%d  data=%s" % (HOST, PORT, DATA_DIR), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()