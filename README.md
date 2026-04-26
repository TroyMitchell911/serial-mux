# serial-mux

[English](README.md) | [中文](README_zh.md)

A serial port multiplexer that lets multiple clients share a single serial port simultaneously. A per-port daemon owns the serial device and fans data out to all connected clients over Unix domain sockets, with persistent logging. Optionally binds SSH for reliable remote access with automatic serial fallback.

Think `tio` or `minicom`, but multiplexed — two people (or a person and an AI agent) can interact with the same serial console at the same time, and everyone sees everything.

## Architecture

```
Serial Device (/dev/ttyUSBx)          SSH Target (user@host)
    ^                                      ^
    |  pyserial (exclusive access)         |  ssh -tt (PTY)
    v                                      v
serial-mux daemon (one per device, background process)
    |             SSH preferred, serial fallback
    +---> Log file (~/.serial-mux/logs/<alias>/YYYY-MM-DD.log)
    |
    +---> Unix socket (~/.serial-mux/sock/<alias>.sock)
              |
              +---> smtty <alias>                          interactive
              +---> smtty-agent <alias>                    interactive
              +---> smtty-agent <alias> --send/--wait      non-interactive
```

Each serial port gets its own independent daemon process. Clients connect and disconnect freely without affecting the daemon or each other. The daemon double-forks to daemonize (no systemd dependency).

## Installation

### One-line install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/TroyMitchell911/serial-mux/main/install.sh | sudo bash
```

The script clones the repo to `/usr/local/lib/serial-mux`, installs dependencies, and creates wrapper scripts in `/usr/local/bin`. It also checks if your user is in the correct serial device group.

### Manual install

#### Prerequisites

- Python >= 3.10
- User must be in the group that owns the serial device

On Arch Linux (group is `uucp`):
```bash
sudo usermod -aG uucp $USER
# Log out and back in for the group change to take effect
```

On Debian/Ubuntu (group is `dialout`):
```bash
sudo usermod -aG dialout $USER
```

#### Install

```bash
git clone https://github.com/TroyMitchell911/serial-mux.git
cd serial-mux
pip install -e .
```

#### Create the smtty-agent symlink

`smtty-agent` is the same binary as `smtty`, accessed via a symlink:

```bash
ln -sf $(which smtty) $(dirname $(which smtty))/smtty-agent
```

Verify both commands exist:
```bash
which smtty        # should resolve
which smtty-agent  # should resolve to the symlink
```

## Usage

### Daemon Management

#### Start a daemon

```bash
# Start with serial port
serial-mux start /dev/ttyUSB0 --baud 115200 --alias die0

# Start with SSH only (no serial port)
serial-mux start --ssh root@192.168.1.100 --alias die0

# Start with both serial and SSH
serial-mux start /dev/ttyUSB0 --alias die0 --ssh root@192.168.1.100
serial-mux start /dev/ttyUSB0 --alias die0 --ssh k3_die0   # uses ~/.ssh/config
```

- `--baud` / `-b` — baud rate (default: `115200`, configurable)
- `--alias` / `-a` — friendly name (default: device basename; **required** when starting without a device)
- `--foreground` / `-f` — don't daemonize, run in foreground (useful for debugging)
- `--ssh` — optional SSH target to bind at start (e.g. `user@192.168.1.1` or an `~/.ssh/config` hostname)

At least one of `DEVICE` or `--ssh` must be specified.

#### Bind/unbind SSH at runtime

```bash
# Bind SSH to a running daemon
serial-mux ssh-bind die0 root@192.168.1.100
serial-mux ssh-bind die0 k3_die0   # hostname from ~/.ssh/config

# Unbind SSH (clients fall back to serial)
serial-mux ssh-unbind die0
```

#### Bind/unbind serial at runtime

```bash
# Bind a serial port to a running daemon (e.g., started with SSH only)
serial-mux serial-bind die0 /dev/ttyUSB0 --baud 115200

# Unbind serial port
serial-mux serial-unbind die0
```

When SSH is bound, clients prefer SSH for I/O. If the SSH connection dies, the daemon falls back to serial automatically. Bare hostnames (without `@`) are validated against `~/.ssh/config` before connecting.

> **Note:** SSH uses `BatchMode=yes` — only key-based authentication is supported. Password prompts are automatically rejected. Set up SSH keys before using this feature.

#### Stop a daemon

```bash
serial-mux stop die0
```

Sends SIGTERM for graceful shutdown. The daemon closes the serial port, disconnects all clients, and cleans up its socket and PID files. Falls back to SIGKILL after 3 seconds if needed.

#### List running daemons

```bash
serial-mux list
```

Output:
```
ALIAS        DEVICE               BAUD       PID      CLIENTS  UPTIME       STATUS     SSH
-----------------------------------------------------------------------------------------------------
die0         /dev/ttyUSB0         115200     12345    1        2h 15m       running    root@192.168.1.100
die1         /dev/ttyUSB1         115200     12346    0        2h 15m       running    -
```

#### Change baud rate

```bash
serial-mux set-baud die0 9600
```

Changes the baud rate of a running daemon on the fly. The daemon updates the serial port, persists the new baud rate to its info file, and notifies all connected clients.

#### Check daemon status

```bash
serial-mux status die0
```

Output:
```
Alias:   die0
Device:  /dev/ttyUSB0
Baud:    115200
PID:     12345
Status:  running
Socket:  /home/user/.serial-mux/sock/die0.sock
SSH:     root@192.168.1.100
Logs:    3 files, 42.5 KB
```

### Interactive Clients

#### smtty — user interactive mode

```bash
smtty die0
```

Connects to the daemon for `die0`, replays scrollback history from the log, then enters live interactive mode.

Use `--timestamps` / `-T` to show timestamps on all lines — history, input (on Enter), and received output:

```bash
smtty die0 --timestamps
```

Output looks like:
```
[16:30:01] Linux login: root
[16:30:02] Password:
[16:30:05] root@board:~# ls [16:30:05]
[16:30:05] bin  etc  home  usr
```

Timestamps are off by default.

#### smtty-agent — agent interactive mode

```bash
smtty-agent die0
```

Identical to `smtty` but commonly used for agents or automation.

#### Detaching

Press `Ctrl+]` to detach from an interactive session. The daemon keeps running — you can reattach at any time.

### Non-Interactive Mode (smtty-agent)

Send a command, optionally wait for a pattern in the output:

```bash
# Send a command and wait for a shell prompt
smtty-agent die0 --send 'ls -la' --wait 'root@' --timeout 5

# Send a command, collect output for 1 second, exit
smtty-agent die0 --send 'uname -a'
```

Flags:
- `--send` / `-s` — command string to send
- `--wait` / `-w` — regex/string pattern to wait for in output
- `--timeout` / `-t` — seconds to wait for the pattern (default: 10)

#### SSH transport

When the daemon has SSH bound, clients automatically use the SSH transport. The interactive attach banner shows the current transport:

```
--- serial-mux: attached to die0 [ssh] (Ctrl+] to detach) ---
```

In non-interactive mode with SSH transport, **echo verification is skipped** — the network layer guarantees reliable delivery, so no retry is needed. If SSH drops mid-session, the daemon switches to serial automatically and notifies all clients.

#### Echo verification

Non-interactive mode verifies that the serial device echoed the command back correctly. If the echo doesn't match (e.g. due to line noise or buffer issues), it retries up to 5 times. If all retries fail, it exits with a non-zero status.

#### Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success (command sent, pattern matched if `--wait` was used) |
| 1    | Connection error or echo verification failed after 5 retries |
| 2    | Timeout waiting for `--wait` pattern |

## Identity Tagging

Every input sent through the serial port is logged with a timestamp:




Device output (responses from the serial device) is logged without modification.

Example session as seen in the log:
```
[2026-04-16 16:30:01] echo hello
[2026-04-16 16:30:01] hello
[2026-04-16 16:30:05] cat /proc/version
[2026-04-16 16:30:05] Linux version 6.x ...
```

## Configuration

Config file: `~/.config/serial-mux/config.yaml`

All settings are optional — sane defaults are used if the file doesn't exist.

```yaml
# How many days to keep log files (daemon purges old logs on startup)
log_retention_days: 7

# Default baud rate when --baud is not specified
default_baud: 115200

# Number of history lines replayed when a client attaches
scrollback_lines: 5000

# SSH ConnectTimeout passed to ssh (seconds)
ssh_connect_timeout: 3

# How long to wait for SSH to establish before declaring failure (seconds)
ssh_probe_timeout: 5
```

## File Layout

```
~/.serial-mux/
├── run/
│   ├── die0.json          # Alias metadata (device, baud, PID, socket path)
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
└── config.yaml            # Optional configuration
```

### run/<alias>.json

```json
{
  "alias": "die0",
  "device": "/dev/ttyUSB0",
  "baud": 115200,
  "pid": 12345,
  "socket": "/home/user/.serial-mux/sock/die0.sock",
  "ssh": "root@192.168.1.100"
}
```

## Log Management

- Logs are split by alias and date: `~/.serial-mux/logs/<alias>/YYYY-MM-DD.log`
- The daemon automatically purges logs older than `log_retention_days` on startup
- No cron job needed
- When a client attaches, the last `scrollback_lines` lines from the log are replayed to stdout, giving you full history in your terminal/tmux scrollback

## Alias System

Device paths like `/dev/ttyUSB0` are unstable — unplug and replug and it might become `ttyUSB1`. Aliases decouple clients from physical device paths. You always connect with `smtty die0` regardless of which `/dev/ttyUSBx` it happens to be on.

If no `--alias` is given at start time, the device basename is used (e.g. `ttyUSB0`).

## Examples

### Basic workflow

```bash
# Start a daemon for a USB serial adapter
serial-mux start /dev/ttyUSB0 --baud 115200 --alias die0

# Attach interactively as a user
smtty die0
# (type commands, see output, Ctrl+] to detach)

# In another terminal, attach another client
smtty-agent die0
# (both sessions see the same output in real time)

# Send a command non-interactively from a script
output=$(smtty-agent die0 --send 'cat /proc/uptime' --wait '# ' --timeout 5)
echo "$output"

# Check what's running
serial-mux list

# Stop the daemon when done
serial-mux stop die0
```

### Multiple serial ports

```bash
serial-mux start /dev/ttyUSB0 --alias die0
serial-mux start /dev/ttyUSB1 --alias die1
serial-mux list

# Each port is fully independent
smtty die0    # terminal 1
smtty die1    # terminal 2
```

### Debugging the daemon

```bash
# Run in foreground to see daemon logs on stderr
serial-mux start /dev/ttyUSB0 --alias die0 --foreground
```

### Scripting with non-interactive mode

```bash
#!/bin/bash
# Reboot a device and wait for it to come back
smtty-agent die0 --send 'reboot' --timeout 1
sleep 10
smtty-agent die0 --send '' --wait 'login:' --timeout 60
smtty-agent die0 --send 'root' --wait '# ' --timeout 5
echo "Device is back up"
```

## Tech Stack

- Python 3.10+
- [pyserial](https://github.com/pyserial/pyserial) — serial port access
- Unix domain sockets — IPC between daemon and clients
- Raw mode stdin/stdout — terminal handling
- [PyYAML](https://pyyaml.org/) — configuration

## License

See [LICENSE](LICENSE) if present.
