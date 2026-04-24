# serial-mux

[English](README.md) | [中文](README_zh.md)

串口多路复用工具 — 允许多个客户端通过 daemon + Unix socket 架构共享同一个串口设备。

## 功能特性

- 一个串口，多个客户端同时访问
- daemon 后台驻留，独占串口，通过 Unix socket 扇出数据
- 交互式客户端 `smtty` / `smtty-agent`
- 非交互模式支持发送命令、等待匹配、echo 校验与自动重试
- alias 机制解耦设备路径，拔插不影响使用
- 按日期分割的持久化日志，自动清理过期文件
- attach 时回放历史行，scrollback 由终端/tmux 管理
- 输出直接到 stdout，复制粘贴正常工作

## 安装

### 一键安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/TroyMitchell911/serial-mux/main/install.sh | sudo bash
```

脚本会将仓库 clone 到 `/usr/local/lib/serial-mux`，安装依赖，并在 `/usr/local/bin` 创建 `serial-mux`、`smtty`、`smtty-agent` 三个命令。同时会检测当前用户是否在串口设备组中。

### 手动安装

```bash
git clone https://github.com/TroyMitchell911/serial-mux.git
cd serial-mux
pip install -e .
```

安装后创建 `smtty-agent` symlink：

```bash
ln -sf $(which smtty) $(dirname $(which smtty))/smtty-agent
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
serial-mux start /dev/ttyUSB0 --baud 115200 --alias die0
```

daemon 会 double-fork 到后台驻留，独占串口设备。

如需前台调试：

```bash
serial-mux start /dev/ttyUSB0 --baud 115200 --alias die0 --foreground
```

### 2. 连接串口

连接串口（以用户身份）：

```bash
smtty die0
```

连接串口（以 Agent 身份）：

```bash
smtty-agent die0
```

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
| `serial-mux list` | 列出所有运行中的串口 |
| `serial-mux status <alias>` | 查看指定串口的详细状态 |

`serial-mux list` 输出示例：

```
ALIAS    DEVICE         BAUD     CLIENTS  UPTIME
die0     /dev/ttyUSB0   115200   1        2h 15m
die1     /dev/ttyUSB1   115200   0        2h 15m
```

### 客户端

| 命令 | 说明 |
|------|------|
| `smtty <alias>` | 交互式客户端 |
| `smtty-agent <alias>` | 交互式客户端 |
| `smtty-agent <alias> --send 'cmd' --wait 'pattern' --timeout 5` | 非交互模式 |

`smtty` 和 `smtty-agent` 是同一个可执行文件的 symlink。

## 非交互模式

专为自动化和 Agent 场景设计：

```bash
smtty-agent die0 --send "ls" --wait "root@" --timeout 5
```

执行流程：

1. 连接 daemon 的 Unix socket
2. 发送命令到串口
3. 从串口回显中校验发送内容是否与原始命令一致
4. 回显不一致 → 自动重发，最多重试 5 次
5. 等待 `--wait` 指定的 pattern 出现，或超时退出
6. 成功 → stdout 输出命令结果，静默退出（exit code 0）
7. 5 次 echo 校验均失败 → 报错退出（非零 exit code）

### echo 校验机制

串口通信可能丢字符或乱序。非交互模式在发送命令后会检查串口回显是否与原始命令一致，不一致则重发，最多 5 次，确保命令被正确接收。

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

设备路径不稳定（拔插后 `/dev/ttyUSB0` 可能变成 `/dev/ttyUSB1`），alias 将客户端与具体设备路径解耦。

alias 映射存储在 `~/.serial-mux/run/<alias>.json`：

```json
{
  "alias": "die0",
  "device": "/dev/ttyUSB0",
  "baud": 115200,
  "pid": 12345,
  "socket": "/home/user/.serial-mux/sock/die0.sock"
}
```

客户端使用 alias 连接时优先查找映射，未匹配时当设备路径处理。

## 配置文件

路径：`~/.config/serial-mux/config.yaml`

```yaml
log_retention_days: 7       # 日志保留天数
default_baud: 115200        # 默认波特率
scrollback_lines: 5000      # attach 时回放的历史行数
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

- 无标记 — 设备自身输出

### 自动清理

daemon 启动时自动扫描 logs 目录，删除超过 `log_retention_days` 天数的日志文件，无需额外 cron job。

## 文件布局

```
~/.serial-mux/
├── run/
│   ├── die0.json          # alias 映射 + PID + socket path
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
- daemon 收到 SIGTERM 后：关闭串口 → 关闭所有客户端连接 → 删除 socket 文件 → 删除 PID file

### stale PID 检测

- `start` / `status` 时检查 PID file 对应的进程是否存在
- 进程不在则自动清理 stale PID file 和 socket 文件

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
