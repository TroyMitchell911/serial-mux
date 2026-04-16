# serial-mux 架构设计文档

## 概述

serial-mux 是一个串口多路复用工具，允许多个客户端同时访问同一个串口设备。daemon 独占串口，通过 Unix socket 将数据扇出给所有连接的客户端，同时持久化日志。

## 架构图

```
串口设备 (/dev/ttyUSBx)
    ^
    |  pyserial
    v
serial-mux daemon (后台进程，每个串口一个)
    |
    +---> 日志文件 (~/.serial-mux/logs/<alias>/YYYY-MM-DD.log)
    |
    +---> Unix socket (~/.serial-mux/sock/<alias>.sock)
              |
              +---> smtty <alias>           交互式客户端 [U]
              +---> smtty-hermes <alias>    交互式客户端 [H]
              +---> smtty-hermes <alias> --send/--wait  非交互式
```

## 命令体系

### daemon 管理

```
serial-mux start <device> --baud <rate> --alias <name>
serial-mux stop <alias>
serial-mux list
serial-mux status <alias>
```

- `start` — 启动 daemon 进程，独占串口，fork 到后台 daemonize
- `stop` — 读 PID file 发 SIGTERM，daemon 优雅关闭串口、清理 socket 和 PID file
- `list` — 列出所有已 start 的串口
- `status` — 查看某个串口的详细状态（连接数、上线时间等）

`list` 输出格式：

```
ALIAS    DEVICE         BAUD     CLIENTS  UPTIME
die0     /dev/ttyUSB0   115200   1        2h 15m
die1     /dev/ttyUSB1   115200   0        2h 15m
```

### 客户端

```
smtty <alias>                                          # 交互式，标记 [U]
smtty-hermes <alias>                                   # 交互式，标记 [H]
smtty-hermes <alias> --send "ls" --wait "root@" --timeout 5   # 非交互式
```

- `smtty` 和 `smtty-hermes` 是同一个可执行文件的 symlink
- 程序通过 `argv[0]` 检测自身名称决定身份标记
- alias 优先查找，未匹配时当设备路径处理

### detach

- 交互模式下 Ctrl+] detach，不影响 daemon

## alias 机制

设备路径不稳定（拔插后 ttyUSB0 可能变 ttyUSB1），alias 将客户端与具体设备路径解耦。

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

## 交互模式

### 输出

- 所有内容直接输出到 stdout，不自管 buffer
- scrollback 完全由终端/tmux 管理
- 复制粘贴、tmux capture-pane 正常工作
- 切换 tmux session 不丢内容

### attach 时的行为

1. 从日志文件读取历史内容，print 到 stdout（进入终端/tmux scrollback）
2. 切换到实时模式，持续将串口数据写到 stdout
3. 用户看到光标在最底部，往上滚即可查看历史
4. 效果类似 `cat log && tail -f log`，但是完整的交互式会话

### 输入

- stdin 设为 raw mode，逐字符读取
- 用户按键通过 Unix socket 发送给 daemon，daemon 转发到串口

### 输出格式

```
[2026-04-16 16:30:01] [U] echo hello
hello
[2026-04-16 16:30:05] [H] cat /proc/version
Linux version 6.x ...
```

- 用户/Hermes 发送的命令带时间戳和来源标记 [U]/[H]
- 设备自身输出不带标记，直接输出

## 非交互模式（Hermes 专用）

```
smtty-hermes die0 --send "ls" --wait "root@" --timeout 5
```

流程：

1. 连接 daemon 的 Unix socket
2. 发送命令到串口
3. 从串口回显中校验发送内容是否与原始命令一致
4. 不一致 → 重发，最多重试 5 次
5. 等待 `--wait` 指定的 pattern 出现，或超时
6. 成功 → 静默退出，stdout 输出命令结果
7. 5 次校验都失败 → 报错退出（非零退出码）

## daemon 进程管理

### 驻留方式

- `serial-mux start` 时 fork 到后台，自行 daemonize
- 不依赖 systemd，无需 service 文件
- PID file: `~/.serial-mux/run/<alias>.pid`

### 停止

- `serial-mux stop <alias>` 读 PID file 发 SIGTERM
- daemon 收到 SIGTERM 后：关闭串口、关闭所有客户端连接、删除 socket 文件、删除 PID file

### stale PID 检测

- `start` / `status` 时检查 PID file 对应的进程是否存在
- 进程不在则自动清理 stale PID file 和 socket 文件

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
```

## 配置文件

路径：`~/.config/serial-mux/config.yaml`

```yaml
log_retention_days: 7       # 日志保留天数
default_baud: 115200        # 默认波特率
scrollback_lines: 5000      # attach 时回放的历史行数
```

- 所有配置项都有合理默认值，config 文件可选
- daemon 启动时读取配置

## 日志

### 格式

日志文件按 alias + 日期分割：`~/.serial-mux/logs/<alias>/YYYY-MM-DD.log`

每行带时间戳和来源标记（如有）：

```
[2026-04-16 16:30:01] [U] echo hello
[2026-04-16 16:30:01] hello
[2026-04-16 16:30:05] [H] cat /proc/version
[2026-04-16 16:30:05] Linux version 6.x ...
```

### 清理

- daemon 启动时扫描 logs 目录
- 删除超过 `log_retention_days` 天数的日志文件
- 无需额外 cron job

## 技术栈

- 语言：Python
- 串口：pyserial
- IPC：Unix domain socket
- 终端：raw mode stdin/stdout
- 配置：PyYAML

## 多串口支持

- 每个串口一个独立的 daemon 进程
- 各自有独立的 PID file、socket、日志目录
- 互不干扰，可独立启停
