# serial-mux

[English](README.md) | [中文](README_zh.md)

A serial port multiplexer that lets multiple clients share a single serial port simultaneously. A per-port daemon owns the serial device and fans data out to all connected clients over Unix domain sockets, with persistent logging.

Think `tio` or `minicom`, but multiplexed — two people (or a person and an AI agent) can interact with the same serial console at the same time, and everyone sees everything.

## Architecture

```
Serial Device (/dev/ttyUSBx)
    ^
    |  pyserial (exclusive access)
    v
serial-mux daemon (one per device, background process)
    |
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
serial-mux start /dev/ttyUSB0 --baud 115200 --alias die0
```

- `--baud` / `-b` — baud rate (default: `115200`, configurable)
- `--alias` / `-a` — friendly name (default: device basename, e.g. `ttyUSB0`)
- `--foreground` / `-f` — don't daemonize, run in foreground (useful for debugging)

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
ALIAS        DEVICE               BAUD       PID      STATUS
die0         /dev/ttyUSB0         115200     12345    running
die1         /dev/ttyUSB1         115200     12350    running
```

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
Logs:    3 files, 42.5 KB
```

### Interactive Clients

#### smtty — user interactive mode

```bash
smtty die0
```

Connects to the daemon for `die0`, replays scrollback history from the log, then enters live interactive mode. Input is logged with the command.

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
  "socket": "/home/user/.serial-mux/sock/die0.sock"
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
