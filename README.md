# IDC 设备协议仿真平台

[![ci](https://github.com/follow2015/ipip-simulator-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/follow2015/ipip-simulator-lab/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/follow2015/ipip-simulator-lab)](https://github.com/follow2015/ipip-simulator-lab/releases)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

用真实协议栈仿真一整个机房的网络设备：SNMP、SSH、Trap/INFORM、REST 与 Web 控制台
跑在统一的状态模型上，而不是返回写死的假数据。适合采集器联调、网管系统回归、
拓扑与告警演练，以及在没有真机的环境里复现设备行为。

## 能力

| 维度 | 覆盖 |
|---|---|
| 协议 | SNMP v2c / v3（USM authPriv、VACM、context）、SSH CLI、UDP Trap/INFORM、REST 与 Web 事件 |
| 设备 | 交换机、路由器、服务器与主机，10 个厂商 profile；Cisco IOS、Huawei VRP、H3C Comware、Junos、FortiOS 命令语法 |
| 状态模型 | 配置事务与 revision、running/startup/candidate、LLDP/ARP/FDB、VLAN/LAG/静态路由/逻辑防火墙、时间积分计数器、传感器与告警 |
| 规模 | 单机 100 台 / 48 口拓扑；Linux 下每台设备独立 namespace、IP、SNMP engine 与 host key |
| 可靠性 | Worker 崩溃恢复、条件快照同步、SQLite 持久化、就绪文件与运行指标、幂等操作 |
| 管理 | Web 控制台（拓扑、配置、诊断、告警）、REST API、实验导入导出、场景与模拟 ping |

仿真的是设备行为而不是真机固件：品牌模板区分「模拟行为」与「真实采集器兼容性」，
后者需要在目标环境验收。

## 安装

要求 Python 3.11+。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
```

也可以从 Releases 下载 wheel 安装：

```sh
python3 -m pip install idc_simulator-1.0.0-py3-none-any.whl
```

安装后入口命令是 `idc-sim`，定义见 [pyproject.toml](pyproject.toml)。

## 快速开始

```sh
.venv/bin/idc-sim validate examples/local.yaml
.venv/bin/idc-sim serve --topology examples/local.yaml
```

打开 `http://127.0.0.1:8080/` 进入 Web 控制台。默认监听：

| 服务 | 地址 |
|---|---|
| REST / Web | `127.0.0.1:8080` |
| SNMP | `127.0.0.1:1161` |
| SSH | `127.0.0.1:8022` |

本地实验默认凭据（仅用于本机）：SNMP community `sim-read` / `sim-write`，v3 用户
`sim-user`（认证 `sim-auth-password`、加密 `sim-priv-password`），SSH `sim` / `sim-password`。

```sh
curl http://127.0.0.1:8080/v1/status
ssh -p 8022 sim@127.0.0.1
snmpget -v2c -c sim-read 127.0.0.1:1161 1.3.6.1.2.1.1.5.0
```

## 自定义拓扑

```sh
.venv/bin/idc-sim generate --devices 100 --ports 48 --layout tree --output topology.yaml
.venv/bin/idc-sim validate topology.yaml
.venv/bin/idc-sim network-plan topology.yaml
.venv/bin/idc-sim serve --topology topology.yaml
```

Web 管理界面也支持按规模生成拓扑，并用拖拽画布调整设备布局和链路；详见Web 管理说明。

[examples](examples/) 下有 7 份可直接运行的拓扑示例。Linux 上用 `--mode namespace`
让每台设备获得独立 IP 与低端口，详见[部署与运行指南](docs/deployment-guide.md)
和 [systemd 单元示例](deploy/idc-simulator.service)。

## 认证与远程访问

控制面默认只监听 `127.0.0.1`。绑定到非回环地址时必须设置 `IDC_API_TOKEN`，否则进程
拒绝启动；启用后 API 请求需带 `Authorization: Bearer <token>`。通过域名或反向代理访问时
用 `IDC_ALLOWED_HOSTS` 显式列出 Host。远程部署请用环境变量替换所有示例口令，清单见
[deploy/environment.example](deploy/environment.example)。

## 开发与校验

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check idc_simulator tests tools
node --check idc_simulator/web/app.js
```

## 文档与许可

- [部署与运行指南](docs/deployment-guide.md)：认证、namespace 部署、环境变量、REST 速查
- [示例拓扑](examples/local.yaml)、[依赖锁定](requirements.lock)
- Apache-2.0，见 [LICENSE](LICENSE)
