# KSAYA collector 部署说明（公网机接收端点）

`ksaya-collector.py` 是 KSAYA 集群「固定向公网机发信息并被接收」的落点，部署在
公网 ECS `182.92.237.205`（或任意后期更换的公网机）上，纯 Python 标准库，零依赖。

## 1. 文件

| 文件 | 作用 |
|---|---|
| `ksaya-collector.py` | 接收端点主程序（HTTP server + 线程池） |
| `ksaya-collector.service` | systemd 常驻单元（开机自启 + 崩溃重启） |

## 2. 部署步骤

```bash
# 1) 上传主程序
scp ksaya-collector.py root@<公网IP>:/root/ksaya-collector.py

# 2) 安装 systemd 服务
scp ksaya-collector.service root@<公网IP>:/etc/systemd/system/ksaya-collector.service
ssh root@<公网IP> '
  systemctl daemon-reload
  systemctl enable --now ksaya-collector.service
  systemctl status ksaya-collector.service --no-pager
'

# 3) 验证
ssh root@<公网IP> 'curl -s http://127.0.0.1/healthz'
# => {"ok": true, "ts": "..."}
```

## 3. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针 |
| GET | `/nodes` | 所有节点最新状态（JSON） |
| GET | `/nodes/<node_id>` | 单节点最新状态 |
| POST | `/report` | 心跳上报入口（`application/json`，需 `node_id` 字段） |

心跳 JSON 字段（节点侧 `ksaya-report.sh` 自动生成）：
`node_id hostname ip profile k3s_role k3s_active uptime load ts`

## 4. 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KSAYA_COLLECTOR_PORT` | `80` | 监听端口（标准 HTTP，需 root 绑定特权端口） |
| `KSAYA_COLLECTOR_HOST` | `0.0.0.0` | 绑定地址 |
| `KSAYA_COLLECTOR_DIR` | `/var/lib/ksaya-collector` | 数据目录 |

## 5. 数据落盘

```
/var/lib/ksaya-collector/
├── nodes/<node_id>.json     # 最新状态
└── history/<node_id>.jsonl  # 追加历史
```

每条落盘会额外打上公网机的接收时间 `received_at` / `received_unixts`，
与节点自报的 `ts` 区分，用于后续判断「上报是否及时」。

## 6. ⚠️ 关键前置

阿里云安全组**默认只放行 22**。要让新机器开机即直连上报，必须在控制台给
安全组放行 TCP `80`。在放行之前，只能通过 SSH 隧道（复用 22）验证链路，不能直连。

## 7. 变更公网机地址

节点侧上报地址集中在 `services.ksaya.report.url`（NixOS）或
`/etc/ksaya/report.conf` 的 `REPORT_URL` 一个字段；换公网机只改这一处即可，
node-id 与周期上报逻辑完全不变。