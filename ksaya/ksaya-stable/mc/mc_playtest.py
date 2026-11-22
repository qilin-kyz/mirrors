#!/usr/bin/env python3
"""KSAYA 集群 Minecraft 真实使用测试（协议级玩家客户端，protocol 777 / MC 26.3）。

不是 ping，而是完整走一遍真实玩家流程：
  handshake -> login start -> set compression -> login success -> login ack
  -> configuration（known packs / registry data / finish）
  -> play（Login/Join Game、确认传送、player_loaded、keep-alive 应答、
     client_tick_end 心跳、发送聊天并验证广播回显）
并用 status ping 验证在线人数 0 -> 1 -> 0。

包 ID 来源：服务端官方 data generator 导出的 packets.json（packets-777.json），
字段布局来源：minecraft.wiki protocol 777 原文。不硬编码猜测。

用法：
  python3 mc_playtest.py --host 10.23.112.21 --port 30065 --name KSAYATester
  python3 mc_playtest.py --host ... --chat "hello" --stay 20
退出码：0 = 全部断言通过；1 = 有失败。
"""
import argparse
import io
import json
import os
import socket
import struct
import sys
import time
import uuid
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PACKETS = json.load(open(os.path.join(HERE, "packets-777.json"), encoding="utf-8"))
PROTO = 777


def pid(state: str, direction: str, name: str) -> int:
    return PACKETS[state][direction]["minecraft:" + name]["protocol_id"]


# ---------------- 编码原语 ----------------
def varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 32
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def string(s: str) -> bytes:
    b = s.encode("utf-8")
    return varint(len(b)) + b


def pack_packet(payload: bytes, threshold: int) -> bytes:
    """threshold < 0 表示压缩未启用。"""
    if threshold < 0:
        return varint(len(payload)) + payload
    if len(payload) >= threshold:
        comp = zlib.compress(payload)
        data = varint(len(payload)) + comp
    else:
        data = varint(0) + payload
    return varint(len(data)) + data


class Reader:
    def __init__(self, data: bytes):
        self.buf = io.BytesIO(data)

    def read(self, n: int) -> bytes:
        b = self.buf.read(n)
        if len(b) != n:
            raise EOFError(f"need {n} got {len(b)}")
        return b

    def varint(self) -> int:
        n = 0
        for i in range(5):
            b = self.read(1)[0]
            n |= (b & 0x7F) << (7 * i)
            if not b & 0x80:
                break
        if n >= 1 << 31:
            n -= 1 << 32
        return n

    def string(self) -> str:
        return self.read(self.varint()).decode("utf-8")

    def uuid(self) -> str:
        return str(uuid.UUID(bytes=self.read(16)))

    def long(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def int(self) -> int:
        return struct.unpack(">i", self.read(4))[0]

    def double(self) -> float:
        return struct.unpack(">d", self.read(8))[0]

    def float(self) -> float:
        return struct.unpack(">f", self.read(4))[0]

    def bool(self) -> bool:
        return self.read(1)[0] != 0

    def byte(self) -> int:
        return self.read(1)[0]

    def remaining(self) -> bytes:
        return self.buf.read()


# ---------------- NBT 跳过器（registry data / text component 用） ----------------
def nbt_skip(r: Reader, tag_type: int, named: bool):
    if named:
        r.string()  # root name（network NBT 无类型前缀时由调用方处理）
    if tag_type == 0:
        return
    if tag_type == 1:
        r.read(1)
    elif tag_type == 2:
        r.read(2)
    elif tag_type == 3:
        r.read(4)
    elif tag_type == 4:
        r.read(8)
    elif tag_type == 5:
        r.read(4)
    elif tag_type == 6:
        r.read(8)
    elif tag_type == 7:
        r.read(r.int())
    elif tag_type == 8:
        r.string()
    elif tag_type == 9:
        et = r.byte()
        n = r.int()
        for _ in range(n):
            nbt_skip(r, et, False)
    elif tag_type == 10:
        while True:
            et = r.byte()
            if et == 0:
                return
            r.string()
            nbt_skip(r, et, False)
    elif tag_type == 11:
        r.read(4 * r.int())
    elif tag_type == 12:
        r.read(8 * r.int())
    else:
        raise ValueError(f"bad nbt tag {tag_type}")


def text_component_extract(data: bytes) -> str:
    """尽力从 Text Component（network NBT）中提取可读文本。"""
    try:
        r = Reader(data)
        tt = r.byte()
        if tt == 8:
            return r.string()
        # compound：递归找 text / translate 字段
        texts = []

        def walk(tag_type):
            if tag_type == 8:
                texts.append(r.string())
            elif tag_type == 10:
                while True:
                    et = r.byte()
                    if et == 0:
                        return
                    name = r.string()
                    if name in ("text", "translate"):
                        if et == 8:
                            texts.append(r.string())
                        else:
                            nbt_skip(r, et, False)
                    else:
                        nbt_skip(r, et, False)
            elif tag_type == 9:
                et = r.byte()
                n = r.int()
                for _ in range(n):
                    walk(et)
            else:
                nbt_skip(r, tag_type, False)

        walk(tt)
        return " ".join(texts)
    except Exception:
        return data[:80].decode("utf-8", "replace")


# ---------------- 连接 ----------------
class MCConn:
    def __init__(self, host: str, port: int, timeout: float = 15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.threshold = -1
        self.buf = b""
        self.host, self.port = host, port

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, state: str, name: str, payload: bytes = b""):
        pkt = varint(pid(state, "serverbound", name)) + payload
        self.sock.sendall(pack_packet(pkt, self.threshold))

    def _fill(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_varint(self) -> int:
        n = 0
        for i in range(5):
            b = self._fill(1)[0]
            n |= (b & 0x7F) << (7 * i)
            if not b & 0x80:
                return n if n < 1 << 31 else n - (1 << 32)
        raise ValueError("varint too long")

    def recv(self) -> tuple:
        length = self._read_varint()
        data = self._fill(length)
        if self.threshold >= 0:
            r = Reader(data)
            dlen = r.varint()
            rest = r.remaining()
            if dlen > 0:
                rest = zlib.decompress(rest)
                if len(rest) != dlen:
                    raise ValueError("decompressed size mismatch")
            data = rest
        r = Reader(data)
        return r.varint(), r


def status_ping(host: str, port: int) -> dict:
    c = MCConn(host, port)
    try:
        hs = varint(PROTO) + string(host) + struct.pack(">H", port) + varint(1)
        c.send("handshake", "intention", hs)
        c.send("status", "status_request")
        p, r = c.recv()
        return json.loads(r.string())
    finally:
        c.close()


# ---------------- 测试主流程 ----------------
class PlayTest:
    def __init__(self, args):
        self.args = args
        self.results = []
        self.keepalive_answered = 0
        self.chat_echo = None
        self.system_msgs = []
        self.join_info = {}
        self.pos = None
        self.chunks = 0
        self.disconnected = None

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)

    def client_information(self) -> bytes:
        return (string("en_US") + bytes([4]) + varint(0) + b"\x01" + bytes([0x7F])
                + varint(1) + b"\x00" + b"\x01" + varint(0))

    def run(self):
        a = self.args
        # 0) 登录前在线人数
        st0 = status_ping(a.host, a.port)
        online0 = st0["players"]["online"]
        self.check("status-ping-before", True,
                   f"version={st0['version']['name']} proto={st0['version']['protocol']} online={online0}")

        c = MCConn(a.host, a.port, timeout=a.timeout)
        try:
            self._login(c)
            self._configuration(c)
            self._play(c)
        finally:
            c.close()

        # 退出后在线人数回落
        time.sleep(1.5)
        st1 = status_ping(a.host, a.port)
        online1 = st1["players"]["online"]
        self.check("online-count-restored", online1 == online0,
                   f"before={online0} after={online1}")

        fails = [r for r in self.results if not r[1]]
        print(f"\nMC-PLAYTEST-SUMMARY total={len(self.results)} pass={len(self.results)-len(fails)} fail={len(fails)}")
        return 1 if fails else 0

    def _login(self, c: MCConn):
        a = self.args
        hs = varint(PROTO) + string(a.host) + struct.pack(">H", a.port) + varint(2)
        c.send("handshake", "intention", hs)
        player_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, "ksaya.offline." + a.name)
        c.send("login", "hello", string(a.name) + player_uuid.bytes)
        got_compression = got_success = False
        deadline = time.time() + a.timeout
        while time.time() < deadline and not got_success:
            p, r = c.recv()
            if p == pid("login", "clientbound", "login_compression"):
                c.threshold = r.varint()
                got_compression = True
            elif p == pid("login", "clientbound", "login_finished"):
                prof_uuid = r.uuid()
                username = r.string()
                nprops = r.varint()
                for _ in range(nprops):
                    r.string(); r.string()
                    if r.bool():
                        r.string()
                session = r.uuid()
                got_success = True
                self.check("login-success", username == a.name,
                           f"user={username} uuid={prof_uuid} compression={got_compression} session={session[:8]}")
                c.send("login", "login_acknowledged")
            elif p == pid("login", "clientbound", "login_disconnect"):
                reason = r.remaining()
                raise RuntimeError(f"login disconnect: {text_component_extract(reason)}")
            elif p == pid("login", "clientbound", "hello"):
                raise RuntimeError("server requested encryption (online-mode=true?)")
            elif p == pid("login", "clientbound", "custom_query"):
                mid = r.varint()
                c.send("login", "custom_query_answer", varint(mid) + b"\x00")
            elif p == pid("login", "clientbound", "cookie_request"):
                key = r.string()
                c.send("login", "cookie_response", string(key) + b"\x00")
        if not got_success:
            raise RuntimeError("login timeout")

    def _configuration(self, c: MCConn):
        a = self.args
        c.send("configuration", "client_information", self.client_information())
        registries = []
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            p, r = c.recv()
            if p == pid("configuration", "clientbound", "select_known_packs"):
                n = r.varint()
                packs = [(r.string(), r.string(), r.string()) for _ in range(n)]
                # 声明已知 minecraft:core（版本一致时服务端可省略 NBT）
                known = [pk for pk in packs if pk[0] == "minecraft" and pk[1] == "core"]
                payload = varint(len(known)) + b"".join(
                    string(ns) + string(i) + string(v) for ns, i, v in known)
                c.send("configuration", "select_known_packs", payload)
            elif p == pid("configuration", "clientbound", "registry_data"):
                reg = r.string()
                n = r.varint()
                for _ in range(n):
                    r.string()          # entry id
                    if r.bool():        # has data
                        tt = r.byte()
                        nbt_skip(r, tt, False)
                registries.append(f"{reg}({n})")
            elif p == pid("configuration", "clientbound", "finish_configuration"):
                c.send("configuration", "finish_configuration")
                self.check("configuration-done", True,
                           f"registries={len(registries)}: " + ",".join(registries[:6]) + "...")
                return
            elif p == pid("configuration", "clientbound", "keep_alive"):
                c.send("configuration", "keep_alive", struct.pack(">q", r.long()))
            elif p == pid("configuration", "clientbound", "ping"):
                c.send("configuration", "pong", struct.pack(">i", r.int()))
            elif p == pid("configuration", "clientbound", "cookie_request"):
                key = r.string()
                c.send("configuration", "cookie_response", string(key) + b"\x00")
            elif p == pid("configuration", "clientbound", "disconnect"):
                raise RuntimeError(f"config disconnect: {text_component_extract(r.remaining())}")
            elif p == pid("configuration", "clientbound", "code_of_conduct"):
                r.string()
                c.send("configuration", "accept_code_of_conduct")
            elif p == pid("configuration", "clientbound", "resource_pack_push"):
                puuid = r.uuid()
                r.string(); r.string()
                if r.bool():
                    r.string()
                c.send("configuration", "resource_pack", puuid.bytes + varint(3))
            # 其余（feature flags / tags / server links / dialogs）无需应答
        raise RuntimeError("configuration timeout")

    def _play(self, c: MCConn):
        a = self.args
        joined = teleported = loaded = False
        chat_sent = False
        ticks = 0
        last_tick = 0.0
        deadline = time.time() + a.stay
        # keep-alive 宽限上限：进入 play 后最多再等 25s（首个 keep-alive 通常 15-20s）
        self._ka_grace_deadline = deadline + 25.0
        while time.time() < deadline:
            # 服务端 keep-alive 每 ~15-20s 一发；若停留窗口即将结束仍没收到，
            # 主动多留最多 25s 等它到达（否则短 --stay 会误报 answered=0）。
            if (self.keepalive_answered == 0
                    and deadline - time.time() < 1.0
                    and time.time() < self._ka_grace_deadline):
                deadline = time.time() + 1.0
            # 每 50ms 一个 client tick（真实客户端行为；服务端 60 tick 后认定已加载）
            now = time.time()
            if now - last_tick >= 0.05:
                c.send("play", "client_tick_end")
                ticks += 1
                last_tick = now
                if self.pos and ticks % 20 == 0:
                    x, y, z, yaw, pitch = self.pos
                    c.send("play", "move_player_pos_rot",
                           struct.pack(">ddd", x, y, z) + struct.pack(">ff", yaw, pitch) + b"\x01")
            try:
                p, r = c.recv()
            except socket.timeout:
                continue
            if p == pid("play", "clientbound", "login"):
                eid = r.int()
                hardcore = r.bool()
                ndim = r.varint()
                dims = [r.string() for _ in range(ndim)]
                maxp = r.varint(); vd = r.varint(); sd = r.varint()
                r.bool(); r.bool(); r.bool()
                dimtype = r.varint()
                dimname = r.string()
                r.long()
                gamemode = r.varint()
                self.join_info = dict(eid=eid, dim=dimname, gamemode=gamemode,
                                      view=vd, sim=sd, dims=dims)
                joined = True
                self.check("join-game", True,
                           f"eid={eid} dim={dimname} gamemode={gamemode} view={vd} dims={len(dims)}")
                c.send("play", "client_information", self.client_information())
                c.send("play", "client_command", varint(1))  # request stats
            elif p == pid("play", "clientbound", "player_position"):
                tid = r.varint()
                x, y, z = r.double(), r.double(), r.double()
                r.double(); r.double(); r.double()
                yaw, pitch = r.float(), r.float()
                flags = r.int()
                self.pos = (x, y, z, yaw, pitch)
                c.send("play", "accept_teleportation",
                       varint(tid) + struct.pack(">ddd", x, y, z) + struct.pack(">ff", yaw, pitch))
                if not teleported:
                    teleported = True
                    self.check("spawn-position", True, f"x={x:.1f} y={y:.1f} z={z:.1f} teleport_id={tid}")
            elif p == pid("play", "clientbound", "keep_alive"):
                c.send("play", "keep_alive", struct.pack(">q", r.long()))
                self.keepalive_answered += 1
            elif p == pid("play", "clientbound", "chunk_batch_finished"):
                n = r.varint()
                self.chunks += n
                c.send("play", "chunk_batch_received", struct.pack(">f", 25.0))
                if not loaded:
                    loaded = True
                    c.send("play", "player_loaded")
            elif p == pid("play", "clientbound", "system_chat"):
                content = r.remaining()
                # 最后一个字节是 overlay bool
                txt = text_component_extract(content[:-1])
                self.system_msgs.append(txt)
            elif p == pid("play", "clientbound", "player_chat"):
                raw = r.remaining()
                if a.chat.encode() in raw:
                    self.chat_echo = a.chat
            elif p == pid("play", "clientbound", "disconnect"):
                self.disconnected = text_component_extract(r.remaining())
                self.check("no-kick", False, f"kicked: {self.disconnected}")
                return
            # 其余 play 包（实体/区块/音效等）消费掉即可

            # 加入 + 传送确认后发送聊天
            if joined and teleported and not chat_sent and ticks >= 10:
                ts = int(time.time() * 1000)
                payload = (string(a.chat) + struct.pack(">qq", ts, 0) + b"\x00"
                           + varint(0) + b"\x00\x00\x00" + b"\x00")
                c.send("play", "chat", payload)
                chat_sent = True
                self._chat_sent_at = time.time()

            # 聊天回显等待最多 8 秒
            if chat_sent and self.chat_echo is None and time.time() - self._chat_sent_at > 8:
                self.check("chat-echo", False, "no player_chat echo within 8s")
                chat_sent = False  # 只判一次
                self._chat_failed = True
            if self.chat_echo and not getattr(self, "_chat_ok", False):
                self._chat_ok = True
                self.check("chat-echo", True, f"broadcast received: {self.chat_echo!r}")

        self.check("keepalive-answered", self.keepalive_answered >= 1,
                   f"answered={self.keepalive_answered}")
        self.check("chunks-received", self.chunks > 0, f"chunks={self.chunks}")
        self.check("player-loaded", loaded, f"ticks_sent={ticks}")
        self.check("stayed-connected", self.disconnected is None,
                   f"stay={a.stay}s system_msgs={len(self.system_msgs)}")
        if self.system_msgs:
            print("  system messages sample:", " | ".join(self.system_msgs[:3])[:200], flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=30065)
    ap.add_argument("--name", default="KSAYATester")
    ap.add_argument("--chat", default="KSAYA cluster playtest: hello from k3s!")
    ap.add_argument("--stay", type=int, default=25, help="加入后停留秒数")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()
    t = PlayTest(args)
    try:
        rc = t.run()
    except Exception as e:
        print(f"FAIL  playtest-exception: {e}", flush=True)
        print("MC-PLAYTEST-SUMMARY total=0 pass=0 fail=1")
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()