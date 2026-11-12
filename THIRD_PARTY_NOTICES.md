# 第三方组件与模板来源

依赖版本锁定于 `requirements.lock`。Python wheel 不捆绑这些库的源代码，安装依赖时各分发包携带自己的 LICENSE；再分发完整环境时应保留这些文本。

| 组件 | 本次版本 | 上游声明 |
|---|---|---|
| PySNMP | 7.1.30 | BSD；以分发包 LICENSE.rst 为准 |
| AsyncSSH | 2.23.0 | EPL-2.0 OR GPL-2.0-or-later |
| aiohttp | 3.13.2 | Apache-2.0 AND MIT |
| PyYAML | 6.0.3 | MIT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| pyasn1 | 0.6.5 | BSD-2-Clause |

来源链接：[PySNMP](https://github.com/lextudio/pysnmp)、[AsyncSSH](https://github.com/ronf/asyncssh)、[aiohttp](https://github.com/aio-libs/aiohttp)、[PyYAML](https://github.com/yaml/pyyaml)、[cryptography](https://github.com/pyca/cryptography)、[pyasn1](https://github.com/pyasn1/pyasn1)。测试、构建及其他传递依赖的许可证见安装分发元数据。

Cisco C9300、Huawei S5735、H3C S5560X 的身份参考来自本仓历史脱敏 `scripts/lab/sim/gen_profiles.py`。它们不提供真实设备 NOS 输出授权或完整私有 MIB。其他 profile 使用公开企业根与 demo 型号，其 metadata.yaml 明列资料来源；没有厂商专有 OID 的虚构采集表。

历史 `scripts/lab/` 中的 snmpsim/paramiko 参考实现不被新运行时调用。本文记录第三方声明，不对本项目新增代码替用户选择开源许可。
