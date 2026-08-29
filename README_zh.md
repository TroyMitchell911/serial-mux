# serial-mux

[English](README.md) | [中文](README_zh.md)

串口多路复用工具 — 允许多个客户端通过 daemon + Unix socket 架构共享同一个串口设备。

## 功能特性

- 一个串口，多个客户端同时访问
- daemon 后台驻留，独占串口，通过 Unix socket 扇出数据
- **可选 SSH 绑定** — SSH 优先，串口作为后备
- 交互式客户端 `smtty`
- 非交互模式支持单次发送命令并等待输出匹配
- alias 记录物理 USB 端口，系统重启后保留映射；USB 拔插会自动使映射失效
- 按日期分割的持久化日志，自动清理过期文件
- attach 时回放历史行，scrollback 由终端/tmux 管理
- 输出直接到 stdout，复制粘贴正常工作

## 安装

### 一键安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/TroyMitchell911/serial-mux/main/install.sh | sudo bash
```

脚本会将仓库 clone 到 `/usr/local/lib/serial-mux`，安装依赖，并在 `/usr/local/bin` 创建 `serial-mux` 和 `smtty` 两个命令。同时会检测当前用户是否在串口设备组中。

### 手动安装

```bash
git clone https://github.com/TroyMitchell911/serial-mux.git
cd serial-mux
pip install -e .
```

### 串口权限

用户需要加入串口设备所属的系统组才能访问设备。例如在 Arch Linux 上，串口设备属于 `uucp` 组：

```bash
sudo usermod -aG uucp $USER
```

加入后需要重新登录或 `newgrp uucp` 使权限生效。不同发行版的组名可能不同（如 Debian/Ubuntu 上是 `dialout`）。

## 快速开始

### 1. 启动 daemon

```bash
# 启动串口 daemon
serial-mux start /dev/ttyUSB0 --baud 115200 --alias die0

# 仅启动 SSH（无串口）
serial-mux start --ssh root@192.168.1.100 --alias die0

# 同时绑定串口和 SSH
serial-mux start /dev/ttyUSB0 --alias die0 --ssh root@192.168.1.100
```

daemon 会 double-fork 到后台驻留。新建 alias 时 `DEVICE` 和 `--ssh` 至少指定一个；
已有保存记录时可以只用 `--alias` 恢复。

`start` 会记录 USB 串口所在的物理端口和本次枚举实例。系统重启后，
`run/<alias>.json` 不会作为 stale 状态删除；第一次执行 `smtty <alias>` 时会按物理端口
找到当前 `/dev/ttyUSBx` 并恢复 daemon，也可以手动执行：

```bash
serial-mux start --alias die0
```

恢复过程以 `usb_port` 为唯一持久依据：扫描 `/sys/class/tty` 找到该物理端口当前
对应的 tty，最后才生成用于打开串口的临时 `/dev/ttyUSBx` 路径。旧 tty 名不会参与
匹配；daemon 退出后，JSON 中的 `device` 会被清空。

同一次开机中若 USB 串口被拔出或重新枚举，旧映射会自动清除，不会把原 alias
静默绑定到后来插入的设备。daemon 仍在时请用 `serial-bind`，否则显式新建映射。

启动时绑定 SSH：

```bash
serial-mux start /dev/ttyUSB0 --alias die0 --ssh root@192.168.1.100
serial-mux start /dev/ttyUSB0 --alias die0 --ssh k3_die0   # 使用 ~/.ssh/config 中的 hostname
```

如需前台调试：

```bash
serial-mux start /dev/ttyUSB0 --baud 115200 --alias die0 --foreground
```

### 2. 连接串口

连接串口（以用户身份）：

```bash
smtty die0
```

使用 `--timestamps` / `-T` 在所有行上显示时间戳（历史回放、输入回车、接收输出）：

```bash
smtty die0 --timestamps
```

默认不显示时间戳。

#### 查看最近日志

不连接 daemon，直接从日志文件打印最后 N 行：

```bash
# 默认最后 50 行
smtty die0 --tail

# 最后 200 行
smtty die0 --tail 200
```

daemon 不需要在运行中，直接读取日志文件。

### 3. 断开连接

交互模式下按 `Ctrl+]` detach，daemon 不受影响，其他客户端继续工作。

### 4. 停止 daemon

```bash
serial-mux stop die0
```

## 命令参考

### daemon 管理

| 命令 | 说明 |
|------|------|
| `serial-mux start <device> --baud <rate> --alias <name>` | 启动 daemon，独占串口 |
| `serial-mux start <device> --baud <rate> --alias <name> --foreground` | 前台模式启动，用于调试 |
| `serial-mux stop <alias>` | 停止指定 daemon |
| `serial-mux list` | 列出运行中的 daemon 和保存的映射 |
| `serial-mux status <alias>` | 查看指定串口的详细状态 |
| `serial-mux set-baud <alias> <baud>` | 动态修改运行中 daemon 的波特率（需已绑定串口） |
| `serial-mux ssh-bind <alias> <target>` | 为运行中的 daemon 绑定 SSH（user@host 或 ssh config hostname） |
| `serial-mux ssh-unbind <alias>` | 解除 SSH 绑定，回退到串口 |
| `serial-mux serial-bind <alias> <device> [--baud]` | 为运行中的 daemon 绑定串口 |
| `serial-mux serial-unbind <alias>` | 解除串口绑定 |

`serial-mux list` 输出示例：

```
ALIAS        DEVICE               BAUD       PID      CLIENTS  UPTIME       STATUS     SSH
-----------------------------------------------------------------------------------------------------
die0         /dev/ttyUSB0         115200     12345    1        2h 15m       running    root@192.168.1.100
die1         /dev/ttyUSB1  115200  -  -  -  saved  -
```

### 客户端

| 命令 | 说明 |
|------|------|
| `smtty <alias>` | 交互式客户端 |
| `smtty <alias> --timestamps` | 交互模式，显示时间戳 |
| `smtty <alias> --tail [N]` | 打印最后 N 行日志并退出（默认 50） |
| `smtty <alias> --send 'cmd' --wait 'pattern' --timeout 5` | 非交互模式 |

## 非交互模式

专为自动化和 Agent 场景设计：

```bash
smtty die0 --send "ls" --wait "root@" --timeout 5
```

执行流程：

1. 连接 daemon 的 Unix socket
2. 将命令向串口或 SSH 发送一次
3. 等待 `--wait` 指定的 pattern 出现，或超时退出
4. 成功 → stdout 输出命令结果，静默退出（exit code 0）

非交互模式不校验终端回显，也不会自动重发命令。

### SSH 传输层

daemon 绑定 SSH 后，客户端自动优先使用 SSH。交互模式的 attach banner 会显示当前传输层：

```
--- serial-mux: attached to die0 [ssh] (Ctrl+] to detach) ---
```

SSH 连接断开时 daemon 自动切换到串口并通知所有客户端。

SSH target 支持两种格式：
- `user@host` — 直接使用
- 裸 hostname — 从 `~/.ssh/config` 中查找验证，不存在则拒绝

> **注意：** SSH 使用 `BatchMode=yes`，仅支持密钥认证。密码登录会被自动拒绝。使用前请先配好 SSH 密钥。

### 命令发送机制

无论使用串口还是 SSH，非交互模式都只发送一次命令，随后立即收集输出或等待指定 pattern，不校验终端回显。

## 交互模式

### 输出行为

- 所有内容直接输出到 stdout，不自管 buffer
- scrollback 完全由终端/tmux 管理
- 复制粘贴、`tmux capture-pane` 正常工作
- 切换 tmux session 不丢内容

### attach 时的行为

1. 从日志文件读取历史内容，print 到 stdout（进入终端/tmux scrollback）
2. 切换到实时模式，持续将串口数据写到 stdout
3. 用户看到光标在最底部，往上滚即可查看历史
4. 效果类似 `cat log && tail -f log`，但是完整的交互式会话

### 输入

- stdin 设为 raw mode，逐字符读取
- 用户按键通过 Unix socket 发送给 daemon，daemon 转发到串口

### Detach

交互模式下按 `Ctrl+]` 断开客户端连接，daemon 继续运行。

## alias 机制

设备路径在系统重启后可能变化（例如 `/dev/ttyUSB0` 变成 `/dev/ttyUSB1`）。alias
保存物理 USB 端口、boot ID 和 USB 枚举实例，因此重启后可以找回同一端口上的设备；
同一次开机中的拔插则会使旧映射失效。

alias 映射存储在 `~/.serial-mux/run/<alias>.json`：

```json
{
  "alias": "die0",
  "device": null,
  "baud": 115200,
  "pid": null,
  "socket": "/home/user/.serial-mux/sock/die0.sock",
  "boot_id": "fafa4fd7-...",
  "usb_port": "pci0000:00/.../usb/usb-2/usb-2:1.0",
  "usb_instance": "1:4",
  "ssh": "root@192.168.1.100",
  "start_time": null,
  "clients_count": 0
}
```

上例是 daemon 未运行时的持久记录。`usb_port` 是恢复主键；`device` 只在 daemon
运行期间表示本次反查得到的当前设备节点，不用于跨重启匹配。端口通过 udev
的 `/dev/serial/by-path` 稳定符号链接解析为当前 tty 节点。

同一次开机中的 USB 拔出或重新枚举**不会**使映射失效——这正是 EMI/热插拔
恢复场景。alias 始终绑定其物理端口，设备重新出现即自动恢复；只有当一只
*不同* 的设备（VID/PID 或 serial 不同）占用了该端口时才清除映射。

客户端使用 alias 连接时优先查找映射，未匹配时当设备路径处理。

## 自动重连

serial-mux 面向硬件 bring-up 中常见的瞬断场景设计：开发板复位、USB 串口重新枚举、daemon 重启，都不需要人工重启任何东西。

### 串口丢失与恢复

当 daemon 的串口传输消失（拔插、供电抖动、设备从 `/dev/ttyUSB1` 重新枚举为 `/dev/ttyUSB0`）时，daemon **不会退出**。它会关闭失效端口、记住设备身份，并轮询 sysfs 直到设备重新出现，然后自动重新打开并恢复扇出。连接的客户端会看到每次切换的状态行：

```
--- serial device lost: USB serial port was unplugged or re-enumerated — waiting to reconnect ---
--- serial restored: /dev/ttyUSB0 ---
```

恢复匹配的是原始**设备**，而不是某个端口或转瞬即逝的 `/dev/ttyUSB*` 名字。设备身份由物理 USB 端口 + VID/PID 组成，并在适配器提供 USB serial 时一并校验。端口通过 udev 的 `/dev/serial/by-path` 稳定符号链接解析（回退到 sysfs 扫描），跨重新枚举和设备名变化保持不变。

多板场景：绝大多数 FT232/CH340 的 VID/PID 相同且没有唯一 USB serial，物理端口是唯一区分手段。每个 daemon 只轮询自己记录的端口；当多块板子因 EMI 依次断开、又按不同顺序恢复（内核可能把 `ttyUSB0`/`ttyUSB1` 等节点名重新分配），每个 daemon 仍会重绑自己的物理设备——`serial-mux list` 里的 tty 名字可能对调，但每个 alias 依旧连着自己那块板。如果同一端口上出现了另一只设备（VID/PID 或 serial 不同），daemon 会继续等待而不是静默绑定错误设备。真正把两只一模一样的适配器对调端口是软件无法识别的——请显式 `serial-bind` 重新映射。

显式执行 `serial-mux serial-unbind <alias>` 仍然会彻底停用该端口——自动重绑只针对**意外**丢失。设置 `serial_reconnect_interval: 0` 可完全关闭自动重绑（停止轮询，而不是空转）。

### 客户端重连 daemon

如果 daemon 进程本身消失（崩溃、重启或 `kill`），交互式客户端 `smtty` 不再退出。它会每 `client_reconnect_interval` 秒重试连接，尽可能从保存的元数据自动恢复已死的 daemon，并在恢复实时 I/O 前回放 scrollback 历史。任何时候按 `Ctrl+]` 都可以 detach 并停止重试。`client_reconnect_attempts` 限制重试次数（0 = 无限重试）。

## 配置文件

路径：`~/.config/serial-mux/config.yaml`

```yaml
log_retention_days: 7       # 日志保留天数
default_baud: 115200        # 默认波特率
scrollback_lines: 5000      # attach 时回放的历史行数
ssh_connect_timeout: 3      # SSH ConnectTimeout（秒）
ssh_probe_timeout: 5        # SSH 探测等待时间（秒），超时判定连接成功
serial_reconnect_interval: 1.0  # daemon 轮询 sysfs 等待丢失的 USB 串口重新出现的间隔（秒），0 禁用自动重绑
client_reconnect_interval: 1.0  # smtty 客户端重连 daemon socket 的间隔（秒）
client_reconnect_attempts: 0    # smtty 最大重连次数（0 = 无限重试）
```

所有配置项都有合理默认值，配置文件可选。daemon 启动时读取配置。

## 日志

### 格式

日志文件按 alias + 日期分割，存储在 `~/.serial-mux/logs/<alias>/YYYY-MM-DD.log`。

每行带时间戳：

```
[2026-04-16 16:30:01] echo hello
[2026-04-16 16:30:01] hello
[2026-04-16 16:30:05] cat /proc/version
[2026-04-16 16:30:05] Linux version 6.x ...
```
所有数据（输入回显和设备输出）统一以时间戳格式记录，不区分来源。

### 自动清理

daemon 启动时自动扫描 logs 目录，删除超过 `log_retention_days` 天数的日志文件，无需额外 cron job。

## 文件布局

```
~/.serial-mux/
├── run/
│   ├── die0.json          # 持久 alias + USB 身份 + daemon 运行信息
│   ├── die0.pid           # PID file
│   ├── die1.json
│   └── die1.pid
├── sock/
│   ├── die0.sock          # Unix domain socket
│   └── die1.sock
└── logs/
    ├── die0/
    │   ├── 2026-04-15.log
    │   └── 2026-04-16.log
    └── die1/
        └── 2026-04-16.log

~/.config/serial-mux/
└── config.yaml            # 配置文件（可选）
```

## daemon 进程管理

### 驻留方式

- `serial-mux start` 时 double-fork 到后台，自行 daemonize
- 不依赖 systemd，无需 service 文件
- PID file：`~/.serial-mux/run/<alias>.pid`

### 停止

- `serial-mux stop <alias>` 读取 PID file 发送 SIGTERM
- daemon 收到 SIGTERM 后：关闭串口 → 关闭所有客户端连接 → 删除 socket
  文件 → 删除 PID file；alias JSON 保留供系统重启恢复
- `serial-mux stop` 在进程退出后额外删除 alias JSON，表示用户明确取消映射

### stale PID 检测

- `start` / `status` 时检查 PID file 对应的进程是否存在
- 进程不在则只清理 stale PID file 和 socket 文件，保留可恢复的 alias JSON
- 系统重启后，alias 以 `saved` 状态保留，并在下一次 `smtty` 连接时自动恢复
- 同一次开机检测到 USB 拔出或重新枚举时，自动清除对应串口映射

## 多串口支持

- 每个串口一个独立的 daemon 进程
- 各自有独立的 PID file、socket、日志目录
- 互不干扰，可独立启停

## 技术栈

- 语言：Python（>= 3.10）
- 串口通信：pyserial
- 进程间通信：Unix domain socket
- 终端处理：raw mode stdin/stdout
- 配置解析：PyYAML

## 架构

详细架构设计文档见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## License

serial-mux v0.1.0
