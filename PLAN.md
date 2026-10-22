# serial-mux 跨平台远程 Agent 计划

## 0. 当前状态与本计划边界

当前项目是 Linux/POSIX 优先的本地串口多路复用器：

- 每个 alias 对应一个 daemon；
- daemon 独占串口或可选 SSH PTY；
- 客户端通过 Unix domain socket 连接 daemon；
- daemon 负责日志、历史回放、广播和输入转发；
- USB alias 以 `usb_port` 为跨重启恢复键，旧 `/dev/ttyUSB*` 路径不是恢复键；
- `smtty --send` 只发送一次，不自动重试。

本计划扩展为跨平台、可认证、可配对的对等 Agent 系统。计划中的 Agent、远程传输、Windows 支持和 fastboot 都尚未实现；文档不得把计划命令描述为当前可用功能。

本计划的第一原则是：**任何时刻一个物理串口只能有一个 owner，所有其他组件只能通过协议访问它。**

## 1. 目标与非目标

### 目标

1. 支持 Linux 和 Windows 上运行 Agent。
2. Agent 可以独占本机串口，向已授权的远程节点提供访问。
3. 多台电脑以对等节点方式配对；不存在固定主节点或从节点。
4. 配对后双方可以分别发布自己允许共享的串口，并访问对方已授权的串口。
5. 本地用户和 AI 继续使用统一的 `smtty <alias>`、`--send`、`--wait` 接口。
6. 保留现有本地 alias、日志和命令兼容性。
7. 远程断线、重连、设备拔插和 Agent 重启都具有明确、可测试的状态语义。
8. 普通单元测试不依赖真实串口、真实网络、真实 SSH 或特定操作系统。

### 非目标

- 不把 fastboot 当作串口 transport。
- 不让客户端直接访问设备电脑上的 COM 或 `/dev/tty*`。
- 不默认暴露公网监听端口。
- 不通过旧 TTY 路径、节点名称或显示名称静默重新绑定设备。
- 不采用中心服务器或固定主从关系。
- 不允许无认证、无配对的局域网节点访问串口。
- 不把一次网络重试伪装成已经确认的 exactly-once 设备执行。
- 第一阶段不实现 fastboot、刷写、擦除等设备变更操作。

## 2. 明确的进程与 owner 模型

### 2.1 两种兼容部署模式

为避免破坏现有用户，系统分两个部署模式，但两种模式都遵守单 owner 规则：

```text
现有本地模式（兼容模式）
smtty -> 本地 daemon -> 本地串口
                         ^ 唯一 owner

Agent 模式（目标模式）
本地/远程客户端 -> 本地 daemon/proxy -> Agent -> 串口
                                            ^ 唯一 owner
```

- 现有 `serial-mux start <device> --alias <alias>` 在兼容模式下仍由 daemon 独占串口。
- Agent 模式下，串口由 Agent 的 `PortManager` 独占；本地 daemon 只作为客户端代理、alias/cache 和兼容接口，不得再次打开该串口。
- 一个 alias 只能绑定一种 owner 来源：`local-daemon`、`local-agent` 或 `remote-agent`。
- 同一物理串口不得同时注册给兼容 daemon 和 Agent。迁移必须先关闭旧 owner，再由新 owner 显式接管。
- Agent 可以枚举设备，但只有显式注册/管理的端口才能打开和共享；仅被发现的端口不能自动获得控制权。

### 2.2 组件职责

#### Agent

Agent 是目标架构中的串口 owner 和远程服务端，负责：

- 枚举本机串口；
- 以平台适配层打开并独占串口；
- 维护串口读写、波特率和设备状态；
- 为每个端口维护输出序列和 owner epoch；
- 保存权威日志和历史；
- 向授权客户端广播输出；
- 接收输入并返回可确认或未知的送达状态；
- 管理端口共享策略；
- 提供认证后的端口列表、打开、关闭和数据服务；
- 处理设备拔出、重连、优雅停止和恢复。

#### 本地 daemon/proxy

本地 daemon 在 Agent 模式下负责：

- 保持现有 `serial-mux` / `smtty` CLI 和本地 alias 语义；
- 连接本地或远程 Agent；
- 缓存远程权威日志，支持本地 `--tail` 和 attach 历史；
- 按输出序列去重和补齐缓存；
- 将远程 transport 状态映射为现有客户端协议；
- 不直接打开已经由 Agent 管理的串口。

兼容模式下，本地 daemon 仍可直接拥有串口；这部分代码和 Agent 的 `PortManager` 必须逐步收敛到同一个 transport 接口，避免两套 owner 逻辑继续分叉。

#### 客户端

`smtty` 不感知底层是本地串口、SSH、local Agent 还是 remote Agent，只处理统一的本地 daemon 协议。交互式和非交互式语义保持不变。

### 2.3 owner 状态迁移

端口 owner 迁移必须使用显式流程：

1. 当前 owner 标记端口为 `draining`，拒绝新的写入；
2. 等待已开始的写操作返回 `accepted`、`rejected` 或 `unknown`；
3. 关闭当前 transport；
4. 新 owner 以新的 `owner_epoch` 打开端口；
5. 广播 `transport_changed` 和端口状态；
6. 迁移失败时不得静默由另一组件接管。

## 3. 日志、历史与输出可靠性

### 3.1 权威日志

串口 owner 是日志唯一权威来源：

- 兼容模式由本地 daemon 写权威日志；
- Agent 模式由 Agent 写权威日志；
- proxy 可以写本地缓存，但缓存不是权威日志；
- 每个输出事件包含 `node_id`、`port_id`、`owner_epoch`、单调递增 `seq` 和数据；
- 输入记录也必须带事件序列和 request ID，但不能把输入执行状态伪装成设备响应。

### 3.2 远程历史与本地缓存

远程 attach 时：

1. proxy 发送自己已缓存的 `(owner_epoch, seq)`；
2. Agent 从该位置之后补发历史或明确返回 `gap`；
3. proxy 按 `(owner_epoch, seq)` 去重后写入本地缓存；
4. 补发完成后切换到 live stream；
5. 如果 owner epoch 已变化，必须先发送 epoch change，再按新 epoch 回放；
6. 如果权威日志无法覆盖缺口，客户端看到明确的 `history_gap`，不能假装连续。

输出不要求在断线后无限保存，但必须区分：

- 已确认补齐；
- 明确存在历史缺口；
- 当前仍未知。

### 3.3 输入语义

第一版明确采用 **at-most-once delivery + unknown on uncertainty**：

- 每次输入有唯一 `request_id`；
- Agent 接受并提交给串口写入后返回 `accepted`；
- 如果连接在提交前后中断，客户端显示 `unknown`，不得自动重发；
- `accepted` 只表示 owner 接收了写请求，不表示设备已经执行命令；
- 服务端可以在短期状态表中按 `request_id` 查询 `accepted/rejected/unknown`；
- 不承诺 exactly-once device execution，因为串口设备通常没有事务或幂等语义；
- 这与现有 `smtty --send` 的一次发送规则一致。

## 4. 协议 envelope 与兼容策略

### 4.1 通用 envelope

保留现有的 **4-byte big-endian length prefix + UTF-8 JSON** 作为消息封装。Agent 协议消息统一包含：

```json
{
  "protocol": {"major": 1, "minor": 0},
  "type": "output",
  "request_id": "optional-request-id",
  "connection_id": "connection-epoch",
  "payload": {}
}
```

实现阶段必须兼容当前本地 daemon/client 的扁平消息格式；远程 Agent 可以使用 envelope v1，proxy 负责两种格式之间的适配。二进制数据仍使用现有 base64 编码，不允许把未经编码的字节放入 JSON。

### 4.2 协商规则

- `major` 不同则拒绝连接，并返回结构化 `protocol_version_incompatible`。
- `minor` 向后兼容：接收方必须忽略未知字段；未知消息类型必须返回结构化错误或安全忽略，不能改变串口状态。
- `hello` 必须携带角色、节点 ID、协议版本和能力列表。
- capability 不得仅由版本号推断；至少协商 request ID、output replay、input status、heartbeat 和 pairing 能力。
- 每个请求使用唯一 `request_id`；响应必须回显它；广播消息不使用请求 ID或明确标记为 broadcast。
- `connection_id`/`owner_epoch` 用于区分旧连接、旧 owner 和重连后的消息。
- 错误必须包含稳定的 `code`、可读 `message` 和必要的 `retryable` 字段；密码、token、串口输入和敏感 SSH 信息不得进入错误文本或日志。

### 4.3 消息类型

第一版正式定义：

- `hello` / `hello_ack`：版本、角色、能力和认证握手；
- `list_ports` / `port_info`：枚举和端口状态；
- `open` / `open_ack` / `close`：申请和释放端口；
- `input` / `input_status`：一次输入请求及 `accepted/rejected/unknown`；
- `output`：带 `owner_epoch`、`seq` 和二进制数据；
- `history_request` / `history_chunk` / `history_complete` / `history_gap`：断点回放；
- `transport_changed`：连接、断开、重连、owner 迁移和 fallback；
- `set_baud` / `baud_ack`：波特率变更；
- `ping` / `pong`：保活；
- `error`：结构化错误；
- `pair_request` / `pair_confirm` / `pair_result`：人工确认配对。

## 5. 身份、发现、认证与配对

### 5.1 第一版认证方案

第一版选择 **TLS 1.3 + 长期节点身份 + 一次性配对码**，不把 SSH/Tailscale 作为协议必需条件。SSH/Tailscale 可以作为部署隧道，但不替代 Agent 应用层认证。

- 每个 Agent 首次启动生成节点 ID 和非导出私钥，存储在平台凭据目录或权限受限的数据目录中；
- TLS 使用节点身份进行认证；
- 未配对节点只能进行候选发现和配对握手，不能列出端口、打开端口或发送输入；
- `pair` 生成短期、一次性确认码，必须由双方人工确认；
- 配对成功后双方持久化对端节点 ID、公钥指纹、名称和权限；
- 凭据支持撤销和轮换；旧凭据撤销后现有连接必须在有限 grace period 后断开；
- 请求包含 nonce/timestamp 或等效防重放字段；TLS 会话外的配对 token 只能使用一次且短期有效；
- token、私钥和配对码不得写入日志、alias JSON、命令输出或普通历史。

### 5.2 监听与发现边界

必须区分三个边界：

```text
发现：可选，返回节点候选信息，不授权访问
控制：TLS + 已配对身份，提供列表/打开/控制
本地 IPC：仅本机，使用平台权限保护
```

- Agent 默认不监听外部可达地址；
- 启用远程功能时必须显式配置 bind address、端口、允许来源和连接超时；
- 发现消息只包含节点名称、节点 ID、协议版本、监听候选地址、能力摘要和一次性配对提示，不包含串口输入输出，也不代表授权；
- 未配对节点不得得到可操作的串口清单；
- 不直接把监听端口暴露到公网，部署文档优先建议受限局域网、TLS、SSH tunnel 或 Tailscale。

### 5.3 对等关系

配对关系是双向信任，但权限仍按端口单独检查。两台电脑都可以同时：

- 提供本机共享串口；
- 访问对方共享串口；
- 接收多个已授权客户端；
- 取消某个端口共享或解除整个配对关系。

## 6. 共享策略与远程 alias

### 6.1 默认共享策略

**默认不共享任何串口。** 配对成功只建立身份信任，不自动授权端口。用户必须通过 `share` 明确发布端口；配置可以选择显式的配对后自动共享策略，但默认值必须保持关闭。

- `share` / `unshare` 立即影响新连接；
- `unshare` 后，已建立的远程端口连接进入 `draining`，拒绝新输入，并在短 grace period 后关闭；
- 共享状态持久化到 Agent 数据目录，Agent 重启后按持久状态恢复；
- 端口未被 Agent 管理、已被其他程序占用或平台拒绝时不得发布；
- 共享状态变化必须广播给所有已配对节点并写入审计日志；
- 远程节点只能看到已配对且当前共享的端口。

### 6.2 远程端口身份与 alias schema

端口身份不能依赖 COM 名称、`/dev/ttyUSB0`、节点显示名称或列表顺序。至少使用以下稳定字段：

```json
{
  "alias": "node-a/board-serial",
  "owner_source": "remote-agent",
  "source_node_id": "stable-node-id",
  "remote_port_id": "stable-port-id",
  "hardware_id": "platform-specific-stable-id-or-null",
  "baud": 115200,
  "protocol_major": 1,
  "credential_ref": "local-secret-reference",
  "availability": "online|offline|unshared|revoked|unknown"
}
```

规则：

- `source_node_id + remote_port_id` 是远程端口的逻辑主键；
- `hardware_id` 用于设备身份辅助确认，不足以单独替代 owner 记录；
- 端口重插后只有在 Agent 依据平台稳定身份确认一致时才能恢复，否则保持 `unknown` 或 `offline`；
- 节点改名不改变 `source_node_id`；
- 自动 alias 必须稳定且与用户自定义 alias 冲突时拒绝，不得覆盖；
- unpair、取消共享或身份撤销后 alias 可以保留日志，但必须显示 `unshared`/`revoked`/`offline`，不得静默指向其他节点或端口；
- 凭据引用只指向本地安全存储，不把秘密写入 alias JSON。

### 6.3 命令

Agent 命令最终至少支持：

```bash
serial-mux agent start [--listen <address>] [--foreground]
serial-mux agent stop
serial-mux agent status
serial-mux agent list
serial-mux agent pair <node-id-or-name>
serial-mux agent unpair <node-id-or-name>
serial-mux agent share <port-or-alias>
serial-mux agent unshare <port-or-alias>
serial-mux agent share-all
serial-mux agent unshare-all
```

`agent list` 至少显示节点身份和连接状态、本机端口及共享状态、远程共享端口、来源节点、端口身份、占用/断线状态以及本地 alias 对应关系。

## 7. 跨平台适配边界

平台差异不只在串口枚举。必须把以下能力放进明确的适配接口：

- `SerialProvider`：枚举、稳定硬件身份、打开、读、写、关闭、波特率和拔出检测；
- `ProcessSupervisor`：前台/后台生命周期、PID/句柄、停止和崩溃检测；
- `LocalIPC`：Linux Unix socket 与 Windows named pipe 或本机受权限保护的 loopback IPC；
- `TerminalAdapter`：raw mode、窗口大小、控制台编码、Ctrl+]、Ctrl+C、窗口关闭和恢复；
- `PathProvider`：配置、运行、日志和凭据目录；
- `SignalAdapter`：POSIX signal 与 Windows console/service 事件。

当前 Linux 实现中的以下依赖不得泄漏到协议和核心业务层：

- `/proc/sys/kernel/random/boot_id`；
- `/sys/class/tty`、`/sys/devices`；
- `os.fork()` 和 double-fork daemonization；
- `termios`、`tty`、Unix-domain socket；
- `/dev/tty*` 路径规则。

Windows 端必须使用 COM 端口枚举和稳定硬件标识，不能用端口号作为跨重启唯一身份。Agent 和 `smtty` 的非交互模式不得依赖终端 API。

## 8. 分阶段实施

### 阶段 1：协议 envelope 与 fake transport

1. 固定协议 major/minor、角色、capability、request ID、connection ID 和错误码。
2. 保留现有长度前缀 + JSON 格式，并定义 envelope v1 和本地扁平协议适配。
3. 明确 `hello`、`list_ports`、`open`、`close`、`input`、`output`、history、transport、heartbeat 和错误消息。
4. 为 output 增加 `node_id`、`port_id`、`owner_epoch`、`seq`。
5. 实现 fake serial transport、fake Agent server 和可重复的协议测试。
6. 验证未知字段、未知消息、版本不兼容、请求超时、输入状态和历史缺口。

### 阶段 2：统一 transport 与 owner 生命周期

1. 从 daemon 抽出统一 transport 接口：`open`、`read`、`write`、`close`、`status`、`set_baud`、`cancel`。
2. 将当前 pyserial 逻辑封装为 local serial transport。
3. 抽出 owner epoch、draining、输入状态和迁移流程。
4. 保持当前本地 daemon 行为和 `smtty --send` 一次发送语义。
5. 明确 daemon 直接 owner 与 Agent owner 不能并存。

### 阶段 3：Linux Agent 与本地 proxy

1. 实现 Linux Agent 的 `PortManager`、权威日志、history replay 和本地权限控制。
2. 实现本地 daemon/proxy 到 Agent 的 remote transport。
3. 远程输出按序列补发、去重和 `history_gap` 映射到现有客户端行为。
4. 实现 TLS 节点身份、一次性配对码、凭据持久化、撤销和轮换。
5. 默认不共享端口；实现显式 `share`/`unshare`。
6. 保留现有 alias、日志、`list`、`status`、`smtty` 和本地模式。

### 阶段 4：Windows Agent 与跨平台本地 IPC/终端

1. 实现 Windows `SerialProvider`、端口枚举、稳定硬件身份和拔出检测。
2. 实现 Windows `ProcessSupervisor`、本地 IPC、路径和凭据适配。
3. 实现 Windows 控制台 raw mode、输出编码、detach、Ctrl+C 和窗口关闭处理。
4. 确保 Linux Agent 与 Windows Agent 之间使用同一远程协议。
5. 增加 GitHub Actions Windows runner 测试，并将真实硬件测试保持为显式 opt-in。

### 阶段 5：对等发现、列表同步与远程 alias

1. 实现只提供候选信息的局域网发现。
2. 实现人工确认的双向配对、unpair、撤销和凭据轮换。
3. 实现配对后的双向端口列表同步和实时上线/下线/共享变化。
4. 实现稳定的 `source_node_id + remote_port_id` alias 和状态恢复。
5. 确保两台电脑都能同时提供和访问对方共享的串口，不引入主从规则。
6. 增加节点/端口离线、取消共享、端口争用和身份变化测试。

### 阶段 6：发布、迁移和运维文档

1. 更新版本、协议兼容说明和迁移说明。
2. 更新英文 README、中文 README、架构文档、Agent 配置、安全部署和故障排查文档。
3. 明确本地模式、Agent 模式、Windows 权限、TLS 凭据和共享策略。
4. 发布前运行 Linux/Windows CI、fake transport 集成测试和 opt-in 真实环境测试。

### 独立计划：fastboot companion

fastboot 不属于本计划的串口 transport。未来应单独设计 companion agent，另行定义设备枚举、只读查询、独占锁、人工确认、审计、权限和 dsh/MCP 集成；它不能复用串口输入协议，也不能因为远程串口 alias 可用就默认获得 fastboot 权限。

## 9. 测试与 GitHub Actions CI

### 9.1 测试分层

- 协议、序列、版本协商和错误码使用纯 Python 单元测试；
- fake serial 和 fake Agent 覆盖并发、断线、重连、历史补发、输入状态和端口争用；
- Linux 集成测试覆盖 `socat`、Unix socket、sysfs fixture、POSIX terminal 和 localhost SSH；
- Windows 集成测试使用 GitHub-hosted Windows runner，覆盖 COM 名称解析、Windows 本地 IPC、进程生命周期和控制台适配；
- 真实串口、真实多机网络和真实硬件测试通过环境变量或 workflow dispatch 显式启用，不进入普通 PR 必跑路径。

### 9.2 GitHub Actions 方案

Windows CI 应使用 GitHub Actions 的 `windows-latest` runner，而不是把 Windows 当作 Linux 环境模拟。GitHub-hosted Windows runner 是真实的托管 Windows 虚拟机；`gh` CLI 只能用于触发、查看和下载 workflow，不能把本地 Linux 进程变成 Windows 环境。

推荐矩阵：

```yaml
strategy:
  matrix:
    include:
      - os: ubuntu-latest
        python: "3.10"
      - os: ubuntu-latest
        python: "3.12"
      - os: windows-latest
        python: "3.10"
      - os: windows-latest
        python: "3.12"
```

CI 设计要求：

- Linux job 安装 `socat`、`openssh-server`，继续运行现有 POSIX/SSH 集成测试；
- Windows job 安装同一 Python 包和 test extras，但跳过明确标记的 Linux-only `socat`、sysfs、Unix socket、fork、termios 测试；
- Windows job 必须运行 fake transport、协议、状态机、CLI 非交互、Agent client/server 和 Windows adapter 测试，不能只执行 `--collect-only`；
- 测试应通过 pytest markers 或平台能力 fixture 精确区分，而不是在测试主体中散落 `sys.platform` 分支；
- 每个 workflow 保留明确的 test summary 和 artifact 日志，禁止输出 token、私钥、串口输入或设备敏感信息；
- workflow 变更后先用等价的本地命令验证，再通过 `gh workflow run` 或 pull request 运行完整矩阵。

### 9.3 测试矩阵最低覆盖

- 协议编码、解码、版本协商、请求 ID、序列、重放和错误；
- Agent 端口枚举、打开、关闭、共享和 owner 状态；
- remote transport 重连、history replay、gap、heartbeat 和断线；
- alias 持久化、节点/端口身份和设备拔插；
- TLS 认证、配对码一次性使用、撤销、轮换和未授权访问；
- `smtty --send`、`--wait`、`--tail` 和 exit code；
- Linux/Windows 路径、端口名、IPC、进程和终端适配；
- 一个 Agent 多客户端、两个对等 Agent 双向共享和 unpair；
- 同一端口的 owner 争用、迁移和取消后的 draining。

## 10. 安全要求

1. Agent 默认只监听本机安全地址；外部监听必须显式开启。
2. 所有远程控制和串口数据连接必须经过 TLS 和已配对身份认证。
3. 发现只提供候选信息，不授予端口访问权。
4. 默认不共享端口；配对、share、open、input 和 set-baud 都必须独立检查权限。
5. token、私钥、配对码和 credential reference 不得写入日志、命令输出、历史或 alias JSON。
6. 远程输入必须带 request ID、连接身份、端口身份和审计时间；权限错误不能泄漏端口细节。
7. nonce、时间窗、TLS 会话和一次性配对码必须防止重放。
8. unshare、unpair、撤销和 owner 迁移必须让已有连接进入明确的 draining/closed 状态。
9. 断线、取消和超时只报告真实状态；对可能已经送达设备的输入禁止自动重试。
10. 日志清理、敏感信息处理和历史回放在远程模式下与本地模式保持一致。
11. 增加认证失败、未授权端口、端口争用、重放 token、异常断线和凭据撤销测试。

## 11. README 和文档更新要求

实现功能的同一变更中必须更新英文和中文文档，不把远程能力只写在代码注释中。

### `README.md` 和 `README_zh.md`

新增内容必须准确说明：

- serial-mux 当前本地模式与 Agent 模式的区别；
- Agent 支持 Windows 和 Linux；
- Agent 是对等节点，没有主从关系；
- 配对不等于端口共享，默认不共享；
- `share`/`unshare`、节点和远程 alias 的状态；
- `smtty` 的本地和远程使用方式；
- 输入 at-most-once、断线后的 unknown 语义；
- TLS、配对、凭据和安全监听要求；
- Windows 串口、控制台、权限和 IPC 差异；
- fastboot 是独立能力，不属于串口 transport。

远程快速开始必须同时说明双向行为：两台电脑配对后，双方都可以看到对方**明确共享**的串口；没有 `share` 的端口不会自动出现。

### `docs/ARCHITECTURE.md`

补充：

- daemon、proxy、Agent、PortManager 的 owner 边界；
- local serial、SSH、local Agent 和 remote Agent 的统一 transport 接口；
- 日志权威来源、缓存和 history replay；
- envelope、版本协商、request ID、序列和错误码；
- TLS 身份、发现、一次性配对、双向信任和凭据撤销；
- 默认不共享、端口权限和 alias schema；
- Linux/Windows 适配层和 GitHub Actions 测试矩阵；
- fastboot 与串口 transport 的隔离。

### `agents.md`

根目录及相关子目录的开发指引应记录：

- 当前实现仍是 Linux/POSIX 本地 daemon；
- Agent、Windows、远程协议均为分阶段计划；
- 单 owner、at-most-once、USB alias 和终端行为不变量；
- fake transport、Linux CI、Windows GitHub Actions runner 的测试要求；
- 协议兼容和安全要求。

## 12. 兼容性与发布原则

1. 当前本地 alias、日志、`smtty`、`serial-mux` 命令继续可用。
2. 远程功能采用显式 opt-in，不改变默认本地监听行为。
3. 本地协议适配层必须让旧客户端能继续连接旧 daemon；不强迫旧用户立即升级 Agent。
4. 协议新字段应允许旧实现安全忽略；不兼容 major 版本必须明确拒绝。
5. 远程 alias 不得覆盖现有本地 alias，也不得因节点离线而静默改变目标。
6. 每个阶段完成后先提交协议/测试/文档，再提交平台实现，减少跨平台变更的回滚范围。
7. 发布前更新版本说明、README、架构文档、迁移说明和安全部署指南。

## 13. 推荐实施顺序

1. 固定协议 envelope、版本协商、错误码、request ID、序列和 owner epoch；
2. 抽取 transport/owner 接口并保持现有本地功能；
3. 实现 fake Agent、fake serial 和 remote transport；
4. 实现 Linux Agent、本地 proxy、TLS 身份和一次性配对；
5. 实现默认不共享、share/unshare、history replay 和远程 alias；
6. 实现 Windows 平台适配并接入 GitHub Actions Windows runner；
7. 实现对等发现、列表同步、unpair、撤销和双向集成测试；
8. 更新 README、架构文档、平台文档和迁移说明；
9. 将 fastboot 作为独立项目另行设计和实现。
