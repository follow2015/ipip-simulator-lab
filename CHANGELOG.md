# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号按
[语义化版本](https://semver.org/lang/zh-CN/)。当前处于 `1.x`：`1.MINOR` 提升表示
**有破坏性变更**（含状态库 schema 迁移），`1.0.PATCH` 只做向后兼容修复。

发布流程：改 `idc_simulator/__init__.py::__version__` → 在此文件加一节 → 提交 →
`git tag v<version>` → 推送 tag，由 `.github/workflows/release.yml` 构建并发布。
tag 与 `__version__` 不一致会在构建前直接失败（`scripts/check_release_tag.py`）。

## [Unreleased]

第六轮评审（v1.0.5 复核）修复（向后兼容，状态库 schema 未变）。

### 修复

- **E1（编辑器并行链路重合）**：同一对设备之间有多条链路时，线与标签原先完全重合，
  后加的链路会挡住先加的，鼠标点不到下面那条。现在按序号沿连线法线等距错开
  （相邻 28 个 viewBox 单位，两条时为 ±14），标签同步偏移；每条边增加 14 单位宽的
  透明命中区，2 px 细线也能直接点选。
- **测试盲点（v6 §9-2）**：`_snapshot` 明确不过滤下划线开头的键，并新增正/反两条回归——
  篡改 `_state_signatures` / `_state_sequences` 必须能被比较发现；故意让草稿与线上共享
  这两个容器（v1.0.4 语义）时，preview 污染必须被差分看见。

### 测试

- `tools/web_topology_editor_smoke.cjs` 改为按 `data-editor-link` 定位具体边，不再用
  `.first()`（字体/排版环境敏感）；新增并行链路偏移、标签盒不重叠、两条边均可点选的断言。

### 发布面

- **公开仓改为「去注释」交付态（档 B）**：`scripts/sync_to_deploy.py` 同步 `.py` / `.js` /
  `.cjs` 时按状态式判据 `deploy ≡ strip(主仓 HEAD)` 删除全部注释与函数·类 docstring
  （模块 docstring 只留首行），空 suite 补 `pass`。实现参考 ipip 的
  `scripts/strip-comments.py`，落在本仓 `scripts/strip_comments.py`。
- **保留功能性指令**：shebang / coding、`# noqa` / `# type: ignore` / `# pragma` 等，
  以及本仓特有的 `# scan-secrets: fixtures-allowed`（测试夹具假密钥豁免标记，
  删掉会让公开仓 CI 扫密误报）。
- **刻意不处理** `.md` / `.yaml` / `.yml` / `.json` / `.html` / `.css` / `.xml` /
  `.sh` / `.sql` / `.service` / `.example` —— 要么注释即交付文档，要么无解析器可安全
  区分字符串里的 `#` / `/*`。
- 新增 `tests/release/test_strip_comments.py`（35 项）：剥离行为、指令保留、幂等、
  真实仓 `.py` 剥离后仍可 `ast.parse`、端到端同步到临时仓验证发布面。
- **公开仓提交/标签文案收紧**：`sync_to_deploy.py` 默认提交文案改为从 HEAD 快照的
  `__version__` 派生 `release: <版本> 同步公开发布面`，不再继承开发仓 subject
  （历史里曾带出内部评审轮次、缺陷编号与内部工具名）。`--message` 可给人工撰写的
  中性说明，最终文案逐字落盘、不再自动补 `release:` 前缀。两种文案都在**任何写盘之前**
  过 `scan-secrets.py --stdin` 消息口径：严扫裸口令/家目录/公网 IP/token，并拉黑内部
  文档路径、评审轮次、缺陷编号、内部工具与自建服务地址、企业邮箱。失败时部署仓零改动。
  `.github/workflows/release.yml` 在发版前按同一口径校验 tag 注释（取不到注释直接失败）。
  新增 `tests/release/test_public_message.py`（20 项）覆盖上述行为。
  ⚠️ 已 push 的公开仓历史与旧 tag 注释不可改写；新策略只对后续提交生效。

## [1.0.5] - 2026-11-11

第四/五轮评审（v1.0.4 与 r2 修复批次）缺陷修复（向后兼容，状态库 schema 未变）。

### 修复

- **正确性（N3 写时复制污染）**：`_transaction_draft` 此前把 `_state_signatures` /
  `_state_sequences` 与线上状态共享，`publish_state` 原地写会污染线上签名与序列号
  （preview 与失败事务同样触发），导致条件快照 `If-None-Match` 误判、增量签名漏标脏。
  现草稿私有化这两个容器，并用 `draft_runtime_row()` 结构化按设备复制 runtime。
  第四轮评审「0 次线上污染」的结论不成立：其差分两侧共享同一对象，比较不出差异。
- **R1（生成子网撞名）**：`/v1/templates/generated` 增加短时预留
  （`_generated_reservations`，TTL 600 s、同名幂等），修掉「先生成两份配置再依次创建」
  仍撞同一 `/24` 的问题。
- **IMP-2（host id 无格式校验）**：`normalize_lab` 对 host id 补
  `[A-Za-z0-9_-]{1,48}` 校验，与 device id 同口径。
- **host MAC 派生**：改 `generated_host_mac()` 用完整 sha256 派生，去掉 `:04x`
  的 65535 台隐式上限。
- **OPT-1 / OPT-2（编辑器）**：重开创建对话框刷新规模警告与确认态；「重置布局」清除
  选中链路描边；两处各补浏览器 smoke 断言。

### 性能

- 100 台规模接口事务中位数 **89.0 ms → 44.9 ms（−50%）**（A-B-B-A 交替两轮，
  对照整库 `deepcopy` 基线），低于第四轮评审建议的 150 ms 目标。

### 测试

- 新增 `tests/simulator/test_review_v4_v5_fixes.py`（16 项）：子网预留、host id 校验、
  MAC 派生、preview/失败事务不污染线上状态、按设备复制与整库深拷贝等价性。
- 固化随机差分：固定种子 150 事务 × chain/tree/ring = **450 次**，对整库深拷贝参照
  **0 状态分歧、0 线上污染**。
- 公开仓白名单决策（编辑器 smoke 随包发布）用 release 回归测试钉死。
- 默认与 `IDC_SIGNATURE_MODE=verify` 两种模式均 **362 passed / 16 skipped**；
  ruff / compileall / `node --check` / 扫密全绿。

### 明确推迟

- **I-1（大规模创建独占控制面）**：按既有决策仅保留 Web 开销警告 + 二次确认，
  不引入异步创建 API（会改变 REST 契约）。本机实测 240×512：`normalize_lab` 558 ms、
  `Controller.create` 合计 2510 ms。

## [1.0.4] - 2026-11-10

第三轮评审（v1.0.3 整体）缺陷修复（向后兼容，状态库 schema 未变）。

### 修复

- **F1（计数器正确性）**：`counter_reset` 一并清零 `in_errors` / `out_errors` /
  `in_discards` / `out_discards`（含 `_remainder`）。此前"清计数器"后错误包与丢包
  计数不归零，会误导采集器的计数器回绕与清零逻辑。
- **F2（告警噪声）**：后台生命周期任务的 `SimulationError`（404/409/400）只记 WARNING，
  不再记 ERROR 堆栈、不再累计 `lifecycle_task_errors_total`；拼错实验名的客户端不再误触发告警。
- **F3/O-1（生成子网撞名）**：`GET /v1/templates/generated` 的管理子网不再按实验名哈希
  撞桶，改为 `available_generated_subnet()` 扫描现有 labs 选空闲 `/24` 并线性探测；
  设备 ID 前缀由 6 位扩到 10 位 hex，降低跨实验重名。
- **N3（事务写放大）**：事务起草改为按设备写时复制（`_transaction_draft`）——目标设备
  深拷贝、对端仅拷接口/`l3_interfaces` 操作字段，仅 `lag/l3/link/host/flow/vlan/route/
  firewall/cross_connect` 等拓扑操作才整库深拷贝 `lab`，替代整库 `deepcopy`（评测显示
  它约占事务耗时的一半）。
- 设备 ID 前缀 6→10 hex 字符，降低跨实验重名导致的排障混淆。

### 测试

- CI 新增 `signature-verify` 门禁：`IDC_SIGNATURE_MODE=verify` 下全量跑，守住增量签名与
  完整重算一致性（增量签名"漏标脏"风险的唯一防线）。
- 全仓回归全绿；`ruff` / `compileall` / 扫密 / 版本校验全过。

## [1.0.3] - 2026-11-10

Web 拓扑编辑器与动态拓扑生成（向后兼容，状态库 schema 未变）。

### 新增

- Web 控制台新增**拓扑编辑器**：在界面上生成与调整实验拓扑；配套 `tools/web_topology_editor_smoke.cjs`（开发仓内部冒烟，不随公开仓分发）。
- 新增 REST 接口 `GET /v1/templates/generated`：按 `devices` / `ports` / `layout` / `name` 查询参数即时生成拓扑；按 `name` 哈希派生独立管理子网（地址空间全局唯一），使多份自动生成拓扑可在同一 controller 内共存而不冲突。参数越界（非整数、`name` 超 48 字符或含非法字符）返回明确错误。
- 文档同步：README 与 `docs/web-management.md`、`docs/common-topologies.md` 补充编辑器与生成接口用法。

### 新增

- 常见告警可注入并真正发出通知：风扇停转/电源丢失（`sensor` 支持 `status`、`low_threshold`
  与 truthvalue 语义）、温度越限（原有语义不变）、CPU/内存越限（新增 `resources` 操作与
  设备 `thresholds`）、错误包与丢包（新增 `errors` 操作，速率按墙钟积分成
  `ifInErrors`/`ifInDiscards`/`ifOutErrors`/`ifOutDiscards`）。CPU/内存/错误包用标准
  RMON `risingAlarm`/`fallingAlarm`（RFC 2819）承载，`alarmVariable` 指向 profile 已声明
  的私有资源 OID 或 HOST-RESOURCES；板卡/电源插拔用 ENTITY-MIB `entConfigChange` 并携带
  实体身份。此前 fan/power/cpu/memory/errors 要么无法注入，要么事件被丢弃（无 OID 映射）。
  压力注入接口与 Web 表单同步放行新类型，并要求对应 payload 字段。
- SNMP 与 CLI 透出真实错误/丢包计数：ifTable 13/14/19/20 原为硬编码 0，`show interfaces`
  与 `show interfaces counters` 的 errors 列同样是写死的 0。现按注入速率积分输出。
- `examples/idc-64gpu.yaml` 补齐物理实体：16 台交换机各带温度/风扇/电源在位 5 个传感器、
  线卡与双电源 3 个 ENTITY 实体，82 台设备声明 `resources` 与 `thresholds`。此前该拓扑
  既无传感器也无板卡，风扇停转、电源丢失、CPU/内存越限在这些设备上无法注入，只能换用
  `local.yaml` 或 `idc-100` 复现。现在六类常见告警都能在这套 82 台拓扑上直接测。
- 环境告警在未声明私有 OID 的模板（`generic/*`）上退回标准 RMON 承载：`alarmVariable`
  指向 `entPhySensorValue`，方向按值的实际走向选 `risingAlarm` 或 `fallingAlarm`。
  此前这类事件会被静默丢弃，风扇停转虽写入了事件却没有 trap 发出。声明了私有
  `sensorAlarm` 的厂商不受影响，仍走原厂 OID。
- 注入回报的 `unmapped_devices` 收窄为真正发不出去的设备：传感器/实体缺失分别判定，
  不再把所有 `generic/*` 设备一律标为不可发。

### 修复

- 压力注入的 `interfaceErrors`/`interfaceErrorsClear` 只给 `ifindex` 时事件写入成功却不发
  trap（`unmapped_devices` 仍为空）：RMON 分支原先要求 payload 里同时有 `port`。现在按
  `ifIndex` 直接发 `ifInErrors`，缺计数时默认 1 包/秒；设备上不存在的 `ifIndex` 会进
  `unmapped_devices`，不再静默丢弃。
- `examples/local.yaml` 的风扇补上高/低速阈值（9000/8000、1000/3000）：原先没有阈值，
  转速掉到 0 只能靠 oper status 触发告警，测不到「转速过低但状态仍 ok」的真实场景。
- CLI 补上端口流量读数：`show interfaces counters`、`show interfaces <接口>`（IOS/NX-OS/EOS）、
  `display interface [<接口>]`、`display interface counters`（VRP/Comware）、
  `show interfaces statistics [<接口>]`（Junos）。此前计数器只在 SNMP 与 REST 暴露，
  `show interfaces counters` 返回 `Unsupported command`，`display interface` 看不到
  input/output rate，`display interface brief` 的 InUti/OutUti 恒为 `0%`。现按
  `runtime.counters` 输出累计字节/包数、速率与利用率，与 SNMP 同源。

### 测试

- 全仓回归全绿；`test_web.py` 新增生成接口回归；ruff / compileall / node --check / 扫密 / 版本校验全过。

## [1.0.2] - 2026-11-10

CI 稳定性修复（纯测试层面，运行期与状态库 schema 无变更，向后兼容）。

### 修复

- `test_linear_host_traversals_and_time_refresh` 的两项性能断言在 CI（共享 runner）上
  偶发红：原守卫用绝对墙钟上限（size≤100 → 0.2s、size=200 → 0.6s），与宿主机 CPU
  速度强相关。改为**与机型无关的近线性伸缩守卫**——以最小规模（25 台）在同主机实测
  中位数为基准，断言 `median(size) < median(25) × (size/25) × 4`：CPU 速度在比值中
  抵消，只拦 O(n²) 级回退并吸收测量噪声。本地 4 档（25/50/100/200）全绿，同一基准下
  未引入 `derive()` 回退。

### 测试

- 全仓回归全绿；`ruff` / `compileall` / `node --check` / 扫密 / 版本校验全过。
- 该批次属纯测试修正，运行期代码与公开边界无变化。

## [1.0.1] - 2026-11-10

向后兼容的修复与优化批次（状态库 schema 未变）。

### 修复与优化

- Windows 上 `idc-sim serve` 启动即崩（`NotImplementedError`）：Proactor 事件循环没有
  `add_signal_handler`，停机信号注册改为先试事件循环、失败退回 `signal.signal`；两条路径
  都不可用时不再抛异常，退化为 `KeyboardInterrupt` 退出。此前 `--model-only` 在 Windows
  完全不可用，浏览器验收只能在 Linux 上跑。
- Web 控制台侧栏版本号改为读取 `/v1/status`，不再写死 `v0.1.0`：发布 1.0.0 后页面仍
  显示旧版本，用户无法从界面判断实际运行版本。
- `show ip route` / `display ip routing-table` 的接口名按厂商拼写输出：IOS 设备此前
  打印 `Vlanif100`（华为拼写），现为 `Vlan100`；H3C 为 `Vlan-interface`、Junos 为
  `irb`、FortiOS 为 `port1`。静态路由的出接口改为按下一跳所属网段解析，不再固定写
  `Vlanif1`。
- 按 2026-11-10 独立评审修复 5 项重要级问题：`RpcClient` 区分连接失败
  （`not_executed`）与请求已写出后的传输失败（`unknown` 并回传操作 ID）；关闭路径给出
  总预算（`IDC_SHUTDOWN_TIMEOUT`），lifecycle 任务、supervisor 停止与部署命令均有上界，
  超时记 `shutdown_timeout_total` 后继续收尾，避免 systemd SIGKILL 丢最后一次 checkpoint；
  `start`/`stop` 的 HTTP 等待改用 `IDC_LIFECYCLE_TIMEOUT`（默认 300 秒）与状态超时解耦；
  后台 lifecycle 失败写日志并累计 `lifecycle_task_errors_total`；浏览器 XSS 断言改为从
  拓扑自适应选择设备与端口，删除 `IDC_WEB_FUNCTIONAL_ONLY` 跳过开关。
- 优化级：公开同步白名单不再整目录同步 `tools/`，仅发布 `tools/web_smoke.cjs`；
  `environment.example` 注释掉重复赋值的 `IDC_SIGNATURE_MODE`；Origin 比较规范化
  scheme/host 大小写与默认端口；损坏的 `network-owner.json` 返回 409 并保留启动原始
  异常；`fcntl` / `resource` 缺失时降级，Windows 仅 `--model-only` 可用；socket 目录
  不再重复创建；`worker_metrics_invalid_lines_total` 增加 device/role 标签；表行比较
  不再把顺序变化误判为脏。
- HTTPS 反代可通过 `IDC_ALLOWED_ORIGINS` 配置精确外部来源，代理须保留 Host。
- 删除/替换超时后保持后台生命周期任务，完成后释放门闩；排队中的状态调用超时可取消，
  503 明确区分 `not_executed` / `unknown`，设备事务和场景启动提前分配操作 ID。
- 增量状态签名比较派生结果并仅计算变化设备，支持 `full` / `verify` 模式。
- namespace 碰撞时升级命名版本，清理依据 owner 记录；Worker socket 使用专属目录，
  指标逐行校验；场景配置读取移入状态执行器。
- 规模基准改用按设备绑定的 RPC 通道并验证负载进程 PID，增加并发写入和事务中位数报告。
- 区分函数自身 TimeoutError 与状态等待超时；执行器容量随实际完成释放，关闭落盘等待
  实际完成；重复启动保留同一目录锁，配置替换先读取完整 HTTP 输入再进入后台任务。

### 验证

- 修复后全仓 **307 passed / 16 skipped**（新增 15 项回归）。本机 Linux（WSL）另有 2 项
  `test_linear_host_traversals_and_time_refresh` 超出时间阈值，`HEAD` 同样失败，属宿主机
  性能差异；同一基准下 `derive()` 中位数 0.367s（修复前 0.366s），未引入回退。
  **这两项目 CI 红已在 1.0.2 改为与机型无关的线性伸缩守卫后解决。**
- 浏览器 18 项未在本轮执行（本机缺 Playwright）；XSS 断言已改为自适应且不可跳过，
  仍需在具备 Chromium 的环境补一次运行记录。
- `ruff` / `compileall` / `node --check` 全过。

### 计划中

- Linux network namespace 下 82/100 台规模验收、24 小时长跑、Chromium 全量复跑：
  本轮未执行，不能以模型短时负载替代。

## [1.0.0] - 2026-11-10

公开发布形态重整：公开仓回收重建、文档边界收紧、README 以能力与安装使用为主。
包本身无破坏性变更，状态库 schema 未变；版本线从 0.1.x 归一到 1.0.0。

### 变更

- 公开仓只发布一份独立撰写的精简[部署与运行指南](docs/deployment-guide.md)：
  完整实现手册、评审报告、验收台账、机器证据（JSON/PNG/XML/TXT）、设计与规划
  全部留在开发仓。同步时公开版 Markdown 的内部链接自动降级或重定向，不留死链。
- README 重写为能力清单与安装/使用入口，去掉内部设计引用与内部文档链接。
- 新增 3 项发布链路回归（最小 docs 白名单、README 重定向与死链裁剪、
  相对链接解析），`tests/release` 共 9 项。

### 测试

- 全仓 **273 passed / 16 skipped（289 项）**；ruff、compileall、node、
  `check_release_tag --tag v1.0.0`、全仓扫密、证据脱敏全绿。

## [0.1.3] - 2026-11-10

设备信息面补齐与发布链路收尾。无破坏性变更，状态库 schema 未变；公开仓同步
白名单未扩大，离线校验工具与内部评估文档仍只在开发仓。

### 新增

- **私有 OID 缺口补齐**：按 IPIP 224 条 SNMP 模板反向核对，把 Cisco CPU/内存/
  温度状态/风扇/电源、华为风扇状态、H3C CPU/内存/风扇+电源共 9 条私有 OID 补齐，
  三家核心品牌模板缺口由 9 降为 0；标准侧补 `dot3StatsDuplexStatus`，覆盖 36/36。
  `oid.py` 私有分支新增 `sensor + attribute` 值策略，值来源缺失时跳过而非 emit 占位值。
- **设备信息与告警**：`notifications.event_varbinds` 为 sensorAlarm/sensorClear 生成
  私有 Trap 与标准 ENTITY-SENSOR-MIB varbinds；`cli.query` 补齐 IPIP 实际下发的查询
  命令并按厂商格式输出；示例拓扑补 `resources` 与 fan1/power1 传感器。
- **离线 MIB 声明校验器**（开发仓工具，不随公开仓分发）：`scripts/verify_mib_claims.py`
  只读核对 OID 声明、模板对齐、标准子树覆盖与可选 MIB 语法，模板快照随开发仓版本化。
  该批次完整评估见开发仓 `docs/mib-claim-verification.md`。

### 修复

- 重启后派生字段 `last_change` 缺失导致 SNMP 全量无应答。
- 示例拓扑的 contact 去掉 mDNS 主机名，避免部署仓扫密命中。
- 离线校验器两处 ruff 违规（F401 未用导入、F841 未用变量）；扫密器把 SMIv2
  internet 子树的四段根前缀误判为公网 IP —— 改为「同行存在更长 OID 链」才放行，
  单独写该值或真实公网 IP 仍然拦截。

### 测试

- 全仓 **270 passed / 16 skipped（286 项）**；`tests/simulator` 165 项、
  `tests/release` 6 项；新增 10 项设备增强回归与 2 项发布链路回归。
- ruff、compileall、node、扫密、证据脱敏、版本校验、wheel 校验全绿。

## [0.1.2] - 2026-11-09

状态同步与实验生命周期的架构评审落地，以及发布链路收尾。无破坏性变更，状态库
schema 未变。

### 新增

- **条件快照同步**：worker 刷新携带 `control_epoch` / `state_seq`，控制面区分"语义状态
  变化"与"计数自然增长"，条件未变时不再传输全量设备状态；旧的完整快照接口保留。
  百台条件模式 40 秒 18,500 次刷新、0 失败、p95 21.2 ms；同版本全量对照 15,286 次、
  p95 174.3 ms（响应 11.9 MB 对 1.14 GB）。GPU82 条件模式 15,908 次、p95 11.9 ms。
- **实验生命周期**：配置预览、停止后原子替换、按名称确认删除；Web 端支持整份 JSON
  编辑与预览，启动输入与持久化状态不一致时明确提示。
- **操作处理器注册表**：命令处理按设备族注册，新增 Junos/FortiOS 语法插件，
  L3 归一化下沉为共用实现。

### 变更

- 网络清理失败重试与数据库回滚加固；场景写入增加闸门，重建设备跳过历史通知。

### 文档

- 新增条件同步与生命周期说明，设计与实施细节见开发仓
  `docs/design/state-sync-lifecycle-design-2026-11-09.md`。
- 开发仓新增 Go 重写评估：对 GIL、协程内存、gosnmp 生态三条论据逐条实测复核，
  附 `scripts/probe_worker_capacity.py` 容量探针；结论为不迁移，并列出决策门槛。

### 测试

- 全仓 **258 passed / 16 skipped**（274 项）；新增 17 项同步与生命周期回归、
  18 项浏览器验收（Chromium，0 JavaScript 错误）。

## [0.1.1] - 2026-11-09

第二轮评审（`docs/review/project-review-2026-11-09-r2.md`）的 1 项阻断 + 3 项重要
问题修复，以及同一批工作区里的 H1/H2、M1–M4、L1–L8 架构重构。

### 新增

- **状态执行器超时**：`IDC_STATE_TIMEOUT`（默认 30 秒）限制单次状态调用执行时长；
  超时返回 503、累加 `state_call_timeouts_total`、日志记录函数名，并在一个超时窗口内
  只放行探针请求，避免队列名额被瞬间占满。新增 `state_call_seconds` 分位数。
- **证据脱敏工具** `scripts/sanitize_evidence.py`：清除 junitxml 里的 `hostname` 与
  家目录绝对路径，支持 `--check` 作为 CI 门禁；`sync_to_deploy.py` 落盘前再脱敏一次。
- **扫密新规则**：`HOME_PATH`（`/Users/<name>`、`/home/<name>`）、`MDNS_HOSTNAME`
  （`*.local`）、`LOCAL_HOSTNAME`（运行机主机名）；占位用户名、示例域名与代码里的
  `args.local` 形态放行。

### 变更

- **事务写放大回收**：普通配置事务只回写目标设备的配置行与 runtime 行，其他设备已
  结算的计数器交由 30 秒检查点落盘；仅拓扑级变更全量回写。82 台单次事务 WAL
  597 KB → 60 KB（约 10 倍）。
- **Host 白名单**：`IDC_ALLOWED_HOSTS` 逐项 `strip()`、忽略空项、按小写比较；
  通配绑定（`0.0.0.0` / `::`）不再自动加入白名单（它不是合法的 Host 名），
  启动日志打印生效白名单并在通配且未配置时告警。
- 状态读写/事务/检查点统一进入单线程执行器（唯一写入者），Web 事件循环不再被
  慢状态操作阻塞；Worker 使用按设备/角色绑定的独立 RPC 通道并校验设备与角色。
- Worker 环境变量白名单化：只保留系统必需键与单设备凭据。
- 拓扑派生改为每主机一次反向遍历 + 前驱指针；配置重建与时间刷新分离。
- 宿主重启判定改用 Linux `boot_id` / macOS `bootsessionuuid`，不依赖墙钟。
- SQLite 主库与 WAL/SHM 以 0600 创建，不修改全局 `umask`。

### 修复

- **公开发布泄露机器标识（阻断）**：`docs/test-results.xml` 含本机主机名与 16 处
  家目录绝对路径，扫密规则此前不覆盖。报告已脱敏，规则与同步脚本双层拦截。
- **全量 runtime 回写**：`transact(..., runtime=None)` 曾把全部设备 runtime 行写入，
  82 台单次 WAL +597 KB、100 台 +1.83 MB。
- **`IDC_ALLOWED_HOSTS="a, b"` 静默 403**：逗号后空格未处理，第二个域名永久拒绝。
- **状态执行器无执行超时**：单次卡死会占满 256 个队列名额并让所有 API 返回 503，
  且无法与"忙"区分。
- **降权后读不到 profile（Linux namespace 首次实测）**：worker 在 setuid 之后才惰性
  加载 `profiles/`，安装路径对 worker uid 不可读时，SNMP 端口已监听但每个请求都在
  `load_profile` 抛 PermissionError、客户端只看到超时。改为降权前预加载。
- **RPC 拒绝被内核转成 RST**：peer 校验失败时未读走已到达的请求就直接关闭，
  CPython 3.11 上客户端收到 `ConnectionResetError` 而非 400 语义；关闭前先消费请求。
- 幂等键绑定设备、操作内容与原始 revision；旧库无摘要的缓存拒绝重用（409）。
- 实验恢复失败标 `degraded` 并继续提供 API，不再让一个实验拖垮启动。
- veth 名称使用 32 位哈希并提前检查碰撞；Worker 停止时清理 metrics 临时文件。

## [0.1.0] - 2026-11-09

首个公开发布版本：状态驱动的 IDC 管理面仿真器，提供真实 SNMP/SSH/UDP 协议栈、
Web 控制台与可复现的实验编排。本版同时落地了 2026-11-09 项目评审的 16 项修复。

### 新增

- **协议面**：SNMP v1/v2c/v3（USM、VACM、context/VRF、trap/inform）、SSH CLI
  （Cisco IOS / Juniper Junos / Huawei VRP / H3C Comware 风格）、真实 UDP 监听。
- **状态模型**：单写者事务（`revision` 乐观并发 + `If-Match`）、按设备分片持久化、
  200 ms 派生闸门、静态路由与跨设备链路的确定性推导。
- **实验编排**：`idc-sim lab` 生命周期（running/degraded/stopped）、场景脚本、
  配置事务预览（`preview`）与幂等 `request_id`。
- **Web 控制台**：拓扑图、表格视图、设备配置编辑、诊断（ping / locate）、
  通知订阅与队列指标。
- **交付形态**：`idc-sim` 命令行（serve / create / generate / plan / bench）、
  systemd 单元与 `deploy/environment.example`、7 份示例拓扑。
- **工程门禁**：`scripts/scan-secrets.py` 机密扫描、`scripts/check_release_tag.py`
  版本一致性、ruff 正确性规则、GitHub Actions（测试 + lint + wheel 校验 + Release）。

### 变更

- **状态库 schema v1 → v2**：`labs` 单行全量 blob 拆为 `lab_meta` / `lab_state` /
  `lab_device` / `lab_runtime`；`deliveries` 增加 `device` / `role` / `time_ns`
  结构化列与联合索引；`operations` 增加 `operation_id` 索引。
  **升级方式**：`Controller` 启动时自动就地迁移，`PRAGMA user_version` 由 1 变 2；
  迁移前请备份 `state.db`（v1 库不再被旧版本读取）。
- 派生表缓存不再落盘：重载后由首次 `derive` 重建，避免把可重算数据写进事务路径。
- `pyproject.toml` 的 `version` 改为 dynamic，唯一源为
  `idc_simulator/__init__.py::__version__`；`/v1/status` 同源。
- 示例拓扑里的 `192.0.3.0/24` 保留原样（历史回归用例），扫描器按示例网段放行。

### 修复

- **启动竞态（阻断）**：就绪文件改为 tmp + `os.replace` 原子写，读取侧容忍空/半写
  文件。此前 Worker 会在文件已创建但内容未落盘的窗口里被 `JSONDecodeError` 打回 400，
  表现为实验随机启动失败（实测 1156 次读取中 1137 次读到空文件）。
- **Junos 静态路由无法删除**：删除按 `prefix + VRF` 匹配（未给 next-hop 时删除该前缀
  全部条目）。此前删除会被伪造成 `next-hop 0.0.0.0`，与身份三元组不匹配，命令返回
  `commit complete` 但路由仍在。
- **非 ASCII 凭据导致连接重置**：`hmac.compare_digest` 改为 UTF-8 bytes 比较
  （SSH 与 HTTP 两处）。此前中文用户名/口令会抛 `TypeError`，客户端看到的是
  `ConnectionLost` 而不是认证失败。
- **后台循环静默死亡**：`worker.synchronize` / `notifications.poll` /
  `notifications.consume` 增加最外层兜底、指数退避与 `*_loop_errors_total` 指标，
  并在 `/metrics` 暴露 `state_sync_age_seconds`，循环卡死不再"看起来正常"。
- **投递查询全表扫描**：`pending_events` 由 `json_extract` 改为命中
  `deliveries_lookup` 联合索引。此前被通知 Worker 每 100 ms 调一次，全表扫描在
  1000 trap/s 下约 8600 万行/天。
- **CLI 语义**：`switchport trunk allowed vlan` 支持 `100-110` / `100 to 110` / 逗号
  列表与 `add` / `remove` 增量语义；此前区间写法会抛 `ValueError`，`add` 会整段覆盖。
- **local 模式多设备**：在派生进程**之前**报错并提示"one device per lab"，不再
  先起进程再失败。
- **`--More--` 分页**：读超时后停止分页并继续输出，不再吞掉剩余内容或杀会话。
- **前端**：`window.onerror` / `unhandledrejection` 全局兜底，渲染异常不再让整页
  白屏；单块渲染失败只影响该块。
- **YAML 导入**：4 MiB + 20 万节点预算（alias 共享节点同样计数），拦下锚点展开
  炸弹；7 份示例拓扑不受影响。
- **迁移回填**：v1 库中无父事件的孤立投递在回填 `time_ns` 时不再触发 `NOT NULL`
  失败（`COALESCE(...,0)`）。
- **测试夹具端口竞态**：UDP 端口分配加 5 次重试绑定，消除 `free_port` 的
  TOCTOU 偶发 setup error。

### 安全

- **无 token 且绑定非回环地址时拒绝启动**：需显式设置 `IDC_API_TOKEN`、绑定回环，
  或加 `--allow-unauthenticated`（隔离实验环境）。`/v1/status` 暴露 `auth` 状态。
- 401 响应补 `WWW-Authenticate: Bearer`，符合 HTTP 语义。
- `deliveries` / `events` / `operations` 增加保留期清理，且**不越过最慢消费者游标**
  （默认 10 万条 / 24 小时）。

### 性能

82 台 / 280 链路（`idc-64gpu`）实测，同一台机器上对比修复前后：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 单次配置事务 | 84–92 ms | 37 ms |
| 单次事务落盘 | 2.75 MB | 111 KB |
| 事务吞吐 | ~11 次/s | ~27 次/s |
| 全量 `derive` | ~30 ms | 24 ms |
| 拓扑读路径 | 每次 690 KB / 全量重算 | 200 ms 闸门 + 缓存 |

### 已知问题

- 事务在单线程状态执行器内串行执行，事件循环保持可响应；规模性能以同机基准为准；
  更大规模或高并发写入会阻塞读路径。
- 单机测试中客户端与模拟器竞争同一硬件，上表是短时本地证据，不是吞吐承诺。
- 100 台以上规模、24 小时长跑、Windows 宿主的 namespace 行为未验证。

[Unreleased]: https://github.com/follow2015/ipip-simulator-lab/compare/v1.0.5...HEAD
[1.0.5]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v1.0.5
[1.0.4]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v1.0.4
[1.0.3]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v1.0.3
[1.0.2]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v1.0.2
[1.0.1]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v1.0.1
[1.0.0]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v1.0.0
[0.1.3]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v0.1.3
[0.1.2]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v0.1.2
[0.1.1]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v0.1.1
[0.1.0]: https://github.com/follow2015/ipip-simulator-lab/releases/tag/v0.1.0
