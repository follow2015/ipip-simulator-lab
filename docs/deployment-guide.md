# 部署与运行指南

本文是公开仓的精简版运行手册，只覆盖安装、启动、认证、Linux namespace 部署与常用 API。
架构细节、内部实现说明、评审与验收台账不随公开仓发布。

## 1. 安装

要求 Python 3.11+。依赖锁定在 [requirements.lock](../requirements.lock)，主要依赖
PySNMP、AsyncSSH、PyYAML、aiohttp。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/idc-sim validate examples/local.yaml
```

也可以从 Releases 下载 wheel 安装：`python -m pip install idc_simulator-<version>-py3-none-any.whl`。
安装后的入口命令是 `idc-sim`，定义见 [pyproject.toml](../pyproject.toml)。

## 2. 本地启动

```sh
.venv/bin/idc-sim serve --topology examples/local.yaml
```

浏览器管理界面在 `http://127.0.0.1:8080/`；默认端口为 API `8080`、SNMP `1161`、SSH `8022`。
示例凭据仅用于本地实验：SNMP community `sim-read` / `sim-write`，v3 用户 `sim-user`，
认证与加密口令 `sim-auth-password` / `sim-priv-password`；SSH 用户与口令 `sim` / `sim-password`。

```sh
ssh -p 8022 sim@127.0.0.1
snmpget -v2c -c sim-read 127.0.0.1:1161 1.3.6.1.2.1.1.5.0
```

Net-SNMP 与 SSH 客户端是可选联调工具，不随 Python 依赖安装。进程状态默认写在 `.sim/`；
同一状态目录不能同时启动两个控制进程。只需要 Web/API 与拓扑模型、不启动协议 Worker 时，
加 `--model-only`。

## 3. 认证与暴露面

- 控制面默认只监听 `127.0.0.1`。用 `--bind` 绑定到非回环地址时**必须**设置
  `IDC_API_TOKEN`，否则进程拒绝启动；隔离实验可显式加 `--allow-unauthenticated`。
- 启用 token 后，除静态页面外的 API 请求需带 `Authorization: Bearer <token>`：

  ```sh
  curl -H "Authorization: Bearer $IDC_API_TOKEN" http://127.0.0.1:8080/v1/status
  ```

- 通过域名或反向代理访问时，用 `IDC_ALLOWED_HOSTS` 显式列出 Host（逗号分隔、不含端口），
  默认只允许 localhost 与回环地址；`0.0.0.0` 这类通配绑定不会自动进入白名单。
- HTTPS 在反向代理终止时，代理须保留浏览器的原始 `Host`，并设置
  `IDC_ALLOWED_ORIGINS=https://sim.example`（非默认端口需带端口）。可用逗号配置多个
  明确的外部 Origin，不带路径或结尾 `/`；服务不会自动信任 `X-Forwarded-Proto`。
  默认仍精确匹配后端同源地址，Host 白名单和 Bearer 认证继续生效。
- 远程部署必须通过环境变量替换所有示例口令，变量清单见
  [deploy/environment.example](../deploy/environment.example)。

## 4. Linux namespace 部署

每台设备有独立 namespace/IP/SNMP engine/host key；Worker 绑定 UDP 161 与 TCP 22 后
降到 `--worker-uid` 指定的账户。控制进程需要创建 namespace/veth 与绑定低端口的权限。

```sh
idc-sim generate --devices 100 --ports 48 --output topology.yaml
idc-sim validate topology.yaml
idc-sim network-plan topology.yaml        # 只打印命令，不创建资源
sudo .venv/bin/idc-sim serve --mode namespace --topology topology.yaml \
  --state-dir /var/lib/idc-simulator --worker-uid 991
```

systemd 单元与环境变量文件示例见
[deploy/idc-simulator.service](../deploy/idc-simulator.service) 与
[deploy/environment.example](../deploy/environment.example)，按安装目录和 UID 调整。
部署节点需由管理员开启 IPv4 转发，并把仿真网段的路由指向该节点；本平台不改上游路由、
云安全组或已有防火墙，也不要用 NAT 改写 Trap 源地址。

namespace 默认沿用旧版 5 位后缀；同一实验发生名称碰撞时使用 8 位后缀，仍有碰撞则拒绝部署。
`network-owner.json` 记录命名版本与设备列表，停止和恢复清理按该记录执行；旧文件未记录版本
时视为版本 1。Worker RPC socket 位于状态目录的 `sock/`，Unix 路径过长时使用 `/tmp` 下
随机创建的专属目录。指定 `--worker-uid` 时目录为 0711，socket 为 0600 并归 Worker UID。

## 5. 常用 REST 接口

| 方法与路径 | 用途 |
|---|---|
| GET `/v1/status` | 模式、版本、认证与保留期配置 |
| GET `/v1/labs` | 实验列表、状态、revision、设备 |
| POST `/v1/labs` | 用 JSON 拓扑创建实验 |
| POST `/v1/labs/import` | 用 YAML/JSON 文本导入实验 |
| GET `/v1/labs/{id}/topology` | 设备、链路、端口/MAC、oper、LLDP 状态 |
| GET `/v1/labs/{id}/config` | 可重新导入的当前拓扑 |
| GET `/v1/labs/{id}/locate` | 按 ip/mac/device+port 定位，返回 ARP/FDB 证据 |
| POST `/v1/labs/{id}/ping` | 模拟双向可达性与路径 |
| POST `/v1/labs/{id}/start`、`stop` | 幂等启停，等待两个 Worker 监听器就绪 |
| GET `/v1/events`、`/v1/operations/{operation_id}` | 事件与操作记录 |

### 超时与重试

状态操作在单线程执行器内串行运行，事件循环继续响应状态探测。503 响应含 `outcome` 和
`Retry-After`：`not_executed` 表示本次调用未入队、排队时已取消，或控制通道连接阶段就
失败，可以确认未执行；`unknown` 表示已开始执行或多步骤操作仍在进行，也包括请求已经
写出但连接中断、回包丢失或无法解析的情况，不能把 503 当作回滚，也不能保证线程会被终止。

大型实验（数十台设备）的启停耗时明显长于单次状态操作。`start`/`stop` 的 HTTP 等待上限
由 `IDC_LIFECYCLE_TIMEOUT`（默认 300 秒）控制，与 `IDC_STATE_TIMEOUT` 解耦；超时响应仍为
`unknown`，后台任务会继续持有实验锁直到结束。

设备 PATCH、save/reboot/transactions 以及场景启动在执行前生成 `operation_id`，即使没有
`Idempotency-Key`，超时响应也返回该 ID。用 `GET /v1/operations/{operation_id}` 查询结果；
进行中的设备事务尚未写入记录时可能暂时返回 404，404 不代表该事务不会完成。事务失败或
操作记录过期后也可能不存在该记录，因此查询并不保证最终一定得到 `complete`。
重试设备事务时使用同一 ID 作为 `Idempotency-Key`，并保持请求内容与原始 `If-Match` 不变；
已提交的请求返回缓存结果，同键不同内容返回 409。客户端主动断开、尚未收到 ID 时，应使用
客户端预先生成的幂等键避免盲目重试。

删除、配置替换超时后由后台任务继续持有实验锁与门闩，直到实际结束再释放。用
`GET /v1/labs` 与 `GET /v1/labs/{id}/config` 确认当前状态；它们不能证明某次历史调用的结果。
其他多步骤写操作（如启动、停止）也可能在超时前已有部分副作用，应查询状态后再决定重试。

## 6. 环境变量速查

| 变量 | 默认值 | 用途 |
|---|---|---|
| `IDC_API_TOKEN` | 空 | 控制 API 的 Bearer token；非回环绑定必填 |
| `IDC_ALLOWED_HOSTS` | 空 | 额外允许的 Host，逗号分隔 |
| `IDC_ALLOWED_ORIGINS` | 空 | 反代外部 Origin 精确白名单，含 scheme 与非默认端口 |
| `IDC_SNMP_COMMUNITY`、`IDC_SNMP_WRITE_COMMUNITY` | `sim-read`、`sim-write` | v2c community |
| `IDC_SNMP_V3_USER`、`IDC_SNMP_V3_AUTH`、`IDC_SNMP_V3_PRIV` | `sim-user` 与示例口令 | v3 authPriv |
| `IDC_SSH_USER`、`IDC_SSH_PASSWORD` | `sim`、`sim-password` | SSH 凭据 |
| `IDC_NOTIFICATION_TARGETS` | `[]` | Trap/INFORM 目标（JSON） |
| `IDC_CREDENTIAL_SETS` | `{}` | 按设备族的凭据集合（JSON） |
| `IDC_STATE_TIMEOUT` | `30` | 单次状态操作执行超时（秒） |
| `IDC_LIFECYCLE_TIMEOUT` | `300` | `start`/`stop`/删除/替换的 HTTP 等待上限（秒），超时后后台继续 |
| `IDC_SHUTDOWN_TIMEOUT` | `max(2×IDC_STATE_TIMEOUT, 60)` | 关闭总预算（秒）；超时记录 `shutdown_timeout_total` 后继续收尾 |
| `IDC_SIGNATURE_MODE` | `incremental` | `incremental` 增量签名、`full` 全量签名、`verify` 对照校验 |
| `IDC_EVENT_RETENTION_ROWS` | `100000` | 事件表保留行数 |
| `IDC_OPERATION_RETENTION_HOURS` | `24` | 操作记录保留小时 |
| `IDC_CHECKPOINT_SECONDS` | `30` | 计数器落盘周期 |

## 7. 排障与边界

- 条件同步异常时可设 `IDC_SIGNATURE_MODE=full` 并重启；`verify` 适合测试，漏标变化会
  抛错并累加实验状态内的 `_signature_verification_failures`。正常运行使用 `incremental`。
- Worker 指标仅接受有界的 `idc_` 数值样本，控制端附加 device/role；无效行丢弃并按
  `device` / `role` 标签累计 `idc_worker_metrics_invalid_lines_total`，单个文件上限
  1 MiB、单行上限 8192 字符。
- Worker 未就绪时查看状态目录下的 `ready.json` 与 `query.log`；`GET /v1/status` 提供
  worker 指标年龄，可识别崩溃后未刷新的数据。
- 100 台以上规模、24 小时长跑、Windows 宿主的 namespace 行为未验收；Windows 没有
  asyncio Unix socket，只有 `--model-only` 的 Web/API 可用，SNMP/SSH Worker 需 Linux。
  单机短时数据不是吞吐承诺。
- 本平台是仿真器，示例口令与 community 必须替换，不应未做隔离就接入生产网络。
