# serial-mux 架构设计文档

## 概述

serial-mux 是一个串口多路复用工具，允许多个客户端同时访问同一个串口设备。daemon 独占串口，通过 Unix socket 将数据扇出给所有连接的客户端，同时持久化日志。可选绑定 SSH，SSH 优先，串口自动后备。

## 架构图

```
串口设备 (/dev/ttyUSBx)              SSH Target (user@host)
    ^                                      ^
    |  pyserial (独占访问)                  |  ssh -tt (PTY)
    v                                      v
serial-mux daemon (后台进程，每个 alias 一个)
    |             SSH 优先，串口后备
    +---> 日志文件 (~/.serial-mux/logs/<alias>/YYYY-MM-DD.log)
    |
    +---> Unix socket (~/.serial-mux/sock/<alias>.sock)
              |
              +---> smtty <alias>                          交互式客户端
              +---> smtty <alias> --send/--wait            非交互式
```

device 和 SSH 至少绑定一个。可以启动时同时绑定，也可以运行时动态 bind/unbind。

## 命令体系

### daemon 管理

```
serial-mux start <device> --baud <rate> --alias <name> [--ssh <target>] [--foreground]
serial-mux stop <alias>
serial-mux list
serial-mux status <alias>
serial-mux set-baud <alias> <baud>
serial-mux ssh-bind <alias> <target>
serial-mux ssh-unbind <alias>
serial-mux serial-bind <alias> <device> [--baud <rate>]
serial-mux serial-unbind <alias>
```

- `start` — 启动 daemon 进程。`DEVICE` 和 `--ssh` 至少指定一个；无 device 时 `--alias` 必填
- `stop` — 读 PID file 发 SIGTERM，daemon 优雅关闭串口、清理 socket 和 PID file；3 秒后回退 SIGKILL
- `list` — 列出所有运行中的 daemon
- `status` — 查看指定 daemon 的详细状态
- `set-baud` — 动态修改波特率（需已绑定串口，SSH-only 模式返回错误）
- `ssh-bind` — 为运行中的 daemon 绑定 SSH（`user@host` 或 `~/.ssh/config` hostname）
- `ssh-unbind` — 解除 SSH 绑定，回退到串口
- `serial-bind` — 为运行中的 daemon 绑定串口设备
- `serial-unbind` — 解除串口绑定

`list` 输出格式：

```
ALIAS        DEVICE               BAUD       PID      CLIENTS  UPTIME       STATUS     SSH
-----------------------------------------------------------------------------------------------------
die0         /dev/ttyUSB0         115200     12345    1        2h 15m       running    root@192.168.1.100
die1         /dev/ttyUSB1         115200     12346    0        45s          running    -
```

### 客户端

```
smtty <alias>                                          # 交互式模式
smtty <alias> --send "ls" --wait "root@" --timeout 5         # 非交互式
```

- alias 优先查找，未匹配时当设备路径处理

### detach

- 交互模式下 Ctrl+] detach，不影响 daemon

## SSH 绑定

### 传输层选择

daemon 同时管理串口和 SSH 两个 I/O 通道。当 SSH 已连接时，所有客户端的输入输出走 SSH；SSH 断开或解绑后自动回退到串口。

### SSH target 验证

- `user@host` 格式 — 直接使用，不检查 `~/.ssh/config`
- 裸 hostname（不含 `@`） — 必须在 `~/.ssh/config` 中存在对应的 `Host` 条目，否则拒绝

### SSH 连接机制

1. daemon 通过 `ssh -tt -o BatchMode=yes` 启动 SSH 子进程（PTY 模式）
2. 等待 `ssh_probe_timeout` 秒，若 SSH 进程仍存活则判定连接成功
3. SSH 进程退出则判定连接失败，返回错误信息
4. 仅支持密钥认证（`BatchMode=yes` 拒绝密码提示）

### fallback 机制

- SSH 连接断开 → daemon 自动切回串口，广播 `transport_changed` 通知所有客户端
- `ssh-unbind` → 立即切回串口
- 串口 + SSH 同时绑定时，SSH 优先；SSH 不可用时串口接管

## alias 机制

设备路径不稳定（拔插后 ttyUSB0 可能变 ttyUSB1），alias 将客户端与具体设备路径解耦。

alias 映射存储在 `~/.serial-mux/run/<alias>.json`：

```json
{
  "alias": "die0",
  "device": "/dev/ttyUSB0",
  "baud": 115200,
  "pid": 12345,
  "socket": "/home/user/.serial-mux/sock/die0.sock",
  "ssh": "root@192.168.1.100",
  "start_time": 1713267600.0,
  "clients_count": 1
}
```

## 交互模式

### 输出

- 所有内容直接输出到 stdout，不自管 buffer
- scrollback 完全由终端/tmux 管理
- 复制粘贴、tmux capture-pane 正常工作
- 切换 tmux session 不丢内容

### attach 时的行为

1. 从日志文件读取历史内容，print 到 stdout（进入终端/tmux scrollback）
2. 切换到实时模式，持续将串口/SSH 数据写到 stdout
3. 用户看到光标在最底部，往上滚即可查看历史
4. 效果类似 `cat log && tail -f log`，但是完整的交互式会话

attach banner 显示当前传输层：

```
--- serial-mux: attached to die0 [ssh] (Ctrl+] to detach) ---
--- serial-mux: attached to die0 [serial] (Ctrl+] to detach) ---
```

### 输入

- stdin 设为 raw mode，逐字符读取
- 用户按键通过 Unix socket 发送给 daemon，daemon 转发到串口或 SSH

## 非交互模式

```
smtty die0 --send "ls" --wait "root@" --timeout 5
```

使用 `--send`/`--wait` 参数进入非交互模式。

流程：

1. 连接 daemon 的 Unix socket
2. 发送命令到串口/SSH
3. **串口传输层**：从串口回显中校验发送内容是否与原始命令一致；不一致 → 重发，最多重试 5 次
4. **SSH 传输层**：跳过 echo 校验（网络层保证可靠传输）
5. 等待 `--wait` 指定的 pattern 出现，或超时
6. 成功 → 静默退出，stdout 输出命令结果
7. 5 次校验都失败 → 报错退出（非零退出码）

## daemon 进程管理

### 驻留方式

- `serial-mux start` 时 double-fork 到后台，自行 daemonize
- 不依赖 systemd，无需 service 文件
- PID file: `~/.serial-mux/run/<alias>.pid`

### 停止

- `serial-mux stop <alias>` 读 PID file 发 SIGTERM
- daemon 收到 SIGTERM 后：关闭串口 → 终止 SSH 子进程 → 关闭所有客户端连接 → 删除 socket 文件 → 删除 PID file
- 3 秒后仍未退出则 SIGKILL

### stale PID 检测

- `start` / `status` 时检查 PID file 对应的进程是否存在
- 进程不在则自动清理 stale PID file 和 socket 文件

## 文件布局

```
~/.serial-mux/
├── run/
│   ├── die0.json          # alias 映射 + PID + socket path + SSH + start_time + clients_count
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

## 配置文件

路径：`~/.config/serial-mux/config.yaml`

```yaml
log_retention_days: 7       # 日志保留天数
default_baud: 115200        # 默认波特率
scrollback_lines: 5000      # attach 时回放的历史行数
ssh_connect_timeout: 3      # SSH ConnectTimeout（秒）
ssh_probe_timeout: 5        # SSH 探测等待时间（秒），超时判定连接成功
```

- 所有配置项都有合理默认值，config 文件可选
- daemon 启动时读取配置

## 日志

### 格式

日志文件按 alias + 日期分割：`~/.serial-mux/logs/<alias>/YYYY-MM-DD.log`

所有数据（输入回显和设备输出）统一以时间戳格式记录，不区分来源：

```
[2026-04-16 16:30:01] echo hello
[2026-04-16 16:30:01] hello
[2026-04-16 16:30:05] cat /proc/version
[2026-04-16 16:30:05] Linux version 6.x ...
```

### 清理

- daemon 启动时扫描 logs 目录
- 删除超过 `log_retention_days` 天数的日志文件
- 无需额外 cron job

## 技术栈

- 语言：Python（>= 3.10）
- 串口通信：pyserial
- 进程间通信：Unix domain socket
- 终端处理：raw mode stdin/stdout
- 配置解析：PyYAML

## 多串口支持

- 每个串口一个独立的 daemon 进程
- 各自有独立的 PID file、socket、日志目录
- 互不干扰，可独立启停
